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
import csv
import heapq
import math
import os
import struct

from dolphin import gui, event, memory

from ww.collision_geo import read_collision


class DolphinReader:
    """read_bytes(gc_addr, n) over Dolphin's in-process emulated memory (big-endian GC RAM)."""
    def read_bytes(self, addr, n):
        return bytes(memory.read_bytes(addr, n))


RD = DolphinReader()
LINK_X = 0x803D78FC   # three consecutive f32: X, Y, Z
FACING = 0x803EA3D2   # u16 heading (0x10000 = 360deg). 0=north(-Z), 16384=east(+X), 49152=west(-X)
# Player-cone dimensions (world units): apex(nose) forward, base back, base radius, lift, segments.
CONE_NOSE, CONE_BACK, CONE_RADIUS, CONE_LIFT, CONE_SEGS = 70.0, 30.0, 31.0, 28.0, 16
# HARD per-frame triangle cap — the canvas builds ONE ImGui draw list with 16-bit indices (65535
# vertex ceiling); filled + wireframe over a whole room can overrun it and CRASH the core. We draw
# only the nearest N tris (see draw()); wireframe (line-quads) costs far more verts, so its cap is
# lower. Keep well under the ceiling.
MAX_DRAW_WIRE = 1100
MAX_DRAW_FILL = 3200

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
C_MOVE      = 0x99C46CFF   # movable-BG fill (purple) — distinguishes dynamic objects from the room
C_MOVE_EDGE = 0xFFD08CFF   # movable-BG edge (bright purple), always drawn so small objects pop
C_FLOOR_HL  = 0xFFFFE23C   # triangle Link stands on
C_LINK      = 0xFF33E6FF   # Link marker — bright cyan (pops over the yellow floor tri)
C_LINK_HALO = 0xFF0A1220   # dark edge/outline on the player prism for contrast
C_TXT       = 0xFFDDDDDD

_FILL = {"ground": C_GROUND, "wall": C_WALL, "roof": C_ROOF}
_SHOW = {"ground": cb_ground, "wall": cb_wall, "roof": cb_roof}

# --- seam-clip overlay --------------------------------------------------------------------------
# Precomputed clippable seams for the current room (CSV built by
# tww_sim/harness/collision/export_seam_csv.py -> ww/data/seam_clips/<stage>/Room<N>__room.csv).
# When "Show Seam Clips (N)" is on, each seam is a clickable dot in the 3D view; clicking one reveals
# "Initial Position" / "Clip Position" buttons that teleport Link (clean-placement vs raw debug-xyz).
ROOM_NO_ADDR = 0x803E9F48       # u8 current room number
PLAYER_PTR   = 0x803AD860       # [ptr] -> daPy_lk_c; the two cXyz pos triples are at +0x10c and +0x120
POS_OFFS     = (0x10C, 0x120)
ROLL_STAB_MAX = 49.2202
_SEAM_DIR = os.path.join(os.path.dirname(__file__), "ww", "data", "seam_clips")

C_CLIP_RS    = 0xFF4CE07A       # roll-stab-reachable seam dot (green)
C_CLIP_PUSH  = 0xFFFFA83C       # needs-push seam dot (amber)
C_CLIP_SEL   = 0xFFFFFFFF       # selection ring
C_HUD_BG     = 0xE01B1E26       # translucent HUD panel background
C_HUD_BORDER = 0xFF5A6072
C_BTN        = 0xFF2A6480       # button fill
C_CHK_ON     = 0xFF4CE0FF       # checkbox tick

_seam = {"stage": None, "room": None, "clips": [], "on": False, "sel": None,
         "dots": [], "cb_rect": None, "btn": {}}


def _room_no():
    try:
        return memory.read_bytes(ROOM_NO_ADDR, 1)[0]
    except Exception:
        return -1


