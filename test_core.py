"""Prueba de extremo a extremo sin tocar el ratón real: dos Core con plataformas simuladas
conectados por loopback (TCP + cifrado reales), más pruebas de la geometría."""
import queue
import socket
import threading
import time

import layout as L
import net
from kvmlink import Core, Link


class FakePlatform:
    def __init__(self, os_name, mons):
        self.os_name, self._m = os_name, mons
        self.events, self.capturing = [], False

    def monitors(self): return [list(m) for m in self._m]
    def begin_capture(self, drag=False): self.capturing = True; self.events.append(("begin", drag))
    def end_capture(self, x, y): self.capturing = False; self.events.append(("end", int(x), int(y)))
    def inject_move(self, x, y): self.events.append(("move", x, y))
    def inject_button(self, b, d): self.events.append(("btn", b, d))
    def inject_scroll(self, dx, dy): self.events.append(("scroll", dx, dy))
    def inject_key(self, k, d): self.events.append(("key", k, d))
    def release_all(self, keep=()):
        self.events.append(("release_all", tuple(keep)) if keep else ("release_all",))
    def cursor_pos(self): return (1, 540)
    def reset_clipboard_watch(self): pass
    def clipboard_files_changed(self): return False, None
    def clipboard_has_files(self): return False
    remote_paths = None       # lo que "se arrastra aquí" mientras el otro equipo nos maneja
    def remote_drag_files(self): self.events.append(("remote_drag_check",)); return self.remote_paths
    def healthy(self): return True
    def emergency_restore(self): self.capturing = False; self.events.append(("restore",))
    # arrastrar archivos: drag_paths simula lo que el sistema dice que se arrastra
    drag_paths = None
    def file_drag_check(self, x, y): return self.drag_paths
    def drag_reset(self): self.events.append(("drag_reset",))
    def end_file_drag(self, at=None): self.events.append(("end_file_drag",) if at is None else ("end_file_drag", at))
    def cancel_file_drag(self): self.events.append(("cancel_file_drag",))


class ClipPlatform(FakePlatform):
    """Además simula el portapapeles de archivos."""
    def __init__(self, *a):
        super().__init__(*a)
        self.clip_paths, self.clip_ver, self._seen = [], 0, None
        self.offered = []
    def copy(self, paths): self.clip_paths, self.clip_ver = list(paths), self.clip_ver + 1
    def reset_clipboard_watch(self): self._seen = self.clip_ver
    def clipboard_files_changed(self):
        if self.clip_ver == self._seen:
            return False, None
        self._seen = self.clip_ver
        return True, list(self.clip_paths)
    def clipboard_has_files(self): return bool(self.clip_paths)


class WinClipPlatform(ClipPlatform):
    def offer_clipboard_files(self, fetch): self.offered.append(fetch); return True


MAC = [[0, 0, 1440, 900, True, 1]]
WIN = [[0, 0, 1920, 1080, True, 1]]


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timeout")


def pair(pw_a="secreto123", pw_b="secreto123", mac_mons=MAC, win_mons=WIN, mac_layout=None, win_layout=None,
         mac_plat=None, win_plat=None):
    mac = Core(mac_plat or FakePlatform("mac", mac_mons), {"clipboard": False}, edge="right", layout=mac_layout)
    win = Core(win_plat or FakePlatform("win", win_mons), {"clipboard": False}, edge="left", layout=win_layout,
               is_server=True)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: win.run_connection(srv.accept()[0], net.derive_master_key(pw_b), True),
                     daemon=True).start()
    c = socket.create_connection(("127.0.0.1", port))
    threading.Thread(target=mac.run_connection, args=(c, net.derive_master_key(pw_a), False), daemon=True).start()
    return mac, win


def connected(mac, win):
    wait_for(lambda: mac.peer and win.peer and mac.layout and win.layout)
    assert (mac.layout["dx"], mac.layout["dy"]) == (-win.layout["dx"], -win.layout["dy"])


