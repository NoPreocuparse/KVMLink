"""Backend macOS: CGEventTap para capturar y CGEventPost para inyectar.

Requiere permisos en Ajustes del Sistema > Privacidad y seguridad:
  - Accesibilidad
  - Monitorización de entrada
para la app que ejecuta el script (Terminal, iTerm o el binario empaquetado).
"""
import queue
import subprocess
import threading
import time

import Quartz as Q

from keymap import MAC_TO_NAME, NAME_TO_MAC

INJECT_TAG = 0x4B564D4C  # marca nuestras inyecciones para no capturarlas de vuelta

# bits de flags dependientes del dispositivo (izq./dcha.) + máscara genérica
MOD_BITS = {
    "LShift": (0x00000002, Q.kCGEventFlagMaskShift),
    "RShift": (0x00000004, Q.kCGEventFlagMaskShift),
    "LCtrl": (0x00000001, Q.kCGEventFlagMaskControl),
    "RCtrl": (0x00002000, Q.kCGEventFlagMaskControl),
    "LAlt": (0x00000020, Q.kCGEventFlagMaskAlternate),
    "RAlt": (0x00000040, Q.kCGEventFlagMaskAlternate),
    "LMeta": (0x00000008, Q.kCGEventFlagMaskCommand),
    "RMeta": (0x00000010, Q.kCGEventFlagMaskCommand),
}
ARROWS = {"Left", "Right", "Up", "Down"}
FN_KEYS = ARROWS | {"Home", "End", "PageUp", "PageDown", "Delete", "Insert"}

BTN_DOWN = {Q.kCGEventLeftMouseDown: "left", Q.kCGEventRightMouseDown: "right"}
BTN_UP = {Q.kCGEventLeftMouseUp: "left", Q.kCGEventRightMouseUp: "right"}
OTHER_BTN = {2: "middle", 3: "x1", 4: "x2"}
OTHER_BTN_NUM = {v: k for k, v in OTHER_BTN.items()}
MOVE_TYPES = (Q.kCGEventMouseMoved, Q.kCGEventLeftMouseDragged,
              Q.kCGEventRightMouseDragged, Q.kCGEventOtherMouseDragged)
TAP_TYPES = (
    Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventRightMouseDown,
    Q.kCGEventRightMouseUp, Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp,
    Q.kCGEventScrollWheel, Q.kCGEventKeyDown, Q.kCGEventKeyUp, Q.kCGEventFlagsChanged,
) + MOVE_TYPES

DOUBLE_CLICK_TIME = 0.5
DOUBLE_CLICK_DIST = 5


KVMLINK_PB_TYPE = "com.kvmlink.offer"   # marca nuestra promesa en el portapapeles
_CLIP_STATE = {}                         # oferta actual: fetch, item -> nombre, proveedor
_PROVIDER_CLASS = []


def _clip_provider_class():
    """Clase Objective-C que entrega los datos prometidos cuando macOS los pide
    (al pegar). Se crea una sola vez por proceso."""
    if _PROVIDER_CLASS:
        return _PROVIDER_CLASS[0]
    import objc
    from Foundation import NSObject, NSURL

    class KvmlinkClipProvider(NSObject, protocols=[objc.protocolNamed("NSPasteboardItemDataProvider")]):
        def pasteboard_item_provideDataForType_(self, pb, item, typ):
            fetch = _CLIP_STATE.get("fetch")
            top = _CLIP_STATE.get("mapping", {}).get(objc.pyobjc_id(item))
            if fetch is None or top is None:
                return
            try:
                fetch.ensure_started()     # aquí se piden los archivos al otro equipo
                if not fetch.wait_done():
                    return                 # error: ya está en el registro
                url = NSURL.fileURLWithPath_(fetch.path_for(top))
                item.setString_forType_(url.absoluteString(), typ)
            except Exception as e:
                _log("[mac] error al pegar:", repr(e))

        def pasteboardFinishedWithDataProvider_(self, pb):
            pass

    _PROVIDER_CLASS.append(KvmlinkClipProvider)
    return KvmlinkClipProvider


_MENUBAR_CLASS = []


