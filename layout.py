"""Geometría de la disposición de pantallas entre los dos equipos.

Cada equipo tiene sus monitores en su propio sistema de coordenadas (píxeles en
Windows, puntos en macOS). La disposición es un desplazamiento D = (dx, dy):

    coordenada_en_este_equipo = coordenada_en_el_otro + D

Así se sitúan todos los monitores del otro equipo en el plano de este, igual que
cuando arrastras un monitor en la configuración de pantalla de Windows. El otro
equipo guarda el desplazamiento opuesto (-dx, -dy).
"""

EDGE_TOLERANCE = 3   # px de holgura al buscar la pantalla vecina al otro lado del borde
RETURN_INSET = 3     # px que se mete el cursor hacia dentro al volver


def mon_rect(m):
    """Monitor como (l, t, r, b). Acepta lista [l,t,r,b,(primary)] o dict."""
    if isinstance(m, dict):
        return (m["l"], m["t"], m["r"], m["b"])
    return tuple(m[:4])


def rects(mons):
    return [mon_rect(m) for m in mons]


def union(rs):
    return (min(r[0] for r in rs), min(r[1] for r in rs),
            max(r[2] for r in rs), max(r[3] for r in rs))


def contains(r, x, y):
    return r[0] <= x < r[2] and r[1] <= y < r[3]


def monitor_at(rs, x, y):
    for r in rs:
        if contains(r, x, y):
            return r
    return None


def nearest_monitor(rs, x, y):
    def dist(r):
        cx = min(max(x, r[0]), r[2] - 1)
        cy = min(max(y, r[1]), r[3] - 1)
        return (cx - x) ** 2 + (cy - y) ** 2
    return min(rs, key=dist)


def clamp_into(r, x, y, inset=0):
    return (min(max(x, r[0] + inset), r[2] - 1 - inset),
            min(max(y, r[1] + inset), r[3] - 1 - inset))


def find_crossing(x, y, mine, peer, d):
    """Si el cursor local (x, y) está en un borde de sus monitores que da a una
    pantalla del otro equipo, devuelve el punto de entrada en coordenadas del otro.
    mine y peer son listas de rectángulos; d el desplazamiento de la disposición."""
    if not mine or not peer or d is None:
        return None
    m = monitor_at(mine, x, y) or nearest_monitor(mine, x, y)
    probes = []
    if x >= m[2] - 1:
        probes.append((1, 0))
    if x <= m[0]:
        probes.append((-1, 0))
    if y >= m[3] - 1:
        probes.append((0, 1))
    if y <= m[1]:
        probes.append((0, -1))
    for sx, sy in probes:
        ex = m[2] if sx > 0 else (m[0] - 1 if sx < 0 else x)
        ey = m[3] if sy > 0 else (m[1] - 1 if sy < 0 else y)
        if monitor_at(mine, ex, ey):
            continue  # borde entre dos monitores de este mismo equipo
        for k in range(EDGE_TOLERANCE + 1):
            qx, qy = ex + sx * k - d[0], ey + sy * k - d[1]
            pm = monitor_at(peer, qx, qy)
            if pm:
                return clamp_into(pm, qx, qy, inset=1)
    return None


def step_remote(vx, vy, last, peer, mine, d):
    """Mueve el cursor virtual en el otro equipo. Devuelve
    ("remote", x, y, monitor) o ("local", x, y, None) si ha vuelto a este equipo."""
    pm = monitor_at(peer, vx, vy)
    if pm:
        return ("remote", vx, vy, pm)
    wx, wy = vx + d[0], vy + d[1]
    m = monitor_at(mine, wx, wy)
    if m:
        x, y = clamp_into(m, wx, wy, inset=RETURN_INSET)
        return ("local", x, y, None)
    last = last or nearest_monitor(peer, vx, vy)
    x, y = clamp_into(last, vx, vy)
    return ("remote", x, y, last)


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def default_offset(mine, peer, side="right"):
    """Coloca el grupo de pantallas del otro equipo pegado al monitor de este que
    está más hacia `side`, centrado respecto a él."""
    pb = union(peer)
    pw, ph = pb[2] - pb[0], pb[3] - pb[1]
    if side == "left":
        m = min(mine, key=lambda r: r[0])
        px, py = m[0] - pw, (m[1] + m[3]) / 2 - ph / 2
    elif side == "top":
        m = min(mine, key=lambda r: r[1])
        px, py = (m[0] + m[2]) / 2 - pw / 2, m[1] - ph
    elif side == "bottom":
        m = max(mine, key=lambda r: r[3])
        px, py = (m[0] + m[2]) / 2 - pw / 2, m[3]
    else:
        m = max(mine, key=lambda r: r[2])
        px, py = m[2], (m[1] + m[3]) / 2 - ph / 2
    return (round(px - pb[0]), round(py - pb[1]))


def snap_offset(mine, peer, d):
    """Ajusta un desplazamiento arbitrario para que el grupo del otro equipo quede
    pegado a algún monitor de este sin solaparse (como al soltar en Windows).
    Igual que snap() en ui.html."""
    pb = union(peer)
    pw, ph = pb[2] - pb[0], pb[3] - pb[1]
    px, py = pb[0] + d[0], pb[1] + d[1]
    ub = union(mine)
    th = 0.04 * max(ub[2] - ub[0], ub[3] - ub[1], 1)
    best = None
    for m in mine:
        ov = max(40, 0.1 * min(m[2] - m[0], m[3] - m[1], pw, ph))

        def align(v, lo, hi, size, a0, a1):
            v = min(max(v, lo), hi)
            for target in (a0, a1 - size, (a0 + a1) / 2 - size / 2):
                if abs(v - target) < th:
                    return target
            return v
        y_side = align(py, m[1] - ph + ov, m[3] - ov, ph, m[1], m[3])
        x_side = align(px, m[0] - pw + ov, m[2] - ov, pw, m[0], m[2])
        for cx, cy in ((m[2], y_side), (m[0] - pw, y_side), (x_side, m[3]), (x_side, m[1] - ph)):
            cand = (cx, cy, cx + pw, cy + ph)
            if any(_overlaps(cand, o) for o in mine):
                continue
            dist = (cx - px) ** 2 + (cy - py) ** 2
            if best is None or dist < best[0]:
                best = (dist, cx, cy)
    if best is None:
        return d
    return (round(best[1] - pb[0]), round(best[2] - pb[1]))
