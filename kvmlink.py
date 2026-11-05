#!/usr/bin/env python3
"""kvmlink — comparte un ratón y teclado entre un Mac y un PC Windows por la red local.

Sin argumentos abre la interfaz gráfica. Desde la terminal también se puede usar así
(Mac a la izquierda, PC a la derecha):
    PC  :  python kvmlink.py --listen  --edge left
    Mac :  python3 kvmlink.py --connect auto --edge right

Atajo de emergencia mientras controlas el otro equipo: Ctrl+Alt+Shift+Q.
"""
import argparse
import collections
import getpass
import json
import os
import queue
import socket
import sys
import threading
import time
from types import SimpleNamespace

import files
import layout as L
import net
import update
import version
from keymap import CMD_CTRL_SWAP, ISO_SWAP

VERSION = 5   # protocolo. 3: arrastrar archivos; 4: copiar y pegar; 5: actualizarse
CLIP_MAX_ENTRIES = 50_000
SIDES = ("left", "right", "top", "bottom")
LOCAL, CONTROLLING, CONTROLLED = "local", "controlling", "controlled"
EMERGENCY = {"LCtrl", "LAlt", "LShift"}
EMERGENCY_KEY = "Q"
CLIP_LIMIT = 1_000_000
PING_EVERY = 1.0
PEER_TIMEOUT = 5.0
BOUNCE_GUARD = 0.25   # s tras cruzar en los que no se puede volver de rebote
MONITOR_REFRESH_TICKS = 2   # cada cuántos segundos se releen los monitores

DEFAULT_OPTIONS = {"speed": 1.0, "invert_scroll": False, "swap_cmd_ctrl": True,
                   "iso_swap": False, "clipboard": True,
                   "receive_dir": "",           # vacío = carpeta Descargas
                   "open_on_receive": True}

# ------------------------------------------------------------------ registro
LOGS = collections.deque(maxlen=400)   # (número de línea, texto)
_log_seq = 0
_log_file = None
_log_lock = threading.Lock()


def logs_since(seq):
    """Líneas del registro posteriores a `seq` (para que la interfaz pida solo lo nuevo)."""
    with _log_lock:
        return [item for item in LOGS if item[0] > seq]


def log(*a):
    global _log_seq
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    with _log_lock:
        _log_seq += 1
        LOGS.append((_log_seq, line))
        if _log_file:
            try:
                _log_file.write(line + "\n")
                _log_file.flush()
            except Exception:
                pass
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw no tiene consola


def open_log_file(path):
    global _log_file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _log_file = open(path, "w", encoding="utf-8")
    except OSError:
        _log_file = None


def load_platform():
    if sys.platform == "win32":
        from platform_win import WinPlatform
        return WinPlatform()
    if sys.platform == "darwin":
        from platform_mac import MacPlatform
        return MacPlatform()
    sys.exit("Sistema no soportado (solo Windows y macOS).")


