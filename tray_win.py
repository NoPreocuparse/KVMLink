"""Icono de kvmlink en la bandeja de Windows (junto al reloj).

Clic: abre la ventana. Clic derecho: menú (Abrir kvmlink / Salir). Al pasar el ratón,
el estado. Corre en el hilo principal, con su propio bucle de mensajes (main_loop).
"""
import ctypes
import ctypes.wintypes as wt

from dragdrop_win import GUID, WNDCLASSEXW, WNDPROC

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")
kernel32 = ctypes.WinDLL("kernel32")

WM_APP, WM_TIMER = 0x8000, 0x0113
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
WM_TRAY = WM_APP + 10
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x80
NOTIFYICON_VERSION_4 = 4
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x100, 0x80, 0x2
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
CMD_OPEN, CMD_QUIT = 1, 2


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.argtypes = (wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
user32.CreateWindowExW.restype = wt.HWND
user32.LoadImageW.argtypes = (wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT)
user32.LoadImageW.restype = wt.HANDLE
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = (wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
user32.TrackPopupMenu.argtypes = (wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p)
user32.SetMenuDefaultItem.argtypes = (wt.HMENU, wt.UINT, wt.UINT)
user32.DestroyMenu.argtypes = (wt.HMENU,)
user32.SetTimer.argtypes = (wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p)
user32.RegisterWindowMessageW.argtypes = (wt.LPCWSTR,)
user32.RegisterWindowMessageW.restype = wt.UINT
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)
shell32.Shell_NotifyIconW.argtypes = (wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
kernel32.GetModuleHandleW.restype = wt.HMODULE


class Tray:
    def __init__(self, icon_path, on_open, on_quit, status, should_stop):
        self.on_open, self.on_quit, self.status, self.should_stop = on_open, on_quit, status, should_stop
        self.tip = ""
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "kvmlinkTray"
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "kvmlinkTray", "kvmlink", 0, 0, 0, 0, 0, None, None, hinst, None)
        # si el Explorador se reinicia, la bandeja se vacía: hay que volver a poner el icono
        self.msg_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        cx = user32.GetSystemMetrics(49)   # SM_CXSMICON
        self.hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, cx, cx, LR_LOADFROMFILE) if icon_path else None
        if not self.hicon:
            self.hicon = user32.LoadIconW(None, ctypes.c_void_p(32512))   # IDI_APPLICATION
        self._add()
        user32.SetTimer(self.hwnd, 1, 500, None)

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        nid.uCallbackMessage, nid.hIcon = WM_TRAY, self.hicon
        nid.szTip = self.tip[:127]
        return nid

    def _add(self):
        self.tip = self._status_text()
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    def _status_text(self):
        try:
            return "kvmlink — " + self.status()
        except Exception:
            return "kvmlink"

    def remove(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Abrir kvmlink")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Salir de kvmlink")
        user32.SetMenuDefaultItem(menu, CMD_OPEN, False)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)   # para que el menú se cierre al hacer clic fuera
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == CMD_OPEN:
            self.on_open()
        elif cmd == CMD_QUIT:
            self.on_quit()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                event = lp & 0xFFFF   # NOTIFYICON_VERSION_4: el evento va en la palabra baja
                if event == WM_LBUTTONUP:
                    self.on_open()
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_TIMER:
                if self.should_stop():
                    user32.PostQuitMessage(0)
                    return 0
                tip = self._status_text()
                if tip != self.tip:
                    self.tip = tip
                    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid(NIF_TIP | NIF_SHOWTIP)))
                return 0
            if msg == self.msg_taskbar_created:
                self._add()
                return 0
        except Exception as e:
            print("[win] error en el icono de la bandeja:", repr(e), flush=True)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self.remove()
