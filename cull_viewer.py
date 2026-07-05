"""cull_viewer.py — LIVE 3D view-frustum / culling viewer, run INSIDE Dolphin.

Toggle this on from Dolphin's Scripts panel while TWW (JP/GZLJ01) is running. It opens a floating
window with a 3D view of the game's view-frustum and every frustum-culled actor's cull box, colored
by whether it is CULLED or VISIBLE this frame. It re-reads the live game every frame
(on_frameadvance) and re-renders, so the scene updates as you play.

The cull verdict is OUR faithful port of J3DUClipper (tww_sim.core.camera.frustum.clip_box) run on
the live view matrix * cullMtx and cull box; validated to match the game's own fopAcCnd_NODRAW_e
(60/60 box actors, 0 mismatches). Any disagreement is drawn magenta.

Self-contained: the culling scanner + J3DUClipper port live in ww/cull.py (vendored from tww_sim),
so cloning tww-python-scripts alone is enough to run this — no sibling repo needed. Memory reads go
through that scanner, fed here by an in-Dolphin reader over `dolphin.memory`.

Controls (mouse, over the canvas):
  Left-drag = ORBIT, right-drag = PAN (grab-toggle: click to grab, move, click to release). The
  "Pan mode" checkbox makes LEFT-drag pan too (a right-click-free fallback). Mouse wheel zooms.
  "Reset view" recenters. Checkboxes toggle culled/visible boxes, the frustum, labels, Follow-Link.

IMPORTANT (stability): all work — memory read, input, draw — happens on the SINGLE on_frameadvance
thread. We deliberately do NOT use on_hostupdate (a second, host-thread tick that runs while paused);
doing input/drawing there crashed the core in this build (that, not the mouse calls, was the cause).
Consequence: the mouse controls and redraw work while the game is RUNNING; a full pause freezes the
view until the next frame advances. That is the accepted trade for stability.
"""
import math

from dolphin import gui, event, memory

from ww.cull import full_snapshot   # self-contained cull scanner (vendored port; see ww/cull.py)


class DolphinReader:
    """read_bytes(gc_addr, n) over Dolphin's in-process emulated memory (big-endian GC RAM)."""
    def read_bytes(self, addr, n):
        return bytes(memory.read_bytes(addr, n))


RD = DolphinReader()

# --- window / canvas / controls ------------------------------------------------------------
W, H = 820, 520
panel = gui.window("TWW Cull Viewer")
cv = panel.canvas(W, H)
cb_panmode = panel.checkbox("Pan mode (drag pans)", False)
cb_follow = panel.checkbox("Follow Link", True)
cb_culled = panel.checkbox("Show culled", True)
cb_visible = panel.checkbox("Show visible", True)
cb_frustum = panel.checkbox("Show frustum", True)
cb_labels = panel.checkbox("Labels", False)
btn_reset = panel.button("Reset view")
status = panel.text("")

# view state, driven by mouse (all polled in on_frameadvance):
#   left-click grab = orbit (or pan while "Pan mode" is checked)   wheel = zoom
_VIEW0 = {"az": 40.0, "el": 24.0, "zoom": 0.0}
_view = dict(_VIEW0)
_pan = [0.0, 0.0, 0.0]        # world-space offset added to the look target (composes with Follow)
_drag = {"mode": None, "ax": 0.0, "ay": 0.0, "az": 0.0, "el": 0.0, "pan": (0.0, 0.0, 0.0)}
_BASE_DIST = 2200.0          # zoom multiplies this
_DRAG_SENS = 0.35            # orbit degrees per pixel
_VFOV = 50.0
_FOCAL = (H * 0.5) / math.tan(math.radians(_VFOV) * 0.5)

# colors (ARGB)
C_BG      = 0xFF14161C
C_FRUSTN  = 0xFF39C6FF   # frustum near face + sides
C_FRUSTF  = 0x6639C6FF   # frustum far face (dim)
C_VIS     = 0xFF37DD6A   # visible actor
C_CULL    = 0x99FF5A5A   # culled actor (dim red)
C_MISM    = 0xFFFF00FF   # verdict disagrees with the game (should never appear)
C_EYE     = 0xFFFFC83C   # game camera eye
C_LINK    = 0xFFFFFFFF
C_TXT     = 0xFFDDDDDD


