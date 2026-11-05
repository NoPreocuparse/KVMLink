"""Interfaz gráfica de kvmlink: un pequeño servidor web local (solo 127.0.0.1) que sirve
ui.html y una API JSON. Se abre en una ventana propia (Edge/Chrome en modo
aplicación) o en el navegador.

Cerrar la ventana NO detiene kvmlink: sigue funcionando y volviendo a abrir el
lanzador se recupera la ventana. Para pararlo, botón "Salir" de la interfaz.
"""
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser

import files
import install
import kvmlink as K
import layout as L
import net
import version

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui.html")
INSTANCE_FILE = os.path.join(K.config_dir(), "ui.json")
LOG_FILE = os.path.join(K.config_dir(), "kvmlink.log")
RESTART_FILE = os.path.join(K.config_dir(), "reinicio.json")
UI_PORT = 24880
PLACEHOLDER_PEER = [[0, 0, 1470, 956, True, 1]]   # hasta conocer las pantallas reales


_ips_cache = (0.0, [])


def local_ips():
    """IPs de este equipo; caché de 30 s (gethostbyname_ex puede tardar)."""
    global _ips_cache
    t, ips = _ips_cache
    if time.monotonic() - t < 30 and ips:
        return ips
    ips = _query_ips()
    _ips_cache = (time.monotonic(), ips)
    return ips