def _load_clips(stage, room):
    """Load the current room's clippable seams. Falls back to the stage's only room CSV when the
    exact Room<N> file is absent (single-room stages / boss arenas whose room id differs)."""
    sdir = os.path.join(_SEAM_DIR, stage)
    if not os.path.isdir(sdir):
        return []
    path = os.path.join(sdir, "Room%d__room.csv" % room)
    if not os.path.exists(path):
        rooms = [f for f in os.listdir(sdir) if f.endswith("__room.csv")]
        if len(rooms) == 1:
            path = os.path.join(sdir, rooms[0])
        else:
            return []
    out = []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                old = (float(row["init_x"]), float(row["init_y"]), float(row["init_z"]))
                new = (float(row["dest_x"]), float(row["dest_y"]), float(row["dest_z"]))
                disp = math.sqrt(sum((a - b) ** 2 for a, b in zip(old, new)))
                out.append({"S": (float(row["seam_x"]), float(row["seam_y"]), float(row["seam_z"])),
                            "old": old, "new": new, "ang": float(row["angle_deg"]),
                            "rollstab": disp <= ROLL_STAB_MAX})
    except Exception:
        return []
    return out


def _write_pos(addr, p):
    memory.write_f32(addr, p[0]); memory.write_f32(addr + 4, p[1]); memory.write_f32(addr + 8, p[2])


def _tp_initial(old):
    """Clean placement (the seam-clip validation method): write BOTH cXyz pos triples AND the debug
    globals, so the frame's swept CrrPos is zero-length and Link holds at the standable initial spot
    (a debug-only write can snap him ~100u off across a wall). See DOLPHIN_CONTROL.md."""
    base = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    for off in POS_OFFS:
        _write_pos(base + off, old)
    _write_pos(LINK_X, old)


def _tp_clip(new):
    """Raw debug-xyz teleport to the clip destination (just the link_x/y/z globals)."""
    _write_pos(LINK_X, new)


def _in(rect, x, y):
    return rect is not None and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _seam_click(pt):
    """Consume a canvas click against the seam HUD / dots. Returns True if handled (so it does not
    also start a camera drag). Checkbox toggles; buttons teleport; a dot selects the nearest seam."""
    x, y = pt
    if _in(_seam["cb_rect"], x, y):
        _seam["on"] = not _seam["on"]
        if not _seam["on"]:
            _seam["sel"] = None
        return True
    if _seam["on"] and _seam["sel"] is not None:
        clip = _seam["clips"][_seam["sel"]]
        if _in(_seam["btn"].get("init"), x, y):
            _tp_initial(clip["old"]); return True
        if _in(_seam["btn"].get("clip"), x, y):
            _tp_clip(clip["new"]); return True
    if _seam["on"]:
        best, bd = None, 13 * 13                          # pick the nearest dot within ~13 px
        for i, sx, sy in _seam["dots"]:
            d = (sx - x) ** 2 + (sy - y) ** 2
            if d < bd:
                bd, best = d, i
        if best is not None:
            _seam["sel"] = best
            return True
    return False


def _border(p0, p1, col):
    x0, y0 = p0; x1, y1 = p1
    cv.line((x0, y0), (x1, y0), col, thickness=1); cv.line((x1, y0), (x1, y1), col, thickness=1)
    cv.line((x1, y1), (x0, y1), col, thickness=1); cv.line((x0, y1), (x0, y0), col, thickness=1)


def _button(x, y, label):
    # canvas text() takes (left, BASELINE); center the label in both axes (baseline sits ~6px above
    # the bottom edge so the glyphs land in the vertical middle, not floating over the top).
    tw = len(label) * 7.5
    w, h = int(tw) + 18, 22
    cv.rect_filled((x, y), (x + w, y + h), C_BTN)
    _border((x, y), (x + w, y + h), C_HUD_BORDER)
    cv.text((x + (w - tw) / 2.0, y + h - 6.0), 0xFFFFFFFF, label)
    return (x, y, x + w, y + h)