def _menubar_class():
    """Objeto Objective-C que recibe las acciones del menú y el temporizador del
    hilo principal. Se crea una sola vez por proceso."""
    if _MENUBAR_CLASS:
        return _MENUBAR_CLASS[0]
    import AppKit
    import objc
    from Foundation import NSObject

    class KvmlinkMenubar(NSObject):
        # Ojo: en una clase de Objective-C hecha en Python, cada método se traduce a un
        # selector según sus guiones bajos (openWindow_ -> "openWindow:", 1 argumento).
        # Un método solo para Python con otros argumentos debe marcarse así; sin la
        # marca, PyObjC rechazaba la clase entera y el icono no llegaba a aparecer.
        @objc.python_method
        def configure(self, plat, ui, should_stop):
            self.plat, self.ui, self.should_stop = plat, ui, should_stop
            self.status_item = None
            self.last = None

        def openWindow_(self, sender):
            self.ui["open"]()

        def quitApp_(self, sender):
            self.ui["quit"]()

        def tick_(self, timer):
            self.plat._run_main_tasks()
            try:
                text = self.ui["status"]()
            except Exception:
                text = ""
            if self.status_item is not None and text != self.last:
                self.last = text
                self.status_item.setTitle_(text or "kvmlink")
            if self.should_stop():
                timer.invalidate()
                app = AppKit.NSApplication.sharedApplication()
                app.stop_(None)
                # stop_ solo se nota al llegar el siguiente evento: mandar uno vacío
                ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    AppKit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0)
                app.postEvent_atStart_(ev, True)

    _MENUBAR_CLASS.append(KvmlinkMenubar)
    return KvmlinkMenubar


def _log(*a):
    """Al registro de kvmlink (visible en la ventana, sección Registro) y a la Terminal."""
    try:
        from kvmlink import log
        log(*a)
    except Exception:
        print(*a, flush=True)


def _field(ev, f):
    return Q.CGEventGetIntegerValueField(ev, f)


