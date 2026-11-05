"""Transferencia de archivos entre los dos equipos por el canal cifrado ya abierto.

Protocolo (todos los mensajes van por la cola de archivos, así que llegan en orden):
    fbatch {id, n, bytes}          comienzo de un envío (uno o varios archivos/carpetas)
    fstart {id, name, size}        comienza un archivo; name es relativo con "/" ("Carpeta/a.txt")
    fdata  {id, d}                 trozo del archivo en base64
    fend   {id}                    fin del archivo
    fdone  {id}                    fin del envío
    fabort {id, reason}            envío cancelado

Los trozos son pequeños y el emisor les da menos prioridad que al ratón y el teclado
(ver Core._sender_loop), para que la copia no trabe el control.
"""
import base64
import os
import queue
import re
import subprocess
import sys
import threading
import time

CHUNK = 64 * 1024
FILEQ_SIZE = 8                 # trozos en vuelo: limita la memoria y no satura la cola
NUDGE = {"t": "_nudge"}        # despierta al hilo emisor; no se envía
_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def default_receive_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def collect(paths):
    """[(ruta_absoluta, nombre_relativo, tamaño)] incluyendo el contenido de carpetas."""
    out = []
    for p in paths:
        p = os.path.abspath(p)
        base = os.path.basename(p.rstrip("/\\")) or "archivo"
        if os.path.isdir(p):
            for root, dirs, fnames in os.walk(p):
                dirs.sort()
                rel_root = os.path.relpath(root, os.path.dirname(p.rstrip("/\\")))
                for fn in sorted(fnames):
                    full = os.path.join(root, fn)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    out.append((full, "/".join(rel_root.split(os.sep) + [fn]), size))
        elif os.path.isfile(p):
            out.append((p, base, os.path.getsize(p)))
    return out


def batch_label(items):
    """'informe.pdf' o 'Fotos y 2 más' (por elementos de primer nivel)."""
    tops = list(dict.fromkeys(rel.split("/")[0] for _, rel, _ in items))
    return tops[0] + (f" y {len(tops) - 1} más" if len(tops) > 1 else "") if tops else "archivos"


def safe_parts(name):
    """Convierte el nombre relativo que manda el otro equipo en partes seguras:
    nada de rutas absolutas, "..", unidades ni caracteres no válidos en Windows."""
    parts = []
    for comp in name.split("/"):
        comp = _BAD_CHARS.sub("_", comp).strip().rstrip(".")
        if comp in ("", ".", ".."):
            continue
        if comp.split(".")[0].upper() in _RESERVED:
            comp = "_" + comp
        parts.append(comp[:200])
    if not parts:
        raise ValueError(f"nombre de archivo no válido: {name!r}")
    return parts


def unique_name(folder, name):
    if not os.path.exists(os.path.join(folder, name)):
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(folder, f"{stem} ({i}){ext}")):
        i += 1
    return f"{stem} ({i}){ext}"