# ------------------------------------------------------------ configuración
def config_dir():
    """La configuración es de cada equipo: se guarda en la carpeta del usuario y no
    junto al programa, así copiar la carpeta de un equipo a otro no la pisa."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "kvmlink")


CONFIG_FILE = os.path.join(config_dir(), "config.json")


def load_config():
    cfg = {"os": sys.platform, "mode": "listen", "target": "auto", "port": net.DEFAULT_PORT,
           "key": None, "edge": None, "layout": None, "peer": None,
           "options": dict(DEFAULT_OPTIONS), "autostart": True}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return cfg
    if saved.get("os") not in (None, sys.platform):
        return cfg  # copiada de otro sistema: no sirve
    for k, v in saved.items():
        if k == "options":
            cfg["options"].update(v or {})
        else:
            cfg[k] = v
    return cfg


def save_config(cfg):
    os.makedirs(config_dir(), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ---------------------------------------------------------------------- núcleo
class Core:
    """Máquina de estados: LOCAL, CONTROLLING (manejamos el otro equipo) y
    CONTROLLED (el otro equipo nos maneja)."""

    def __init__(self, plat, options, edge=None, layout=None, is_server=False):
        self.p = plat
        self.args = SimpleNamespace(**{**DEFAULT_OPTIONS, **(options or {})})
        self.edge = edge                  # solo para la disposición por defecto
        self.layout = layout              # {"dx", "dy", "ts"} o None
        self.is_server = is_server
        self.on_layout_changed = None     # callback(layout) para guardarla
        self.on_peer_changed = None       # callback(peer) para recordarlo
        self.state = LOCAL
        self.lock = threading.RLock()
        self.chan = None
        self.outq = None
        self.peer = None
        self.buttons_down = 0
        self.cooldown_until = 0.0
        self.vx = self.vy = 0.0
        self.vmon = None
        self.entered_at = 0.0
        self.cap_keys = set()
        self.last_rx = time.monotonic()
        self.fileq = None                 # trozos de archivo pendientes (menos prioridad)
        self.drag_files = None            # archivos que se están arrastrando al otro equipo
        self.remote_buttons = set()       # botones pulsados en el otro equipo mientras lo manejamos
        self.clip_offer = None            # archivos copiados aquí y ofrecidos al otro equipo
        self._clip_fetch = None           # archivos copiados allí y ofrecidos en nuestro portapapeles
        self.program_dir = version.PROGRAM_DIR   # (las pruebas usan otra carpeta)
        self.build = version.BUILD
        self.code_hash = version.code_hash(version.manifest(self.program_dir))
        self.updater = None               # actualización desde el otro equipo en curso
        self.on_update_applied = None     # callback para reiniciar tras actualizarse
        self.transfers = files.Transfers()
        self.receiver = files.Receiver(self, log)
        mons = plat.monitors()
        self._mon_cache = (mons, L.rects(mons))

    # ------------------------------------------------------------- utilidades
    # Los monitores se leen de una caché. Los hooks de ratón llaman a my_rects() en
    # cada movimiento y NO deben consultar al sistema: Windows corta un hook que
    # tarda en responder. La caché la refresca el hilo vigilante (refresh_monitors).
    def monitors(self):
        return self._mon_cache[0]

    def my_rects(self):
        return self._mon_cache[1]

    def refresh_monitors(self):
        """Relee los monitores del sistema; si han cambiado (se ha enchufado o
        desenchufado uno), actualiza la caché y avisa al otro equipo."""
        mons = self.p.monitors()
        if mons == self._mon_cache[0]:
            return False
        self._mon_cache = (mons, L.rects(mons))   # una sola asignación: atómica
        log("las pantallas de este equipo han cambiado")
        self.send({"t": "monitors", "monitors": mons})
        return True

    def offset(self):
        lay = self.layout
        return None if lay is None else (lay["dx"], lay["dy"])

    def send(self, msg):
        q = self.outq
        if q is not None:
            q.put(msg)

    def _send_clipboard_async(self):
        def work():
            try:
                if self.p.clipboard_has_files():
                    # lo copiado son archivos: van como oferta (ver _clip_watch); su
                    # nombre como texto pisaría esa oferta en el otro equipo
                    return
                import pyperclip
                text = pyperclip.paste()
            except Exception as e:
                log("portapapeles no disponible:", e)
                return
            if text and len(text) <= CLIP_LIMIT:
                self.send({"t": "clip", "text": text})
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _set_clipboard_async(text):
        def work():
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception as e:
                log("no se pudo escribir el portapapeles:", e)
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- disposición
    def set_layout(self, dx, dy, ts=None, notify=True):
        """Fija la disposición (coordenada aquí = coordenada allí + (dx, dy))."""
        lay = {"dx": int(dx), "dy": int(dy), "ts": ts if ts is not None else time.time()}
        self.layout = lay
        if notify:
            self.send({"t": "layout", **lay})
        if self.on_layout_changed:
            self.on_layout_changed(lay)
        return lay

    def default_layout_offset(self, peer_rects):
        return L.default_offset(self.my_rects(), peer_rects, self.edge or "right")

    def _negotiate_layout(self, hello):
        theirs = hello.get("layout")
        mine = self.layout
        t_mine = mine["ts"] if mine else -1
        t_theirs = theirs["ts"] if theirs else -1
        if theirs and (t_theirs > t_mine or (t_theirs == t_mine and not self.is_server)):
            self.set_layout(-theirs["dx"], -theirs["dy"], ts=t_theirs, notify=False)
        elif mine is None and self.is_server:
            dx, dy = self.default_layout_offset(L.rects(self.peer["monitors"]))
            self.set_layout(dx, dy, ts=0)
            log("disposición inicial: el otro equipo a la", {"left": "izquierda", "right": "derecha",
                                                             "top": "parte de arriba",
                                                             "bottom": "parte de abajo"}[self.edge or "right"])

    # ---------------------------------------------- eventos locales (hooks)
    def on_local_button(self, down):
        self.buttons_down = max(0, self.buttons_down + (1 if down else -1))
        if not self.buttons_down:
            self.p.drag_reset()

    def on_local_move(self, x, y):
        """Devuelve True si este movimiento ha iniciado el control remoto.

        Solo llegan aquí movimientos del ratón FÍSICO de este equipo (las inyecciones
        se filtran antes). También vale estando CONTROLLED: si el otro equipo nos está
        manejando y usas el ratón propio para cruzar, tomas tú el control del otro
        equipo y este, al recibir "enter", suelta la captura.

        Con un botón pulsado solo se cruza si se están arrastrando archivos: entonces
        se sueltan en el otro equipo (ver capture)."""
        if self.state == CONTROLLING or self.peer is None:
            return False
        if time.monotonic() < self.cooldown_until:
            return False
        entry = L.find_crossing(x, y, self.my_rects(), self.peer["rects"], self.offset())
        if entry is None:
            return False
        drag = None
        if self.buttons_down:
            # ¿arrastre de archivos? (en Windows la primera llamada solo prepara la
            # detección y devuelve None; se cruza en un movimiento siguiente)
            drag = self.p.file_drag_check(x, y)
            if not drag:
                return False
        with self.lock:
            if self.state == CONTROLLING or self.peer is None:
                return False
            if self.state == CONTROLLED:
                # soltar teclas/botones que el otro equipo tuviera pulsados aquí
                self.p.release_all()
                log("tomando el control con el ratón de este equipo")
            self.vx, self.vy = entry
            self.vmon = L.monitor_at(self.peer["rects"], *entry)
            self.entered_at = time.monotonic()
            self.state = CONTROLLING
            self.cap_keys.clear()
            self.remote_buttons.clear()
            self.drag_files = drag
            self.p.begin_capture(drag=bool(drag))
            self.send({"t": "enter", "x": int(self.vx), "y": int(self.vy)})
        log(f"→ controlando {self.peer.get('name', 'el otro equipo')}"
            + (f" arrastrando {len(drag)} elemento(s)" if drag else ""))
        if self.args.clipboard:
            self._send_clipboard_async()
        return True

    def capture(self, msg):
        """Evento capturado mientras controlamos el otro equipo (hilo del hook)."""
        if self.state != CONTROLLING:
            return
        t = msg["t"]
        if t == "mm":
            s = self.args.speed
            where, x, y, mon = L.step_remote(self.vx + msg["dx"] * s, self.vy + msg["dy"] * s,
                                             self.vmon, self.peer["rects"], self.my_rects(),
                                             self.offset())
            if where == "local":
                if time.monotonic() - self.entered_at < BOUNCE_GUARD:
                    # recién cruzado: un temblor de la mano (o un delta falso) no debe
                    # devolver el control al instante; se queda en el borde del otro equipo
                    x, y = L.clamp_into(self.vmon, self.vx + msg["dx"] * s, self.vy + msg["dy"] * s)
                    mon = self.vmon
                else:
                    self.leave_remote(to=(x, y))
                    return
            self.vx, self.vy, self.vmon = x, y, mon
            self.send({"t": "m", "x": int(x), "y": int(y)})
        elif t == "k":
            name = msg["k"]
            if self.p.os_name == "mac" and self.args.iso_swap:
                name = ISO_SWAP.get(name, name)
            if msg["d"]:
                self.cap_keys.add(name)
                if name == EMERGENCY_KEY and EMERGENCY <= self.cap_keys:
                    log("atajo de emergencia: recuperando el control local")
                    self.leave_remote()
                    return
            else:
                self.cap_keys.discard(name)
            self.send({"t": "k", "k": name, "d": msg["d"]})
        elif t == "mb" and self.drag_files and msg["b"] == "left" and not msg["d"]:
            self.drop_files()   # se ha soltado en el otro equipo lo que se arrastraba
        else:
            if t == "mb":       # recordar qué botones siguen pulsados en el otro equipo
                (self.remote_buttons.add if msg["d"] else self.remote_buttons.discard)(msg["b"])
            self.send(msg)

    def drop_files(self):
        """Fin de un arrastre de archivos al otro equipo: se termina (o cancela) el
        arrastre en este sistema sin dejar caer nada aquí, y se envían los archivos."""
        paths, self.drag_files = self.drag_files, None
        self.buttons_down = 0
        self.p.end_file_drag()
        self.send_files(paths)

    def _drag_out_to_peer(self):
        """El otro equipo nos manejaba y ha vuelto al suyo con el botón pulsado aquí.
        Si lo que se arrastraba aquí son archivos, se termina el arrastre sin dejarlos
        caer en ningún sitio y se envían al otro equipo. Si no, se suelta el botón como
        siempre."""
        try:
            paths = self.p.remote_drag_files()
        except Exception as e:
            log("no se pudo comprobar el arrastre:", repr(e))
            paths = None
        if not paths:
            self.p.release_all()
            return
        log(f"arrastre de {len(paths)} elemento(s) hacia {self.peer['name'] if self.peer else 'el otro equipo'}")
        self.p.release_all(keep=("left",))   # el botón izquierdo lo suelta end_file_drag
        self.p.end_file_drag(at=self.p.cursor_pos())
        self.send_files(paths)

    def send_files(self, paths):
        if self.chan is None or self.fileq is None:
            log("no se pueden enviar archivos: no hay conexión")
            return None
        if self.peer and self.peer.get("version", 1) < 3:
            log("el otro equipo tiene una versión de kvmlink que no recibe archivos")
            return None
        sender = files.Sender(self, paths, log)
        sender.start()
        return sender

    def nudge(self):
        q = self.outq
        if q is not None:
            q.put(files.NUDGE)

    def panic(self):
        """Algo ha ido mal: devolver el control local sí o sí. Nunca lanza."""
        try:
            if self.state == CONTROLLING:
                log("recuperando el control local por seguridad")
                self.leave_remote()
        except Exception as e:
            log("error al recuperar el control:", repr(e))
        finally:
            if self.state == CONTROLLING:
                self.state = LOCAL
            if getattr(self.p, "capturing", False):
                self.p.emergency_restore()

    def watchdog(self):
        """Hilo vigilante: si estamos controlando el otro equipo y se pierde la
        conexión, se cuelga la recepción o el sistema desactiva nuestros hooks,
        devuelve el control local. También refresca la caché de monitores."""
        bad = 0
        tick = 0
        while True:
            time.sleep(1.0)
            tick += 1
            if tick % MONITOR_REFRESH_TICKS == 0:
                try:
                    self.refresh_monitors()
                except Exception as e:
                    log("no se pudieron leer las pantallas:", repr(e))
            if self.state != CONTROLLING:
                bad = 0
                continue
            problem = None
            if self.chan is None:
                problem = "sin conexión"
            elif time.monotonic() - self.last_rx > PEER_TIMEOUT:
                problem = "el otro equipo no responde"
            elif not self.p.healthy():
                bad += 1
                if bad >= 2:
                    problem = "el sistema ha desactivado la captura de ratón/teclado"
            else:
                bad = 0
            if problem:
                log("vigilante:", problem)
                self.panic()

    def leave_remote(self, to=None, notify=True):
        """Vuelve al control local. `to`: dónde dejar el cursor (por defecto, el centro
        del monitor principal). Solo cuando se vuelve cruzando el borde (`to` dado) con
        el botón pulsado en el otro equipo se trata como un posible arrastre de archivos
        desde allí; el atajo de emergencia o un fallo nunca lo son."""
        to_edge = to is not None
        with self.lock:
            if self.state != CONTROLLING:
                return
            self.state = LOCAL
            if to is None:
                mons = self.monitors()
                prim = next((m for m in mons if m[4]), mons[0])
                to = ((prim[0] + prim[2]) / 2, (prim[1] + prim[3]) / 2)
            if self.drag_files:
                # vuelve con el botón aún pulsado: el arrastre sigue en este equipo
                self.drag_files = None
                self.p.cancel_file_drag()
            # ¿se vuelve arrastrando algo en el otro equipo? (botón izquierdo pulsado allí)
            remote_drag = "left" in self.remote_buttons and to_edge
            self.remote_buttons.clear()
            self.p.end_capture(*to)
            self.cooldown_until = time.monotonic() + 0.3
            self.buttons_down = 0
            if notify:
                self.send({"t": "leave", "drag": remote_drag})
        log("← control local" + (" (con un posible arrastre de archivos desde allí)" if remote_drag else ""))

    # ------------------------------------------------- mensajes del remoto
    def handle_remote(self, msg):
        t = msg["t"]
        if t == "m":
            if self.state == CONTROLLED:
                self.p.inject_move(msg["x"], msg["y"])
        elif t == "k":
            if self.state == CONTROLLED:
                name = msg["k"]
                if self.args.swap_cmd_ctrl and self.peer and self.peer["os"] != self.p.os_name:
                    name = CMD_CTRL_SWAP.get(name, name)
                if self.p.os_name == "mac" and self.args.iso_swap:
                    name = ISO_SWAP.get(name, name)
                self.p.inject_key(name, msg["d"])
        elif t == "mb":
            if self.state == CONTROLLED:
                self.p.inject_button(msg["b"], msg["d"])
        elif t == "sc":
            if self.state == CONTROLLED:
                k = -1 if self.args.invert_scroll else 1
                self.p.inject_scroll(msg["dx"] * k, msg["dy"] * k)
        elif t == "enter":
            # estábamos manejando el otro equipo y allí han usado su propio ratón para
            # venir hacia aquí: ese ratón manda, soltamos la captura
            if self.state == CONTROLLING:
                self.leave_remote(notify=False)
            self.state = CONTROLLED
            self.p.inject_move(msg["x"], msg["y"])
            log("← el otro equipo controla este")
        elif t == "leave":
            if self.state == CONTROLLED:
                self.state = LOCAL
                log("→ el otro equipo ha recuperado su control")
                if msg.get("drag"):
                    # se ha ido con el botón pulsado: quizá arrastrando archivos de aquí.
                    # La detección tarda (en Windows hasta ~1 s): fuera del hilo de red.
                    threading.Thread(target=self._drag_out_to_peer, daemon=True).start()
                else:
                    self.p.release_all()
                if self.args.clipboard:
                    self._send_clipboard_async()
        elif t == "layout":
            # el otro equipo ha movido la disposición: guardamos la opuesta
            if self.layout is None or msg["ts"] >= self.layout["ts"]:
                self.set_layout(-msg["dx"], -msg["dy"], ts=msg["ts"], notify=False)
                log("disposición de pantallas actualizada desde el otro equipo")
        elif t == "monitors":
            if self.peer:
                self.peer["monitors"] = msg["monitors"]
                self.peer["rects"] = L.rects(msg["monitors"])
                if self.on_peer_changed:
                    self.on_peer_changed(self.peer)
        elif t == "clip":
            self._set_clipboard_async(msg.get("text", ""))
        elif t in ("fbatch", "fstart", "fdata", "fend", "fdone", "fabort"):
            self.receiver.handle(msg)
        elif t == "clipoffer":
            self._receive_clip_offer(msg)
        elif t == "clipget":
            self._serve_clip_request(msg["id"])
        elif t == "upd_req":
            self._serve_update(msg["id"])
        elif t == "upd_manifest":
            if self.updater and self.updater.id == msg.get("id"):
                self.updater.set_manifest(msg)

    def on_disconnect(self):
        with self.lock:
            if self.state == CONTROLLING:
                self.leave_remote(notify=False)
            elif self.state == CONTROLLED:
                self.p.release_all()
            self.state = LOCAL
            self.peer = None
            self.chan = None
            self.outq = None
            self.fileq = None
            self.receiver.connection_lost()

    # ------------------------------------------------------------- conexión
    def run_connection(self, sock, master_key, is_server):
        self.is_server = is_server
        try:
            chan = net.SecureChannel(sock, master_key, is_server)
            chan.send({"t": "hello", "v": VERSION, "os": self.p.os_name, "monitors": self.monitors(),
                       "name": socket.gethostname(), "layout": self.layout,
                       "build": self.build, "code": self.code_hash})
            hello = chan.recv()
            if hello.get("t") != "hello":
                raise net.AuthError("saludo inesperado")
        except (net.AuthError, OSError, ValueError) as e:
            log("conexión rechazada:", e)
            sock.close()
            return
        mons = hello["monitors"]
        if hello.get("v", 1) < VERSION:
            log("AVISO: el otro equipo usa una versión antigua de kvmlink; actualízalo.")
        chan.sock.settimeout(PEER_TIMEOUT)
        outq = queue.Queue()
        fileq = queue.Queue(maxsize=files.FILEQ_SIZE)
        with self.lock:
            self.chan, self.outq, self.fileq = chan, outq, fileq
            self.peer = {"os": hello["os"], "name": hello.get("name", "?"),
                         "monitors": mons, "rects": L.rects(mons), "version": hello.get("v", 1),
                         "build": hello.get("build"), "code": hello.get("code")}
            self._negotiate_layout(hello)
        if self.on_peer_changed:
            self.on_peer_changed(self.peer)
        self.last_rx = time.monotonic()
        log(f"conectado con {self.peer['name']} ({self.peer['os']}), {len(mons)} pantalla(s)")

        stop = threading.Event()
        sender = threading.Thread(target=self._sender_loop, args=(chan, outq, fileq, stop), daemon=True)
        sender.start()
        if self.peer.get("version", 1) >= 4:
            threading.Thread(target=self._clip_watch, args=(stop,), name="clip-watch", daemon=True).start()
        try:
            while not stop.is_set():
                msg = chan.recv()
                self.last_rx = time.monotonic()
                self.handle_remote(msg)
        except socket.timeout:
            log("el otro equipo no responde")
        except (OSError, ConnectionError, net.AuthError, ValueError) as e:
            if not stop.is_set() and not chan.closed:
                log("conexión perdida:", e)
        finally:
            stop.set()
            outq.put(None)
            chan.close()
            self.on_disconnect()
            log("desconectado")

    # ------------------------------------------- copiar y pegar archivos
    def _clip_watch(self, stop):
        """Si se copian archivos en este equipo, se ofrecen al otro (solo nombres y
        tamaños; el contenido viaja cuando allí se pegan)."""
        try:
            self.p.reset_clipboard_watch()
        except Exception as e:
            log("no se puede vigilar el portapapeles:", repr(e))
            return
        while not stop.wait(0.6):
            try:
                changed, paths = self.p.clipboard_files_changed()
            except Exception as e:
                log("error al leer el portapapeles:", repr(e))
                continue
            if changed:
                if paths:
                    self._offer_clipboard(paths)
                else:
                    self.clip_offer = None

    def _offer_clipboard(self, paths):
        items = files.collect(paths)
        if not items:
            return
        if len(items) > CLIP_MAX_ENTRIES:
            log(f"demasiados archivos copiados ({len(items)}) para ofrecerlos al otro equipo")
            return
        oid = f"c{int(time.time() * 1000):x}"
        label = files.batch_label(items)
        self.clip_offer = {"id": oid, "items": items}
        self.send({"t": "clipoffer", "id": oid, "label": label,
                   "items": [[rel, size] for _, rel, size in items]})
        log(f"copiado {label} ({files.human(sum(s for _, _, s in items))}): "
            f"se puede pegar en {self.peer['name'] if self.peer else 'el otro equipo'}")

    def _receive_clip_offer(self, msg):
        if not hasattr(self.p, "offer_clipboard_files"):
            log(f"{msg.get('label')} copiado en el otro equipo; pegar archivos en este "
                "equipo aún no está disponible")
            return
        root = files.clip_temp_root()
        if self._clip_fetch:
            self._clip_fetch.close()
        files.clean_clip_temp(root, keep=msg["id"])
        fetch = self._clip_fetch = files.ClipFetch(self, msg["id"], msg["items"],
                                                   msg.get("label", "archivos"), root, log)
        self.p.offer_clipboard_files(fetch)
        log(f"{fetch.label} copiado en {self.peer['name'] if self.peer else 'el otro equipo'}: "
            "listo para pegar aquí")

    def _serve_clip_request(self, oid):
        offer = self.clip_offer
        if not offer or offer["id"] != oid:
            self.send({"t": "fabort", "id": oid, "reason": "lo copiado ya no está disponible"})
            return
        files.Sender(self, None, log, items=offer["items"], tid=oid).start()

    # ------------------------------------------------ actualizarse entre equipos
    def update_info(self):
        """Para la interfaz: ¿hay diferencia de versión con el otro equipo?"""
        peer = self.peer
        if not peer or peer.get("version", 1) < 5:
            return None
        up = self.updater
        return {"my_build": self.build, "peer_build": peer.get("build"),
                "differs": peer.get("code") != self.code_hash,
                "peer_newer": (peer.get("build") or 0) > self.build,
                "status": up.status if up else "inactivo", "detail": up.detail if up else ""}

    def request_update(self, config_dir):
        """Actualiza este equipo con la versión del otro (solo si el usuario lo pide)."""
        info = self.update_info()
        if not info or not info["differs"]:
            raise ValueError("No hay ninguna versión distinta en el otro equipo")
        if self.updater and self.updater.status in ("descargando", "instalando"):
            raise ValueError("Ya se está actualizando")
        self.updater = update.Updater(self, log, config_dir, program_dir=self.program_dir,
                                      on_applied=self.on_update_applied)
        self.updater.start()

    def _serve_update(self, uid):
        """El otro equipo ha pedido nuestra versión: le enviamos los archivos del programa
        (los tipos que no conozca los aparta sin instalarlos, ver update.Updater)."""
        man = version.manifest(self.program_dir)
        self.send({"t": "upd_manifest", "id": uid, "build": self.build,
                   "files": [list(f) for f in man]})
        items = [(os.path.join(self.program_dir, n), n, s) for n, s, _ in man]
        log(f"enviando la versión {self.build} de kvmlink al otro equipo")
        files.Sender(self, None, log, items=items, tid=uid,
                     label=f"kvmlink versión {self.build}").start()

    def _sender_loop(self, chan, outq, fileq, stop):
        """Envía ratón/teclado (outq) con prioridad; los trozos de archivo (fileq)
        solo cuando no hay nada de ratón/teclado pendiente, de uno en uno."""
        try:
            while not stop.is_set():
                items = []
                while True:
                    try:
                        items.append(outq.get_nowait())
                    except queue.Empty:
                        break
                if not items:
                    try:
                        chunk = fileq.get_nowait()
                    except queue.Empty:
                        chunk = None
                    if chunk is not None:
                        chan.send(chunk)
                        continue
                    try:
                        items = [outq.get(timeout=PING_EVERY)]
                    except queue.Empty:
                        chan.send({"t": "ping"})
                        continue
                out = []
                for it in items:
                    if it is None:
                        return
                    if it is files.NUDGE:
                        continue                # solo servía para despertar este hilo
                    if it["t"] == "m" and out and out[-1]["t"] == "m":
                        out[-1] = it            # agrupar movimientos acumulados
                    else:
                        out.append(it)
                if out:
                    chan.send_many(out)         # una sola escritura en el socket
        except OSError:
            pass
        finally:
            stop.set()
            chan.close()


# ---------------------------------------------------------- gestor de red
class Link:
    """Mantiene la conexión con el otro equipo en un hilo; se puede parar y relanzar."""

    def __init__(self, core):
        self.core = core
        self.status = "detenido"
        self._thread = None
        self._stop = threading.Event()
        self._srv = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, mode, target, port, key, beacon=True):
        self.stop()
        self._stop = threading.Event()
        fn = self._serve if mode == "listen" else self._connect
        self._thread = threading.Thread(target=fn, args=(target, port, key, beacon, self._stop),
                                        name="link", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        srv, self._srv = self._srv, None
        if srv:
            try:
                srv.close()
            except OSError:
                pass
        chan = self.core.chan
        if chan:
            chan.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        self.status = "detenido"

    def _serve(self, target, port, key, beacon, stop):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                # en Windows SO_REUSEADDR permite que otro programa escuche en el mismo
                # puerto; sin él Windows lo impide y aun así se puede reabrir enseguida
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(1)
            srv.settimeout(1.0)
        except OSError as e:
            self.status = f"error: no se puede usar el puerto {port} ({e})"
            log(self.status)
            return
        self._srv = srv
        if beacon:
            threading.Thread(target=net.beacon_forever, args=(port, stop), daemon=True).start()
        log(f"esperando conexión en el puerto {port} …")
        try:
            while not stop.is_set():
                self.status = f"esperando conexión (puerto {port})"
                try:
                    sock, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                sock.settimeout(None)
                log("conexión entrante de", addr[0])
                self.status = f"conectado ({addr[0]})"
                self.core.run_connection(sock, key, is_server=True)
        finally:
            try:
                srv.close()
            except OSError:
                pass

    def _connect(self, target, port, key, beacon, stop):
        while not stop.is_set():
            host, p = target, port
            if host == "auto":
                self.status = "buscando el otro equipo en la red…"
                log("buscando el otro equipo en la red local …")
                found = net.discover(timeout=5)
                if not found:
                    continue
                host, p, name = found
                log(f"encontrado {name} en {host}:{p}")
            self.status = f"conectando con {host}…"
            try:
                sock = socket.create_connection((host, p), timeout=5)
            except OSError as e:
                self.status = f"no se pudo conectar con {host} (reintentando)"
                log(f"no se pudo conectar a {host}:{p}: {e}; reintento en 3 s")
                stop.wait(3)
                continue
            if stop.is_set():
                sock.close()
                break
            self.status = f"conectado ({host})"
            self.core.run_connection(sock, key, is_server=False)
            stop.wait(1)


def install_exit_signals(on_signal=None):
    """Cerrar la ventana de la Terminal (SIGHUP), apagar o un kill (SIGTERM) deben
    pasar por la salida ordenada para restaurar ratón y cursor. on_signal: en vez de
    lanzar SystemExit (que no sirve si el bucle principal lo lleva macOS), pedir salir."""
    import signal

    def _exit(signum, frame):
        if on_signal:
            on_signal()
        else:
            raise SystemExit(0)
    for name in ("SIGTERM", "SIGHUP", "SIGINT" if on_signal else ""):
        if name and hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _exit)
            except ValueError:
                pass


# ------------------------------------------------------------- modo terminal
def main_cli(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", action="store_true", help="esperar a que el otro equipo se conecte")
    mode.add_argument("--connect", metavar="IP|auto", help="conectar a la IP indicada o descubrirla (auto)")
    ap.add_argument("--edge", required=True, choices=SIDES,
                    help="lado de ESTA pantalla en el que está el otro equipo")
    ap.add_argument("--port", type=int, default=net.DEFAULT_PORT)
    ap.add_argument("--password", help="contraseña compartida (o variable KVMLINK_PASSWORD)")
    ap.add_argument("--speed", type=float, default=1.0, help="multiplicador de velocidad del ratón remoto")
    ap.add_argument("--invert-scroll", action="store_true", help="invertir el scroll recibido")
    ap.add_argument("--no-swap-cmd-ctrl", dest="swap_cmd_ctrl", action="store_false",
                    help="no intercambiar Cmd y Ctrl entre Mac y Windows")
    ap.add_argument("--iso-swap", action="store_true",
                    help="(Mac) intercambiar las teclas º/< si salen cambiadas")
    ap.add_argument("--no-clipboard", dest="clipboard", action="store_false",
                    help="no sincronizar el portapapeles de texto")
    ap.add_argument("--no-beacon", action="store_true", help="no anunciarse por broadcast en la LAN")
    args = ap.parse_args(argv)

    password = (args.password or os.environ.get("KVMLINK_PASSWORD")
                or getpass.getpass("Contraseña compartida: "))
    if len(password) < 6:
        sys.exit("Usa una contraseña de al menos 6 caracteres (la misma en ambos equipos).")
    key = net.derive_master_key(password)

    plat = load_platform()
    opts = {k: getattr(args, k) for k in DEFAULT_OPTIONS}
    core = Core(plat, opts, edge=args.edge, is_server=args.listen)
    plat.start(core)
    threading.Thread(target=core.watchdog, name="watchdog", daemon=True).start()
    install_exit_signals()
    log(f"kvmlink en {plat.os_name}, pantallas {core.monitors()}")
    link = Link(core)
    link.start("listen" if args.listen else "connect", args.connect, args.port, key,
               beacon=not args.no_beacon)
    try:
        plat.main_loop(lambda: False)   # en el Mac atiende las peticiones del portapapeles
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()
        core.on_disconnect()
        plat.stop()
        log("adiós")


def _wait_for_exit(pid, timeout=30):
    """Al reiniciarse tras una actualización: esperar a que termine la versión anterior
    (que aún tiene el puerto, los hooks y la ventana) antes de arrancar."""
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
        if h:
            ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000))
            ctypes.windll.kernel32.CloseHandle(h)
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def main():
    argv = sys.argv[1:]
    if "--wait-pid" in argv:
        i = argv.index("--wait-pid")
        _wait_for_exit(int(argv[i + 1]))
        del argv[i:i + 2]
    if not argv or set(argv) <= {"--gui", "--setup", "--no-window"}:
        import gui
        sys.exit(gui.main(argv))
    main_cli(argv)


if __name__ == "__main__":
    main()