class MacPlatform:
    os_name = "mac"

    def __init__(self):
        self.core = None
        self.capturing = False
        self._tap = None
        self._src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        self._mods = set()          # modificadores inyectados mantenidos
        self._caps = False
        self._held_keys = set()
        self._held_buttons = set()
        self._pos = self.cursor_pos()
        self._last_click = (None, 0.0, 0, 0, 0)  # botón, t, x, y, cuenta
        self._click_count = 1
        self._park = None
        self._cursor_hidden = False
        self._cgs = None
        self._keep = None
        self._hide_logged = False
        self._drag_pb = None
        self._drag_cc0 = None          # changeCount del arrastre al pulsar el botón
        self._drag_cache = (None, [])  # (changeCount, rutas)
        self._clip_cc = None           # changeCount del portapapeles ya revisado
        self._main_q = queue.Queue()   # tareas para el hilo principal (main_loop)
        self._missing = []             # permisos que faltan (Accesibilidad, Monitorización…)

    # -------------------------------------------------------------- geometría
    def bounds(self):
        err, ids, n = Q.CGGetActiveDisplayList(16, None, None)
        l = t = float("inf")
        r = b = float("-inf")
        for d in ids[:n]:
            rc = Q.CGDisplayBounds(d)
            l, t = min(l, rc.origin.x), min(t, rc.origin.y)
            r = max(r, rc.origin.x + rc.size.width)
            b = max(b, rc.origin.y + rc.size.height)
        return (int(l), int(t), int(r), int(b))

    def monitors(self):
        """[[l, t, r, b, principal, número], ...] en puntos."""
        main = Q.CGMainDisplayID()
        err, ids, n = Q.CGGetActiveDisplayList(16, None, None)
        out = []
        for d in ids[:n]:
            rc = Q.CGDisplayBounds(d)
            out.append([int(rc.origin.x), int(rc.origin.y),
                        int(rc.origin.x + rc.size.width), int(rc.origin.y + rc.size.height),
                        d == main, 0])
        out.sort(key=lambda m: (not m[4], m[0], m[1]))  # la principal primero
        for i, m in enumerate(out, 1):
            m[5] = i
        return out

    def cursor_pos(self):
        p = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return p.x, p.y

    # -------------------------------------------------------------- captura
    @staticmethod
    def _check_permissions(request=False):
        """Qué permisos faltan ([] si está todo). Con request, además se los pide a
        macOS (que muestra su aviso solo la primera vez)."""
        missing = []
        for check, ask, name in (
                ("CGPreflightListenEventAccess", "CGRequestListenEventAccess", "Monitorización de entrada"),
                ("CGPreflightPostEventAccess", "CGRequestPostEventAccess", "Accesibilidad")):
            pre = getattr(Q, check, None)
            if pre is not None and not pre():
                if request and getattr(Q, ask, None) is not None:
                    getattr(Q, ask)()
                missing.append(name)
        return missing

    def permissions_missing(self):
        """Para la interfaz: qué permisos faltan mientras no se haya podido empezar."""
        return [] if self._tap is not None else (self._missing or ["Accesibilidad", "Monitorización de entrada"])

    def start(self, core):
        self.core = core
        # por si una ejecución anterior murió a mitad de captura
        Q.CGAssociateMouseAndMouseCursorPosition(True)
        self._missing = self._check_permissions(request=True)
        ready = threading.Event()

        def run():
            mask = 0
            for t in TAP_TYPES:
                mask |= Q.CGEventMaskBit(t)
            warned = False
            # Sin permisos no se puede crear el tap. En vez de cerrarse (como app no
            # hay Terminal donde leer el error), se reintenta hasta que se conceden.
            while True:
                tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                         Q.kCGEventTapOptionDefault, mask, self._callback, None)
                if tap is not None:
                    break
                if not warned:
                    warned = True
                    _log("[mac] faltan permisos: en Ajustes del Sistema → Privacidad y seguridad, "
                         "activa kvmlink (o la Terminal, si lo abres desde ella) en «Accesibilidad» "
                         "y en «Monitorización de entrada». kvmlink empezará solo al concederlos.")
                    ready.set()
                time.sleep(2)
                self._missing = self._check_permissions()
            self._tap = tap
            if warned:
                _log("[mac] permisos concedidos: kvmlink ya captura el ratón y el teclado")
            src = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), src, Q.kCFRunLoopCommonModes)
            Q.CGEventTapEnable(self._tap, True)
            ready.set()
            Q.CFRunLoopRun()

        threading.Thread(target=run, name="mac-event-tap", daemon=True).start()
        ready.wait()

    def open_permission_settings(self):
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])

    def stop(self):
        self.emergency_restore()

    def emergency_restore(self):
        """Deja el Mac usable pase lo que pase: ratón asociado y cursor visible."""
        self.capturing = False
        self._park = None
        Q.CGAssociateMouseAndMouseCursorPosition(True)
        if self._cursor_hidden:
            Q.CGDisplayShowCursor(Q.CGMainDisplayID())
            self._cursor_hidden = False

    def healthy(self):
        return self._tap is None or bool(Q.CGEventTapIsEnabled(self._tap))

    def _callback(self, proxy, etype, ev, refcon):
        try:
            return self._handle(etype, ev)
        except Exception as e:  # nunca dejar que una excepción mate el tap
            _log("[mac] error en tap:", repr(e))
            try:
                self.core.panic()
            except Exception:
                self.emergency_restore()
            return ev

    def _handle(self, etype, ev):
        if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            Q.CGEventTapEnable(self._tap, True)
            return ev
        if _field(ev, Q.kCGEventSourceUserData) == INJECT_TAG:
            return ev

        if not self.capturing:
            if etype in MOVE_TYPES:
                loc = Q.CGEventGetLocation(ev)
                if self.core.on_local_move(loc.x, loc.y):
                    return None
            elif etype in BTN_DOWN or etype == Q.kCGEventOtherMouseDown:
                if etype == Q.kCGEventLeftMouseDown:
                    # para saber luego si este clic ha empezado un arrastre de archivos:
                    # el Finder escribe en el portapapeles de arrastre al empezar a arrastrar
                    self._drag_cc0 = self._drag_change_count()
                self.core.on_local_button(1)
            elif etype in BTN_UP or etype == Q.kCGEventOtherMouseUp:
                self.core.on_local_button(0)
            return ev

        # --- capturando: reenviar y bloquear localmente
        cap = self.core.capture
        if etype in MOVE_TYPES:
            dx = _field(ev, Q.kCGMouseEventDeltaX)
            dy = _field(ev, Q.kCGMouseEventDeltaY)
            if dx or dy:
                cap({"t": "mm", "dx": dx, "dy": dy})
            # macOS mueve el cursor en la capa HID aunque descartemos el evento:
            # lo devolvemos al punto de aparcamiento en cada movimiento
            if self.capturing and self._park is not None:
                Q.CGWarpMouseCursorPosition(self._park)
        elif etype in BTN_DOWN:
            cap({"t": "mb", "b": BTN_DOWN[etype], "d": 1})
        elif etype in BTN_UP:
            cap({"t": "mb", "b": BTN_UP[etype], "d": 0})
        elif etype in (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp):
            b = OTHER_BTN.get(_field(ev, Q.kCGMouseEventButtonNumber))
            if b:
                cap({"t": "mb", "b": b, "d": int(etype == Q.kCGEventOtherMouseDown)})
        elif etype == Q.kCGEventScrollWheel:
            if _field(ev, Q.kCGScrollWheelEventIsContinuous):   # trackpad / Magic Mouse
                dy = _field(ev, Q.kCGScrollWheelEventPointDeltaAxis1) * 3
                dx = -_field(ev, Q.kCGScrollWheelEventPointDeltaAxis2) * 3
            else:                                                # rueda clásica
                dy = _field(ev, Q.kCGScrollWheelEventDeltaAxis1) * 120
                dx = -_field(ev, Q.kCGScrollWheelEventDeltaAxis2) * 120
            if dx or dy:
                cap({"t": "sc", "dx": dx, "dy": dy})
        elif etype in (Q.kCGEventKeyDown, Q.kCGEventKeyUp):
            name = MAC_TO_NAME.get(_field(ev, Q.kCGKeyboardEventKeycode))
            if name:
                cap({"t": "k", "k": name, "d": int(etype == Q.kCGEventKeyDown)})
        elif etype == Q.kCGEventFlagsChanged:
            name = MAC_TO_NAME.get(_field(ev, Q.kCGKeyboardEventKeycode))
            flags = Q.CGEventGetFlags(ev)
            if name in MOD_BITS:
                cap({"t": "k", "k": name, "d": int(bool(flags & MOD_BITS[name][0]))})
            elif name == "CapsLock":
                cap({"t": "k", "k": name, "d": 1})
                cap({"t": "k", "k": name, "d": 0})
        return None

    def _allow_background_cursor(self):
        """Permite ocultar el cursor aunque la Terminal no sea la app activa
        (propiedad privada de WindowServer que usan Barrier/Deskflow). Best effort:
        se aplica antes de cada ocultación, como hace Deskflow."""
        # Todo con ctypes y CoreFoundation: si se crean con PyObjC (NSString/NSNumber),
        # PyObjC los convierte en str/bool de Python y no se pueden pasar como
        # punteros CF ("TypeError: not an Objective-C object").
        try:
            import ctypes
            if self._cgs is None:
                cg = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
                cg._CGSDefaultConnection.restype = ctypes.c_int
                cg._CGSDefaultConnection.argtypes = ()
                cg.CGSSetConnectionProperty.restype = ctypes.c_int
                cg.CGSSetConnectionProperty.argtypes = (ctypes.c_int, ctypes.c_int,
                                                        ctypes.c_void_p, ctypes.c_void_p)
                cf = ctypes.cdll.LoadLibrary(
                    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                cf.CFStringCreateWithCString.argtypes = (ctypes.c_void_p, ctypes.c_char_p,
                                                         ctypes.c_uint32)
                key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground",
                                                   0x08000100)  # kCFStringEncodingUTF8
                true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
                if not key or not true:
                    return "no se pudieron crear los valores CF"
                self._cgs = cg
                self._keep = (key, true)   # se reutilizan; viven lo que el proceso
            conn = self._cgs._CGSDefaultConnection()
            return self._cgs.CGSSetConnectionProperty(conn, conn, self._keep[0], self._keep[1])
        except Exception as e:
            return repr(e)

    # ------------------------------------------------- arrastrar archivos
    def _drag_pasteboard(self):
        if self._drag_pb is None:
            import AppKit
            name = getattr(AppKit, "NSPasteboardNameDrag", None) or getattr(AppKit, "NSDragPboard")
            self._drag_pb = AppKit.NSPasteboard.pasteboardWithName_(name)
        return self._drag_pb

    def _drag_change_count(self):
        try:
            return self._drag_pasteboard().changeCount()
        except Exception as e:
            _log("[mac] no se puede leer el arrastre:", repr(e))
            return None

    def file_drag_check(self, x, y):
        """Archivos que se están arrastrando, si este clic ha empezado un arrastre
        de archivos (el portapapeles de arrastre ha cambiado desde que se pulsó)."""
        if self._drag_cc0 is None:
            return None
        cc = self._drag_change_count()
        if cc is None or cc == self._drag_cc0:
            return None
        if self._drag_cache[0] != cc:
            paths = []
            try:
                import AppKit
                urls = self._drag_pasteboard().readObjectsForClasses_options_(
                    [AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
                paths = [str(u.path()) for u in urls if u.path()]
            except Exception as e:
                _log("[mac] no se pudieron leer los archivos arrastrados:", repr(e))
            self._drag_cache = (cc, paths)
        return self._drag_cache[1] or None

    def drag_reset(self):
        self._drag_cc0 = None

    # ------------------------------------------- archivos en el portapapeles
    def _clip_file_paths(self, pb):
        import AppKit
        urls = pb.readObjectsForClasses_options_(
            [AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        return [str(u.path()) for u in urls if u.path()]

    def reset_clipboard_watch(self):
        """Al conectar: lo que ya estaba copiado no se ofrece (no se sabe si es más
        reciente que lo copiado en el otro equipo); solo lo que se copie a partir de ahora."""
        try:
            import AppKit
            self._clip_cc = AppKit.NSPasteboard.generalPasteboard().changeCount()
        except Exception:
            self._clip_cc = None

    @staticmethod
    def _pb_types(pb):
        return [str(t) for t in (pb.types() or [])]

    def clipboard_files_changed(self):
        """(cambiado, rutas): si el portapapeles ha cambiado desde la última vez, y
        qué archivos tiene (lista vacía si lo copiado no son archivos o si es la
        promesa que ha puesto kvmlink: leerla la descargaría)."""
        import AppKit
        pb = AppKit.NSPasteboard.generalPasteboard()
        cc = pb.changeCount()
        if cc == self._clip_cc:
            return False, None
        self._clip_cc = cc
        types = self._pb_types(pb)
        if KVMLINK_PB_TYPE in types or "public.file-url" not in types:
            return True, []
        return True, self._clip_file_paths(pb)

    def clipboard_has_files(self):
        """Solo mira los tipos (sin pedir los datos, que con nuestra promesa
        provocarían la descarga)."""
        try:
            import AppKit
            types = self._pb_types(AppKit.NSPasteboard.generalPasteboard())
            return "public.file-url" in types and KVMLINK_PB_TYPE not in types
        except Exception:
            return False

    def offer_clipboard_files(self, fetch):
        """Pone en el portapapeles una promesa por cada archivo o carpeta de primer
        nivel copiado en el otro equipo. Al pegar, macOS pide su ubicación: entonces
        se descarga todo (fetch) y se da la ruta de la copia temporal."""
        tops = []
        for key, _, _ in fetch.entries:
            top = key.split("/")[0]
            if top not in tops:
                tops.append(top)

        def do():
            import AppKit
            import objc
            prov = _clip_provider_class().alloc().init()
            items, mapping = [], {}
            for i, top in enumerate(tops):
                it = AppKit.NSPasteboardItem.alloc().init()
                it.setDataProvider_forTypes_(prov, ["public.file-url"])
                if i == 0:
                    it.setString_forType_(fetch.offer_id, KVMLINK_PB_TYPE)
                mapping[objc.pyobjc_id(it)] = top
                items.append(it)
            _CLIP_STATE.update(fetch=fetch, mapping=mapping, provider=prov, items=items)
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            if not pb.writeObjects_(items):
                fetch.log("no se pudo poner los archivos en el portapapeles del Mac")
            self._clip_cc = pb.changeCount()   # no tomarlo como algo copiado aquí
        self.run_on_main(do)
        return True

    # --------------------------------------------- hilo principal (run loop)
    def run_on_main(self, fn):
        """Ejecuta fn en el hilo principal, que es donde macOS entrega las peticiones
        del portapapeles (ver main_loop)."""
        self._main_q.put(fn)

    def _run_main_tasks(self):
        while True:
            try:
                fn = self._main_q.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as e:
                _log("[mac] error en el hilo principal:", repr(e))

    def main_loop(self, should_stop, ui=None):
        """El hilo principal atiende a macOS: peticiones de las promesas del
        portapapeles, tareas encargadas con run_on_main y, con ui (dict con open, quit
        y status), el icono de kvmlink en la barra de menús."""
        if ui:
            try:
                self._run_menubar_app(should_stop, ui)
                return
            except Exception as e:
                _log("[mac] sin icono en la barra de menús:", repr(e))
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop
        rl = NSRunLoop.currentRunLoop()
        while not should_stop():
            self._run_main_tasks()
            rl.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.25))

    def _run_menubar_app(self, should_stop, ui):
        """kvmlink como aplicación de la barra de menús (sin icono en el Dock)."""
        import AppKit
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        helper = _menubar_class().alloc().init()
        helper.configure(self, ui, should_stop)
        item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        image = None
        if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("display.2", "kvmlink")
        if image is not None:
            image.setTemplate_(True)   # se adapta al modo claro/oscuro de la barra
            item.button().setImage_(image)
        else:
            item.button().setTitle_("kvm")
        menu = AppKit.NSMenu.alloc().init()
        status = menu.addItemWithTitle_action_keyEquivalent_("kvmlink", None, "")
        status.setEnabled_(False)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Abrir kvmlink", "openWindow:", "").setTarget_(helper)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("Salir de kvmlink", "quitApp:", "").setTarget_(helper)
        item.setMenu_(menu)
        helper.status_item = status
        self._menubar = (item, menu, helper)   # que no se liberen
        timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.25, helper, "tick:", None, True)
        # modo "común": el temporizador sigue funcionando con el menú abierto
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
        app.run()

    def remote_drag_files(self):
        """El otro equipo nos manejaba y se ha ido con el botón pulsado aquí: ¿se
        estaban arrastrando archivos del Finder?"""
        return self.file_drag_check(0, 0)

    def end_file_drag(self, at=None):
        """Se ha soltado en el otro equipo: cancelar el arrastre aquí (Esc) y soltar el
        botón sobre la barra de menús, donde no se puede dejar caer nada. Sin mover el
        cursor (un salto con warp estropea los deltas, ver begin_capture)."""
        self._drag_cc0 = None
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(self._src, 0x35, down)   # Esc
            self._post(ev)
        rc = Q.CGDisplayBounds(Q.CGMainDisplayID())
        spot = (rc.origin.x + rc.size.width / 2, rc.origin.y + 4)
        ev = Q.CGEventCreateMouseEvent(self._src, Q.kCGEventLeftMouseUp, spot, Q.kCGMouseButtonLeft)
        self._post(ev)
        self._held_buttons.discard("left")

    def cancel_file_drag(self):
        pass   # al volver, el arrastre del Finder sigue donde estaba

    def begin_capture(self, drag=False):
        self.capturing = True
        # El cursor se queda donde salió (2 px hacia dentro). NO se lleva a otro
        # sitio: un salto grande con CGWarpMouseCursorPosition hacía que macOS
        # mandara un movimiento falso enorme al otro equipo y el control rebotaba.
        x, y = self.cursor_pos()
        l, t, r, b = self.bounds()
        self._park = (min(max(x, l + 2), r - 3), min(max(y, t + 2), b - 3))
        # sin esto, cada warp bloquea el ratón físico ~0,25 s
        try:
            Q.CGSetLocalEventsSuppressionInterval(0.0)
        except Exception:
            pass
        Q.CGWarpMouseCursorPosition(self._park)
        Q.CGAssociateMouseAndMouseCursorPosition(False)
        if not self._cursor_hidden:
            prop = self._allow_background_cursor()
            err = Q.CGDisplayHideCursor(Q.CGMainDisplayID())
            self._cursor_hidden = (err == 0)
            if not self._hide_logged:
                self._hide_logged = True
                _log(f"[mac] ocultar cursor: SetsCursorInBackground={prop}, "
                     f"CGDisplayHideCursor={err} (0 = correcto)")

    def end_capture(self, x, y):
        self.capturing = False
        self._park = None
        Q.CGWarpMouseCursorPosition((x, y))
        Q.CGAssociateMouseAndMouseCursorPosition(True)
        if self._cursor_hidden:
            Q.CGDisplayShowCursor(Q.CGMainDisplayID())
            self._cursor_hidden = False

    # -------------------------------------------------------------- inyección
    def _flags(self):
        f = 0
        for m in self._mods:
            dev, generic = MOD_BITS[m]
            f |= dev | generic
        if self._caps:
            f |= Q.kCGEventFlagMaskAlphaShift
        return f

    def _post(self, ev):
        Q.CGEventSetIntegerValueField(ev, Q.kCGEventSourceUserData, INJECT_TAG)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def inject_move(self, x, y):
        held = self._held_buttons
        if "left" in held:
            etype, btn = Q.kCGEventLeftMouseDragged, Q.kCGMouseButtonLeft
        elif "right" in held:
            etype, btn = Q.kCGEventRightMouseDragged, Q.kCGMouseButtonRight
        elif held:
            etype, btn = Q.kCGEventOtherMouseDragged, Q.kCGMouseButtonCenter
        else:
            etype, btn = Q.kCGEventMouseMoved, Q.kCGMouseButtonLeft
        px, py = self._pos
        ev = Q.CGEventCreateMouseEvent(self._src, etype, (x, y), btn)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, int(x - px))
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, int(y - py))
        Q.CGEventSetFlags(ev, self._flags())
        self._pos = (x, y)
        self._post(ev)

    def inject_button(self, button, down):
        x, y = self._pos
        if down:
            lb, lt, lx, ly, cnt = self._last_click
            now = time.monotonic()
            if (lb == button and now - lt < DOUBLE_CLICK_TIME
                    and abs(x - lx) <= DOUBLE_CLICK_DIST and abs(y - ly) <= DOUBLE_CLICK_DIST):
                cnt += 1
            else:
                cnt = 1
            self._last_click = (button, now, x, y, cnt)
            self._click_count = cnt
            self._held_buttons.add(button)
            if button == "left":
                # el otro equipo nos maneja: anotar el estado del arrastre igual que con un
                # clic físico, por si con este clic empieza a arrastrar archivos del Finder
                self._drag_cc0 = self._drag_change_count()
        else:
            self._held_buttons.discard(button)
        if button == "left":
            etype = Q.kCGEventLeftMouseDown if down else Q.kCGEventLeftMouseUp
            btn = Q.kCGMouseButtonLeft
        elif button == "right":
            etype = Q.kCGEventRightMouseDown if down else Q.kCGEventRightMouseUp
            btn = Q.kCGMouseButtonRight
        else:
            etype = Q.kCGEventOtherMouseDown if down else Q.kCGEventOtherMouseUp
            btn = OTHER_BTN_NUM.get(button, 2)
        ev = Q.CGEventCreateMouseEvent(self._src, etype, (x, y), btn)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, self._click_count)
        if btn not in (Q.kCGMouseButtonLeft, Q.kCGMouseButtonRight):
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventButtonNumber, btn)
        Q.CGEventSetFlags(ev, self._flags())
        self._post(ev)

    def inject_scroll(self, dx, dy):
        # unidades de protocolo: 120 = un "clic" de rueda ≈ 40 px
        ev = Q.CGEventCreateScrollWheelEvent(self._src, Q.kCGScrollEventUnitPixel, 2,
                                             int(dy / 3), int(-dx / 3))
        Q.CGEventSetFlags(ev, self._flags())
        self._post(ev)

    def inject_key(self, name, down):
        kc = NAME_TO_MAC.get(name)
        if kc is None:
            return
        ev = Q.CGEventCreateKeyboardEvent(self._src, kc, bool(down))
        if name in MOD_BITS:
            (self._mods.add if down else self._mods.discard)(name)
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        elif name == "CapsLock":
            if not down:
                return
            self._caps = not self._caps
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        flags = self._flags()
        if name in FN_KEYS:
            flags |= Q.kCGEventFlagMaskSecondaryFn
        if name in ARROWS:
            flags |= Q.kCGEventFlagMaskNumericPad
        Q.CGEventSetFlags(ev, flags)
        (self._held_keys.add if down else self._held_keys.discard)(name)
        self._post(ev)

    def release_all(self, keep=()):
        """Suelta lo que hayamos dejado pulsado al inyectar (salvo los botones de `keep`)."""
        for k in list(self._held_keys):
            self.inject_key(k, 0)
        for b in list(self._held_buttons):
            if b not in keep:
                self.inject_button(b, 0)
        self._mods.clear()