def _sub(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _dot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _norm(v):
    m = math.sqrt(_dot(v, v)) or 1.0
    return (v[0]/m, v[1]/m, v[2]/m)


def _orbit_basis(az, el):
    """Camera basis (right, up, fwd) for an orbit at azimuth/elevation — target-independent."""
    el = max(-89.0, min(89.0, el))
    ar, er = math.radians(az), math.radians(el)
    d = (math.cos(er)*math.sin(ar), math.sin(er), math.cos(er)*math.cos(ar))
    fwd = (-d[0], -d[1], -d[2])                          # look direction (target - pos), unit
    right = _norm(_cross(fwd, (0.0, 1.0, 0.0)))
    up = _cross(right, fwd)
    return right, up, fwd


class OrbitCam:
    """Turns (target, azimuth, elevation, distance) into a world->screen projector."""
    NEAR = 5.0
    def __init__(self, target, az, el, dist):
        self.right, self.up, self.fwd = _orbit_basis(az, el)
        self.pos = (target[0]-self.fwd[0]*dist, target[1]-self.fwd[1]*dist, target[2]-self.fwd[2]*dist)
        self.focal = _FOCAL

    def cam(self, w):
        rel = _sub(w, self.pos)
        return (_dot(rel, self.right), _dot(rel, self.up), _dot(rel, self.fwd))  # (x,y,depth)

    def screen(self, c):
        if c[2] <= self.NEAR:
            return None
        return (W*0.5 + self.focal * c[0]/c[2], H*0.5 - self.focal * c[1]/c[2])

    def edge(self, w0, w1):
        """Project a world segment, clipped to the near plane. Returns (p0,p1) screen or None."""
        a, b = self.cam(w0), self.cam(w1)
        if a[2] <= self.NEAR and b[2] <= self.NEAR:
            return None
        if a[2] <= self.NEAR or b[2] <= self.NEAR:
            t = (self.NEAR - a[2]) / (b[2] - a[2])
            m = (a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, self.NEAR)
            a, b = (a, m) if a[2] > self.NEAR else (m, b)
        return self.screen(a), self.screen(b)


def _dist():
    return max(150.0, min(60000.0, _BASE_DIST * (0.85 ** _view["zoom"])))


def _apply_input():
    """Mouse orbit/pan/zoom, polled on the on_frameadvance thread (runs only while the game runs).
    Uses only take_click/mouse_pos/take_wheel — NOT take_right_click, and NOT on_hostupdate — both of
    which crashed the core here. Grab-toggle (click to start, move, click to release) because the
    canvas API exposes click events, not button-held state."""
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

    if click is not None:                    # left-click: orbit (or pan if the checkbox is set)
        want = "pan" if cb_panmode.checked else "orbit"
        _start(None, click) if _drag["mode"] == want else _start(want, click)
    if rclick is not None:                   # right-click: pan grab-toggle
        _start(None, rclick) if _drag["mode"] == "pan" else _start("pan", rclick)
    if _drag["mode"] == "orbit":
        _view["az"] = _drag["az"] - (mx - _drag["ax"]) * _DRAG_SENS
        _view["el"] = max(-85.0, min(85.0, _drag["el"] + (my - _drag["ay"]) * _DRAG_SENS))
    elif _drag["mode"] == "pan":
        right, up, _ = _orbit_basis(_view["az"], _view["el"])
        wpp = _dist() / _FOCAL                       # world units per screen pixel at target depth
        dx = (mx - _drag["ax"]) * wpp
        dy = (my - _drag["ay"]) * wpp
        p0 = _drag["pan"]                            # grab-the-scene: target moves opposite the cursor
        _pan[0] = p0[0] - dx * right[0] + dy * up[0]
        _pan[1] = p0[1] - dx * right[1] + dy * up[1]
        _pan[2] = p0[2] - dx * right[2] + dy * up[2]


_BOX_EDGES = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]


