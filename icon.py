"""Icono de kvmlink dibujado por código (sin archivos binarios en el proyecto).

Dos pantallas, una al lado de la otra, sobre un cuadrado redondeado azul, con una flecha
que pasa de una a otra. Se rasteriza con funciones de distancia (bordes suavizados) y se
guarda como PNG; a partir de él se hacen el .ico de Windows y el .icns del Mac.
"""
import struct
import zlib

BG_TOP, BG_BOTTOM = (0, 120, 212), (0, 84, 166)
WHITE = (255, 255, 255)


def _rrect(px, py, cx, cy, hw, hh, r):
    """Distancia con signo a un rectángulo redondeado (negativo = dentro)."""
    qx, qy = abs(px - cx) - hw + r, abs(py - cy) - hh + r
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return (ox * ox + oy * oy) ** 0.5 + min(max(qx, qy), 0.0) - r


def _tri(px, py, a, b, c):
    """Distancia aproximada con signo a un triángulo."""
    def edge(p0, p1):
        ex, ey = p1[0] - p0[0], p1[1] - p0[1]
        ln = (ex * ex + ey * ey) ** 0.5
        return ((px - p0[0]) * ey - (py - p0[1]) * ex) / ln
    return max(edge(a, b), edge(b, c), edge(c, a))


def _cov(d, aa):
    return min(max(0.5 - d / aa, 0.0), 1.0)


def render(size):
    """Bytes RGBA (size × size) del icono."""
    s = size / 100.0
    aa = max(1.0, 100.0 / size) * s   # suavizado de ~1 px
    out = bytearray(size * size * 4)
    for y in range(size):
        fy = (y + 0.5)
        t = y / max(1, size - 1)
        bg = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(size):
            fx = (x + 0.5)
            d_bg = _rrect(fx, fy, 50 * s, 50 * s, 46 * s, 46 * s, 20 * s)
            a_bg = _cov(d_bg, aa)
            if a_bg <= 0:
                continue
            # dos pantallas (marco blanco, interior azul) y sus pies
            white = 0.0
            for cx in (31, 69):
                frame = _cov(_rrect(fx, fy, cx * s, 44 * s, 15 * s, 11 * s, 3 * s), aa)
                inner = _cov(_rrect(fx, fy, cx * s, 44 * s, 11.5 * s, 7.5 * s, 1.5 * s), aa)
                stand = _cov(_rrect(fx, fy, cx * s, 60 * s, 2.2 * s, 5 * s, 0.5 * s), aa)
                foot = _cov(_rrect(fx, fy, cx * s, 66 * s, 8 * s, 1.8 * s, 1.5 * s), aa)
                white = max(white, frame - inner, stand, foot)
            # flecha que cruza de una pantalla a la otra
            shaft = _cov(_rrect(fx, fy, 47 * s, 78 * s, 12 * s, 2.4 * s, 1.2 * s), aa)
            head = _cov(_tri(fx, fy, (57 * s, 71 * s), (68 * s, 78 * s), (57 * s, 85 * s)), aa)
            white = max(white, shaft, head)
            r = bg[0] + (WHITE[0] - bg[0]) * white
            g = bg[1] + (WHITE[1] - bg[1]) * white
            b = bg[2] + (WHITE[2] - bg[2]) * white
            i = (y * size + x) * 4
            out[i:i + 4] = bytes((int(r), int(g), int(b), int(255 * a_bg)))
    return bytes(out)


def png(size):
    raw = render(size)
    rows = b"".join(b"\x00" + raw[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def write_ico(path, sizes=(16, 24, 32, 48, 64, 256)):
    """.ico con las imágenes en PNG (lo admite Windows desde Vista)."""
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(images))


def write_iconset(folder):
    """Carpeta .iconset para iconutil (Mac)."""
    import os
    os.makedirs(folder, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            if px > 512:
                continue
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            with open(os.path.join(folder, name), "wb") as f:
                f.write(png(px))


if __name__ == "__main__":
    import sys
    with open(sys.argv[1] if len(sys.argv) > 1 else "kvmlink.png", "wb") as f:
        f.write(png(256))