# ------------------------------------------------------------------ geometría
def test_geometry():
    pc = [(0, 0, 1920, 1080), (-1920, 0, 0, 1080)]          # como el PC real (2 a la izquierda)
    mac = [(0, 0, 1470, 956)]
    d = L.default_offset(pc, mac, "left")                   # Mac a la izquierda del monitor 2
    assert d == (-1920 - 1470, 62), d
    # borde entre monitores del PC: no cruza
    assert L.find_crossing(-1, 500, pc, mac, d) is None
    assert L.find_crossing(0, 500, pc, mac, d) is None
    # borde derecho del monitor 1: no hay nada allí
    assert L.find_crossing(1919, 500, pc, mac, d) is None
    # borde izquierdo del monitor 2: entra al Mac por su derecha a la misma altura física
    e = L.find_crossing(-1920, 500, pc, mac, d)
    assert e == (1468, 438), e
    # fuera del tramo que se solapa (el Mac es más bajo): no cruza
    assert L.find_crossing(-1920, 30, pc, mac, d) is None
    # el Mac desplazado hacia abajo con snap: sigue pegado y sin solaparse
    d2 = L.snap_offset(pc, mac, (d[0] + 200, d[1] + 300))
    assert d2[0] == d[0] and d2[1] != d[1], d2
    # el Mac debajo del monitor 1
    d3 = L.snap_offset(pc, mac, (300, 1100))
    assert d3[1] == 1080, d3
    e = L.find_crossing(700, 1079, pc, mac, d3)
    assert e is not None and e[1] == 1, e
    # volver: desde el Mac, subir por encima de su borde superior cae en el monitor 1
    where, x, y, _ = L.step_remote(e[0], -5, None, mac, pc, d3)
    assert where == "local" and 0 <= x < 1920 and 1070 <= y <= 1080 - 4, (where, x, y)
    # moverse dentro del Mac hacia un lado sin vecino: se queda en el borde
    where, x, y, _ = L.step_remote(1600, 400, mac[0], mac, pc, d3)
    assert where == "remote" and x == 1469, (where, x)
    print("ok  geometría: offsets, bordes internos, tramo solapado, snap, ida y vuelta")


# -------------------------------------------------------------- extremo a extremo
def test_wrong_password():
    mac, win = pair("secreto123", "otraclave99")
    time.sleep(1.5)
    assert mac.peer is None and win.peer is None
    print("ok  contraseña incorrecta rechazada")


def test_full_flow():
    mac, win = pair()
    connected(mac, win)
    assert win.layout["ts"] == 0 and (win.layout["dx"], win.layout["dy"]) == (-1440, 90)
    assert mac.on_local_move(500, 450) is False          # lejos del borde
    assert mac.on_local_move(1439, 450) is True          # borde derecho -> cruza
    wait_for(lambda: win.state == "controlled")
    assert ("move", 1, 540) in win.p.events, win.p.events  # misma altura física
    mac.capture({"t": "mm", "dx": 100, "dy": 10})
    mac.capture({"t": "k", "k": "LMeta", "d": 1})        # Cmd en el Mac
    mac.capture({"t": "k", "k": "C", "d": 1})
    mac.capture({"t": "mb", "b": "left", "d": 1})
    mac.capture({"t": "sc", "dx": 0, "dy": 120})
    wait_for(lambda: ("scroll", 0, 120) in win.p.events)
    assert ("move", 101, 550) in win.p.events
    assert ("key", "LCtrl", 1) in win.p.events, "Cmd debería llegar como Ctrl"
    assert ("key", "C", 1) in win.p.events and ("btn", "left", 1) in win.p.events
    mac.entered_at -= 1                                   # pasado el margen anti-rebote
    mac.capture({"t": "mm", "dx": -500, "dy": 0})        # vuelve por el borde izquierdo del PC
    assert mac.state == "local"
    end = [e for e in mac.p.events if e[0] == "end"][-1]
    assert end == ("end", 1041, 460), end                # sigue el recorrido: 101 - 500 + 1440
    wait_for(lambda: win.state == "local" and ("release_all",) in win.p.events)
    time.sleep(0.35)
    assert win.on_local_move(0, 540) is True             # ahora el PC controla el Mac
    wait_for(lambda: mac.state == "controlled")
    assert ("move", 1438, 450) in mac.p.events
    win.capture({"t": "k", "k": "LCtrl", "d": 1})
    wait_for(lambda: ("key", "LMeta", 1) in mac.p.events)  # Ctrl del PC -> Cmd en el Mac
    win.capture({"t": "k", "k": "LAlt", "d": 1})
    win.capture({"t": "k", "k": "LShift", "d": 1})
    win.capture({"t": "k", "k": "Q", "d": 1})            # atajo de emergencia
    assert win.state == "local"
    wait_for(lambda: mac.state == "local")
    assert ("key", "Q", 1) not in mac.p.events
    print("ok  cruce Mac→PC, reenvío, Cmd/Ctrl, retorno a la misma altura, PC→Mac y atajo de emergencia")