def _draw_edges(oc, corners, edges, color, thickness=1):
    for i, j in edges:
        e = oc.edge(corners[i], corners[j])
        if e and e[0] and e[1]:
            cv.line(e[0], e[1], color, thickness=thickness)


def _draw_marker(oc, w, color, r=5):
    s = oc.screen(oc.cam(w))
    if s:
        cv.circle_filled(s, r, color)


def draw(snap):
    cv.clear()
    cv.rect_filled((0, 0), (W, H), C_BG)

    if snap is None:
        cv.text((16, 24), C_TXT, "waiting for game / camera...")
        cv.commit()
        return

    cam, actors = snap["camera"], snap["actors"]
    eye = tuple(cam["eye"])
    link = tuple(snap["link"])
    base = link if cb_follow.checked else eye
    target = (base[0] + _pan[0], base[1] + _pan[1], base[2] + _pan[2])
    oc = OrbitCam(target, _view["az"], _view["el"], _dist())

    if cb_frustum.checked:
        fc = [tuple(c) for c in snap["frustum_corners"]]
        _draw_edges(oc, fc, [(0,1),(1,2),(2,3),(3,0),(0,4),(1,5),(2,6),(3,7)], C_FRUSTN, 1)
        _draw_edges(oc, fc, [(4,5),(5,6),(6,7),(7,4)], C_FRUSTF, 1)

    n_vis = n_cull = n_mism = 0
    for a in actors:
        culled = a["our_culled"]
        if culled is None or a["corners"] is None:
            continue
        mism = a["agree"] is False
        if culled:
            n_cull += 1
            if not cb_culled.checked and not mism:
                continue
            color = C_MISM if mism else C_CULL
        else:
            n_vis += 1
            if not cb_visible.checked and not mism:
                continue
            color = C_MISM if mism else C_VIS
        if mism:
            n_mism += 1
        corners = [tuple(c) for c in a["corners"]]
        _draw_edges(oc, corners, _BOX_EDGES, color, 2 if mism else 1)
        if cb_labels.checked:
            s = oc.screen(oc.cam(tuple(a["pos"])))
            if s:
                cv.text((s[0]+4, s[1]-6), color, a["name"])

    _draw_marker(oc, eye, C_EYE, 6)
    ce = oc.edge(eye, tuple(cam["center"]))
    if ce and ce[0] and ce[1]:
        cv.line(ce[0], ce[1], C_EYE, thickness=1)
    _draw_marker(oc, link, C_LINK, 4)

    c = snap["counts"]
    cv.text((10, 16), C_TXT,
            f"cull_far={snap['cull_far']:.0f}  fov={cam['fovy']:.0f}  "
            f"actors {c['boxed']} box: {n_vis} visible / {n_cull} culled"
            + (f"  MISMATCH={n_mism}" if n_mism else "  (all match game)"))
    mode = _drag["mode"]
    grab = f"   [{mode.upper()} — click to release]" if mode else ""
    cv.text((10, H-16), C_TXT,
            "L-drag: orbit   R-drag: pan   wheel: zoom   "
            "(yellow=cam eye, white=Link)" + grab)
    cv.commit()


_last = [None]        # latest snapshot from the last successful read
_diag = [False]       # one-time first-frame confirmation to the SCRIPTING log


@event.on_frameadvance
def on_frame():
    """Single handler, emu thread only: read live memory, poll mouse input, draw. No on_hostupdate
    and no take_right_click — both crashed the core in this build."""
    try:
        _last[0] = full_snapshot(RD)
        c = _last[0]["counts"]
        status.set(f"actors={c['total']} boxed={c['boxed']} agree={c['agree']} mismatch={c['mismatch']}")
        if not _diag[0]:
            print(f"[cull_viewer] first live frame OK: {c}"); _diag[0] = True
    except Exception as e:
        status.set(f"read error: {e}")   # keep last good snapshot in _last[0] (e.g. during boot)
        if not _diag[0]:
            import traceback as _tb; print("[cull_viewer] read error:\n" + _tb.format_exc()); _diag[0] = True
    try:
        _apply_input()
    except Exception:
        pass
    draw(_last[0])


draw(None)  # show the window immediately, before the first frame advances
