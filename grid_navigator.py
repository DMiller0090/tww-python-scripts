# grid_navigator.py
"""
Grid Navigator — a stripped-down, stage-agnostic cousin of ss_navigator.

Instead of the Great Sea chart + superswim projection, this draws a plain
world-coordinate grid (no background image) whose gridlines grow/shrink as you
zoom. Click anywhere to drop a target, flip the "Move toward target" toggle, and
it drives Link's main stick straight at that world position — full deflection,
camera-aware, so his facing swings onto the target as fast as the stick allows.

This is *normal movement*, not superswim: no walk-speed model, no projection. It
just keeps pointing the stick at the target (camera-derotated) and releases once
Link is within STOP_DIST. Meant for closed rooms (~<=20k units across), so it
opens zoomed in much further than ss_navigator.

The de-rotation runs on `land_steer`, which models the LAND camera
(`dCamera_c::manualCamera`) with the bit-exact tww_sim port and inverts the bearing
through the decomp-ported clamped stick decode. `cam_sync` -- the SWIM subject-camera
model this script used to steer with -- stays as the fallback; see land_steer's
docstring for what the two disagree about and by how much. The HUD carries an aim
readout so the choice is visible rather than assumed.

- Scroll to zoom (toward the cursor). No panning — zoom out to see more.
- Click the grid to set a target; or snap it to Link with the button.
- Frame advance reads live state and drives the stick; host updates own all
  input and drawing, so the view stays live while emulation is paused.
"""
from __future__ import annotations
import math
import os
import sys
from typing import Optional, Tuple

from dolphin import event, gui, controller
from ww import mathutils
from ww.actors.player import Player
from ww.mathutils import deg_to_halfword, wrap_deg
from ww.context.context import set_region
from ww.context.detect import detect_region

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so the siblings below resolve
import cam_sync
import land_steer

# ── configuration ───────────────────────────────────────────────────
DEFAULT_VIEW_SPAN = 20_000.0   # world units across the smaller canvas dimension at load
CAM_PREDICT_STEPS = 1          # cam_sync FALLBACK horizon only (the SWIM convention; land_steer uses its own)
STOP_DIST         = 30.0       # world units: release the stick once Link is this close to the target

# Ease-in ("glide"): below WALK_CAP units to go, ask for a partial mStickDistance instead of full
# deflection. The land walk cap is msd*(17*msd) = 17*msd^2 u/frame, so msd = sqrt(dist/17) asks for
# roughly the remaining distance and Link creeps in rather than blowing through the target at 17
# u/frame. It does not model deceleration, so a small overshoot is still possible.
WALK_CAP          = 17.0       # land walk speed cap (u/frame) at full deflection
MSD_MIN           = 0.12       # floor: mStickDistance <= 0.05 is the game's neutral gate (no input)
GLIDE_STOP_DIST   = 3.0        # release distance while gliding (full-deflection STOP_DIST is 30)
GRID_TARGET_PX    = 70.0       # desired on-screen spacing between gridlines (drives the "nice" step)
SCALE_MIN         = 1.0e-4     # px per world unit (zoom-out limit)
SCALE_MAX         = 50.0       # px per world unit (zoom-in limit)

CANVAS_W   = 360
CANVAS_H   = 345

if not land_steer.loaded:
    print("[grid_navigator] land_steer unavailable (%s) -- falling back to the cam_sync SWIM model"
          % land_steer.load_error)
if not cam_sync.loaded:
    print("[grid_navigator] cam_sync tables not loaded: %s" % cam_sync.load_error)

# ── colours (ARGB 0xAARRGGBB) ───────────────────────────────────────
C_BG       = 0xFF12121E   # canvas backdrop
C_GRID     = 0xFF2C2C44   # minor gridlines
C_GRID_MAJ = 0xFF454568   # major gridlines (every 5th)
C_AXIS     = 0xFF6A6A95   # world x=0 / z=0 axes
C_LABEL    = 0xFF6E7390   # coordinate labels
C_LINK     = 0xFF00CC00   # Link dot
C_FACING   = 0xFFFFFF00   # facing arrow
C_DEST     = 0xFFFF2222   # target marker
C_MOVE     = 0xFF38E08A   # active "moving" pulse (green)
C_PILL_BG  = 0xCC10101A   # status pill background
C_CAM      = 0xFF7FC7FF   # camera / aim-error readout

# ── view transform: world <-> canvas, zoom-around-cursor, no pan ─────

