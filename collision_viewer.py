"""collision_viewer.py — LIVE 3D STAGE-COLLISION viewer, run INSIDE Dolphin.

Toggle this on from Dolphin's Scripts panel while TWW (JP/GZLJ01) is running. It opens a floating
window with a 3D view of the current stage's collision TRIANGLES — the actual walkable surfaces the
game tests against — colored by surface class (ground / wall / roof), with the triangle Link is
standing on highlighted. It re-reads the live game every frame (on_frameadvance) and re-renders, so
movable-BG collision follows the game and the view updates as you play.

The mesh comes from OUR reader of TWW's runtime background-collision manager `dBgS`
(ww.collision_geo.read_collision), which walks the 256-slot registry and pulls each registered
cBgW's WORLD-space vertices (+0x90) + triangle table. Validated live on stage H_test: the floor
triangle it reports matches dBgS_LinkAcch's current ground poly. See knowledge/mechanics/collision.md.

Sibling to cull_viewer.py — reuses the same OrbitCam / mouse-orbit-pan-zoom scaffold. Since the
canvas has no depth buffer, filled triangles are drawn back-to-front (painter's algorithm) with
translucent fills; a distance filter around the orbit target keeps the drawn triangle count (and
frame time) bounded on large rooms.

Controls (mouse, over the canvas):
  Left-drag = ORBIT, right-drag = PAN (grab-toggle: click to grab, move, click to release). The
  "Pan mode" checkbox makes LEFT-drag pan too. Mouse wheel zooms. "Reset view" recenters.
  Checkboxes toggle ground/wall/roof, filled vs wireframe, Follow-Link. The Radius slider limits
  how far from the orbit target triangles are drawn (0 = no limit).

STABILITY: all work (memory read, input, draw) happens on the SINGLE on_frameadvance thread — no
on_hostupdate (a second host-thread tick that crashed the core in this build). The view updates
while the game is RUNNING; a full pause freezes it until the next frame advances.
"""
import math

from dolphin import gui, event, memory

from ww.collision_geo import read_collision, tri_normal, classify


class DolphinReader:
    """read_bytes(gc_addr, n) over Dolphin's in-process emulated memory (big-endian GC RAM)."""
    def read_bytes(self, addr, n):
        return bytes(memory.read_bytes(addr, n))


RD = DolphinReader()
LINK_X = 0x803D78FC   # three consecutive f32: X, Y, Z

# --- window / canvas / controls ------------------------------------------------------------
W, H = 860, 560
panel = gui.window("TWW Collision Viewer")
cv = panel.canvas(W, H)
cb_panmode = panel.checkbox("Pan mode (drag pans)", False)
cb_follow = panel.checkbox("Follow Link", True)
cb_ground = panel.checkbox("Ground", True)
cb_wall = panel.checkbox("Walls", True)
cb_roof = panel.checkbox("Roofs", False)
cb_filled = panel.checkbox("Filled (painter)", True)
cb_wire = panel.checkbox("Wireframe", True)
cb_movebg = panel.checkbox("Movable BG", True)
sld_radius = panel.slider_float("Draw radius", 0.0, 12000.0)
btn_reset = panel.button("Reset view")
status = panel.text("")

# view state, driven by mouse (all polled in on_frameadvance):
_VIEW0 = {"az": 40.0, "el": 30.0, "zoom": 0.0}
_view = dict(_VIEW0)
_pan = [0.0, 0.0, 0.0]
_drag = {"mode": None, "ax": 0.0, "ay": 0.0, "az": 0.0, "el": 0.0, "pan": (0.0, 0.0, 0.0)}
_BASE_DIST = 2600.0
_DRAG_SENS = 0.35
_VFOV = 50.0
_FOCAL = (H * 0.5) / math.tan(math.radians(_VFOV) * 0.5)

# colors (ARGB) — translucent fills so painter-sorted overlaps read as depth
C_BG        = 0xFF12141A
C_GROUND    = 0x9955DD66
C_WALL      = 0x99FF6C55
C_ROOF      = 0x9945A6FF
C_EDGE      = 0x33FFFFFF
C_FLOOR_HL  = 0xFFFFE23C   # triangle Link stands on
C_LINK      = 0xFFFFFFFF
C_TXT       = 0xFFDDDDDD

