"""Detección de arrastres de archivos en Windows.

Windows no permite a otro programa leer qué se está arrastrando. Se usa el truco de
Synergy: cuando el cursor llega con el botón pulsado al borde que da al otro equipo,
se coloca bajo él una ventana casi invisible registrada como destino de arrastre
(IDropTarget, implementado aquí con ctypes). Si lo que se arrastra son archivos,
Windows llama a DragEnter y nos da la lista (formato CF_HDROP).

Al soltar en el otro equipo, se suelta también aquí encima de esta ventana, que
acepta el arrastre como COPIA sin hacer nada: el archivo original no se toca.

Todo lo que toca la ventana ocurre en su propio hilo (con su bucle de mensajes y
OLE inicializado); el hook de ratón solo le manda mensajes con PostMessage, así
nunca se bloquea.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
shell32 = ctypes.WinDLL("shell32")
kernel32 = ctypes.WinDLL("kernel32")
gdi32 = ctypes.WinDLL("gdi32")
gdi32.GetStockObject.restype = ctypes.c_void_p

HRESULT = ctypes.c_long
LRESULT = ctypes.c_ssize_t
S_OK, E_NOINTERFACE = 0, -2147467262
DROPEFFECT_NONE, DROPEFFECT_COPY = 0, 1
CF_HDROP, DVASPECT_CONTENT, TYMED_HGLOBAL = 15, 1, 1
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x8000000
LWA_ALPHA = 0x2
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40
HWND_TOPMOST = -1
SW_HIDE = 0
WM_APP = 0x8000
MSG_SHOW, MSG_HIDE, MSG_DROP = WM_APP + 1, WM_APP + 2, WM_APP + 3
SIZE = 48                      # lado de la ventana en píxeles
ALPHA = 4                      # opacidad /255 (1 = Windows la ignora al arrastrar)
PROBE_TIMEOUT = 1.0            # s sin DragEnter => no es un arrastre de archivos


class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        h = s.strip("{}").replace("-", "")
        return cls(int(h[0:8], 16), int(h[8:12], 16), int(h[12:16], 16),
                   (ctypes.c_ubyte * 8)(*bytes.fromhex(h[16:])))

    def key(self):
        return bytes(self)


IID_IUNKNOWN = GUID.parse("00000000-0000-0000-C000-000000000046").key()
IID_IDROPTARGET = GUID.parse("00000122-0000-0000-C000-000000000046").key()


class FORMATETC(ctypes.Structure):
    _fields_ = [("cfFormat", wt.WORD), ("ptd", ctypes.c_void_p), ("dwAspect", wt.DWORD),
                ("lindex", wt.LONG), ("tymed", wt.DWORD)]


class STGMEDIUM(ctypes.Structure):
    _fields_ = [("tymed", wt.DWORD), ("hGlobal", ctypes.c_void_p), ("pUnkForRelease", ctypes.c_void_p)]


# POINTL va por valor: en x64 una estructura de 8 bytes se pasa como un entero de 64 bits
QI_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
REF_T = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)
ENTER_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_uint64,
                             ctypes.POINTER(wt.DWORD))
OVER_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wt.DWORD, ctypes.c_uint64, ctypes.POINTER(wt.DWORD))
LEAVE_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
GETDATA_T = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM))


class IDropTargetVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T), ("Release", REF_T),
                ("DragEnter", ENTER_T), ("DragOver", OVER_T), ("DragLeave", LEAVE_T), ("Drop", ENTER_T)]


class IDropTargetObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(IDropTargetVtbl))]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = (ctypes.POINTER(WNDCLASSEXW),)
user32.CreateWindowExW.argtypes = (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
user32.CreateWindowExW.restype = wt.HWND
user32.SetLayeredWindowAttributes.argtypes = (wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD)
user32.SetWindowPos.argtypes = (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
user32.ShowWindow.argtypes = (wt.HWND, ctypes.c_int)
user32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
ole32.RegisterDragDrop.argtypes = (wt.HWND, ctypes.c_void_p)
ole32.ReleaseStgMedium.argtypes = (ctypes.POINTER(STGMEDIUM),)
shell32.DragQueryFileW.argtypes = (ctypes.c_void_p, wt.UINT, wt.LPWSTR, wt.UINT)
shell32.DragQueryFileW.restype = wt.UINT
kernel32.GetModuleHandleW.restype = wt.HMODULE


def files_from_dataobject(pdata):
    """Lista de rutas del formato CF_HDROP de un IDataObject, o [] si no hay archivos."""
    vtbl = ctypes.cast(pdata, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    get_data = GETDATA_T(vtbl[3])
    fmt = FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
    stg = STGMEDIUM()
    if get_data(pdata, ctypes.byref(fmt), ctypes.byref(stg)) != S_OK:
        return []
    try:
        n = shell32.DragQueryFileW(stg.hGlobal, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(n):
            ln = shell32.DragQueryFileW(stg.hGlobal, i, None, 0)
            buf = ctypes.create_unicode_buffer(ln + 1)
            shell32.DragQueryFileW(stg.hGlobal, i, buf, ln + 1)
            out.append(buf.value)
        return out
    finally:
        ole32.ReleaseStgMedium(ctypes.byref(stg))


class DropWindow:
    """Ventana destino de arrastre. Estados: idle -> probing -> files|nodrag."""

    def __init__(self, inject_left_up, inject_move=None):
        self._inject_left_up = inject_left_up
        self._inject_move = inject_move
        self.hwnd = None
        self.state = "idle"
        self.files = None
        self.dropped = threading.Event()
        self._probe_t = 0.0
        self._target = (0, 0)
        self._accept_drop = False
        self._ready = threading.Event()
        self._keep = []
        threading.Thread(target=self._run, name="drop-window", daemon=True).start()
        self._ready.wait(5)

    # ------------------------------------------------ API (desde cualquier hilo)
    def check(self, x, y):
        """Llamado desde el hook al llegar al borde con el botón pulsado.
        Devuelve la lista de archivos cuando ya se sabe que es un arrastre de archivos."""
        if self.hwnd is None:
            return None
        if self.state == "idle":
            self.state, self.files, self._probe_t = "probing", None, time.monotonic()
            self._accept_drop = False
            self._post(MSG_SHOW, x, y)
            return None
        if self.state == "probing" and time.monotonic() - self._probe_t > PROBE_TIMEOUT:
            self.state = "nodrag"
            self._post(MSG_HIDE)
        return self.files if self.state == "files" else None

    def park(self, x, y):
        """Durante el arrastre al otro equipo, la ventana se pone donde se aparca el
        cursor, para que el arrastre de Windows siga encima de ella."""
        self._post(MSG_SHOW, x, y)

    def drop(self, x, y):
        """Suelta aquí el arrastre (sobre nuestra ventana) para terminarlo sin efectos."""
        self._accept_drop = True
        self.dropped.clear()
        self._post(MSG_DROP, x, y)

    def reset(self):
        if self.state != "idle":
            self.state, self.files = "idle", None
            self._post(MSG_HIDE)

    # ----------------------------------------------------- hilo de la ventana
    def _post(self, msg, x=0, y=0):
        self._target = (int(x), int(y))
        user32.PostMessageW(self.hwnd, msg, 0, 0)

    def _place(self):
        x, y = self._target
        user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOPMOST), x - SIZE // 2, y - SIZE // 2, SIZE, SIZE,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        # el bucle de arrastre de Windows solo mira debajo del cursor cuando le llega
        # un movimiento del ratón: un empujón de 1 px (como movimiento de verdad, no
        # SetCursorPos) para que detecte la ventana aunque el cursor esté quieto
        if self._inject_move:
            self._inject_move(x + 1, y)
            self._inject_move(x, y)
        else:
            user32.SetCursorPos(x + 1, y)
            user32.SetCursorPos(x, y)

    def detect(self, x, y, timeout=PROBE_TIMEOUT + 0.3):
        """Versión bloqueante de check() (para usar fuera del hook): espera a saber
        si en (x, y) se están arrastrando archivos."""
        self.state, self.files = "idle", None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.check(x, y)
            if found:
                return found
            if self.state == "nodrag":
                return None
            time.sleep(0.05)
        self.reset()
        return None

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == MSG_SHOW:
                self._place()
                return 0
            if msg == MSG_HIDE:
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if msg == MSG_DROP:
                self._place()
                self._inject_left_up()
                threading.Timer(2.0, lambda: self._post(MSG_HIDE)).start()
                return 0
        except Exception as e:
            print("[win] error en la ventana de arrastre:", repr(e), flush=True)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # IDropTarget
    def _qi(self, this, riid, ppv):
        if riid.contents.key() in (IID_IUNKNOWN, IID_IDROPTARGET):
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, this):
        return 1

    def _drag_enter(self, this, pdata, keys, pt, peffect):
        try:
            found = files_from_dataobject(pdata)
        except Exception:
            found = []
        if found and self.state in ("probing", "files"):
            self.files, self.state = found, "files"
        peffect[0] = DROPEFFECT_COPY if found else DROPEFFECT_NONE
        return S_OK

    def _drag_over(self, this, keys, pt, peffect):
        peffect[0] = DROPEFFECT_COPY if self.files else DROPEFFECT_NONE
        return S_OK

    def _drag_leave(self, this):
        return S_OK

    def _drop(self, this, pdata, keys, pt, peffect):
        # Solo se acepta (como copia, sin hacer nada) cuando lo hemos provocado
        # nosotros al soltar en el otro equipo; si no, se rechaza y no pasa nada.
        peffect[0] = DROPEFFECT_COPY if self._accept_drop else DROPEFFECT_NONE
        self._accept_drop = False
        self.state, self.files = "idle", None
        self.dropped.set()
        user32.ShowWindow(self.hwnd, SW_HIDE)
        return S_OK

    def _run(self):
        try:
            ole32.OleInitialize(None)
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hbrBackground = gdi32.GetStockObject(4)   # BLACK_BRUSH
            wc.lpszClassName = "kvmlinkDropTarget"
            user32.RegisterClassExW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE,
                                          "kvmlinkDropTarget", "kvmlink", WS_POPUP, 0, 0, SIZE, SIZE,
                                          None, None, hinst, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW")
            # Alfa 4/255: invisible a la vista. OJO: con alfa 1 el sistema de arrastre de
            # Windows ignora la ventana (comprobado); desde 2 ya la tiene en cuenta.
            user32.SetLayeredWindowAttributes(hwnd, 0, ALPHA, LWA_ALPHA)
            vt = IDropTargetVtbl(QI_T(self._qi), REF_T(self._addref), REF_T(self._addref),
                                 ENTER_T(self._drag_enter), OVER_T(self._drag_over),
                                 LEAVE_T(self._drag_leave), ENTER_T(self._drop))
            obj = IDropTargetObj(ctypes.pointer(vt))
            self._keep = [vt, obj]   # que no los libere el recolector
            hr = ole32.RegisterDragDrop(hwnd, ctypes.byref(obj))
            if hr != S_OK:
                raise OSError(hr, "RegisterDragDrop")
            self.hwnd = hwnd
        except Exception as e:
            print("[win] arrastrar archivos no disponible:", repr(e), flush=True)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