def test_layout_sync():
    mac, win = pair()
    connected(mac, win)
    lay = mac.set_layout(1440, 200)                       # el usuario mueve la disposición en el Mac
    wait_for(lambda: win.layout["ts"] == lay["ts"])
    assert (win.layout["dx"], win.layout["dy"]) == (-1440, -200)
    # con la nueva disposición, el cruce cae 200 px más arriba en el PC
    assert mac.on_local_move(1439, 450) is True
    wait_for(lambda: ("move", 1, 250) in win.p.events)
    print("ok  disposición movida en un equipo se aplica en el otro")


def test_newest_layout_wins_on_connect():
    old = {"dx": -1440, "dy": 0, "ts": 100.0}
    new = {"dx": 1440, "dy": 50, "ts": 200.0}             # el Mac tiene una disposición más reciente
    mac, win = pair(mac_layout=new, win_layout=old)
    connected(mac, win)
    assert win.layout == {"dx": -1440, "dy": -50, "ts": 200.0}, win.layout
    print("ok  al conectar gana la disposición más reciente")


PC_REAL = [[0, 0, 1920, 1080, True, 1], [-1920, 0, 0, 1080, False, 2]]
MAC_REAL = [[0, 0, 1470, 956, True, 1]]


def test_user_layout_mac_below_monitor2():
    """Disposición de la captura del usuario: el Mac debajo del monitor 2 del PC."""
    pc_layout = {"dx": -1695, "dy": 1080, "ts": 500.0}
    mac, win = pair(mac_mons=MAC_REAL, win_mons=PC_REAL, win_layout=pc_layout)
    connected(mac, win)
    assert (mac.layout["dx"], mac.layout["dy"]) == (1695, -1080)
    # el lado derecho del Mac no da a nada: no cruza
    assert mac.on_local_move(1469, 400) is False
    # subir por el borde superior del Mac entra en el monitor 2 por abajo
    assert mac.on_local_move(700, 0) is True
    wait_for(lambda: win.state == "controlled")
    assert ("move", -995, 1078) in win.p.events, win.p.events[-3:]
    # un temblor hacia abajo justo después de cruzar no debe devolver el control
    mac.capture({"t": "mm", "dx": 1, "dy": 4})
    assert mac.state == "controlling"
    mac.capture({"t": "mm", "dx": 0, "dy": -300})        # sube por el monitor 2
    assert mac.state == "controlling" and int(mac.vy) == 779, mac.vy   # 1079 - 300
    wait_for(lambda: ("move", -994, 779) in win.p.events)
    # pasado el margen, bajar de nuevo vuelve al Mac a la misma x
    mac.entered_at -= 1
    mac.capture({"t": "mm", "dx": 0, "dy": 320})
    assert mac.state == "local"
    end = [e for e in mac.p.events if e[0] == "end"][-1]
    assert end == ("end", 701, 19), end                  # 779 + 320 - 1080
    wait_for(lambda: win.state == "local")
    # y desde el PC: bajar por el monitor 2 entra al Mac por arriba
    time.sleep(0.35)
    assert win.on_local_move(-1000, 1079) is True
    wait_for(lambda: ("move", 695, 1) in mac.p.events)
    # pero bajar por el monitor 1 no (debajo no hay nada)
    win.entered_at -= 1
    win.capture({"t": "mm", "dx": 0, "dy": -50})
    win.leave_remote()
    time.sleep(0.35)
    assert win.on_local_move(1000, 1079) is False
    print("ok  disposición real (Mac bajo el monitor 2): cruce, anti-rebote, ida y vuelta")


def test_takeover_with_own_mouse():
    mac, win = pair()
    connected(mac, win)
    assert win.on_local_move(0, 540) is True             # el ratón del PC pasa al Mac
    wait_for(lambda: mac.state == "controlled")
    win.capture({"t": "k", "k": "LShift", "d": 1})
    wait_for(lambda: ("key", "LShift", 1) in mac.p.events)
    assert mac.on_local_move(1439, 300) is True          # el ratón del propio Mac cruza al PC
    assert mac.state == "controlling" and ("release_all",) in mac.p.events
    wait_for(lambda: win.state == "controlled")
    assert not win.p.capturing
    assert ("move", 1, 390) in win.p.events
    print("ok  ratón del PC en el Mac y se usa el ratón del Mac para cruzar al PC")