def _draw_seam_ui(oc):
    """Draw clickable seam dots (when enabled) + the HUD row (checkbox, and, once a seam is selected,
    the two teleport buttons to its right). Hit-rects are stashed in ``_seam`` for :func:`_seam_click`."""
    clips = _seam["clips"]
    _seam["dots"] = []
    if _seam["on"]:
        for i, c in enumerate(clips):
            sp = oc.screen(oc.cam(c["S"]))
            if not sp or not (0 <= sp[0] <= W and 0 <= sp[1] <= H):
                continue
            _seam["dots"].append((i, sp[0], sp[1]))
            col = C_CLIP_RS if c["rollstab"] else C_CLIP_PUSH
            sel = (i == _seam["sel"])
            r = 6 if sel else 4
            cv.circle_filled(sp, r + 2, C_LINK_HALO)      # dark halo so dots pop over any surface
            if sel:
                cv.circle_filled(sp, r + 2, C_CLIP_SEL)   # white selection ring
            cv.circle_filled(sp, r, col)

    # HUD row: a checkbox with the live clip count, plus teleport buttons when a seam is selected.
    n = len(clips)
    bx, by, bs = 12, 40, 15
    label = "Show Seam Clips (%d)" % n
    lw = 8 + bs + 6 + 7 * len(label)
    cv.rect_filled((bx - 6, by - 6), (bx - 6 + lw + 8, by + bs + 8), C_HUD_BG)
    cv.rect_filled((bx, by), (bx + bs, by + bs), 0xFF20242E)
    _border((bx, by), (bx + bs, by + bs), C_HUD_BORDER)
    if _seam["on"]:
        cv.rect_filled((bx + 3, by + 3), (bx + bs - 3, by + bs - 3), C_CHK_ON)
    cv.text((bx + bs + 6, by + 2), C_TXT, label)
    _seam["cb_rect"] = (bx - 6, by - 6, bx - 6 + lw + 8, by + bs + 8)

    _seam["btn"] = {}
    if _seam["on"] and _seam["sel"] is not None and 0 <= _seam["sel"] < n:
        rx = bx - 6 + lw + 8 + 12
        _seam["btn"]["init"] = _button(rx, by - 3, "Initial Position")
        _seam["btn"]["clip"] = _button(_seam["btn"]["init"][2] + 8, by - 3, "Clip Position")
        c = clips[_seam["sel"]]
        cv.text((bx, by + bs + 10), C_TXT,
                "seam (%.0f, %.0f, %.0f)   angle %.1f   %s"
                % (c["S"][0], c["S"][1], c["S"][2], c["ang"],
                   "roll-stab reachable" if c["rollstab"] else "needs push"))


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
    if click is not None and _seam_click(click):
        click = None                                     # consumed by the seam HUD / a dot
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
    return struct.unpack(">3f", RD.read_bytes(LINK_X, 12))


def _link_facing_dir():
    """World-space forward unit vector Link's model points along, from the u16 heading.
    fwd = (sin th, 0, cos th) — measured live against travel direction: raw 16384 (east) → +X,
    49152 (west) → -X, 0 (north) → +Z (dot=+1.000 both E/W walks)."""
    raw = struct.unpack(">H", RD.read_bytes(FACING, 2))[0]
    th = raw * (2.0 * math.pi / 65536.0)
    return (math.sin(th), 0.0, math.cos(th))


_cache = [None]        # last collision snapshot (feeds cache= to reuse static room tables)
_last = [None]         # last (snap, link) for redraw
_diag = [False]