class WorldView:
    """Maps world (x, z) <-> canvas pixels. Canvas X tracks world X, canvas Y
    tracks world Z (so larger Z draws lower on screen, matching ss_navigator and
    the facing-arrow sin/cos convention)."""

    def __init__(self, default_span: float):
        self._default_span = default_span
        self._cx = 0.0           # world X at canvas centre
        self._cz = 0.0           # world Z at canvas centre
        self._scale = 0.0        # px per world unit; 0 until the first viewport
        self._zw = 0.0
        self._zh = 0.0

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def ready(self) -> bool:
        return self._scale > 0.0 and self._zw > 0.0 and self._zh > 0.0

    def set_viewport(self, w: float, h: float) -> None:
        if w <= 0 or h <= 0:
            return
        self._zw, self._zh = w, h
        if self._scale <= 0.0:
            self._scale = min(w, h) / self._default_span

    def center_on(self, wx: float, wz: float) -> None:
        self._cx, self._cz = wx, wz

    def w2c(self, wx: float, wz: float) -> Tuple[float, float]:
        return (self._zw / 2.0 + (wx - self._cx) * self._scale,
                self._zh / 2.0 + (wz - self._cz) * self._scale)

    def c2w(self, cx: float, cy: float) -> Tuple[float, float]:
        return (self._cx + (cx - self._zw / 2.0) / self._scale,
                self._cz + (cy - self._zh / 2.0) / self._scale)

    def zoom(self, notches: float, cx: float, cy: float) -> None:
        # Keep the world point under the cursor fixed while scaling.
        wx, wz = self.c2w(cx, cy)
        self._scale = max(SCALE_MIN, min(SCALE_MAX, self._scale * (1.15 ** notches)))
        self._cx = wx - (cx - self._zw / 2.0) / self._scale
        self._cz = wz - (cy - self._zh / 2.0) / self._scale


def _nice_step(scale: float) -> float:
    """A 1/2/5·10ⁿ world step whose on-screen spacing is ~GRID_TARGET_PX."""
    if scale <= 0:
        return 1000.0
    raw = GRID_TARGET_PX / scale            # world units per target spacing
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    base = raw / (10.0 ** exp)
    if base < 1.5:
        mult = 1.0
    elif base < 3.5:
        mult = 2.0
    elif base < 7.5:
        mult = 5.0
    else:
        mult = 10.0
    return mult * (10.0 ** exp)


def _fmt_units(v: float) -> str:
    """Compact label for a world distance (e.g. 2000 -> '2k', 1500 -> '1.5k')."""
    a = abs(v)
    if a >= 1000.0:
        s = "%.1fk" % (v / 1000.0)
        return s.replace(".0k", "k")
    return "%.0f" % v


# ── single window: grid canvas on top, controls below ────────────────
_WIN_QSS = """
QWidget { background: #1a1a2e; color: #e6e6f0; font-size: 13px; }
QLabel { color: #9aa0b5; font-size: 11px; padding: 2px 0; }
QCheckBox { spacing: 8px; padding: 5px 2px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #555; border-radius: 3px; background: #12121e; }
QCheckBox::indicator:hover { border-color: #00aaff; }
QCheckBox::indicator:checked { background: #00aaff; border-color: #00aaff; }
QPushButton {
    background: #1f8f4e; color: #ffffff; font-weight: bold;
    border: none; border-radius: 5px; padding: 8px 12px; margin: 4px 0;
}
QPushButton:hover { background: #25a85c; }
QPushButton:pressed { background: #18713e; }
QLineEdit {
    background: #12121e; border: 1px solid #3a3a55; border-radius: 4px;
    padding: 4px 6px; color: #e6e6f0; max-width: 80px;
}
QLineEdit:focus { border-color: #00aaff; }
"""

set_region(detect_region())

_view = WorldView(DEFAULT_VIEW_SPAN)

_panel  = gui.window("Grid Navigator", style=_WIN_QSS)
_canvas = _panel.canvas(CANVAS_W, CANVAS_H)
_status = _panel.text("Click the grid to set a target")
_btn_here = _panel.button("Set target to Link's position")
# Manual target entry: type world X/Z and apply (an alternative to clicking the grid).
_inp_x   = _panel.input_text("target X", "0")
_inp_z   = _panel.input_text("target Z", "0")
_btn_xz  = _panel.button("Set target to X / Z")
_chk_move  = _panel.checkbox("Move toward target")
_chk_glide = _panel.checkbox("Ease in near target", True)

# ── shared state (emu thread writes _cur_*/_have_state/_facing_hw; host thread
#    writes _dest_*/_armed). Plain globals under the GIL, same pattern as ss_navigator. ──
_dest_x: float = 0.0
_dest_z: float = 0.0
_dest_set: bool = False

