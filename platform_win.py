"""Backend Windows: hooks de bajo nivel (vía pynput) para capturar y SendInput para inyectar."""
import atexit
import ctypes
import ctypes.wintypes as wt
import time

from keymap import NAME_TO_WIN, WIN_TO_NAME

user32 = ctypes.WinDLL("user32", use_last_error=True)

try:  # coordenadas en píxeles físicos aunque haya escalado (125 %, 150 %...)
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

from pynput import keyboard, mouse  # noqa: E402  (después de fijar DPI awareness)

# ---------------------------------------------------------------- SendInput
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.CreateCursor.restype = wt.HANDLE
user32.CreateCursor.argtypes = (wt.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
user32.SetSystemCursor.argtypes = (wt.HANDLE, wt.DWORD)
user32.SystemParametersInfoW.argtypes = (wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
INJECT_TAG = 0x4B564D4C  # "KVML"

BUTTON_FLAGS = {  # nombre -> (flag down, flag up, mouseData)
    "left": (0x0002, 0x0004, 0), "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0), "x1": (0x0080, 0x0100, 1), "x2": (0x0080, 0x0100, 2),
}

WM_MOUSEMOVE = 0x200
WM_MOUSE_BUTTONS = {0x201: ("left", 1), 0x202: ("left", 0), 0x204: ("right", 1),
                    0x205: ("right", 0), 0x207: ("middle", 1), 0x208: ("middle", 0)}
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x20B, 0x20C
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x20A, 0x20E
WM_KEYDOWN, WM_SYSKEYDOWN = 0x100, 0x104
LLMHF_INJECTED = 0x01
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10

OCR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645,
           32646, 32648, 32649, 32650, 32651)
SPI_SETCURSORS = 0x57


_HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)
_MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


shell32 = ctypes.WinDLL("shell32")
shell32.DragQueryFileW.argtypes = (ctypes.c_void_p, wt.UINT, wt.LPWSTR, wt.UINT)
shell32.DragQueryFileW.restype = wt.UINT
user32.GetClipboardData.restype = ctypes.c_void_p
user32.RegisterClipboardFormatW.argtypes = (wt.LPCWSTR,)
user32.RegisterClipboardFormatW.restype = wt.UINT
CF_HDROP = 15
CF_KVMLINK = user32.RegisterClipboardFormatW("kvmlink.offer")   # lo pone clipboard_win

user32.EnumDisplayMonitors.argtypes = (wt.HDC, ctypes.c_void_p, _MONITORENUMPROC, wt.LPARAM)
user32.GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW))


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse_input(dx=0, dy=0, data=0, flags=0):
    return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(
        dx, dy, data & 0xFFFFFFFF, flags, 0, INJECT_TAG)))