def _collect_tris(snap, link, radius, cap):
    """Gather the triangles to draw, using each mesh's CACHED per-tri centroid + class (no per-frame
    cross-product/sqrt or classify). Movable-BG tris are always kept (few, dynamic); static-room
    tris are pre-selected to the `cap` NEAREST LINK *before* projection, so we never project the
    whole room. Selection is purely LINK-relative (no camera term) — the drawn set is invariant to
    orbit/pan and changes only when Link moves. Returns (tris, total_shown) with
    tris = [(v0,v1,v2,cen,cls,is_floor,is_move), ...] (already bounded to <= cap)."""
    floor = snap.get("floor")
    r2 = radius * radius
    lx, ly, lz = link
    movebg = []
    static = []                    # (d2link, v0, v1, v2, cen, cls, is_floor)
    for bg, m in snap["meshes"].items():
        is_move = m["is_movebg"]
        if is_move and not cb_movebg.checked:
            continue
        verts = m["verts"]; cents = m["centroids"]; clss = m["classes"]; tris = m["tris"]
        floor_poly = floor[1] if (floor and floor[0] == bg) else -1
        for pi in range(len(tris)):
            cls = clss[pi]
            is_floor = (pi == floor_poly)
            if not _SHOW[cls].checked and not is_floor:
                continue
            a, b, c = tris[pi][0], tris[pi][1], tris[pi][2]
            cen = cents[pi]
            if is_move:
                movebg.append((verts[a], verts[b], verts[c], cen, cls, is_floor, True))
                continue
            # distance to LINK (not the camera target) — both the radius filter and the cap rank
            # by this, so panning/orbiting the camera never changes which static tris are eligible.
            dx = cen[0]-lx; dy = cen[1]-ly; dz = cen[2]-lz
            d2 = dx*dx + dy*dy + dz*dz
            if radius > 0.0 and d2 > r2:
                continue
            static.append((d2, verts[a], verts[b], verts[c], cen, cls, is_floor))

    total_shown = len(movebg) + len(static)
    keep_static = max(0, cap - len(movebg))
    if len(static) > keep_static:
        # Nearest Link only. The floor tri Link stands on is ~0 distance away, so it's always in
        # this set (no separate rescue needed).
        static = heapq.nsmallest(keep_static, static, key=lambda t: t[0])
    out = movebg + [(t[1], t[2], t[3], t[4], t[5], t[6], False) for t in static]
    return out, total_shown


_CYAN = (0x33, 0xE6, 0xFF)   # base color the prism faces are shaded from
_LIGHT = (0.30, 0.90, 0.45)  # directional light for face shading (normalized below)


def _shade(k):
    """Flat-shade the base cyan by factor k (0..1) → opaque ARGB."""
    r = min(255, int(_CYAN[0] * k)); g = min(255, int(_CYAN[1] * k)); b = min(255, int(_CYAN[2] * k))
    return 0xFF000000 | (r << 16) | (g << 8) | b


def _face_normal(a, b, c):
    ax, ay, az = b[0]-a[0], b[1]-a[1], b[2]-a[2]
    bx, by, bz = c[0]-a[0], c[1]-a[1], c[2]-a[2]
    nx, ny, nz = ay*bz-az*by, az*bx-ax*bz, ax*by-ay*bx
    m = math.sqrt(nx*nx+ny*ny+nz*nz) or 1.0
    return (nx/m, ny/m, nz/m)