_cur_x: float = 0.0
_cur_z: float = 0.0
_facing_hw: Optional[int] = None
_have_state: bool = False

_armed: bool = False         # host-thread latch of the "Move toward target" checkbox
_glide: bool = True          # host-thread latch of the "Ease in near target" checkbox
_at_target: bool = False     # set by the emu thread when within the release distance (for the HUD)
_cam_line: str = ""          # emu-thread-rendered camera/aim readout (host thread only reads it)
_monitor = land_steer.AimMonitor()
_prev_pos: Optional[Tuple[float, float]] = None   # last frame's (x, z), for the track metric
_anim: int = 0               # host-side tick for the canvas pulse
_centered: bool = False      # one-shot: centre the view on Link the first time we can


def _set_main(sx: int, sy: int) -> None:
    inp = controller.get_gc_buttons(0)
    inp["StickX"] = max(0, min(int(sx), 255))
    inp["StickY"] = max(0, min(int(sy), 255))
    controller.set_gc_buttons(0, inp)


def _msd_for(dist: float) -> float:
    """Requested mStickDistance for `dist` units still to go. Full deflection until the remaining
    distance is under one full-speed frame, then sqrt(dist / WALK_CAP) so the commanded speed cap
    (17*msd^2) tracks what is actually left. See the GLIDE constants."""
    if not _glide or dist >= WALK_CAP:
        return 1.0
    return max(MSD_MIN, math.sqrt(dist / WALK_CAP))


def _drive_toward(cur_x: float, cur_z: float) -> None:
    """Point the main stick at the world bearing to the target, de-rotated for the camera as it
    will stand when the game DECODES the stick (not as it stands now). Emu-thread only (memory +
    controller).

    Preferred path is land_steer: the bit-exact `manualCamera` integrator seeded from live RAM,
    inverted through the decomp-ported clamped stick decode. It falls back to cam_sync's SWIM
    model whenever the sim import failed or the camera pointer chain is unreadable -- so the
    script still steers (less accurately) on a frame where the camera is between instances."""
    global _at_target, _cam_line, _prev_pos

    dist = mathutils.dist2d(cur_x, cur_z, _dest_x, _dest_z)
    if dist <= (GLIDE_STOP_DIST if _glide else STOP_DIST):
        _at_target = True
        _prev_pos = None
        return   # within threshold: leave the stick centred (set_gc_buttons auto-clears)
    _at_target = False

    bearing_deg = wrap_deg(math.degrees(math.atan2(_dest_x - cur_x, _dest_z - cur_z)))
    bearing_hw  = deg_to_halfword(bearing_deg) & 0xFFFF

    # Where Link actually went since last frame vs where he was told to go -- the one end-to-end
    # number, and the only one that catches a PREDICT_STEPS that is a frame out (land_steer.track).
    if _prev_pos is not None:
        _monitor.track(cur_x - _prev_pos[0], cur_z - _prev_pos[1], dist)
    _prev_pos = (cur_x, cur_z)

    inp = controller.get_gc_buttons(0)
    csx, csy = int(inp.get("CStickX", 128)), int(inp.get("CStickY", 128))
    msd = _msd_for(dist)

    if land_steer.loaded:
        cam = land_steer.read_cam()
        (msx, msy), pred = land_steer.stick_for_world_bearing(bearing_hw, csx, csy,
                                                              msd=msd, cam=cam)
        if pred is not None:
            _monitor.submit(bearing_hw, (msx, msy), pred)
            _cam_line = _describe_cam(cam, msd)
            _set_main(msx, msy)
            return
        _cam_line = "camera unreadable - cam_sync fallback"

    if cam_sync.loaded:
        (msx, msy), _pred = cam_sync.charge_stick(bearing_hw, csx, csy, CAM_PREDICT_STEPS)
        _set_main(msx, msy)


def _describe_cam(cam, msd: float) -> str:
    """Render the camera/aim readout to a string ON THE EMU THREAD. Everything it summarises is
    written from this thread, so handing the drawing thread a finished string keeps it from
    iterating an AimMonitor deque while a frame is appending to it."""
    if cam is None:
        return "camera unreadable"
    style = cam["style_name"] or ("style %d" % cam["style"])
    if cam["dmc"]:
        note = " DMC(frozen)"
    elif cam["mode"] != land_steer.MODE_MANUAL:
        note = " mode %d(blip)" % cam["mode"]
    else:
        note = ""
    if land_steer.last_extrapolated:
        note += " cs:hold"       # the pad history could not cover the horizon; assumed a hold
    return "%s %.0f°/f%s  msd %.2f  %s" % (style, cam["scale"], note, msd, _monitor.text())