_FILL = {"ground": C_GROUND, "wall": C_WALL, "roof": C_ROOF}
_SHOW = {"ground": cb_ground, "wall": cb_wall, "roof": cb_roof}


def _sub(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _dot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _norm(v):
    m = math.sqrt(_dot(v, v)) or 1.0
    return (v[0]/m, v[1]/m, v[2]/m)


def _orbit_basis(az, el):
    el = max(-89.0, min(89.0, el))
    ar, er = math.radians(az), math.radians(el)
    d = (math.cos(er)*math.sin(ar), math.sin(er), math.cos(er)*math.cos(ar))
    fwd = (-d[0], -d[1], -d[2])
    right = _norm(_cross(fwd, (0.0, 1.0, 0.0)))
    up = _cross(right, fwd)
    return right, up, fwd


class OrbitCam:
    NEAR = 5.0
    def __init__(self, target, az, el, dist):
        self.right, self.up, self.fwd = _orbit_basis(az, el)
        self.pos = (target[0]-self.fwd[0]*dist, target[1]-self.fwd[1]*dist, target[2]-self.fwd[2]*dist)
        self.focal = _FOCAL

    def cam(self, w):
        rel = _sub(w, self.pos)
        return (_dot(rel, self.right), _dot(rel, self.up), _dot(rel, self.fwd))

    def screen(self, c):
        if c[2] <= self.NEAR:
            return None
        return (W*0.5 + self.focal * c[0]/c[2], H*0.5 - self.focal * c[1]/c[2])


def _dist():
    return max(150.0, min(90000.0, _BASE_DIST * (0.85 ** _view["zoom"])))


def _apply_input():
    """Mouse orbit/pan/zoom on the on_frameadvance thread (mirrors cull_viewer)."""
    if btn_reset.clicked:
        _view.update(_VIEW0); _pan[:] = [0.0, 0.0, 0.0]; _drag["mode"] = None
    w = cv.take_wheel()
    if w:
        _view["zoom"] += w
    mx, my, inside = cv.mouse_pos()
    click = cv.take_click()
    rclick = cv.take_right_click()

    def _start(mode, at):
        _drag["mode"] = mode
        _drag.update(ax=at[0], ay=at[1], az=_view["az"], el=_view["el"], pan=tuple(_pan))

    if click is not None:
        want = "pan" if cb_panmode.checked else "orbit"
        _start(None, click) if _drag["mode"] == want else _start(want, click)
    if rclick is not None:
        _start(None, rclick) if _drag["mode"] == "pan" else _start("pan", rclick)
    if _drag["mode"] == "orbit":
        _view["az"] = _drag["az"] - (mx - _drag["ax"]) * _DRAG_SENS
        _view["el"] = max(-85.0, min(85.0, _drag["el"] + (my - _drag["ay"]) * _DRAG_SENS))
    elif _drag["mode"] == "pan":
        right, up, _ = _orbit_basis(_view["az"], _view["el"])
        wpp = _dist() / _FOCAL
        dx = (mx - _drag["ax"]) * wpp
        dy = (my - _drag["ay"]) * wpp
        p0 = _drag["pan"]
        _pan[0] = p0[0] - dx * right[0] + dy * up[0]
        _pan[1] = p0[1] - dx * right[1] + dy * up[1]
        _pan[2] = p0[2] - dx * right[2] + dy * up[2]


def _link_pos():
    b = RD.read_bytes(LINK_X, 12)
    import struct
    return struct.unpack(">3f", b)


_cache = [None]        # last collision snapshot (feeds cache= to reuse static room tables)
_last = [None]         # last (snap, link) for redraw
_diag = [False]


def _collect_tris(snap, target, radius):
    """Flatten meshes → list of (depth_key, screen_pts, fill_color) ready to paint, plus the
    highlighted floor triangle. Applies the radius filter + near-plane skip. depth_key is set by
    caller's camera; here we just gather world tris + colors."""
    out = []
    floor = snap.get("floor")
    r2 = radius * radius
    for bg, m in snap["meshes"].items():
        if m["is_movebg"] and not cb_movebg.checked:
            continue
        verts = m["verts"]
        floor_poly = floor[1] if (floor and floor[0] == bg) else -1
        for pi, (a, b, c, tid, grp) in enumerate(m["tris"]):
            v0, v1, v2 = verts[a], verts[b], verts[c]
            cx = (v0[0]+v1[0]+v2[0])/3.0
            cy = (v0[1]+v1[1]+v2[1])/3.0
            cz = (v0[2]+v1[2]+v2[2])/3.0
            if radius > 0.0:
                dx, dy, dz = cx-target[0], cy-target[1], cz-target[2]
                if dx*dx+dy*dy+dz*dz > r2:
                    continue
            cls = classify(tri_normal(v0, v1, v2)[1])
            if not _SHOW[cls].checked and not (pi == floor_poly):
                continue
            out.append((v0, v1, v2, (cx, cy, cz), cls, pi == floor_poly))
    return out


def draw(payload):
    cv.clear()
    cv.rect_filled((0, 0), (W, H), C_BG)
    if payload is None:
        cv.text((16, 24), C_TXT, "waiting for game / stage collision...")
        cv.commit()
        return
    snap, link = payload
    base = link if cb_follow.checked else (0.0, 0.0, 0.0)
    target = (base[0]+_pan[0], base[1]+_pan[1], base[2]+_pan[2])
    oc = OrbitCam(target, _view["az"], _view["el"], _dist())
    radius = sld_radius.value

    tris = _collect_tris(snap, target, radius)

    # Project to screen (skip tris crossing the near plane), keep camera depth for painter sort.
    drawable = []
    for v0, v1, v2, cen, cls, is_floor in tris:
        c0, c1, c2 = oc.cam(v0), oc.cam(v1), oc.cam(v2)
        if c0[2] <= oc.NEAR or c1[2] <= oc.NEAR or c2[2] <= oc.NEAR:
            continue
        s0 = oc.screen(c0); s1 = oc.screen(c1); s2 = oc.screen(c2)
        if not (s0 and s1 and s2):
            continue
        depth = (c0[2]+c1[2]+c2[2])/3.0
        drawable.append((depth, s0, s1, s2, cls, is_floor))

    drawable.sort(key=lambda t: t[0], reverse=True)   # far first

    n = {"ground": 0, "wall": 0, "roof": 0}
    for depth, s0, s1, s2, cls, is_floor in drawable:
        n[cls] += 1
        if cb_filled.checked:
            col = C_FLOOR_HL if is_floor else _FILL[cls]
            cv.triangle_filled(s0, s1, s2, col)
        if cb_wire.checked or is_floor:
            ec = C_FLOOR_HL if is_floor else C_EDGE
            th = 2 if is_floor else 1
            cv.line(s0, s1, ec, thickness=th)
            cv.line(s1, s2, ec, thickness=th)
            cv.line(s2, s0, ec, thickness=th)

    # Link marker
    ls = oc.screen(oc.cam(link))
    if ls:
        cv.circle_filled(ls, 5, C_LINK)

    floor = snap.get("floor")
    ftxt = f"floor tri {floor[1]} (slot {floor[0]})" if floor else "airborne / no floor"
    cv.text((10, 16), C_TXT,
            f"stage {snap['stage']}  meshes={len(snap['meshes'])}  drawn {len(drawable)}/{len(tris)}"
            f"  [G {n['ground']}  W {n['wall']}  R {n['roof']}]  {ftxt}")
    mode = _drag["mode"]
    grab = f"   [{mode.upper()} — click to release]" if mode else ""
    cv.text((10, H-16), C_TXT,
            "L-drag: orbit   R-drag: pan   wheel: zoom   (green=ground red=wall blue=roof "
            "yellow=Link's floor, white=Link)" + grab)
    cv.commit()


@event.on_frameadvance
def on_frame():
    try:
        snap = read_collision(RD, cache=_cache[0])
        _cache[0] = snap
        link = _link_pos()
        _last[0] = (snap, link)
        if not _diag[0]:
            tot = sum(m["t_num"] for m in snap["meshes"].values())
            print(f"[collision_viewer] first live frame OK: stage={snap['stage']} "
                  f"meshes={len(snap['meshes'])} tris={tot} floor={snap['floor']}")
            _diag[0] = True
        status.set(f"stage={snap['stage']} meshes={len(snap['meshes'])} floor={snap['floor']}")
    except Exception as e:
        status.set(f"read error: {e}")
        if not _diag[0]:
            import traceback as _tb; print("[collision_viewer] read error:\n" + _tb.format_exc()); _diag[0] = True
    try:
        _apply_input()
    except Exception:
        pass
    draw(_last[0])


draw(None)
