"""Pegar en Windows archivos copiados en el otro equipo, sin enviarlos hasta pegar.

Se pone en el portapapeles un IDataObject con "archivos virtuales" (formatos
FileGroupDescriptorW + FileContents, como hace Outlook con los adjuntos). Copiar
no transfiere nada: solo viajan nombres y tamaños. Al pegar, el Explorador pide el
contenido de cada archivo como un IStream; la primera petición hace que kvmlink pida
los archivos al otro equipo, y las lecturas del IStream esperan a que vayan llegando.
El Explorador muestra su propia ventana de progreso.

Los objetos COM (IDataObject, IEnumFORMATETC, IStream) están hechos con ctypes y
viven en un hilo STA propio con su bucle de mensajes, que es donde el Explorador
llama (OLE se encarga de pasar las llamadas entre procesos).
"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading

from dragdrop_win import FORMATETC, GUID, QI_T, REF_T, STGMEDIUM

ole32 = ctypes.WinDLL("ole32")
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")


def _hr(x):
    return x - (1 << 32) if x & 0x80000000 else x


S_OK, S_FALSE = 0, 1
E_NOTIMPL, E_NOINTERFACE = _hr(0x80004001), _hr(0x80004002)
DV_E_FORMATETC, DV_E_LINDEX, DV_E_TYMED = _hr(0x80040064), _hr(0x80040068), _hr(0x80040069)
OLE_E_ADVISENOTSUPPORTED = _hr(0x80040003)
DATA_S_SAMEFORMATETC = 0x00040130
STG_E_ACCESSDENIED, STG_E_READFAULT, STG_E_INVALIDFUNCTION = _hr(0x80030005), _hr(0x8003001E), _hr(0x80030001)
TYMED_HGLOBAL, TYMED_ISTREAM = 1, 4
DATADIR_GET = 1
GMEM_MOVEABLE = 0x2
FD_ATTRIBUTES, FD_FILESIZE, FD_PROGRESSUI, FD_UNICODE = 0x4, 0x40, 0x4000, 0x80000000
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL = 0x10, 0x80
DROPEFFECT_COPY = 1
WM_APP = 0x8000

user32.RegisterClipboardFormatW.argtypes = (wt.LPCWSTR,)
user32.RegisterClipboardFormatW.restype = wt.UINT
CF_FILEDESCRIPTORW = user32.RegisterClipboardFormatW("FileGroupDescriptorW")
CF_FILEDESCRIPTORA = user32.RegisterClipboardFormatW("FileGroupDescriptor")
CF_FILECONTENTS = user32.RegisterClipboardFormatW("FileContents")
CF_PREFERREDDROPEFFECT = user32.RegisterClipboardFormatW("Preferred DropEffect")
CF_KVMLINK = user32.RegisterClipboardFormatW("kvmlink.offer")   # para reconocer lo nuestro

kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
ole32.OleSetClipboard.argtypes = (ctypes.c_void_p,)
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.GetMessageW.argtypes = (ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT)

IID_IUNKNOWN = GUID.parse("00000000-0000-0000-C000-000000000046").key()
IID_IDATAOBJECT = GUID.parse("0000010e-0000-0000-C000-000000000046").key()
IID_IENUMFORMATETC = GUID.parse("00000103-0000-0000-C000-000000000046").key()
IID_ISTREAM = GUID.parse("0000000c-0000-0000-C000-000000000046").key()
IID_ISEQUENTIALSTREAM = GUID.parse("0c733a30-2a1c-11ce-ade5-00aa0044773d").key()


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


class FILEDESCRIPTORW(ctypes.Structure):
    _fields_ = [("dwFlags", wt.DWORD), ("clsid", GUID), ("sizel", wt.SIZE), ("pointl", wt.POINT),
                ("dwFileAttributes", wt.DWORD), ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME), ("ftLastWriteTime", FILETIME),
                ("nFileSizeHigh", wt.DWORD), ("nFileSizeLow", wt.DWORD), ("cFileName", wt.WCHAR * 260)]


class FILEDESCRIPTORA(ctypes.Structure):
    _fields_ = [("dwFlags", wt.DWORD), ("clsid", GUID), ("sizel", wt.SIZE), ("pointl", wt.POINT),
                ("dwFileAttributes", wt.DWORD), ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME), ("ftLastWriteTime", FILETIME),
                ("nFileSizeHigh", wt.DWORD), ("nFileSizeLow", wt.DWORD), ("cFileName", ctypes.c_char * 260)]


class STATSTG(ctypes.Structure):
    _fields_ = [("pwcsName", ctypes.c_void_p), ("type", wt.DWORD), ("cbSize", ctypes.c_uint64),
                ("mtime", FILETIME), ("ctime", FILETIME), ("atime", FILETIME), ("grfMode", wt.DWORD),
                ("grfLocksSupported", wt.DWORD), ("clsid", GUID), ("grfStateBits", wt.DWORD),
                ("reserved", wt.DWORD)]


PFMT, PSTG = ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)
PVOIDP = ctypes.POINTER(ctypes.c_void_p)
HR = ctypes.c_long
W = ctypes.WINFUNCTYPE


class IDataObjectVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T), ("Release", REF_T),
                ("GetData", W(HR, ctypes.c_void_p, PFMT, PSTG)),
                ("GetDataHere", W(HR, ctypes.c_void_p, PFMT, PSTG)),
                ("QueryGetData", W(HR, ctypes.c_void_p, PFMT)),
                ("GetCanonicalFormatEtc", W(HR, ctypes.c_void_p, PFMT, PFMT)),
                ("SetData", W(HR, ctypes.c_void_p, PFMT, PSTG, wt.BOOL)),
                ("EnumFormatEtc", W(HR, ctypes.c_void_p, wt.DWORD, PVOIDP)),
                ("DAdvise", W(HR, ctypes.c_void_p, PFMT, wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD))),
                ("DUnadvise", W(HR, ctypes.c_void_p, wt.DWORD)),
                ("EnumDAdvise", W(HR, ctypes.c_void_p, PVOIDP))]


class IEnumFORMATETCVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T), ("Release", REF_T),
                ("Next", W(HR, ctypes.c_void_p, wt.ULONG, PFMT, ctypes.POINTER(wt.ULONG))),
                ("Skip", W(HR, ctypes.c_void_p, wt.ULONG)),
                ("Reset", W(HR, ctypes.c_void_p)),
                ("Clone", W(HR, ctypes.c_void_p, PVOIDP))]


class IStreamVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", QI_T), ("AddRef", REF_T), ("Release", REF_T),
                ("Read", W(HR, ctypes.c_void_p, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG))),
                ("Write", W(HR, ctypes.c_void_p, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG))),
                ("Seek", W(HR, ctypes.c_void_p, ctypes.c_int64, wt.DWORD, ctypes.POINTER(ctypes.c_uint64))),
                ("SetSize", W(HR, ctypes.c_void_p, ctypes.c_uint64)),
                ("CopyTo", W(HR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
                             ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64))),
                ("Commit", W(HR, ctypes.c_void_p, wt.DWORD)),
                ("Revert", W(HR, ctypes.c_void_p)),
                ("LockRegion", W(HR, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, wt.DWORD)),
                ("UnlockRegion", W(HR, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, wt.DWORD)),
                ("Stat", W(HR, ctypes.c_void_p, ctypes.POINTER(STATSTG), wt.DWORD)),
                ("Clone", W(HR, ctypes.c_void_p, PVOIDP))]


class _ComBase:
    """Objeto COM mínimo: una estructura cuyo primer campo apunta a la vtable.
    Las referencias se cuentan pero el objeto Python vive mientras siga en _LIVE."""
    IIDS = (IID_IUNKNOWN,)
    VTBL = None

    def __init__(self, **methods):
        self._refs = 1
        base = {"QueryInterface": QI_T(self._qi), "AddRef": REF_T(self._addref),
                "Release": REF_T(self._release)}
        base.update(methods)
        vt_cls = self.VTBL
        self._vt = vt_cls(*[base[name] if name in base else None for name, _ in vt_cls._fields_])
        self._obj = (ctypes.c_void_p * 1)(ctypes.cast(ctypes.pointer(self._vt), ctypes.c_void_p))
        _LIVE.add(self)

    @property
    def ptr(self):
        return ctypes.cast(self._obj, ctypes.c_void_p).value

    def _qi(self, this, riid, ppv):
        if riid.contents.key() in self.IIDS:
            ppv[0] = this
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs -= 1
        if self._refs <= 0:
            _LIVE.discard(self)
            return 0
        return self._refs


_LIVE = set()


def _hglobal(data: bytes):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


class EnumFormats(_ComBase):
    IIDS = (IID_IUNKNOWN, IID_IENUMFORMATETC)
    VTBL = IEnumFORMATETCVtbl

    def __init__(self, formats, pos=0):
        self.formats, self.pos = formats, pos
        super().__init__(Next=self.VTBL._fields_[3][1](self._next), Skip=self.VTBL._fields_[4][1](self._skip),
                         Reset=self.VTBL._fields_[5][1](self._reset), Clone=self.VTBL._fields_[6][1](self._clone))

    def _next(self, this, celt, rgelt, fetched):
        n = 0
        while n < celt and self.pos < len(self.formats):
            cf, tymed, lindex = self.formats[self.pos]
            rgelt[n] = FORMATETC(cf, None, 1, lindex, tymed)
            n += 1
            self.pos += 1
        if fetched:
            fetched[0] = n
        return S_OK if n == celt else S_FALSE

    def _skip(self, this, celt):
        self.pos += celt
        return S_OK if self.pos <= len(self.formats) else S_FALSE

    def _reset(self, this):
        self.pos = 0
        return S_OK

    def _clone(self, this, ppenum):
        ppenum[0] = EnumFormats(self.formats, self.pos).ptr
        return S_OK


class FileStream(_ComBase):
    """IStream de solo lectura sobre un archivo que todavía puede estar llegando."""
    IIDS = (IID_IUNKNOWN, IID_ISEQUENTIALSTREAM, IID_ISTREAM)
    VTBL = IStreamVtbl

    def __init__(self, fetch, rel, size):
        self.fetch, self.rel, self.size, self.pos = fetch, rel, size, 0
        f = dict(IStreamVtbl._fields_)
        notimpl = lambda *a: E_NOTIMPL  # noqa: E731
        super().__init__(
            Read=f["Read"](self._read), Write=f["Write"](lambda *a: STG_E_ACCESSDENIED),
            Seek=f["Seek"](self._seek), SetSize=f["SetSize"](lambda *a: STG_E_ACCESSDENIED),
            CopyTo=f["CopyTo"](notimpl), Commit=f["Commit"](lambda *a: S_OK), Revert=f["Revert"](lambda *a: S_OK),
            LockRegion=f["LockRegion"](notimpl), UnlockRegion=f["UnlockRegion"](notimpl),
            Stat=f["Stat"](self._stat), Clone=f["Clone"](notimpl))

    def _read(self, this, pv, cb, pread):
        try:
            data = self.fetch.read(self.rel, self.pos, cb)   # espera a que llegue
        except Exception as e:
            self.fetch.log(f"no se pudo pegar «{self.rel}»:", e)
            if pread:
                pread[0] = 0
            return STG_E_READFAULT
        if data:
            ctypes.memmove(pv, data, len(data))
        self.pos += len(data)
        if pread:
            pread[0] = len(data)
        return S_OK if len(data) == cb else S_FALSE

    def _seek(self, this, move, origin, newpos):
        base = {0: 0, 1: self.pos, 2: self.size}.get(origin)
        if base is None or base + move < 0:
            return STG_E_INVALIDFUNCTION
        self.pos = base + move
        if newpos:
            newpos[0] = self.pos
        return S_OK

    def _stat(self, this, pstat, flag):
        st = STATSTG()
        st.type = 2   # STGTY_STREAM
        st.cbSize = self.size
        pstat[0] = st
        return S_OK


class VirtualFiles(_ComBase):
    """IDataObject con la lista de archivos (sin datos) del otro equipo."""
    IIDS = (IID_IUNKNOWN, IID_IDATAOBJECT)
    VTBL = IDataObjectVtbl

    def __init__(self, fetch, entries):
        """entries: [(nombre_relativo_con_barras, tamaño, es_carpeta)] en orden; las
        carpetas van antes que su contenido."""
        self.fetch, self.entries = fetch, entries
        self.descriptor = self._build_descriptor(entries, FILEDESCRIPTORW)
        # versión antigua (ANSI): además de la W, como Outlook; sin ella el Explorador
        # rechaza los archivos virtuales cuando llegan por el portapapeles
        self.descriptor_a = self._build_descriptor(entries, FILEDESCRIPTORA)
        self.formats = [(CF_FILEDESCRIPTORW, TYMED_HGLOBAL, -1), (CF_FILEDESCRIPTORA, TYMED_HGLOBAL, -1),
                        (CF_FILECONTENTS, TYMED_ISTREAM, -1),
                        (CF_PREFERREDDROPEFFECT, TYMED_HGLOBAL, -1), (CF_KVMLINK, TYMED_HGLOBAL, -1)]
        f = dict(IDataObjectVtbl._fields_)
        super().__init__(
            GetData=f["GetData"](self._get_data), GetDataHere=f["GetDataHere"](lambda *a: E_NOTIMPL),
            QueryGetData=f["QueryGetData"](self._query), GetCanonicalFormatEtc=f["GetCanonicalFormatEtc"](self._canon),
            SetData=f["SetData"](lambda *a: E_NOTIMPL), EnumFormatEtc=f["EnumFormatEtc"](self._enum),
            DAdvise=f["DAdvise"](lambda *a: OLE_E_ADVISENOTSUPPORTED),
            DUnadvise=f["DUnadvise"](lambda *a: OLE_E_ADVISENOTSUPPORTED),
            EnumDAdvise=f["EnumDAdvise"](lambda *a: OLE_E_ADVISENOTSUPPORTED))

    @staticmethod
    def _build_descriptor(entries, struct):
        n = len(entries)
        size_one = ctypes.sizeof(struct)
        buf = (ctypes.c_byte * (4 + n * size_one))()
        ctypes.c_uint32.from_buffer(buf, 0).value = n
        wide = struct is FILEDESCRIPTORW
        for i, (rel, size, is_dir) in enumerate(entries):
            fd = struct.from_buffer(buf, 4 + i * size_one)
            fd.dwFlags = (FD_ATTRIBUTES | FD_PROGRESSUI | (FD_UNICODE if wide else 0)
                          | (0 if is_dir else FD_FILESIZE))
            fd.dwFileAttributes = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL
            fd.nFileSizeHigh, fd.nFileSizeLow = size >> 32, size & 0xFFFFFFFF
            name = rel.replace("/", "\\")[:259]
            fd.cFileName = name if wide else name.encode("mbcs", "replace")[:259]
        return bytes(buf)

    def _supported(self, fmt):
        f = fmt.contents
        if f.cfFormat == CF_FILECONTENTS:
            return f.tymed & TYMED_ISTREAM
        return (f.cfFormat in (CF_FILEDESCRIPTORW, CF_FILEDESCRIPTORA, CF_PREFERREDDROPEFFECT, CF_KVMLINK)
                and f.tymed & TYMED_HGLOBAL)

    def _query(self, this, fmt):
        return S_OK if self._supported(fmt) else DV_E_FORMATETC

    def _canon(self, this, fin, fout):
        if fout:
            fout[0] = fin[0]
            fout[0].ptd = None
        return DATA_S_SAMEFORMATETC

    def _enum(self, this, direction, ppenum):
        if direction != DATADIR_GET:
            return E_NOTIMPL
        ppenum[0] = EnumFormats(self.formats).ptr
        return S_OK

    def _get_data(self, this, fmt, medium):
        try:
            f = fmt.contents
            if f.cfFormat == CF_FILECONTENTS and f.tymed & TYMED_ISTREAM:
                idx = f.lindex
                if not 0 <= idx < len(self.entries) or self.entries[idx][2]:
                    return DV_E_LINDEX
                rel, size, _ = self.entries[idx]
                self.fetch.ensure_started()       # aquí es cuando se piden los archivos
                medium[0] = STGMEDIUM(TYMED_ISTREAM, FileStream(self.fetch, rel, size).ptr, None)
                return S_OK
            if not f.tymed & TYMED_HGLOBAL:
                return DV_E_TYMED
            if f.cfFormat == CF_FILEDESCRIPTORW:
                data = self.descriptor
            elif f.cfFormat == CF_FILEDESCRIPTORA:
                data = self.descriptor_a
            elif f.cfFormat == CF_PREFERREDDROPEFFECT:
                data = DROPEFFECT_COPY.to_bytes(4, "little")
            elif f.cfFormat == CF_KVMLINK:
                data = str(self.fetch.offer_id).encode()
            else:
                return DV_E_FORMATETC
            medium[0] = STGMEDIUM(TYMED_HGLOBAL, _hglobal(data), None)
            return S_OK
        except Exception as e:
            self.fetch.log("error al pegar:", repr(e))
            return DV_E_FORMATETC


class ClipboardOwner:
    """Hilo STA dueño del portapapeles: todo lo de OLE se hace en él."""

    def __init__(self, log):
        self.log = log
        self._q = queue.Queue()
        self._tid = None
        self._current = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), name="clipboard", daemon=True).start()
        ready.wait(5)

    def _run(self, ready):
        ole32.OleInitialize(None)
        self._tid = kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # crea la cola de mensajes
        ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            while True:
                try:
                    fn = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as e:
                    self.log("error en el portapapeles:", repr(e))

    def _call(self, fn):
        self._q.put(fn)
        user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def offer(self, fetch, entries):
        """Pone en el portapapeles los archivos del otro equipo (sin descargarlos)."""
        def do():
            obj = VirtualFiles(fetch, entries)
            hr = ole32.OleSetClipboard(obj.ptr)
            if hr != S_OK:
                self.log(f"no se pudo poner los archivos en el portapapeles (0x{hr & 0xFFFFFFFF:08x})")
                return
            self._current = obj   # mantenerlo vivo mientras esté en el portapapeles
        self._call(do)
