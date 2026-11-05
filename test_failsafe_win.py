"""Pruebas reales en Windows de los mecanismos de seguridad (ocultan el cursor unos segundos).

1. Proceso matado a la fuerza con el cursor oculto -> al relanzar kvmlink vuelve a verse.
2. Ctrl+Break / cierre de consola -> el manejador de consola restaura el cursor.
3. Excepción dentro del hook mientras se controla el otro equipo -> se recupera el control.
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = (wt.HINSTANCE, ctypes.c_void_p)


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


user32.GetIconInfo.argtypes = (wt.HANDLE, ctypes.POINTER(ICONINFO))
gdi32.GetBitmapBits.argtypes = (wt.HBITMAP, wt.LONG, ctypes.c_void_p)
gdi32.DeleteObject.argtypes = (wt.HGDIOBJ,)


def arrow_is_blank():
    h = user32.LoadCursorW(None, ctypes.c_void_p(32512))
    ii = ICONINFO()
    if not user32.GetIconInfo(h, ctypes.byref(ii)):
        raise OSError("GetIconInfo")
    try:
        if ii.hbmColor:
            return False
        buf = (ctypes.c_ubyte * 4096)()
        n = gdi32.GetBitmapBits(ii.hbmMask, 4096, buf)
        half = n // 2
        return all(b == 0xFF for b in buf[:half]) and all(b == 0 for b in buf[half:n])
    finally:
        for b in (ii.hbmMask, ii.hbmColor):
            if b:
                gdi32.DeleteObject(b)


CHILD = r"""
import sys, time
sys.path.insert(0, r'%s')
from platform_win import WinPlatform
p = WinPlatform()
p._hide_cursor()
print('hidden', flush=True)
time.sleep(30)
"""


def spawn_hidden(flags=0):
    import os
    code = CHILD % os.path.dirname(os.path.abspath(__file__))
    pr = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                          creationflags=flags)
    assert pr.stdout.readline().strip() == "hidden"
    time.sleep(0.3)
    assert arrow_is_blank(), "el cursor debería estar oculto"
    return pr


def test_hard_kill():
    pr = spawn_hidden()
    pr.kill()          # TerminateProcess: no se ejecuta nada de limpieza
    pr.wait()
    still_hidden = arrow_is_blank()
    from platform_win import WinPlatform
    WinPlatform()      # relanzar kvmlink
    assert not arrow_is_blank(), "relanzar debería restaurar el cursor"
    print(f"ok  proceso matado: cursor oculto tras matar={still_hidden}; restaurado al relanzar")


def test_console_break():
    pr = spawn_hidden(subprocess.CREATE_NEW_PROCESS_GROUP)
    ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, pr.pid)  # CTRL_BREAK_EVENT
    pr.wait(timeout=10)
    time.sleep(0.2)
    assert not arrow_is_blank(), "el manejador de consola debería restaurar el cursor"
    print("ok  evento de consola (cierre/Ctrl+Break): cursor restaurado al morir")


def test_exception_in_hook():
    from platform_win import WinPlatform
    p = WinPlatform()
    calls = []

    class Core:
        def panic(self):
            calls.append("panic")
            p.end_capture(*p.cursor_pos())

    p.core = Core()
    p._mouse_l = p._kbd_l = None
    before = p.cursor_pos()
    p.begin_capture()
    assert arrow_is_blank()

    class Broken:
        flags = 0
        @property
        def pt(self):
            raise RuntimeError("fallo simulado")
    r = p._mouse_filter(0x200, Broken())   # WM_MOUSEMOVE con datos corruptos
    assert r is True and calls == ["panic"] and not p.capturing
    assert not arrow_is_blank()
    ctypes.windll.user32.SetCursorPos(*before)
    print("ok  excepción en el hook: no se propaga, se recupera el control y el cursor")


if __name__ == "__main__":
    try:
        test_hard_kill()
        test_console_break()
        test_exception_in_hook()
        print("TODO OK")
    finally:
        user32.SystemParametersInfoW(0x57, 0, None, 0)  # pase lo que pase, cursor visible
