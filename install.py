#!/usr/bin/env python3
"""Instalar kvmlink como programa del sistema (y desinstalarlo).

    python install.py              instala (o reinstala) y abre kvmlink
    python install.py --uninstall  quita accesos directos, app e inicio automático

Los archivos del programa se quedan en su carpeta (así siguen funcionando las
actualizaciones desde el otro equipo); solo se crean los enlaces del sistema:

Windows: accesos directos en el menú Inicio y el escritorio, inicio al iniciar sesión
         (carpeta Inicio, arranca en la bandeja) y entrada en "Aplicaciones instaladas".
Mac:     ~/Applications/kvmlink.app (un pequeño lanzador compilado aquí con clang que
         arranca Python: así los permisos de Accesibilidad son de kvmlink y no de la
         Terminal) y un agente de inicio de sesión.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "kvmlink.py")
APP_NAME = "kvmlink"


def config_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "kvmlink")
    return os.path.expanduser("~/Library/Application Support/kvmlink")


# =========================================================================== Windows
def _win_python(windowless=True):
    exe = sys.executable
    if windowless:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            return pyw
    return exe


def _win_folder(kind):
    """Carpetas especiales del usuario (respetan Escritorio en OneDrive, etc.)."""
    import ctypes
    from ctypes import wintypes
    ids = {"programs": 0x0002, "startup": 0x0007, "desktop": 0x0010}
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, ids[kind], None, 0, buf)
    return buf.value


def win_icon_path():
    path = os.path.join(config_dir(), "kvmlink.ico")
    if not os.path.exists(path):
        import icon
        os.makedirs(config_dir(), exist_ok=True)
        icon.write_ico(path)
    return path


def _win_shortcut(path, args, description):
    """Crea un acceso directo (.lnk) con WScript.Shell. Las rutas van por variables de
    entorno para no tener problemas con comillas, espacios o tildes."""
    env = dict(os.environ, LNK_PATH=path, LNK_TARGET=_win_python(), LNK_ARGS=args, LNK_DIR=HERE,
               LNK_ICON=win_icon_path(), LNK_DESC=description)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK_PATH);"
          "$s.TargetPath=$env:LNK_TARGET;$s.Arguments=$env:LNK_ARGS;$s.WorkingDirectory=$env:LNK_DIR;"
          "$s.IconLocation=$env:LNK_ICON;$s.Description=$env:LNK_DESC;$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _win_args(extra=""):
    return f'"{SCRIPT}"' + (f" {extra}" if extra else "")


WIN_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\kvmlink"


def _win_register_uninstall():
    import winreg
    import version
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_UNINSTALL_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "kvmlink")
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, str(version.BUILD))
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "kvmlink")
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, win_icon_path())
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, HERE)
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                          f'"{_win_python(windowless=False)}" "{os.path.join(HERE, "install.py")}" --uninstall')
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def _win_paths():
    return {"menu": os.path.join(_win_folder("programs"), "kvmlink.lnk"),
            "desktop": os.path.join(_win_folder("desktop"), "kvmlink.lnk"),
            "startup": os.path.join(_win_folder("startup"), "kvmlink.lnk")}


# =============================================================================== Mac
MAC_APP = os.path.expanduser("~/Applications/kvmlink.app")
MAC_AGENT = os.path.expanduser("~/Library/LaunchAgents/com.kvmlink.agent.plist")

LAUNCHER_M = r"""
// Lanzador de kvmlink.app (Objective-C, se compila al instalar).
//  - Arranca Python con kvmlink.py como proceso hijo: así macOS atribuye los permisos
//    de Accesibilidad a kvmlink.app.
//  - Si Python sale con el código 42 (se ha actualizado desde el otro equipo), lo
//    vuelve a arrancar; si sale por otra cosa, el lanzador termina con él.
//  - Si se vuelve a abrir kvmlink.app mientras ya está en marcha (Finder, Spotlight,
//    Launchpad), macOS se lo dice a esta app ("reopen") y se abre la ventana de kvmlink.
//  - Al cerrar sesión o apagar, cierra Python de forma ordenada.
#import <Cocoa/Cocoa.h>
#include <signal.h>

@interface KvmlinkLauncher : NSObject <NSApplicationDelegate>
@property (strong) NSTask *task;
@property (strong) NSArray *dispatchSources;
@end