def test_peer_dies_while_controlling():
    mac, win = pair()
    connected(mac, win)
    assert mac.on_local_move(1439, 450) is True
    wait_for(lambda: win.state == "controlled")
    win.chan.close()                                     # el PC "muere" de golpe
    wait_for(lambda: mac.state == "local" and not mac.p.capturing)
    print("ok  el otro equipo se cae mientras lo controlas: control local recuperado")


def _solo_mac():
    mac = Core(FakePlatform("mac", MAC), {"clipboard": False}, layout={"dx": 1440, "dy": -90, "ts": 0})
    mac.peer = {"os": "win", "name": "pc", "monitors": WIN, "rects": L.rects(WIN)}
    mac.chan = object()
    mac.outq = queue.Queue()
    threading.Thread(target=mac.watchdog, daemon=True).start()
    assert mac.on_local_move(1439, 450) is True
    return mac


def test_watchdog_silent_peer():
    mac = _solo_mac()
    mac.last_rx = time.monotonic() - 60                  # red colgada sin cerrar el socket
    wait_for(lambda: mac.state == "local" and not mac.p.capturing, timeout=3)
    print("ok  vigilante: red colgada sin desconexión -> control local recuperado")


def test_watchdog_hooks_disabled():
    mac = _solo_mac()
    mac.p.healthy = lambda: False                         # el SO desactiva el hook
    wait_for(lambda: mac.state == "local", timeout=4)
    print("ok  vigilante: hooks desactivados por el sistema -> control local recuperado")


def test_hot_path_and_monitor_refresh():
    mac, win = pair()
    connected(mac, win)
    calls = []
    real = mac.p.monitors
    mac.p.monitors = lambda: calls.append(1) or real()
    for i in range(2000):                                # 2000 movimientos de ratón
        mac.on_local_move(100 + i % 500, 300)
    assert calls == [], "los hooks no deben consultar los monitores al sistema"
    # se enchufa un segundo monitor: el refresco lo detecta y el otro equipo lo recibe
    mac.p._m = MAC + [[1440, 0, 3360, 1080, False, 2]]
    assert mac.refresh_monitors() is True and mac.refresh_monitors() is False
    wait_for(lambda: len(win.peer["rects"]) == 2)
    print("ok  los hooks usan la caché de monitores; un monitor nuevo llega al otro equipo")


def test_logs_since():
    import kvmlink as K
    K.log("uno"); K.log("dos")
    last = K.LOGS[-1][0]
    assert [l for _, l in K.logs_since(last - 1)][-1].endswith("dos")
    assert K.logs_since(last) == []
    print("ok  el registro se pide de forma incremental")


def _make_tree(base):
    import os
    os.makedirs(os.path.join(base, "Fotos", "viaje"), exist_ok=True)
    with open(os.path.join(base, "nota.txt"), "wb") as f:
        f.write("hola ñandú €".encode())
    with open(os.path.join(base, "Fotos", "a.bin"), "wb") as f:
        f.write(os.urandom(200_000))                      # varios trozos
    with open(os.path.join(base, "Fotos", "viaje", "b.bin"), "wb") as f:
        f.write(b"")                                       # vacío
    return [os.path.join(base, "nota.txt"), os.path.join(base, "Fotos")]


def _same_tree(a, b):
    import filecmp
    cmp = filecmp.dircmp(a, b)
    return not (cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files) and \
        all(_same_tree(f"{a}/{d}", f"{b}/{d}") for d in cmp.common_dirs)


def test_drag_files_to_other_computer():
    import os
    import tempfile
    src, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
    paths = _make_tree(src)
    mac, win = pair()
    connected(mac, win)
    win.args.receive_dir = dest
    win.args.open_on_receive = False
    # botón pulsado pero sin archivos (seleccionar texto, mover una ventana): no cruza
    mac.on_local_button(1)
    assert mac.on_local_move(1439, 450) is False
    # arrastrando archivos: cruza
    mac.p.drag_paths = paths
    assert mac.on_local_move(1439, 450) is True
    assert ("begin", True) in mac.p.events and mac.drag_files == paths
    wait_for(lambda: win.state == "controlled")
    mac.capture({"t": "mm", "dx": 300, "dy": 0})
    mac.capture({"t": "mb", "b": "left", "d": 0})        # suelta en el PC
    assert ("end_file_drag",) in mac.p.events and mac.drag_files is None
    assert mac.buttons_down == 0
    wait_for(lambda: win.transfers.snapshot() and win.transfers.snapshot()[0]["status"] == "recibido", 10)
    assert not any(e[0] == "btn" for e in win.p.events), "la suelta no debe llegar como clic"
    assert _same_tree(src, dest), os.listdir(dest)
    assert not [f for r, _, fs in os.walk(dest) for f in fs if f.endswith(".kvmpart")]
    out = mac.transfers.snapshot()[0]
    assert out["status"] == "enviado" and out["done"] == out["total"] == 200_000 + len("hola ñandú €".encode())
    # un segundo envío de lo mismo no pisa nada: "nota (1).txt", "Fotos (1)"
    mac.send_files(paths)
    wait_for(lambda: len([t for t in win.transfers.snapshot() if t["status"] == "recibido"]) == 2, 10)
    assert sorted(os.listdir(dest)) == ["Fotos", "Fotos (1)", "nota (1).txt", "nota.txt"]
    print("ok  arrastrar archivos y carpetas al otro equipo: copia exacta, sin pisar nada")