def _signed16(v):
    v = (v >> 16) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class WinPlatform:
    os_name = "win"

    def __init__(self):
        self.core = None
        self.capturing = False
        self.cursor_hidden = False
        self._mouse_l = None
        self._kbd_l = None
        self._held_keys = set()
        self._held_buttons = set()
        self._drop = None          # ventana destino para arrastrar archivos (start)
        self._clip = None          # dueño del portapapeles para pegar archivos del otro equipo
        self._clip_seq = None
        self._park = (0, 0)
        # Si una ejecución anterior murió con el cursor oculto (proceso matado,
        # apagón, cierre forzado), lo restauramos al arrancar.
        user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        atexit.register(self.emergency_restore)
        # Cerrar la ventana de la consola, cerrar sesión o apagar: atexit no se
        # ejecuta en esos casos, así que restauramos desde el manejador de consola.
        self._ctrl_handler = _HANDLER_ROUTINE(self._on_console_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)

    def _on_console_event(self, event):
        self.emergency_restore()
        return False  # dejar que Windows siga con el cierre normal

    def emergency_restore(self):
        """Deja el equipo usable pase lo que pase: sin captura y con cursor visible."""
        self.capturing = False
        try:
            user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        finally:
            self.cursor_hidden = False

    def healthy(self):
        """False si Windows o una excepción han desactivado alguno de los hooks."""
        return all(l is None or l.running for l in (self._mouse_l, self._kbd_l))

    # -------------------------------------------------------------- geometría
    def bounds(self):
        gsm = user32.GetSystemMetrics
        x, y, w, h = gsm(76), gsm(77), gsm(78), gsm(79)
        return (x, y, x + w, y + h)

    def monitors(self):
        """[[l, t, r, b, principal, número], ...] en píxeles físicos."""
        out = []

        def cb(hmon, hdc, lprect, data):
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                rc = mi.rcMonitor
                digits = "".join(ch for ch in mi.szDevice if ch.isdigit())
                out.append([rc.left, rc.top, rc.right, rc.bottom,
                            bool(mi.dwFlags & 1), int(digits) if digits else len(out) + 1])
            return True
        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(cb), 0)
        if not out:
            l, t, r, b = self.bounds()
            out = [[l, t, r, b, True, 1]]
        return out

    def cursor_pos(self):
        p = wt.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    # -------------------------------------------------------------- captura
    def start(self, core):
        self.core = core
        try:
            from dragdrop_win import DropWindow
            self._drop = DropWindow(self._inject_left_up, self.inject_move)
            if self._drop.hwnd is None:
                self._drop = None
        except Exception as e:
            print("[win] arrastrar archivos no disponible:", repr(e), flush=True)
            self._drop = None
        self._mouse_l = mouse.Listener(win32_event_filter=self._mouse_filter)
        self._kbd_l = keyboard.Listener(win32_event_filter=self._kbd_filter)
        self._mouse_l.start()
        self._kbd_l.start()

    def stop(self):
        self.emergency_restore()
        for l in (self._mouse_l, self._kbd_l):
            if l:
                l.stop()

    # Una excepción dentro de un filtro haría que pynput quitara el hook dejando
    # la captura a medias (cursor oculto). Por eso los filtros nunca lanzan:
    # ante cualquier error se devuelve el control local y se deja pasar el evento.
    # suppress_event() se llama FUERA del try porque funciona lanzando una excepción.
    def _mouse_filter(self, msg, data):
        try:
            suppress = self._mouse_event(msg, data)
        except Exception as e:
            print("[win] error en el hook de ratón:", repr(e))
            self.core.panic()
            suppress = False
        if suppress:
            self._mouse_l.suppress_event()
        return True

    def _kbd_filter(self, msg, data):
        try:
            suppress = self._kbd_event(msg, data)
        except Exception as e:
            print("[win] error en el hook de teclado:", repr(e))
            self.core.panic()
            suppress = False
        if suppress:
            self._kbd_l.suppress_event()
        return True

    def _mouse_event(self, msg, data):
        if data.flags & LLMHF_INJECTED:
            return False  # nuestras propias inyecciones u otro software
        if not self.capturing:
            if msg == WM_MOUSEMOVE:
                # si empieza la captura, bloquear este movimiento para que el
                # cursor se quede donde lo aparcó begin_capture()
                return self.core.on_local_move(data.pt.x, data.pt.y)
            if msg in WM_MOUSE_BUTTONS:
                self.core.on_local_button(WM_MOUSE_BUTTONS[msg][1])
            return False
        # --- capturando: reenviar y bloquear localmente
        if msg == WM_MOUSEMOVE:
            cx, cy = self.cursor_pos()
            dx, dy = data.pt.x - cx, data.pt.y - cy
            if dx or dy:
                self.core.capture({"t": "mm", "dx": dx, "dy": dy})
        elif msg in WM_MOUSE_BUTTONS:
            b, d = WM_MOUSE_BUTTONS[msg]
            self.core.capture({"t": "mb", "b": b, "d": d})
        elif msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            b = "x1" if _signed16(data.mouseData) == 1 else "x2"
            self.core.capture({"t": "mb", "b": b, "d": int(msg == WM_XBUTTONDOWN)})
        elif msg == WM_MOUSEWHEEL:
            self.core.capture({"t": "sc", "dx": 0, "dy": _signed16(data.mouseData)})
        elif msg == WM_MOUSEHWHEEL:
            self.core.capture({"t": "sc", "dx": _signed16(data.mouseData), "dy": 0})
        # bloquear también si capture() acaba de devolver el control: así el cursor
        # se queda donde lo colocó end_capture() en vez de saltar a data.pt
        return True

    def _kbd_event(self, msg, data):
        if data.flags & LLKHF_INJECTED:
            return False
        down = int(msg in (WM_KEYDOWN, WM_SYSKEYDOWN))
        sc = data.scanCode
        name = None
        if sc <= 0xFF:  # sc > 0xFF = Ctrl falso que Windows genera con AltGr
            if data.vkCode == 0x90:
                name = "NumLock"
            else:
                ext = 0xE000 if data.flags & LLKHF_EXTENDED else 0
                name = WIN_TO_NAME.get(ext | sc) or WIN_TO_NAME.get(sc)
        if not self.capturing:
            return False
        if name:
            self.core.capture({"t": "k", "k": name, "d": down})
        return True

    def begin_capture(self, drag=False):
        self.capturing = True
        self._hide_cursor()
        l, t, r, b = self.bounds()
        self._park = ((l + r) // 2, (t + b) // 2)
        user32.SetCursorPos(*self._park)
        if drag and self._drop:
            # el arrastre de Windows sigue activo: que quede encima de nuestra
            # ventana (y no de otra aplicación) mientras se maneja el otro equipo
            self._drop.park(*self._park)

    # ------------------------------------------------- arrastrar archivos
    def file_drag_check(self, x, y):
        return self._drop.check(x, y) if self._drop else None

    def drag_reset(self):
        if self._drop:
            self._drop.reset()

    def end_file_drag(self, at=None):
        """Se ha soltado en el otro equipo: soltar también aquí, sobre nuestra ventana
        (en `at`, o donde está aparcado el cursor mientras se maneja el otro equipo)."""
        if self._drop:
            self._drop.drop(*(at or self._park))
        self._held_buttons.discard("left")

    def remote_drag_files(self):
        """El otro equipo nos manejaba y se ha ido con el botón pulsado aquí: ¿se
        estaban arrastrando archivos? (bloquea hasta ~1 s; no llamar desde el hook)"""
        if not self._drop:
            return None
        x, y = self.cursor_pos()
        return self._drop.detect(x, y)

    def cancel_file_drag(self):
        """Se vuelve a este equipo con el botón pulsado: el arrastre sigue aquí."""
        self.drag_reset()

    # ------------------------------------------- archivos en el portapapeles
    def offer_clipboard_files(self, fetch):
        """Pone en el portapapeles los archivos copiados en el otro equipo, sin
        descargarlos: se piden cuando algo los pega (clipboard_win)."""
        if self._clip is None:
            from clipboard_win import ClipboardOwner
            self._clip = ClipboardOwner(fetch.log)
        self._clip.offer(fetch, fetch.entries)
        return True

    # Archivos copiados en este PC (Ctrl+C en el Explorador: formato CF_HDROP) para
    # ofrecerlos al otro equipo. Se mira el número de secuencia del portapapeles,
    # que es barato; solo se abre el portapapeles cuando ha cambiado.
    def reset_clipboard_watch(self):
        """Lo ya copiado al conectar no se ofrece: solo lo que se copie a partir de ahora."""
        self._clip_seq = user32.GetClipboardSequenceNumber()

    def _clipboard_is_ours(self):
        return bool(user32.IsClipboardFormatAvailable(CF_KVMLINK))

    def clipboard_files_changed(self):
        """(cambiado, rutas): si el portapapeles ha cambiado, y qué archivos tiene
        (lista vacía si no son archivos o si es lo que ha puesto kvmlink)."""
        seq = user32.GetClipboardSequenceNumber()
        if seq == self._clip_seq:
            return False, None
        if self._clipboard_is_ours() or not user32.IsClipboardFormatAvailable(CF_HDROP):
            self._clip_seq = seq
            return True, []
        paths = self._read_hdrop()
        if paths is None:          # otro programa lo tiene abierto: se reintenta luego
            return False, None
        self._clip_seq = seq
        return True, paths

    @staticmethod
    def _read_hdrop():
        for _ in range(5):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.02)
        else:
            return None
        try:
            h = user32.GetClipboardData(CF_HDROP)
            if not h:
                return []
            n = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            out = []
            for i in range(n):
                ln = shell32.DragQueryFileW(h, i, None, 0)
                buf = ctypes.create_unicode_buffer(ln + 1)
                shell32.DragQueryFileW(h, i, buf, ln + 1)
                out.append(buf.value)
            return out
        finally:
            user32.CloseClipboard()

    def clipboard_has_files(self):
        return bool(user32.IsClipboardFormatAvailable(CF_HDROP)) and not self._clipboard_is_ours()

    def main_loop(self, should_stop, ui=None):
        """Hilo principal. Con ui (dict con open, quit, status e icon) muestra el icono
        en la bandeja de Windows; sin ella (modo terminal) solo espera."""
        if ui:
            try:
                from tray_win import Tray
                Tray(ui.get("icon"), ui["open"], ui["quit"], ui["status"], should_stop).run()
                return
            except Exception as e:
                print("[win] sin icono en la bandeja:", repr(e), flush=True)
        while not should_stop():
            time.sleep(0.25)

    @staticmethod
    def _inject_left_up():
        _send(_mouse_input(flags=BUTTON_FLAGS["left"][1]))

    def end_capture(self, x, y):
        self.capturing = False
        user32.SetCursorPos(int(x), int(y))
        self._show_cursor()

    def _hide_cursor(self):
        if self.cursor_hidden:
            return
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)()
        for ocr in OCR_IDS:
            cur = user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
            if cur:
                user32.SetSystemCursor(cur, ocr)
        self.cursor_hidden = True

    def _show_cursor(self):
        if self.cursor_hidden:
            user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
            self.cursor_hidden = False

    # -------------------------------------------------------------- inyección
    def inject_move(self, x, y):
        l, t, r, b = self.bounds()
        nx = round((x - l) * 65535 / max(1, r - l - 1))
        ny = round((y - t) * 65535 / max(1, b - t - 1))
        _send(_mouse_input(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK))

    def inject_button(self, button, down):
        if button not in BUTTON_FLAGS:
            return
        fdown, fup, data = BUTTON_FLAGS[button]
        _send(_mouse_input(data=data, flags=fdown if down else fup))
        (self._held_buttons.add if down else self._held_buttons.discard)(button)

    def inject_scroll(self, dx, dy):
        if dy:
            _send(_mouse_input(data=int(dy), flags=MOUSEEVENTF_WHEEL))
        if dx:
            _send(_mouse_input(data=int(dx), flags=MOUSEEVENTF_HWHEEL))

    def inject_key(self, name, down):
        code = NAME_TO_WIN.get(name)
        if code is None:
            return
        flags = KEYEVENTF_SCANCODE
        if code & 0xE000:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= KEYEVENTF_KEYUP
        _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(
            0, code & 0xFF, flags, 0, INJECT_TAG))))
        (self._held_keys.add if down else self._held_keys.discard)(name)

    def release_all(self, keep=()):
        """Suelta lo que hayamos dejado pulsado al inyectar (salvo los botones de `keep`)."""
        for k in list(self._held_keys):
            self.inject_key(k, 0)
        for b in list(self._held_buttons):
            if b not in keep:
                self.inject_button(b, 0)