@implementation KvmlinkLauncher
- (NSTask *)pythonWithArgs:(NSArray *)extra {
    NSTask *t = [NSTask new];
    t.launchPath = @PYTHON_PATH;
    t.arguments = [@[@SCRIPT_PATH] arrayByAddingObjectsFromArray:extra];
    t.currentDirectoryPath = @PROGRAM_DIR;
    NSMutableDictionary *env = [NSProcessInfo.processInfo.environment mutableCopy];
    env[@"KVMLINK_LAUNCHER"] = @"app";
    t.environment = env;
    return t;
}
- (void)startWithArgs:(NSArray *)extra {
    self.task = [self pythonWithArgs:extra];
    __weak KvmlinkLauncher *weakSelf = self;
    self.task.terminationHandler = ^(NSTask *t) {
        BOOL restart = t.terminationReason == NSTaskTerminationReasonExit && t.terminationStatus == 42;
        dispatch_async(dispatch_get_main_queue(), ^{
            if (restart) [weakSelf startWithArgs:@[]];      // tras actualizarse
            else [NSApp terminate:nil];
        });
    };
    NSError *err = nil;
    if (![self.task launchAndReturnError:&err]) {
        NSLog(@"kvmlink: no se pudo arrancar Python: %@", err);
        [NSApp terminate:nil];
    }
}
- (void)applicationDidFinishLaunching:(NSNotification *)note {
    NSMutableArray *extra = [NSMutableArray new];
    NSArray *argv = NSProcessInfo.processInfo.arguments;
    for (NSUInteger i = 1; i < argv.count; i++)
        if (![argv[i] hasPrefix:@"-psn_"]) [extra addObject:argv[i]];
    [self startWithArgs:extra];
    // SIGTERM (launchd, kill) o SIGINT: cerrar Python con orden antes de salir
    NSMutableArray *sources = [NSMutableArray new];
    for (NSNumber *sig in @[@(SIGTERM), @(SIGINT), @(SIGHUP)]) {
        signal(sig.intValue, SIG_IGN);
        dispatch_source_t s = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, sig.intValue, 0,
                                                     dispatch_get_main_queue());
        dispatch_source_set_event_handler(s, ^{ [NSApp terminate:nil]; });
        dispatch_resume(s);
        [sources addObject:s];
    }
    self.dispatchSources = sources;
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible {
    // abrir kvmlink.app otra vez: una segunda copia de kvmlink.py ve que ya hay una en
    // marcha, abre su ventana y termina
    [[self pythonWithArgs:@[]] launchAndReturnError:nil];
    return NO;
}
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    NSTask *t = self.task;
    if (t.running) {
        t.terminationHandler = nil;
        [t terminate];                                   // SIGTERM: kvmlink sale ordenado
        for (int i = 0; i < 50 && t.running; i++) [NSThread sleepForTimeInterval:0.1];
    }
    return NSTerminateNow;
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        KvmlinkLauncher *launcher = [KvmlinkLauncher new];
        app.delegate = launcher;
        [app run];
    }
    return 0;
}
"""


def _c_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# OJO: macOS recuerda los permisos de una app sin firmar por la huella de su firma local,
# que incluye el lanzador compilado y este Info.plist. Si cambian, los permisos ya
# concedidos dejan de valer (aunque en Ajustes sigan apareciendo activados). Por eso aquí
# no va el número de versión (cambia en cada actualización) y la app solo se vuelve a
# compilar cuando de verdad cambia algo de esto.
MAC_PLIST = {
    "CFBundleName": "kvmlink", "CFBundleDisplayName": "kvmlink",
    "CFBundleIdentifier": "com.kvmlink.app", "CFBundleExecutable": APP_NAME,
    "CFBundleIconFile": "kvmlink", "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1", "CFBundleVersion": "1",
    "LSUIElement": True,          # sin icono en el Dock: vive en la barra de menús
    "LSMinimumSystemVersion": "10.15",
    "NSHumanReadableCopyright": "kvmlink",
}
MAC_TCC_SERVICES = ("Accessibility", "ListenEvent", "PostEvent")


def _mac_launcher_source():
    return (f"#define PYTHON_PATH {_c_str(sys.executable)}\n#define SCRIPT_PATH {_c_str(SCRIPT)}\n"
            f"#define PROGRAM_DIR {_c_str(HERE)}\n" + LAUNCHER_M)


def _mac_app_fingerprint(source):
    import hashlib
    import icon
    h = hashlib.sha256(source.encode())
    h.update(repr(sorted(MAC_PLIST.items())).encode())
    with open(icon.__file__, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _mac_reset_permissions():
    """Quita las entradas de kvmlink en Privacidad (solo las suyas): tras recompilar la
    app ya no valen, y así macOS vuelve a pedir los permisos limpiamente."""
    for service in MAC_TCC_SERVICES:
        subprocess.run(["tccutil", "reset", service, MAC_PLIST["CFBundleIdentifier"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _mac_build_app():
    """Crea kvmlink.app. Devuelve False si ya estaba al día (no se toca: conserva sus
    permisos) y True si se ha compilado de nuevo."""
    import icon
    source = _mac_launcher_source()
    fingerprint = _mac_app_fingerprint(source)
    stamp = os.path.join(config_dir(), "app-huella.txt")
    exe = os.path.join(MAC_APP, "Contents", "MacOS", APP_NAME)
    try:
        with open(stamp) as f:
            if os.path.exists(exe) and f.read().strip() == fingerprint:
                return False
    except OSError:
        pass
    if not shutil.which("clang"):
        raise RuntimeError("falta clang: instala las herramientas de desarrollo con  xcode-select --install")
    had_app = os.path.exists(MAC_APP)
    tmp = tempfile.mkdtemp()
    shutil.rmtree(MAC_APP, ignore_errors=True)
    macos, res = os.path.join(MAC_APP, "Contents", "MacOS"), os.path.join(MAC_APP, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(res)
    src = os.path.join(tmp, "launcher.m")
    with open(src, "w") as f:
        f.write(source)
    subprocess.run(["clang", "-fobjc-arc", "-O2", "-framework", "Cocoa", "-mmacosx-version-min=10.15",
                    "-o", exe, src], check=True)
    iconset = os.path.join(tmp, "kvmlink.iconset")
    icon.write_iconset(iconset)
    subprocess.run(["iconutil", "-c", "icns", "-o", os.path.join(res, "kvmlink.icns"), iconset], check=True)
    with open(os.path.join(MAC_APP, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(MAC_PLIST, f)
    # firma local ("ad hoc"): macOS la necesita para recordar los permisos de la app
    subprocess.run(["codesign", "--force", "--sign", "-", MAC_APP], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    if had_app:
        _mac_reset_permissions()
    os.makedirs(config_dir(), exist_ok=True)
    with open(stamp, "w") as f:
        f.write(fingerprint)
    return True


def _mac_agent_args():
    return [os.path.join(MAC_APP, "Contents", "MacOS", APP_NAME), "--no-window"]


# ===================================================== inicio al iniciar sesión (API)
def autostart_supported():
    if sys.platform == "win32":
        return True, ""
    if sys.platform == "darwin":
        if os.path.exists(MAC_APP):
            return True, ""
        return False, "Instala la app con «instalar.command» para poder abrirlo al iniciar sesión."
    return False, "No disponible en este sistema."


def autostart_enabled():
    if sys.platform == "win32":
        return os.path.exists(_win_paths()["startup"])
    if sys.platform == "darwin":
        return os.path.exists(MAC_AGENT)
    return False


def set_autostart(enabled):
    if sys.platform == "win32":
        path = _win_paths()["startup"]
        if enabled:
            _win_shortcut(path, _win_args("--no-window"), "kvmlink (al iniciar sesión)")
        elif os.path.exists(path):
            os.remove(path)
    elif sys.platform == "darwin":
        if enabled:
            ok, why = autostart_supported()
            if not ok:
                raise ValueError(why)
            os.makedirs(os.path.dirname(MAC_AGENT), exist_ok=True)
            with open(MAC_AGENT, "wb") as f:
                plistlib.dump({"Label": "com.kvmlink.agent", "ProgramArguments": _mac_agent_args(),
                               "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
                               "ProcessType": "Interactive"}, f)
        elif os.path.exists(MAC_AGENT):
            os.remove(MAC_AGENT)


# ===================================================================== instalar / quitar
def stop_running_instance(timeout=8):
    """Si kvmlink está en marcha, le pide que se cierre (por su API local, con su clave)
    y espera a que termine. Devuelve True si había uno en marcha."""
    import json
    import time
    import urllib.request
    try:
        with open(os.path.join(config_dir(), "ui.json"), encoding="utf-8") as f:
            info = json.load(f)
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        return False
    print("Cerrando el kvmlink que estaba en marcha…")
    end = time.time() + timeout
    while time.time() < end and os.path.exists(os.path.join(config_dir(), "ui.json")):
        time.sleep(0.2)
    time.sleep(1.0)   # que suelte puertos y hooks
    return True



def _ensure_dependencies():
    import importlib
    needed = ["cryptography", "pyperclip"] + (["pynput"] if sys.platform == "win32" else ["AppKit", "Quartz"])
    try:
        for mod in needed:
            importlib.import_module(mod)
        return
    except ImportError:
        pass
    print("Instalando dependencias…")
    cmd = [sys.executable, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt")]
    if sys.platform == "darwin":
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "pip"], check=True)
        cmd[4:4] = ["--user", "--prefer-binary"]
    subprocess.run(cmd, check=True)


STAMP_FILE = "instalado.txt"   # versión con la que se hizo (o puso al día) la instalación
WIN_DESCRIPTION = "kvmlink: un ratón y un teclado para dos equipos"


def _write_stamp():
    import version
    os.makedirs(config_dir(), exist_ok=True)
    with open(os.path.join(config_dir(), STAMP_FILE), "w") as f:
        f.write(str(version.BUILD))


def _read_stamp():
    try:
        with open(os.path.join(config_dir(), STAMP_FILE)) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def is_installed():
    if sys.platform == "win32":
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_UNINSTALL_KEY))
            return True
        except OSError:
            return os.path.exists(_win_paths()["menu"])
    if sys.platform == "darwin":
        return os.path.exists(MAC_APP)
    return False


def refresh_installation(log=print):
    """Tras actualizarse desde el otro equipo: si kvmlink está instalado y la instalación
    es de otra versión, la pone al día sin preguntar (lo que antes obligaba a volver a
    ejecutar el instalador). Devuelve "relanzar" si en el Mac se ha recompilado la app (hay
    que arrancar la nueva), "al día" si se ha retocado algo, o None si no hacía falta."""
    import version
    if not is_installed() or _read_stamp() == version.BUILD:
        return None
    result = "al día"
    if sys.platform == "win32":
        paths = _win_paths()
        # solo lo que existe: si borraste el acceso del escritorio, no se vuelve a poner
        for key in ("menu", "desktop"):
            if os.path.exists(paths[key]):
                _win_shortcut(paths[key], _win_args(), WIN_DESCRIPTION)
        if autostart_enabled():
            set_autostart(True)
        _win_register_uninstall()
        log("instalación puesta al día (accesos directos y Aplicaciones instaladas)")
    elif sys.platform == "darwin":
        if _mac_build_app():
            result = "relanzar"
            log("la app kvmlink.app ha cambiado en esta versión: se ha vuelto a crear")
        if autostart_enabled():
            set_autostart(True)
    _write_stamp()
    return result


def install():
    _ensure_dependencies()
    stop_running_instance()   # para que lo que se abra al terminar sea lo recién instalado
    if sys.platform == "win32":
        paths = _win_paths()
        _win_shortcut(paths["menu"], _win_args(), WIN_DESCRIPTION)
        _win_shortcut(paths["desktop"], _win_args(), WIN_DESCRIPTION)
        set_autostart(True)
        _win_register_uninstall()
        _write_stamp()
        print("Accesos directos creados en el menú Inicio y en el escritorio.")
        print("kvmlink se abrirá solo al iniciar sesión (se puede desactivar en su ventana).")
        subprocess.Popen([_win_python(), SCRIPT], cwd=HERE, creationflags=0x8 | 0x200, close_fds=True)
    elif sys.platform == "darwin":
        rebuilt = _mac_build_app()
        set_autostart(True)
        _write_stamp()
        print("kvmlink se abrirá solo al iniciar sesión (se puede desactivar en su ventana).")
        if rebuilt:
            print(f"App creada: {MAC_APP}")
            print()
            print("IMPORTANTE: macOS pedirá permisos para kvmlink. En Ajustes del Sistema →")
            print("Privacidad y seguridad, activa kvmlink en «Accesibilidad» y en «Monitorización")
            print("de entrada» (los de una instalación anterior se han borrado: ya no valían).")
            print("Ya no hace falta dárselos a la Terminal.")
        else:
            print(f"La app ({MAC_APP}) ya estaba al día: no se ha tocado y conserva sus permisos.")
        subprocess.run(["open", MAC_APP])
    else:
        sys.exit("Sistema no soportado (solo Windows y macOS).")


def uninstall():
    stop_running_instance()
    if sys.platform == "win32":
        for path in _win_paths().values():
            if os.path.exists(path):
                os.remove(path)
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, WIN_UNINSTALL_KEY)
        except OSError:
            pass
    elif sys.platform == "darwin":
        set_autostart(False)
        shutil.rmtree(MAC_APP, ignore_errors=True)
        _mac_reset_permissions()
    for name in ("app-huella.txt", STAMP_FILE):
        try:
            os.remove(os.path.join(config_dir(), name))
        except OSError:
            pass
    print("kvmlink desinstalado: accesos directos, app e inicio automático quitados.")
    print(f"La carpeta del programa ({HERE}) y tu configuración ({config_dir()}) no se han")
    print("borrado; puedes eliminarlas a mano si ya no las necesitas.")


if __name__ == "__main__":
    try:
        uninstall() if "--uninstall" in sys.argv else install()
    except Exception as e:
        print("\nNo se pudo completar:", e)
        sys.exit(1)