def test_drag_return_with_button_held():
    mac, win = pair()
    connected(mac, win)
    mac.on_local_button(1)
    mac.p.drag_paths = ["/tmp/x"]
    assert mac.on_local_move(1439, 450) is True
    mac.entered_at -= 1
    mac.capture({"t": "mm", "dx": -500, "dy": 0})        # vuelve sin soltar
    assert mac.state == "local" and ("cancel_file_drag",) in mac.p.events
    assert mac.drag_files is None and mac.transfers.snapshot() == []
    print("ok  volver con el botón pulsado cancela el envío y el arrastre sigue en este equipo")


def test_drag_from_controlled_computer():
    """El caso que faltaba: con el ratón del Mac se maneja el PC, se arrastran archivos
    en el Explorador del PC y se vuelve al Mac sin soltar: deben llegar al Mac."""
    import tempfile
    src, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
    paths = _make_tree(src)
    mac, win = pair()
    connected(mac, win)
    mac.args.receive_dir, mac.args.open_on_receive = dest, False
    assert mac.on_local_move(1439, 450) is True           # el ratón del Mac pasa al PC
    wait_for(lambda: win.state == "controlled")
    mac.capture({"t": "mb", "b": "left", "d": 1})          # pulsa sobre un archivo del PC
    mac.capture({"t": "mm", "dx": 40, "dy": 0})            # lo arrastra por el PC
    wait_for(lambda: ("btn", "left", 1) in win.p.events)
    win.p.remote_paths = paths                             # (el PC ve que se arrastran archivos)
    mac.entered_at -= 1
    mac.capture({"t": "mm", "dx": -500, "dy": 0})          # vuelve al Mac sin soltar
    assert mac.state == "local"
    wait_for(lambda: ("end_file_drag", (1, 540)) in win.p.events)
    assert ("release_all", ("left",)) in win.p.events     # el resto se suelta; el botón, no
    assert ("btn", "left", 0) not in win.p.events, "no debe soltarse sobre lo que haya en el borde"
    wait_for(lambda: mac.transfers.snapshot() and mac.transfers.snapshot()[0]["status"] == "recibido", 10)
    assert _same_tree(src, dest)
    print("ok  arrastrar desde el equipo que se maneja de vuelta al propio: llegan los archivos")


def test_button_held_without_files_releases_normally():
    mac, win = pair()
    connected(mac, win)
    assert mac.on_local_move(1439, 450) is True
    wait_for(lambda: win.state == "controlled")
    mac.capture({"t": "mb", "b": "left", "d": 1})          # p.ej. seleccionando texto en el PC
    win.p.remote_paths = None
    mac.entered_at -= 1
    mac.capture({"t": "mm", "dx": -500, "dy": 0})
    wait_for(lambda: ("release_all",) in win.p.events)     # se suelta todo, botón incluido
    assert not any(e[0] == "end_file_drag" for e in win.p.events)
    assert win.transfers.snapshot() == []
    # el atajo de emergencia con el botón pulsado nunca cuenta como arrastre
    time.sleep(0.35)
    assert mac.on_local_move(1439, 450) is True
    wait_for(lambda: win.state == "controlled")
    n = len([e for e in win.p.events if e == ("remote_drag_check",)])
    mac.capture({"t": "mb", "b": "left", "d": 1})
    for k in ("LCtrl", "LAlt", "LShift", "Q"):
        mac.capture({"t": "k", "k": k, "d": 1})
    wait_for(lambda: win.state == "local")
    time.sleep(0.2)
    assert len([e for e in win.p.events if e == ("remote_drag_check",)]) == n
    print("ok  volver con el botón pulsado sin archivos (o con el atajo) suelta el botón como siempre")