def open_folder(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 — abre el Explorador en esa carpeta
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
    except OSError:
        pass


class Transfers:
    """Estado de los envíos/recepciones para la interfaz."""

    def __init__(self, keep=8):
        self._lock = threading.Lock()
        self._items = {}
        self._keep = keep

    def start(self, tid, direction, label, total):
        with self._lock:
            self._items[tid] = {"id": tid, "dir": direction, "label": label, "done": 0,
                                "total": total, "status": "en curso", "t": time.time()}
            for old in sorted(self._items.values(), key=lambda x: x["t"])[:-self._keep]:
                if old["status"] != "en curso":
                    del self._items[old["id"]]

    def progress(self, tid, n):
        with self._lock:
            if tid in self._items:
                self._items[tid]["done"] += n

    def finish(self, tid, status, detail=None):
        with self._lock:
            if tid in self._items:
                self._items[tid]["status"] = status
                if detail:
                    self._items[tid]["detail"] = detail

    def snapshot(self):
        with self._lock:
            return sorted((dict(x) for x in self._items.values()), key=lambda x: -x["t"])


class Sender(threading.Thread):
    """Lee los archivos y los pone, en trozos, en la cola de archivos de la conexión."""

    def __init__(self, core, paths, log, items=None, tid=None, label=None):
        """items: lista ya preparada con collect() (para el portapapeles, la misma que
        se anunció al copiar); tid: identificador del envío (el que pidió el otro equipo);
        label: nombre que se muestra en el progreso."""
        super().__init__(name="file-sender", daemon=True)
        self.core, self.paths, self.log = core, list(paths or []), log
        self.items = items
        self.label = label
        self.tid = tid or f"{int(time.time() * 1000):x}"
        self.chan = core.chan
        self.fileq = core.fileq

    def _put(self, msg):
        while True:
            if self.core.chan is not self.chan or self.fileq is None:
                raise ConnectionError("conexión perdida")
            try:
                self.fileq.put(msg, timeout=0.5)
                self.core.nudge()
                return
            except queue.Full:
                continue

    def run(self):
        items = self.items if self.items is not None else collect(self.paths)
        total = sum(s for _, _, s in items)
        if not items:
            self.log("arrastre sin archivos que enviar")
            return
        peer = (self.core.peer or {}).get("name", "el otro equipo")
        label = self.label or batch_label(items)
        self.core.transfers.start(self.tid, "out", label, total)
        self.log(f"enviando {len(items)} archivo(s), {human(total)}, a {peer}")
        try:
            self._put({"t": "fbatch", "id": self.tid, "n": len(items), "bytes": total, "label": label})
            for full, rel, size in items:
                self._put({"t": "fstart", "id": self.tid, "name": rel, "size": size})
                with open(full, "rb") as f:
                    while True:
                        data = f.read(CHUNK)
                        if not data:
                            break
                        self._put({"t": "fdata", "id": self.tid, "d": base64.b64encode(data).decode()})
                        self.core.transfers.progress(self.tid, len(data))
                self._put({"t": "fend", "id": self.tid})
            self._put({"t": "fdone", "id": self.tid})
            self.core.transfers.finish(self.tid, "enviado")
            self.log(f"envío terminado: {label}")
        except (OSError, ConnectionError) as e:
            self.core.transfers.finish(self.tid, "error", str(e))
            self.log("envío cancelado:", e)
            try:
                self._put({"t": "fabort", "id": self.tid, "reason": str(e)})
            except ConnectionError:
                pass


def clip_temp_root():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "kvmlink-portapapeles")


def clean_clip_temp(root, keep=None):
    """Borra lo descargado para pegados anteriores (ya se copió a su destino)."""
    import shutil
    try:
        for name in os.listdir(root):
            if name != keep:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    except OSError:
        pass


def clip_entries(items):
    """De [(nombre_relativo, tamaño)] que llega del otro equipo a la lista para el
    portapapeles: nombres seguros, y cada carpeta antes que su contenido.
    Devuelve [(nombre_seguro, tamaño, es_carpeta)]."""
    out, seen = [], set()
    for rel, size in items:
        parts = safe_parts(rel)
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in seen:
                seen.add(d)
                out.append((d, 0, True))
        out.append(("/".join(parts), size, False))
    return out