def _query_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))   # no envía nada; solo elige la interfaz de salida
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class App:
    def __init__(self):
        self.cfg = K.load_config()
        self.lock = threading.RLock()
        self.plat = K.load_platform()
        self.core = K.Core(self.plat, self.cfg["options"], edge=self.cfg.get("edge"),
                           layout=self.cfg.get("layout"), is_server=self.cfg["mode"] == "listen")
        self.core.on_layout_changed = self._layout_changed
        self.core.on_peer_changed = self._peer_changed
        self.core.on_update_applied = self._update_applied
        self.link = K.Link(self.core)
        self.quit_event = threading.Event()
        self.restart_requested = False

    def autostart_info(self):
        ok, why = install.autostart_supported()
        return {"supported": ok, "hint": why, "enabled": ok and install.autostart_enabled()}

    def status_text(self):
        """Estado en una línea, para el icono de la bandeja / barra de menús."""
        peer = self.core.peer
        if peer:
            state = {"controlling": f"manejando {peer['name']}",
                     "controlled": f"{peer['name']} maneja este equipo"}.get(self.core.state)
            return state or f"conectado con {peer['name']}"
        return self.link.status if self.link.running else "detenido"

    def _update_applied(self):
        """Se ha instalado una versión nueva: cerrar y relanzar (ver main)."""
        self.restart_requested = True
        threading.Timer(1.5, self.quit_event.set).start()   # que la interfaz lo vea antes

    # -------------------------------------------------------- persistencia
    def save(self):
        with self.lock:
            try:
                K.save_config(self.cfg)
            except OSError as e:
                K.log("no se pudo guardar la configuración:", e)

    def _layout_changed(self, lay):
        with self.lock:
            self.cfg["layout"] = lay
        self.save()

    def _peer_changed(self, peer):
        with self.lock:
            self.cfg["peer"] = {"name": peer["name"], "os": peer["os"], "monitors": peer["monitors"]}
        self.save()

    # ----------------------------------------------------------- arranque
    def start(self):
        self.plat.start(self.core)
        threading.Thread(target=self.core.watchdog, name="watchdog", daemon=True).start()
        K.log(f"kvmlink en {self.plat.os_name}, pantallas: {self.core.monitors()}")
        if self.cfg.get("key") and self.cfg.get("autostart", True):
            self.start_link()

    def start_link(self):
        cfg = self.cfg
        if not cfg.get("key"):
            raise ValueError("Falta la contraseña")
        self.core.is_server = cfg["mode"] == "listen"
        self.link.start(cfg["mode"], cfg.get("target") or "auto", int(cfg.get("port") or net.DEFAULT_PORT),
                        bytes.fromhex(cfg["key"]))

    def shutdown(self):
        try:
            self.link.stop()
            self.core.on_disconnect()
        finally:
            self.plat.stop()

    # --------------------------------------------------------------- API
    def peer_view(self):
        """Pantallas del otro equipo: reales si está conectado, recordadas si no."""
        peer = self.core.peer
        if peer:
            return {"name": peer["name"], "os": peer["os"], "monitors": peer["monitors"],
                    "connected": True, "outdated": peer.get("version", 1) < K.VERSION}
        saved = self.cfg.get("peer")
        if saved:
            return {**saved, "connected": False}
        return {"name": "Otro equipo", "os": "mac" if sys.platform == "win32" else "win",
                "monitors": PLACEHOLDER_PEER, "connected": False, "placeholder": True}

    def state(self, since=0):
        peer = self.peer_view()
        lay = self.core.layout
        if lay is None:
            dx, dy = self.core.default_layout_offset(L.rects(peer["monitors"]))
            lay = {"dx": dx, "dy": dy, "ts": None}
        cfg = self.cfg
        return {
            "me": {"name": socket.gethostname(), "os": self.plat.os_name,
                   "monitors": self.core.monitors(), "ips": local_ips()},
            "peer": peer,
            "layout": lay,
            "link": {"running": self.link.running, "status": self.link.status},
            "state": self.core.state,
            "config": {"mode": cfg["mode"], "target": cfg.get("target") or "auto",
                       "port": cfg.get("port") or net.DEFAULT_PORT, "has_key": bool(cfg.get("key")),
                       "options": cfg["options"]},
            "logs": K.logs_since(since),   # [[número, línea], ...] solo las nuevas
            "transfers": self.core.transfers.snapshot(),
            "receive_dir_default": files.default_receive_dir(),
            "config_file": K.CONFIG_FILE,
            "update": self.core.update_info(),
            "autostart": self.autostart_info(),
            "permissions_missing": getattr(self.plat, "permissions_missing", lambda: [])(),
            "restarting": self.restart_requested,
            "build": version.BUILD,
            "version_name": version.NAME,
        }

    def api(self, path, body):
        if path == "/api/state":
            return self.state(int(body.get("since") or 0))
        if path == "/api/connection":
            mode = body.get("mode")
            if mode not in ("listen", "connect"):
                raise ValueError("Modo no válido")
            target = (body.get("target") or "auto").strip() or "auto"
            port = int(body.get("port") or net.DEFAULT_PORT)
            if not 1 <= port <= 65535:
                raise ValueError("Puerto no válido")
            pw = body.get("password") or ""
            with self.lock:
                if pw:
                    if len(pw) < 6:
                        raise ValueError("La contraseña debe tener al menos 6 caracteres")
                    self.cfg["key"] = net.derive_master_key(pw).hex()
                if not self.cfg.get("key"):
                    raise ValueError("Escribe una contraseña (la misma en los dos equipos)")
                self.cfg.update(mode=mode, target=target, port=port, autostart=True)
            self.save()
            threading.Thread(target=self.start_link, daemon=True).start()
            return {"ok": True}
        if path == "/api/stop":
            with self.lock:
                self.cfg["autostart"] = False
            self.save()
            threading.Thread(target=self.link.stop, daemon=True).start()
            return {"ok": True}
        if path == "/api/start":
            with self.lock:
                self.cfg["autostart"] = True
            self.save()
            threading.Thread(target=self.start_link, daemon=True).start()
            return {"ok": True}
        if path == "/api/layout":
            peer = self.peer_view()
            d = (int(body["dx"]), int(body["dy"]))
            d = L.snap_offset(L.rects(self.core.monitors()), L.rects(peer["monitors"]), d)
            lay = self.core.set_layout(*d)
            K.log("disposición de pantallas guardada")
            return {"ok": True, "layout": lay}
        if path == "/api/options":
            with self.lock:
                opts = self.cfg["options"]
                for k, default in K.DEFAULT_OPTIONS.items():
                    if k in body:
                        v = body[k]
                        if isinstance(default, float):
                            opts[k] = float(v)
                        elif isinstance(default, str):
                            opts[k] = str(v).strip()
                        else:
                            opts[k] = bool(v)
                rd = opts.get("receive_dir")
                if rd:
                    rd = os.path.expanduser(rd)
                    try:
                        os.makedirs(rd, exist_ok=True)
                    except OSError as e:
                        raise ValueError(f"No se puede usar esa carpeta: {e}")
                    opts["receive_dir"] = rd
                opts["speed"] = min(max(opts["speed"], 0.2), 5.0)
                for k, v in opts.items():
                    setattr(self.core.args, k, v)
            self.save()
            return {"ok": True}
        if path == "/api/autostart":
            install.set_autostart(bool(body.get("enabled")))
            K.log("abrir kvmlink al iniciar sesión:", "sí" if install.autostart_enabled() else "no")
            return {"ok": True, "autostart": self.autostart_info()}
        if path == "/api/permissions":
            getattr(self.plat, "open_permission_settings", lambda: None)()
            return {"ok": True}
        if path == "/api/update":
            self.core.request_update(K.config_dir())
            return {"ok": True}
        if path == "/api/quit":
            self.quit_event.set()
            return {"ok": True}
        if path == "/api/ping":
            return {"ok": True, "pid": os.getpid()}
        raise KeyError(path)


class UIServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # En Windows SO_REUSEADDR deja que OTRO programa se ponga a escuchar en el mismo
    # puerto (comprobado: una segunda copia de kvmlink le "robaba" el 24880 a la que ya
    # estaba en marcha). Sin él, Windows lo impide y el puerto se puede reabrir enseguida.
    allow_reuse_address = sys.platform != "win32"


def make_handler(app, token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            # protege la API de otras webs abiertas en el navegador
            return secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path.startswith("/api/") and self._authorized():
                self._api(path, dict(urllib.parse.parse_qsl(query)))
            else:
                self._send(404, {"error": "no encontrado"})

        def do_POST(self):
            path = self.path.split("?")[0]
            if not (path.startswith("/api/") and self._authorized()):
                return self._send(403, {"error": "no autorizado"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "JSON no válido"})
            self._api(path, body)

        def _api(self, path, body):
            try:
                self._send(200, app.api(path, body))
            except KeyError:
                self._send(404, {"error": "no encontrado"})
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                K.log("error en la interfaz:", repr(e))
                self._send(500, {"error": str(e)})
    return Handler


# ------------------------------------------------------- ventana / instancia
def open_window(url):
    """Ventana tipo aplicación si hay Edge o Chrome; si no, el navegador por defecto."""
    try:
        if sys.platform == "win32":
            cands = [shutil.which("msedge"), shutil.which("chrome")]
            for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                         os.environ.get("LOCALAPPDATA")):
                if base:
                    cands += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                              os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
            for exe in cands:
                if exe and os.path.exists(exe):
                    subprocess.Popen([exe, f"--app={url}", "--window-size=1100,920"])
                    return
        elif sys.platform == "darwin":
            for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
                if os.path.exists(f"/Applications/{app}.app"):
                    subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                                      "--window-size=1100,920"])
                    return
            subprocess.Popen(["open", url])
            return
    except OSError:
        pass
    webbrowser.open(url)


def existing_instance_url():
    try:
        with open(INSTANCE_FILE, encoding="utf-8") as f:
            info = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping",
                                     headers={"X-Token": info["token"]})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            if json.load(r).get("ok"):
                return f"http://127.0.0.1:{info['port']}/#{info['token']}"
    except Exception:
        pass
    return None


def fatal(msg):
    K.log(msg)
    if sys.platform == "win32" and (sys.stdout is None or "pythonw" in sys.executable.lower()):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "kvmlink", 0x10)


RESTART_CODE = 42   # kvmlink.command vuelve a lanzar kvmlink si sale con este código


def _take_restart_marker():
    """Si venimos de reiniciarnos tras actualizarnos, reutilizar la clave de la
    interfaz: la ventana abierta se reconecta sola y no hace falta abrir otra."""
    try:
        with open(RESTART_FILE, encoding="utf-8") as f:
            info = json.load(f)
        os.remove(RESTART_FILE)
        if time.time() - info.get("t", 0) < 120:
            return info.get("token")
    except (OSError, ValueError):
        pass
    return None