# ── memory read + stick drive (emu thread only) ─────────────────────
def _read_state() -> None:
    global _cur_x, _cur_z, _facing_hw, _have_state, _at_target, _cam_line

    # Score first, drive second: the csangle standing at the top of this frame is the one the
    # stick submitted `horizon` frames ago is being decoded against, so ticking here -- before
    # this frame's submit -- is what lines the monitor's ages up with reality.
    if land_steer.loaded:
        # Unconditionally, every frame, before anything predicts: the camera consumes PAST pads, so
        # the prediction wants the real history, not an assumed hold. A gap here (skipping the push
        # on an idle frame) would shift every entry onto the wrong frame -- worse than no history.
        try:
            _inp = controller.get_gc_buttons(0)
            land_steer.push_cstick(int(_inp.get("CStickX", 128)), int(_inp.get("CStickY", 128)))
        except Exception:
            land_steer.reset_cstick()
        cam = land_steer.read_cam()
        _monitor.tick(cam["csangle"] if cam else None)
        if not _armed:
            _cam_line = _describe_cam(cam, 1.0)

    # Rebuild the Player each frame: its gptr is recreated across stage/room
    # transitions, so a cached instance would read a stale base. Cheap (one ptr read).
    try:
        p = Player()
        valid = p._valid
    except Exception:
        valid = False
    if not valid:
        _have_state = False
        _at_target = False
        land_steer.reset_cstick()      # no Link == a load or a transition: the history is stale
        return

    try:
        _cur_x = p.debug_x()
        _cur_z = p.debug_z()
        _facing_hw = p.angle_y
        _have_state = True
    except Exception:
        _have_state = False
        return

# ── input + draw (host thread; safe while paused) ───────────────────
def _update_view() -> None:
    global _dest_x, _dest_z, _dest_set, _armed, _glide, _anim, _centered

    _anim += 1
    cur_x, cur_z = _cur_x, _cur_z
    facing_hw = _facing_hw
    have = _have_state

    _armed = _chk_move.checked
    _glide = _chk_glide.checked

    # ── responsive: track the live canvas size ─────────────────────
    try:
        cw = float(_canvas.width)
        ch = float(_canvas.height)
    except (AttributeError, TypeError):
        cw, ch = float(CANVAS_W), float(CANVAS_H)
    _view.set_viewport(cw, ch)

    # Centre on Link once, the first time we have both a position and a viewport.
    if not _centered and have and _view.ready:
        _view.center_on(cur_x, cur_z)
        _centered = True

    # ── zoom toward the cursor ─────────────────────────────────────
    wheel = _canvas.take_wheel()
    if wheel:
        mx, my, inside = _canvas.mouse_pos()
        if inside:
            _view.zoom(wheel, mx, my)

    # ── set target: button (Link's pos), manual X/Z, or click (grid pos) ────────
    if _btn_here.clicked and have:
        _dest_x, _dest_z = cur_x, cur_z
        _dest_set = True

    bad_xz = False
    if _btn_xz.clicked:
        try:
            _dest_x, _dest_z = float(_inp_x.value), float(_inp_z.value)
            _dest_set = True
        except (ValueError, TypeError):
            bad_xz = True

    click = _canvas.take_click()
    if click is not None and _view.ready:
        _dest_x, _dest_z = _view.c2w(click[0], click[1])
        _dest_set = True

    # Right-click anywhere in the grid removes the target so Link stops moving.
    if _canvas.take_right_click() is not None:
        _dest_set = False

    moving = bool(_armed and have and _dest_set and not _at_target)

    # ── draw ───────────────────────────────────────────────────────
    _canvas.clear()
    _canvas.rect_filled((0.0, 0.0), (cw, ch), C_BG)

    step = _nice_step(_view.scale) if _view.ready else 1000.0
    if _view.ready:
        _draw_grid(cw, ch, step)

        if have:
            lx, ly = _view.w2c(cur_x, cur_z)
            if moving:
                PING = 36.0
                rgb = C_MOVE & 0x00FFFFFF
                for off in (0.0, 0.5):
                    pr = ((_anim + off * PING) % PING) / PING
                    a = int(170 * (1.0 - pr))
                    if a > 0:
                        _canvas.circle((lx, ly), 7.0 + pr * 24.0, (a << 24) | rgb, 2.0)
                _canvas.circle((lx, ly), 8.5, C_MOVE, 2.0)
            _canvas.circle_filled((lx, ly), 6.0, C_LINK)
            if facing_hw is not None:
                rad = math.radians(mathutils.halfword_to_deg(facing_hw))
                _canvas.line((lx, ly),
                             (lx + 30.0 * math.sin(rad), ly + 30.0 * math.cos(rad)),
                             C_FACING, 2.0)

        if _dest_set:
            dx, dy = _view.w2c(_dest_x, _dest_z)
            R = 8.0
            _canvas.line((dx - R, dy - R), (dx + R, dy + R), C_DEST, 2.0)
            _canvas.line((dx + R, dy - R), (dx - R, dy + R), C_DEST, 2.0)
            _canvas.circle((dx, dy), R, C_DEST, 2.0)

    # ── grid-size pill (top-left), camera/aim readout under it ─────
    grid_label = "Grid: %s u" % _fmt_units(step)
    pill_w = 22.0 + len(grid_label) * 7.0
    _canvas.rect_filled((6, 6), (6 + pill_w, 25), C_PILL_BG, 5.0)
    _canvas.text((12, 10), C_LABEL, grid_label)

    # Rendered on the emu thread (see _describe_cam) -- read-only here.
    cam_label = _cam_line
    if cam_label:
        pill_w = 22.0 + len(cam_label) * 6.5
        _canvas.rect_filled((6, 29), (6 + pill_w, 48), C_PILL_BG, 5.0)
        _canvas.text((12, 33), C_CAM, cam_label)

    _canvas.commit()

    # ── status line ────────────────────────────────────────────────
    if bad_xz:
        _status.set("Bad X/Z — enter numbers")
    elif not (land_steer.loaded or cam_sync.loaded):
        _status.set("neither land_steer nor cam_sync loaded — cannot steer (see console)")
    elif not have:
        _status.set("Waiting for Link…")
    elif _dest_set:
        dist = mathutils.dist2d(cur_x, cur_z, _dest_x, _dest_z)
        atd  = mathutils.angle2d_hw(cur_x, cur_z, _dest_x, _dest_z)
        state = ("AT TARGET" if _at_target else "MOVING") if _armed else "idle"
        _status.set("Target X=%.0f Z=%.0f   dist=%.0f   angle=%d   [%s]"
                    % (_dest_x, _dest_z, dist, atd, state))
    else:
        _status.set("Click the grid to set a target")