def _read_all(fetch, key, size, chunk=50_000):
    out, pos = bytearray(), 0
    while True:
        data = fetch.read(key, pos, chunk)
        out += data
        pos += len(data)
        if len(data) < chunk or pos >= size:
            return bytes(out)


def test_copy_paste_files_mac_to_pc():
    import os
    import tempfile
    src = tempfile.mkdtemp()
    paths = _make_tree(src)
    macp, winp = ClipPlatform("mac", MAC), WinClipPlatform("win", WIN)
    macp.copy([paths[0]])                                  # copiado ANTES de conectar
    mac, win = pair(mac_plat=macp, win_plat=winp)
    connected(mac, win)
    time.sleep(1.5)
    assert winp.offered == [], "lo copiado antes de conectar no se ofrece"
    macp.copy(paths)                                       # Cmd+C en el Finder
    wait_for(lambda: winp.offered, 4)
    fetch = winp.offered[-1]
    assert [e for e in fetch.entries] == [("nota.txt", len("hola ñandú €".encode()), False),
                                          ("Fotos", 0, True), ("Fotos/a.bin", 200_000, False),
                                          ("Fotos/viaje", 0, True), ("Fotos/viaje/b.bin", 0, False)]
    time.sleep(0.5)
    assert mac.transfers.snapshot() == [] and win.transfers.snapshot() == [], "copiar no transfiere nada"
    # al pegar, el Explorador lee cada archivo: ahí se piden y van llegando
    fetch.ensure_started()
    for key, size, is_dir in fetch.entries:
        if not is_dir:
            want = open(os.path.join(src, *key.split("/")), "rb").read()
            assert _read_all(fetch, key, size) == want, key
    wait_for(lambda: win.transfers.snapshot()[0]["status"] == "pegado")
    # pegar otra vez: ya está descargado, no se vuelve a pedir
    n_out = len(mac.transfers.snapshot())
    assert _read_all(fetch, "nota.txt", 20) == "hola ñandú €".encode()
    time.sleep(0.3)
    assert len(mac.transfers.snapshot()) == n_out
    # copiar otra cosa en el Mac: la oferta anterior caduca y pedirla falla limpio
    macp.copy([paths[0]])
    wait_for(lambda: len(winp.offered) == 2, 4)
    old = files_mod().ClipFetch(win, fetch.offer_id + "x", [["nota.txt", 5]], "viejo",
                                files_mod().clip_temp_root(), lambda *a: None)
    old.ensure_started()
    try:
        old.read("nota.txt", 0, 5)
        raise AssertionError("debería fallar")
    except IOError:
        pass
    # con archivos copiados, el texto (sus nombres) no se manda al cruzar
    sent = []
    real_send = mac.send
    mac.send = lambda m: (sent.append(m), real_send(m))
    mac.args.clipboard = True
    mac._send_clipboard_async()
    time.sleep(0.3)
    assert not [m for m in sent if m.get("t") == "clip"]
    print("ok  copiar en el Mac y pegar en el PC: solo se transfiere al pegar, archivos idénticos")


def test_copy_paste_files_pc_to_mac():
    """Fase 2: copiar en el Explorador del PC y pegar en el Finder del Mac. El Finder
    pide la ubicación de cada elemento de primer nivel; el Mac descarga todo y la da."""
    import os
    import tempfile
    src = tempfile.mkdtemp()
    paths = _make_tree(src)
    winp, macp = ClipPlatform("win", WIN), WinClipPlatform("mac", MAC)   # el Mac recibe la oferta
    mac, win = pair(mac_plat=macp, win_plat=winp)
    connected(mac, win)
    winp.copy(paths)                                       # Ctrl+C en el Explorador
    wait_for(lambda: macp.offered, 4)
    fetch = macp.offered[-1]
    time.sleep(0.5)
    assert win.transfers.snapshot() == [], "copiar no transfiere nada"
    # Cmd+V en el Finder: pide la ubicación del primer elemento y espera
    fetch.ensure_started()
    assert fetch.wait_done() is True
    tops = list(dict.fromkeys(k.split("/")[0] for k, _, _ in fetch.entries))
    assert tops == ["nota.txt", "Fotos"]
    assert open(fetch.path_for("nota.txt"), "rb").read() == open(paths[0], "rb").read()
    assert _same_tree(paths[1], fetch.path_for("Fotos"))
    assert os.path.getsize(fetch.path_for("Fotos/viaje/b.bin")) == 0
    print("ok  copiar en el PC y pegar en el Mac: se descarga al pegar y llega idéntico")