def _restart(token):
    """Relanza kvmlink (la versión recién instalada). Devuelve el código de salida."""
    os.makedirs(os.path.dirname(RESTART_FILE), exist_ok=True)
    with open(RESTART_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": token, "t": time.time()}, f)
    if sys.platform == "darwin" and os.environ.get("KVMLINK_LAUNCHER") in ("command", "app"):
        # en el Mac lo relanza quien lo arrancó (kvmlink.app o kvmlink.command): así
        # conserva los permisos de Accesibilidad, que son de ese lanzador
        return RESTART_CODE
    exe = sys.executable
    if sys.platform == "win32":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else exe
    cmd = [exe, os.path.join(HERE, "kvmlink.py"), "--wait-pid", str(os.getpid())]
    if sys.platform == "win32":
        subprocess.Popen(cmd, creationflags=0x8 | 0x200, close_fds=True)   # DETACHED | NEW_GROUP
    else:
        subprocess.Popen(cmd, start_new_session=True, close_fds=True)
    return 0


def _relaunch_mac_app(token):
    """Se ha recompilado kvmlink.app: este proceso cuelga del lanzador antiguo, así que
    sale (el lanzador sale con él) y un proceso aparte abre la app nueva. Si había una
    ventana abierta, la app nueva reutiliza su clave y la ventana se reconecta."""
    if token:
        with open(RESTART_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "t": time.time()}, f)
    K.log("abriendo la app nueva; macOS volverá a pedir los permisos de kvmlink")
    subprocess.Popen(["/bin/sh", "-c", 'sleep 3; open "$1"', "sh", install.MAC_APP],
                     start_new_session=True, close_fds=True)
    return 0


def main(argv=()):
    no_window = "--no-window" in argv
    url = existing_instance_url()
    if url:   # ya está funcionando: solo abrir su ventana
        if not no_window:
            open_window(url)
        return 0

    K.open_log_file(LOG_FILE)
    try:
        app = App()
    except Exception:
        fatal("No se pudo iniciar kvmlink:\n\n" + traceback.format_exc())
        sys.exit(1)
    # apagar, cerrar sesión o un kill: salir de forma ordenada (restaurando el cursor)
    K.install_exit_signals(on_signal=app.quit_event.set)

    token = _take_restart_marker()
    if token:
        no_window = True   # la ventana que había sigue abierta y se reconecta
        K.log(f"kvmlink reiniciado tras actualizarse (versión {version.BUILD})")
    # si la actualización cambia la instalación (accesos directos, app del Mac), ponerla
    # al día aquí, sin tener que volver a ejecutar el instalador
    try:
        refreshed = install.refresh_installation(K.log)
    except Exception as e:
        refreshed = None
        K.log("no se pudo poner al día la instalación:", repr(e))
    if refreshed == "relanzar" and os.environ.get("KVMLINK_LAUNCHER") == "app":
        return _relaunch_mac_app(token)
    token = token or secrets.token_urlsafe(18)
    try:
        server = UIServer(("127.0.0.1", UI_PORT), make_handler(app, token))
    except OSError:
        server = UIServer(("127.0.0.1", 0), make_handler(app, token))
    port = server.server_address[1]
    os.makedirs(K.config_dir(), exist_ok=True)
    with open(INSTANCE_FILE, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)

    threading.Thread(target=server.serve_forever, name="ui", daemon=True).start()
    try:
        app.start()
    except Exception as e:
        fatal(f"No se pudo iniciar la captura de ratón/teclado:\n\n{e}")
        server.shutdown()
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/#{token}"
    K.log("interfaz en", url.split("#")[0])
    if not no_window:
        open_window(url)
    tray = {"open": lambda: open_window(url), "quit": app.quit_event.set, "status": app.status_text}
    if sys.platform == "win32":
        try:
            tray["icon"] = install.win_icon_path()
        except Exception as e:
            K.log("no se pudo crear el icono:", repr(e))
    try:
        # el hilo principal atiende al sistema: icono de la bandeja / barra de menús y,
        # en el Mac, las peticiones del portapapeles
        app.plat.main_loop(app.quit_event.is_set, tray)
        time.sleep(0.3)   # que la interfaz reciba la respuesta de "Salir"
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.shutdown()
        server.server_close()   # libera el puerto para la versión que se va a lanzar
        try:
            os.remove(INSTANCE_FILE)
        except OSError:
            pass
        K.log("adiós")
    if app.restart_requested:
        K.log("reiniciando con la versión nueva …")
        return _restart(token)
    return 0