def _draw_player_cone(oc, link, fwd):
    """Draw Link as a shaded 3D cone whose APEX (point) faces the heading `fwd`. Facets are
    painter-sorted among themselves and drawn opaque on top of the scene."""
    # Orthonormal frame around fwd: right = fwd x worldUp, upn = right x fwd.
    wu = (0.0, 1.0, 0.0)
    rx, ry, rz = fwd[1]*wu[2]-fwd[2]*wu[1], fwd[2]*wu[0]-fwd[0]*wu[2], fwd[0]*wu[1]-fwd[1]*wu[0]
    rm = math.sqrt(rx*rx+ry*ry+rz*rz) or 1.0
    right = (rx/rm, ry/rm, rz/rm)
    ux, uy, uz = right[1]*fwd[2]-right[2]*fwd[1], right[2]*fwd[0]-right[0]*fwd[2], right[0]*fwd[1]-right[1]*fwd[0]
    um = math.sqrt(ux*ux+uy*uy+uz*uz) or 1.0
    upn = (ux/um, uy/um, uz/um)

    c = (link[0], link[1] + CONE_LIFT, link[2])
    apex = (c[0]+fwd[0]*CONE_NOSE, c[1]+fwd[1]*CONE_NOSE, c[2]+fwd[2]*CONE_NOSE)
    bc = (c[0]-fwd[0]*CONE_BACK, c[1]-fwd[1]*CONE_BACK, c[2]-fwd[2]*CONE_BACK)
    ring = []
    for i in range(CONE_SEGS):
        a = 2.0 * math.pi * i / CONE_SEGS
        ca, sa = math.cos(a), math.sin(a)
        ring.append((bc[0] + (right[0]*ca + upn[0]*sa)*CONE_RADIUS,
                     bc[1] + (right[1]*ca + upn[1]*sa)*CONE_RADIUS,
                     bc[2] + (right[2]*ca + upn[2]*sa)*CONE_RADIUS))
    faces = []
    for i in range(CONE_SEGS):
        j = (i + 1) % CONE_SEGS
        faces.append((apex, ring[i], ring[j]))   # side facet
        faces.append((bc, ring[j], ring[i]))     # base cap facet

    lm = math.sqrt(sum(x*x for x in _LIGHT)) or 1.0
    ld = tuple(x/lm for x in _LIGHT)
    items = []
    for a, b, d in faces:
        n = _face_normal(a, b, d)
        sh = 0.5 + 0.5 * abs(n[0]*ld[0] + n[1]*ld[1] + n[2]*ld[2])   # winding-independent
        ca, cb, cd = oc.cam(a), oc.cam(b), oc.cam(d)
        if ca[2] <= oc.NEAR or cb[2] <= oc.NEAR or cd[2] <= oc.NEAR:
            continue
        sa, sb, sd = oc.screen(ca), oc.screen(cb), oc.screen(cd)
        if not (sa and sb and sd):
            continue
        items.append(((ca[2]+cb[2]+cd[2])/3.0, sa, sb, sd, sh))
    items.sort(key=lambda t: t[0], reverse=True)
    for _, sa, sb, sd, sh in items:
        cv.triangle_filled(sa, sb, sd, _shade(sh))


def _clip_near_project(oc, cverts):
    """Clip a camera-space triangle to the half-space z >= NEAR (Sutherland-Hodgman) and project the
    result to screen. Returns (screen_pts, avg_depth) — a 3- or 4-point polygon — or (None, 0).
    This keeps LARGE triangles that straddle the camera plane (e.g. the tall Hyrule barrier walls),
    which an all-or-nothing 'skip if any vertex behind near' test would drop entirely."""
    near = oc.NEAR
    out = []
    n = len(cverts)
    for i in range(n):
        a = cverts[i]; b = cverts[(i + 1) % n]
        ain = a[2] >= near
        if ain:
            out.append(a)
        if ain != (b[2] >= near):                       # edge crosses the near plane → add cut point
            t = (near - a[2]) / (b[2] - a[2])
            out.append((a[0] + (b[0]-a[0])*t, a[1] + (b[1]-a[1])*t, near))
    if len(out) < 3:
        return None, 0.0
    foc = oc.focal
    spts = [(W*0.5 + foc*c[0]/c[2], H*0.5 - foc*c[1]/c[2]) for c in out]
    return spts, sum(c[2] for c in out) / len(out)