@event.on_frameadvance
def on_frameadvance() -> None:
    """Refresh live state, then apply movement for this frame.

    Drawing and input stay OFF the emu thread: _update_view() consumes one-shot
    canvas events and rebuilds the canvas non-atomically, so running it here too
    would race the host thread and could commit a half-built frame.
    """
    _read_state()
    if _armed and _dest_set and _have_state:
        try:
            _drive_toward(_cur_x, _cur_z)
        except Exception:
            pass


@event.on_hostupdate
def on_hostupdate() -> None:
    """Keep the detached window interactive while the emulator is paused."""
    _update_view()


def _draw_grid(cw: float, ch: float, step: float) -> None:
    """Vertical lines at world X multiples of `step`, horizontal at world Z
    multiples, every 5th brighter, with the x=0 / z=0 axes brightest and small
    coordinate labels along the top/left edges."""
    wx0, wz0 = _view.c2w(0.0, 0.0)       # top-left world corner
    wx1, wz1 = _view.c2w(cw, ch)         # bottom-right world corner

    # Vertical lines (constant world X), left → right.
    i0 = int(math.floor(wx0 / step))
    i1 = int(math.ceil(wx1 / step))
    for i in range(i0, i1 + 1):
        wx = i * step
        cx, _ = _view.w2c(wx, 0.0)
        col = C_AXIS if i == 0 else (C_GRID_MAJ if i % 5 == 0 else C_GRID)
        _canvas.line((cx, 0.0), (cx, ch), col, 1.0)
        if i % 5 == 0:
            _canvas.text((cx + 2.0, 28.0), C_LABEL, _fmt_units(wx))

    # Horizontal lines (constant world Z), top → bottom.
    j0 = int(math.floor(wz0 / step))
    j1 = int(math.ceil(wz1 / step))
    for j in range(j0, j1 + 1):
        wz = j * step
        _, cy = _view.w2c(0.0, wz)
        col = C_AXIS if j == 0 else (C_GRID_MAJ if j % 5 == 0 else C_GRID)
        _canvas.line((0.0, cy), (cw, cy), col, 1.0)
        if j % 5 == 0:
            _canvas.text((4.0, cy + 2.0), C_LABEL, _fmt_units(wz))