class ClipFetch:
    """Archivos copiados en el otro equipo y ofrecidos en nuestro portapapeles.
    No se descargan hasta que algo los pega (ensure_started); read() espera a que
    lleguen los bytes pedidos. Se guardan en una carpeta temporal."""

    STALL_TIMEOUT = 30.0

    def __init__(self, core, offer_id, items, label, root, log):
        self.core, self.offer_id, self.label, self.log = core, offer_id, label, log
        self.entries = clip_entries(items)
        self.root = os.path.join(root, offer_id)
        self.cond = threading.Condition()
        self.progress = {}     # nombre_seguro -> [bytes_escritos, terminado]
        self.started = False
        self.done = False
        self.error = None
        self.last_activity = time.monotonic()
        self._handles = {}

    def ensure_started(self):
        with self.cond:
            if self.started:
                return
            self.started = True
            self.last_activity = time.monotonic()
        if self.core.chan is None:
            self.fail("no hay conexión con el otro equipo")
            return
        os.makedirs(self.root, exist_ok=True)
        self.core.receiver.targets[self.offer_id] = self
        self.core.transfers.start(self.offer_id, "in", "Pegando " + self.label,
                                  sum(s for _, s, d in self.entries if not d))
        self.log(f"pegando {self.label}: pidiendo los archivos al otro equipo")
        self.core.send({"t": "clipget", "id": self.offer_id})

    # --- llamados por el Receiver al ir llegando
    def path_for(self, key):
        return os.path.join(self.root, *key.split("/"))

    def on_start(self, key):
        with self.cond:
            self.progress[key] = [0, False]
            self.last_activity = time.monotonic()
            self.cond.notify_all()

    def on_data(self, key, n):
        with self.cond:
            self.progress[key][0] += n
            self.last_activity = time.monotonic()
            self.cond.notify_all()

    def on_end(self, key):
        with self.cond:
            self.progress.setdefault(key, [0, False])[1] = True
            self.cond.notify_all()

    def on_done(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()
        self.core.transfers.finish(self.offer_id, "pegado")

    def fail(self, reason):
        with self.cond:
            self.error = reason
            self.cond.notify_all()
        self.core.transfers.finish(self.offer_id, "error", reason)
        self.log("no se pudieron pegar los archivos:", reason)

    def wait_done(self):
        """Espera a que llegue todo (para el Mac, que necesita los archivos completos
        antes de dar su ubicación al Finder). True si terminó bien."""
        with self.cond:
            while not (self.done or self.error):
                if time.monotonic() - self.last_activity > self.STALL_TIMEOUT:
                    self.error = "el otro equipo dejó de enviar"
                    break
                self.cond.wait(0.5)
            return self.done and not self.error

    # --- llamado por el IStream que lee el Explorador (bloquea)
    def read(self, key, pos, n):
        with self.cond:
            while True:
                if self.error:
                    raise IOError(self.error)
                st = self.progress.get(key)
                if st and (st[1] or st[0] >= pos + n):
                    break
                if self.done:   # terminó sin este archivo
                    raise IOError("el archivo no llegó")
                if time.monotonic() - self.last_activity > self.STALL_TIMEOUT:
                    raise IOError("el otro equipo dejó de enviar")
                self.cond.wait(0.5)
        f = self._handles.get(key)
        if f is None:
            f = self._handles[key] = open(self.path_for(key), "rb")
        f.seek(pos)
        data = f.read(n)
        if st[1] and pos + len(data) >= os.path.getsize(self.path_for(key)):
            f.close()                      # archivo leído entero: no dejarlo abierto
            self._handles.pop(key, None)
        return data

    def close(self):
        for f in self._handles.values():
            try:
                f.close()
            except OSError:
                pass
        self._handles.clear()


class Receiver:
    """Escribe lo que llega en la carpeta de destino. Solo un envío a la vez (llegan
    en orden por la misma conexión). Los envíos que pedimos nosotros (targets: al pegar,
    ClipFetch; al actualizar, update.Updater) van a su carpeta con los nombres exactos
    y avisando del progreso."""

    def __init__(self, core, log):
        self.core, self.log = core, log
        self.batch = None
        self.f = None
        self.cur = None    # (ruta_final, ruta_parcial)
        self.targets = {}   # id del envío pedido -> ClipFetch o Updater

    def _dest(self):
        d = getattr(self.core.args, "receive_dir", "") or default_receive_dir()
        os.makedirs(d, exist_ok=True)
        return d

    def handle(self, msg):
        t, tid = msg["t"], msg.get("id")
        if t == "fabort" and tid in self.targets and (self.batch is None or self.batch["id"] != tid):
            # el otro equipo no puede dar lo que se pidió (p.ej. al pegar, ya copió otra cosa)
            self.targets.pop(tid).fail(msg.get("reason") or "cancelado por el otro equipo")
            return
        try:
            if t == "fbatch":
                self.abort("nuevo envío")
                clip = self.targets.get(tid)
                if clip:   # pedido por nosotros: ya tiene su entrada de progreso
                    self.batch = {"id": tid, "clip": clip}
                    return
                dest = self._dest()
                self.batch = {"id": tid, "dest": dest, "tops": {}}
                self.core.transfers.start(tid, "in", msg.get("label", "archivos"), msg.get("bytes", 0))
                self.log(f"recibiendo {msg.get('n')} archivo(s), {human(msg.get('bytes', 0))}, en {dest}")
                return
            b = self.batch
            if b is None or b["id"] != tid:
                return
            if b.get("clip"):
                self._handle_clip(b["clip"], t, msg)
                return
            if t == "fstart":
                parts = safe_parts(msg["name"])
                top = parts[0]
                if top not in b["tops"]:
                    b["tops"][top] = unique_name(b["dest"], top)
                final = os.path.join(b["dest"], b["tops"][top], *parts[1:])
                os.makedirs(os.path.dirname(final), exist_ok=True)
                partial = final + ".kvmpart"
                self.f = open(partial, "wb")
                self.cur = (final, partial)
            elif t == "fdata" and self.f:
                data = base64.b64decode(msg["d"])
                self.f.write(data)
                self.core.transfers.progress(tid, len(data))
            elif t == "fend" and self.f:
                self.f.close()
                self.f = None
                final, partial = self.cur
                if os.path.exists(final):
                    final = os.path.join(os.path.dirname(final),
                                         unique_name(os.path.dirname(final), os.path.basename(final)))
                os.replace(partial, final)
                self.cur = None
            elif t == "fdone":
                self.core.transfers.finish(tid, "recibido", b["dest"])
                self.log(f"archivos recibidos en {b['dest']}")
                if getattr(self.core.args, "open_on_receive", True):
                    open_folder(b["dest"])
                self.batch = None
            elif t == "fabort":
                self.abort(msg.get("reason") or "cancelado por el otro equipo")
        except (OSError, ValueError) as e:
            self.log("error al recibir archivos:", e)
            self.core.transfers.finish(tid, "error", str(e))
            self.abort(str(e), quiet=True)

    def _handle_clip(self, clip, t, msg):
        tid = msg.get("id")
        if t == "fstart":
            key = "/".join(safe_parts(msg["name"]))
            path = clip.path_for(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.f = open(path, "wb")
            self.cur = ("clip", key)
            clip.on_start(key)
        elif t == "fdata" and self.f:
            data = base64.b64decode(msg["d"])
            self.f.write(data)
            self.f.flush()            # el Explorador lo lee mientras sigue llegando
            clip.on_data(self.cur[1], len(data))
            self.core.transfers.progress(tid, len(data))
        elif t == "fend" and self.f:
            self.f.close()
            self.f = None
            clip.on_end(self.cur[1])
            self.cur = None
        elif t == "fdone":
            self.targets.pop(tid, None)
            clip.on_done()
            self.batch = None
        elif t == "fabort":
            self.abort(msg.get("reason") or "cancelado por el otro equipo")

    def abort(self, reason, quiet=False):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None
        clip = self.batch.get("clip") if self.batch else None
        if self.cur and not clip:
            try:
                os.remove(self.cur[1])
            except OSError:
                pass
        self.cur = None
        if clip:
            clip.fail(reason)
            self.targets.pop(self.batch["id"], None)
            self.batch = None
            return
        if self.batch:
            if not quiet:
                self.core.transfers.finish(self.batch["id"], "cancelado", reason)
                self.log("recepción cancelada:", reason)
            self.batch = None

    def connection_lost(self):
        """Cancela lo que se estuviera recibiendo y los pegados que esperaban datos."""
        self.abort("conexión perdida")
        for c in list(self.targets.values()):
            c.fail("conexión perdida")
        self.targets.clear()