def draw(payload):
    cv.clear()
    cv.rect_filled((0, 0), (W, H), C_BG)
    if payload is None:
        cv.text((16, 24), C_TXT, "waiting for game / stage collision...")
        cv.commit()
        return
    snap, link = payload[0], payload[1]
    base = link if cb_follow.checked else (0.0, 0.0, 0.0)
    target = (base[0]+_pan[0], base[1]+_pan[1], base[2]+_pan[2])
    oc = OrbitCam(target, _view["az"], _view["el"], _dist())
    radius = sld_radius.value

    # Collect already caps to the nearest-Link `cap` (movable BG always kept) using cached centroids
    # — so only ~cap triangles are ever projected, and the ImGui draw list can't overflow.
    cap = MAX_DRAW_WIRE if cb_wire.checked else MAX_DRAW_FILL
    tris, total_shown = _collect_tris(snap, link, radius, cap)

    # Project to screen with near-plane clipping (large tris straddling the camera become 3-4 pt
    # polygons instead of being dropped). Keep avg depth for the painter sort.
    drawable = []
    for v0, v1, v2, cen, cls, is_floor, is_move in tris:
        spoly, depth = _clip_near_project(oc, (oc.cam(v0), oc.cam(v1), oc.cam(v2)))
        if spoly is None:
            continue
        drawable.append((depth, spoly, cls, is_floor, is_move))

    drawable.sort(key=lambda t: t[0], reverse=True)   # far first (painter's order)
    clipped = total_shown - len(tris)

    n = {"ground": 0, "wall": 0, "roof": 0}
    n_move = 0
    for depth, spoly, cls, is_floor, is_move in drawable:
        n[cls] += 1
        if is_move:
            n_move += 1
        if cb_filled.checked:
            col = C_FLOOR_HL if is_floor else (C_MOVE if is_move else _FILL[cls])
            for i in range(1, len(spoly) - 1):          # fan-triangulate the (clipped) polygon
                cv.triangle_filled(spoly[0], spoly[i], spoly[i + 1], col)
        # Always outline movable-BG + the floor tri (they're small / important); room edges follow
        # the Wireframe toggle.
        if cb_wire.checked or is_floor or is_move:
            ec = C_FLOOR_HL if is_floor else (C_MOVE_EDGE if is_move else C_EDGE)
            th = 2 if (is_floor or is_move) else 1
            m = len(spoly)
            for i in range(m):
                cv.line(spoly[i], spoly[(i + 1) % m], ec, thickness=th)

    # Link as a directional cone (apex = facing); fall back to a haloed dot if facing is unavailable.
    fwd = payload[2]
    if fwd is not None:
        _draw_player_cone(oc, link, fwd)
    else:
        ls = oc.screen(oc.cam(link))
        if ls:
            cv.circle_filled(ls, 9, C_LINK_HALO)
            cv.circle_filled(ls, 6, C_LINK)

    _draw_seam_ui(oc)

    floor = snap.get("floor")
    ftxt = f"floor tri {floor[1]} (slot {floor[0]})" if floor else "airborne / no floor"
    cliptxt = f"  CLIPPED {clipped} (lower Draw radius / zoom in to see all)" if clipped else ""
    cv.text((10, 16), C_TXT,
            f"stage {snap['stage']}  meshes={len(snap['meshes'])}  drawn {len(drawable)}/{total_shown}vis"
            f"  [G {n['ground']}  W {n['wall']}  R {n['roof']}  MoveBG {n_move}]  {ftxt}{cliptxt}")
    mode = _drag["mode"]
    grab = f"   [{mode.upper()} — click to release]" if mode else ""
    cv.text((10, H-16), C_TXT,
            "L-drag: orbit   R-drag: pan   wheel: zoom   (green=ground red=wall blue=roof "
            "purple=movable BG, yellow=Link's floor, cyan cone=Link)" + grab)
    cv.commit()


@event.on_frameadvance
def on_frame():
    try:
        snap = read_collision(RD, cache=_cache[0])
        _cache[0] = snap
        stg, rm = snap["stage"], _room_no()
        if (stg, rm) != (_seam["stage"], _seam["room"]):
            _seam["stage"], _seam["room"] = stg, rm
            _seam["clips"] = _load_clips(stg, rm)
            _seam["sel"] = None
        link = _link_pos()
        try:
            fwd = _link_facing_dir()
        except Exception:
            fwd = None
        _last[0] = (snap, link, fwd)
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