def _program(folder, files_):
    import os
    os.makedirs(folder, exist_ok=True)
    for name, content in files_.items():
        with open(os.path.join(folder, name), "wb") as f:
            f.write(content)


def test_update_from_other_computer():
    """El Mac tiene una versión anterior y se actualiza con la del PC."""
    import os
    import tempfile
    import version
    base = tempfile.mkdtemp()
    old_dir, new_dir, cfg = (os.path.join(base, d) for d in ("mac", "pc", "config"))
    _program(old_dir, {"kvmlink.py": b"# vieja\n", "ui.html": b"<p>vieja</p>", "README.md": b"igual",
                       "kvmlink.command": b"#!/bin/bash\n", "notas.txt": b"no es del programa"})
    _program(new_dir, {"kvmlink.py": b"# nueva\n", "ui.html": b"<p>nueva</p>", "README.md": b"igual",
                       "kvmlink.command": b"#!/bin/bash\necho nueva\n", "nuevo_modulo.py": b"X = 1\n",
                       "secreto.key": b"no se debe enviar"})
    mac = Core(FakePlatform("mac", MAC), {"clipboard": False}, edge="right")
    win = Core(FakePlatform("win", WIN), {"clipboard": False}, edge="left", is_server=True)
    for core, folder, build in ((mac, old_dir, 1), (win, new_dir, 2)):
        core.program_dir, core.build = folder, build
        core.code_hash = version.code_hash(version.manifest(folder))
    applied = []
    mac.on_update_applied = lambda: applied.append(True)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    key = net.derive_master_key("secreto123")
    threading.Thread(target=lambda: win.run_connection(srv.accept()[0], key, True), daemon=True).start()
    threading.Thread(target=mac.run_connection,
                     args=(socket.create_connection(srv.getsockname()), key, False), daemon=True).start()
    wait_for(lambda: mac.peer and win.peer)
    info = mac.update_info()
    assert info["differs"] and info["peer_newer"] and info["peer_build"] == 2, info
    assert not win.update_info()["peer_newer"]
    mac.request_update(cfg)
    wait_for(lambda: applied, 10)
    assert mac.updater.status == "listo", mac.updater.detail
    assert version.manifest(old_dir) == version.manifest(new_dir) or \
        version.code_hash(version.manifest(old_dir)) == version.code_hash(version.manifest(new_dir))
    assert open(os.path.join(old_dir, "notas.txt"), "rb").read() == b"no es del programa"
    assert not os.path.exists(os.path.join(old_dir, "secreto.key")), "solo archivos del programa"
    backups = os.listdir(os.path.join(cfg, "copias"))
    assert len(backups) == 1
    assert open(os.path.join(cfg, "copias", backups[0], "kvmlink.py"), "rb").read() == b"# vieja\n"
    if os.name != "nt":
        assert os.stat(os.path.join(old_dir, "kvmlink.command")).st_mode & 0o111
    print("ok  actualizar desde el otro equipo: archivos idénticos, copia de la versión anterior")


def test_update_rejects_bad_files():
    import os
    import tempfile
    import update
    import version
    assert not version.is_program_file("../kvmlink.py")
    assert not version.is_program_file("sub/kvmlink.py")
    assert not version.is_program_file("virus.exe")
    assert not version.is_program_file(".bashrc")
    assert version.is_program_file("kvmlink.command") and version.is_program_file("gui.py")

    class C:
        transfers = files_mod().Transfers()
        receiver = type("R", (), {"targets": {}})()
        def send(self, m): pass
    tmp = tempfile.mkdtemp()
    up = update.Updater(C(), lambda *a: None, tmp, program_dir=tmp)
    # nombres peligrosos o tipos desconocidos: se apartan, nunca se instalan
    up.set_manifest({"build": 9, "files": [["../../evil.py", 3, "x"], ["kvmlink.py", 1, "y"],
                                           ["nuevo_tipo.xyz", 2, "z"]]})
    assert [n for n, _, _ in up.manifest] == ["kvmlink.py"] and up.status != "error"
    os.makedirs(up.staging, exist_ok=True)
    p = up.path_for("..\\evil.py")
    assert os.path.dirname(p) == os.path.join(up.staging, "_descartados"), p
    up = update.Updater(C(), lambda *a: None, tmp, program_dir=tmp)
    os.makedirs(up.staging, exist_ok=True)
    # un archivo que no coincide con su sha256 no se instala
    up.set_manifest({"build": 9, "files": [["kvmlink.py", 3, "0" * 64]]})
    with open(os.path.join(up.staging, "kvmlink.py"), "wb") as f:
        f.write(b"abc")
    up._apply()
    assert up.status == "error" and "dañado" in up.detail
    assert not os.path.exists(os.path.join(tmp, "kvmlink.py"))
    print("ok  actualización: solo archivos del programa, y comprobados antes de instalar")


def files_mod():
    import files
    return files


def test_input_keeps_priority_during_big_transfer():
    import os
    import tempfile
    src, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
    big = os.path.join(src, "grande.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(8 * 1024 * 1024))
    mac, win = pair()
    connected(mac, win)
    win.args.receive_dir, win.args.open_on_receive = dest, False
    assert mac.on_local_move(1439, 450) is True
    wait_for(lambda: win.state == "controlled")
    mac.send_files([big])
    wait_for(lambda: win.transfers.snapshot() and win.transfers.snapshot()[0]["done"] > 0)
    t0 = time.monotonic()
    mac.capture({"t": "mm", "dx": 10, "dy": 0})          # en mitad del envío
    wait_for(lambda: ("move", 11, 540) in win.p.events)
    lat = time.monotonic() - t0
    still = win.transfers.snapshot()[0]["status"] == "en curso"
    wait_for(lambda: win.transfers.snapshot()[0]["status"] == "recibido", 30)
    import filecmp
    assert filecmp.cmp(big, os.path.join(dest, "grande.bin"), shallow=False)
    assert lat < 0.2, lat
    print(f"ok  envío de 8 MB: el ratón llega en {lat * 1000:.0f} ms aunque se esté copiando"
          + ("" if still else " (el envío ya había terminado)"))


def test_receiver_rejects_bad_names():
    import files
    assert files.safe_parts("../../etc/passwd") == ["etc", "passwd"]
    assert files.safe_parts("C:\\Windows\\x.exe") == ["C__Windows_x.exe"]
    assert files.safe_parts("/abs/./ok.txt") == ["abs", "ok.txt"]
    assert files.safe_parts("con.txt") == ["_con.txt"]
    assert files.safe_parts("dir/nombre raro?.txt.") == ["dir", "nombre raro_.txt"]
    try:
        files.safe_parts("../..")
        raise AssertionError("debería fallar")
    except ValueError:
        pass
    print("ok  nombres peligrosos del otro equipo: no pueden salir de la carpeta de destino")


def test_link_start_stop():
    win = Core(FakePlatform("win", WIN), {"clipboard": False}, edge="left", is_server=True)
    mac = Core(FakePlatform("mac", MAC), {"clipboard": False}, edge="right")
    lw, lm = Link(win), Link(mac)
    key = net.derive_master_key("secreto123")
    lw.start("listen", "auto", 24890, key, beacon=False)
    lm.start("connect", "127.0.0.1", 24890, key)
    wait_for(lambda: win.peer and mac.peer, timeout=8)
    lm.stop()
    wait_for(lambda: win.peer is None and mac.peer is None, timeout=8)
    lm.start("connect", "127.0.0.1", 24890, key)          # reconectar tras parar
    wait_for(lambda: win.peer and mac.peer, timeout=8)
    lw.stop(); lm.stop()
    print("ok  gestor de conexión: iniciar, parar y volver a conectar")


if __name__ == "__main__":
    test_geometry()
    test_wrong_password()
    test_full_flow()
    test_layout_sync()
    test_newest_layout_wins_on_connect()
    test_user_layout_mac_below_monitor2()
    test_takeover_with_own_mouse()
    test_peer_dies_while_controlling()
    test_watchdog_silent_peer()
    test_watchdog_hooks_disabled()
    test_hot_path_and_monitor_refresh()
    test_logs_since()
    test_drag_files_to_other_computer()
    test_drag_return_with_button_held()
    test_drag_from_controlled_computer()
    test_button_held_without_files_releases_normally()
    test_copy_paste_files_mac_to_pc()
    test_copy_paste_files_pc_to_mac()
    test_update_from_other_computer()
    test_update_rejects_bad_files()
    test_input_keeps_priority_during_big_transfer()
    test_receiver_rejects_bad_names()
    test_link_start_stop()
    print("TODO OK")
