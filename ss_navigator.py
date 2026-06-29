# ss_navigator.py
"""
Superswim Navigator — shows where Link is on the Great Sea, which way he is
facing, and projects the expected superswim path toward a clicked destination.
- Scroll to zoom. Click the map to set a destination.
- Driven by on_hostupdate so it stays live while the game is paused.
"""
from __future__ import annotations
import math
import os
import sys
from typing import Optional, Tuple
from dolphin import event, gui, controller, memory
from ww import mathutils, game
from ww.actors.player import Player
from ww.mathutils import deg_to_halfword, wrap_deg
from ww.context.context import set_region
from ww.context.detect import detect_region
from ww.game import current_stage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `import cam_sync` resolves
import cam_sync

# ── configuration ───────────────────────────────────────────────────
PROJECTION_MAX_STEPS    = 4000   # safety cap; actual length tracks distance-to-dest
PROJECTION_BUFFER_STEPS = 6      # extra steps carried straight through the destination
SS_SPEED                = 7000.0

# ── charge config (merged from ss_charge_destination / ss_charge_facing_dir) ──
# Two armed modes steer the superswim charge straight off the clicked destination, both
# de-rotated through cam_sync so they hold their world axis while the camera spins:
#   arrow-swim (default) = ss_charge_destination: reorient onto the axis PERPENDICULAR to
#       the Link->dest bearing (so the perpendicular arrow drift heads toward the dest),
#       then alternate across that axis with instant-turnaround snaps.
#   straight-axis        = ss_charge_facing_dir: 180-flip charge along the live Link->dest
#       bearing (a fixed-axis primitive that re-aims at the dest as Link moves).
OFFSET_DEG        = 90    # 90 = charge axis perpendicular to the dest bearing (arrow-swim toward it)
ARROW_SWIM_DEG    = 0     # drift tilt toward the dest (0 = pure charge, no drift)
CAM_PREDICT_STEPS = 1     # 1 = predict next-frame csangle; 0 reproduces the old stale read
FACING_ADDR       = 0x803EA3D2   # shape_angle.y (u16) -- the facing the reorient snap operates on
_ARROW_HW         = int(round(ARROW_SWIM_DEG * 65536 / 360.0))

# Minimum turn (deg) to accept a turnaround as a DIRECT charge snap; below it we reorient.
# The hard game boundary is 135° (DIR_BACKWARD); sitting a degree under keeps near-180°
# alternations that drift a hair from needlessly dropping into a reorient (more failure-
# prone). Raise toward 135 for stricter snaps, lower to reorient even less.
TURNAROUND_SNAP_DEG = 134.0
_TURNAROUND_SNAP_HW = int(round(TURNAROUND_SNAP_DEG * 65536 / 360.0))

if not cam_sync.loaded:
    print("[ss_navigator] cam_sync tables not loaded: %s" % cam_sync.load_error)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(_SCRIPT_DIR, "assets", "great_sea_chart.png").replace("\\", "/")

IMG_W = 624
IMG_H = 593

WORLD_LEFT   = -350_000.0
WORLD_RIGHT  =  350_000.0
WORLD_TOP    = -350_000.0
WORLD_BOTTOM =  350_000.0

# Default grid padding (image px); editable live in the controls window.
DEF_LEFT, DEF_RIGHT, DEF_TOP, DEF_BOTTOM = 12.0, 615.0, 12.0, 585.0

# Canvas sized to the chart's aspect ratio (+margins) so the whole map fits at
# load with backdrop showing all around. IMG_W/IMG_H ratio ≈ 1.05.
CANVAS_W   = 360
MAP_ZONE_H = 345            # canvas (map) height; controls flow below in the window
MAP_MARGIN = 32             # min-zoom inset so the frame/backdrop stay visible
DEFAULT_ZOOM = 1.0          # multiple of fit scale: 1.0 = whole chart, 2.0 = 2x in

# ── colours ─────────────────────────────────────────────────────────
C_LINK   = 0xFF00CC00
C_DEST   = 0xFFFF2222
C_TRAVEL = 0xFFFFFF00
C_PROJ   = 0xFF00AAFF
C_FG     = 0xFFEEEEEE
C_DIM    = 0xFF777777
C_SWIM   = 0xFF38E08A   # charging pulse (green) — sonar ping around Link + status pill
C_REORI  = 0xFFFFB638   # reorienting pulse (amber)
# decorative frame around the chart
C_SEA      = 0xFF12203A   # deep-sea backdrop behind/around the map
C_SEA_DK   = 0xFF0A1424   # darker vignette toward the edges
C_FRAME_O  = 0xFF7A5A2E   # outer bronze frame
C_FRAME_I  = 0xFFD9B86A   # inner gold rim
C_CORNER   = 0xFFF0DCA0   # corner accents

# ── view transform ──────────────────────────────────────────────────

class ViewTransform:
    def __init__(self, img_w: int, img_h: int, margin: float = 0.0, default_zoom: float = 0.0):
        self._img_w = img_w
        self._img_h = img_h
        self._margin = margin
        self._pan_ix = img_w / 2.0
        self._pan_iy = img_h / 2.0
        self._zw = 0.0
        self._zh = 0.0
        self._min_scale = 1.0
        self._scale = 1.0
        self._default_zoom = default_zoom
        self._zoom_set = False  # apply default_zoom on the first valid viewport

    def _fit_scale(self) -> float:
        # Scale at which the chart fits the viewport with `margin` px of backdrop.
        return min((self._zw - 2 * self._margin) / self._img_w,
                   (self._zh - 2 * self._margin) / self._img_h)

    def set_viewport(self, zone_w: float, zone_h: float) -> None:
        """Adapt to a new canvas size (responsive resize)."""
        if zone_w <= 0 or zone_h <= 0 or (zone_w == self._zw and zone_h == self._zh):
            return
        self._zw = zone_w
        self._zh = zone_h
        self._min_scale = self._fit_scale()
        if not self._zoom_set:
            # default_zoom is a multiplier on the fit scale: 1.0 = fully zoomed
            # out (fit), 2.0 = 2x zoomed in. <=0 falls back to fit.
            if self._default_zoom > 0:
                self._scale = max(self._min_scale, self._min_scale * self._default_zoom)
            else:
                self._scale = self._min_scale
            self._zoom_set = True
        else:
            self._scale = max(self._min_scale, self._scale)
        self._clamp_pan()

    def img_to_canvas(self, ix: float, iy: float) -> Tuple[float, float]:
        return (
            (ix - self._pan_ix) * self._scale + self._zw / 2.0,
            (iy - self._pan_iy) * self._scale + self._zh / 2.0,
        )

    def canvas_to_img(self, cx: float, cy: float) -> Tuple[float, float]:
        return (
            (cx - self._zw / 2.0) / self._scale + self._pan_ix,
            (cy - self._zh / 2.0) / self._scale + self._pan_iy,
        )

    def _clamp_pan(self) -> None:
        # Keep the visible window within the image so you can't pan past the edges.
        half_w = (self._zw / 2.0) / self._scale
        half_h = (self._zh / 2.0) / self._scale
        if half_w >= self._img_w / 2.0:
            self._pan_ix = self._img_w / 2.0
        else:
            self._pan_ix = max(half_w, min(self._img_w - half_w, self._pan_ix))
        if half_h >= self._img_h / 2.0:
            self._pan_iy = self._img_h / 2.0
        else:
            self._pan_iy = max(half_h, min(self._img_h - half_h, self._pan_iy))

    def src_crop(self) -> Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float, float, float]]:
        ix0, iy0 = self.canvas_to_img(0, 0)
        ix1, iy1 = self.canvas_to_img(self._zw, self._zh)
        ix0c = max(0.0, ix0); iy0c = max(0.0, iy0)
        ix1c = min(float(self._img_w), ix1); iy1c = min(float(self._img_h), iy1)
        if ix1c <= ix0c or iy1c <= iy0c:
            return (0.0, 0.0), (0.0, 0.0), (0.0, 0.0, 1.0, 1.0)
        sx0 = ix0c / self._img_w; sy0 = iy0c / self._img_h
        sx1 = ix1c / self._img_w; sy1 = iy1c / self._img_h
        cx0, cy0 = self.img_to_canvas(ix0c, iy0c)
        cx1, cy1 = self.img_to_canvas(ix1c, iy1c)
        pos  = (max(0.0, cx0), max(0.0, cy0))
        size = (cx1 - max(0.0, cx0), cy1 - max(0.0, cy0))
        return pos, size, (sx0, sy0, sx1, sy1)

    def zoom(self, notches: float, cx: float, cy: float) -> None:
        ix, iy = self.canvas_to_img(cx, cy)
        self._scale = max(self._min_scale, min(40.0, self._scale * (1.15 ** notches)))
        self._pan_ix = ix - (cx - self._zw / 2.0) / self._scale
        self._pan_iy = iy - (cy - self._zh / 2.0) / self._scale
        self._clamp_pan()

    def pan(self, dcx: float, dcy: float) -> None:
        self._pan_ix -= dcx / self._scale
        self._pan_iy -= dcy / self._scale
        self._clamp_pan()

# ── superswim projection ────────────────────────────────────────────

def _ang_dist(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d

def ss_projection_points(sx, sz, facing_hw, dx, dz, max_steps, speed, buffer_steps=0):
    """
    Trace the expected superswim path toward (dx, dz).

    Superswimming alternates the controller every frame, so Link's heading flips
    between his facing angle H and H+180 each frame. You pick whichever frame heads
    closer to the destination, so each step advances along the better of the two.

    Once the path reaches the destination the heading is locked so the line carries
    `buffer_steps` straight through the destination before stopping.
    """
    pts = [(sx, sz)]
    x, z = sx, sz
    h = mathutils.halfword_to_deg(facing_hw)
    locked: Optional[float] = None
    extra = 0
    for _ in range(max_steps):
        if locked is None:
            h_alt   = (h + 180.0) % 360.0
            to_dest = mathutils.angle2d_deg(x, z, dx, dz)
            chosen  = h if _ang_dist(h, to_dest) <= _ang_dist(h_alt, to_dest) else h_alt
        else:
            chosen = locked
        x, z = mathutils.project2d(x, z, chosen, speed)
        pts.append((x, z))
        if locked is None and mathutils.dist2d(x, z, dx, dz) < speed:
            locked = chosen  # carry the heading straight through the destination
        if locked is not None:
            extra += 1
            if extra >= buffer_steps:
                break
    return pts

# ── single window: map canvas on top, controls flowing below ────────
_WIN_QSS = """
QWidget { background: #1a1a2e; color: #e6e6f0; font-size: 13px; }

/* status / section headers */
QLabel { color: #9aa0b5; font-size: 11px; padding: 2px 0; }

/* checkboxes */
QCheckBox { spacing: 8px; padding: 5px 2px; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #555; border-radius: 3px; background: #12121e; }
QCheckBox::indicator:hover { border-color: #00aaff; }
QCheckBox::indicator:checked { background: #00aaff; border-color: #00aaff; }

/* primary action button (accent green to match Link's marker) */
QPushButton {
    background: #1f8f4e; color: #ffffff; font-weight: bold;
    border: none; border-radius: 5px; padding: 8px 12px; margin: 4px 0;
}
QPushButton:hover { background: #25a85c; }
QPushButton:pressed { background: #18713e; }

/* text inputs */
QLineEdit {
    background: #12121e; border: 1px solid #3a3a55; border-radius: 4px;
    padding: 5px 7px; color: #e6e6f0; selection-background-color: #00aaff;
}
QLineEdit:focus { border-color: #00aaff; }

/* bipolar arrow-swim tilt slider: red (−, port) → neutral center → green (+, starboard) */
QSlider::groove:horizontal {
    height: 8px; border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #ff4d4d, stop:0.46 #2a2a40, stop:0.5 #6b6b85,
        stop:0.54 #2a2a40, stop:1 #36d97a);
}
QSlider::handle:horizontal {
    width: 14px; margin: -6px 0; border-radius: 7px;
    background: #ffd95a; border: 2px solid #ffffff;
}
QSlider::handle:horizontal:hover  { background: #fff0a8; }
QSlider::handle:horizontal:pressed { background: #ffe070; border-color: #00aaff; }
"""
set_region(detect_region())
_player: Optional[Player] = None

_view = ViewTransform(IMG_W, IMG_H, MAP_MARGIN, DEFAULT_ZOOM)

_panel  = gui.window("Superswim Navigator", style=_WIN_QSS)
_canvas = _panel.canvas(CANVAS_W, MAP_ZONE_H)
_status = _panel.text("Click the map to set a destination")

# --- primary actions ---
_btn_here = _panel.button("Set destination to Link's position")
_chk_proj = _panel.checkbox("Show projection", checked=True)

# --- superswim charge (drives the controller toward the selected destination) ---
# One checkbox per mode; they're kept mutually exclusive in update() (checking one
# clears the other). Both unchecked = disarmed.
_chk_dest  = _panel.checkbox("Swim to destination")
_chk_angle = _panel.checkbox("Swim at current angle")
# Arrow-swim drift tilt (deg), biases the perpendicular drift to one side of the
# straight path. Bipolar and centered at 0: − (red, port/left) ◀ … ▶ + (green,
# starboard/right). Live; only affects "Swim to destination". ±45 keeps the
# alternating snap comfortably past the 135° turnaround threshold.
# The tilted turnaround angle is (180° − tilt), so the snap only holds while
# tilt < 180 − TURNAROUND_SNAP_DEG. Cap the slider at exactly that budget so the modifier
# can never push a turnaround past the snap threshold (at the extreme the CHARGE* fallback
# still preserves the snap untilted). Tracks TURNAROUND_SNAP_DEG automatically.
ARROW_TILT_MAX = 180.0 - TURNAROUND_SNAP_DEG
# Label flanks the bar with − / + (the caption renders directly above the slider). The
# centered start relies on the ScriptWindowManager SliderFloat patch honoring .value.
_sld_arrow = _panel.slider_float("−        Arrow swim modifier        +",
                                 -ARROW_TILT_MAX, ARROW_TILT_MAX)
_sld_arrow.value = 0.0          # start centered (no drift bias)
_txt_arrow = _panel.text("Modifier:  0.0°")

# --- calibration (collapsible) ---
_chk_cal  = _panel.checkbox("Show grid calibration", checked=False)
_cal_head   = _panel.text("Grid calibration (image px)")
_inp_left   = _panel.input_text("left",   str(DEF_LEFT))
_inp_right  = _panel.input_text("right",  str(DEF_RIGHT))
_inp_top    = _panel.input_text("top",    str(DEF_TOP))
_inp_bottom = _panel.input_text("bottom", str(DEF_BOTTOM))
_cal_widgets = (_cal_head, _inp_left, _inp_right, _inp_top, _inp_bottom)

def _set_calibration_visible(show: bool) -> None:
    """Toggle the calibration widgets. Requires a `visible` property on widgets
    (pending C++ support); guarded so it's a no-op on builds without it."""
    for w in _cal_widgets:
        try:
            w.visible = show
        except (AttributeError, TypeError):
            pass

_cal_shown: Optional[bool] = None  # force an initial sync on first frame

def _padding() -> Tuple[float, float, float, float]:
    """Read padding fields, falling back to defaults on bad input."""
    def f(widget, default):
        try:
            return float(widget.value)
        except (ValueError, TypeError):
            return default
    return (f(_inp_left, DEF_LEFT), f(_inp_right, DEF_RIGHT),
            f(_inp_top, DEF_TOP), f(_inp_bottom, DEF_BOTTOM))

_dest_x: float = 0.0
_dest_z: float = 0.0
_dest_set: bool = False

# Cached game state — written on the emu thread (frameadvance), read on the host
# thread (hostupdate). Reading emulated memory off the emu thread is unsafe and
# crashes Dolphin, so all memory access stays in _read_state().
_cur_x: float = 0.0
_cur_z: float = 0.0
_facing_hw: Optional[int] = None
_have_state: bool = False
_current_stage: str = ""

# Charge control. _armed / _angle_mode / _ARROW_HW are written on the host thread (the
# UI in update()) and read on the emu thread (_run_charge() in _read_state()), mirroring
# the _dest_* pattern; plain Python globals under the GIL, no cross-interpreter bridge.
#   _angle_mode False = "Swim to destination" (arrow-swim toward the clicked dest)
#   _angle_mode True  = "Swim at current angle" (hold Link's facing at arm time)
_armed: bool = False
_angle_mode: bool = False
_dest_prev: bool = False      # last-frame checkbox states, for the mutual-exclusion edge check
_angle_prev: bool = False
_chg_active: bool = False    # charging this frame (latch identity tracked by _chg_key)
_chg_key = None             # plan identity -- recapture the held facing when this changes
_chg_goal: int = 0          # angle mode: Link's facing captured at arm time (the fixed axis)
_chg_expected: int = 0      # MODEL facing: where the last snap put Link. Drives the alternation
                            # instead of laggy live facing (the snap manifests 1 game frame late).
_chg_last_frame = None      # game frame of the last plan advance (gate; None = advance now)
_chg_target: int = 0        # current commanded world facing target (also HUD)
_chg_phase: str = ""        # HUD: current charge phase
_anim: int = 0              # host-side frame tick for canvas animation (swim ping / pill blink)

SEA_STAGE = "sea"


def _set_main(sx: float, sy: float) -> None:
    inp = controller.get_gc_buttons(0)
    inp["StickX"] = max(0, min(int(sx), 255))
    inp["StickY"] = max(0, min(int(sy), 255))
    controller.set_gc_buttons(0, inp)


def _reset_charge() -> None:
    global _chg_active
    _chg_active = False


def _run_charge(cur_x: float, cur_z: float) -> None:
    """Drive the main stick to charge the superswim, validating every turnaround.

    Emu-thread only (memory + controller live here). The PLAN advances once per GAME frame
    (game.frame() @ 0x803E9D34), re-issuing the held target on the other VI callbacks --
    on_frameadvance fires per VI frame (60 Hz) while game logic / the snap run at 30 Hz, so
    advancing the plan per-callback would burn two flips per game frame and the snap never
    fires (the original open-loop bug). The stick itself is re-issued every callback so it
    de-rotates against the live camera (set_gc_buttons auto-clears).

    Facing is MODELED (_chg_expected = where the last snap put Link), not read live: the
    instant-turnaround snaps shape_angle exactly onto the commanded charge each game frame
    but manifests in RAM a frame late, so live facing lags and breaks the alternation.
    Seeded once from the real facing at arm time; thereafter it equals the last command.

    Each game frame: pick the charge axis, take the end FARTHER from the modeled facing as
    the turnaround target (auto-alternates 180°), and check it's inside the instant-snap
    window -- |angdiff(target, facing)| > 135° (0x6000), the DIR_BACKWARD cone from the
    decomp. If yes, snap it (clean charge). If NOT (the axis rotated near-perpendicular to
    facing, or the arrow tilt pushed the target under the 45° budget) a single snap can't
    reach it, so plan a chain of valid >135° snaps onto the axis with reorient_targets and
    take step 1 -- re-planned each frame, exactly the start-of-script reorient, now applied
    continuously to any turnaround that falls outside the window.
    """
    global _chg_active, _chg_key, _chg_goal, _chg_expected, _chg_last_frame
    global _chg_target, _chg_phase

    if not cam_sync.loaded:
        return

    inp = controller.get_gc_buttons(0)
    csx, csy = int(inp.get("CStickX", 128)), int(inp.get("CStickY", 128))
    arrow_on = _ARROW_HW != 0

    # (Re)arm: seed the modeled facing from the real facing once, and force a plan advance
    # this callback. Angle mode also captures that facing as its fixed axis.
    key = ("angle",) if _angle_mode else ("dest",)
    if (not _chg_active) or key != _chg_key:
        _chg_key = key
        _chg_active = True
        _chg_expected = memory.read_u16(FACING_ADDR) & 0xFFFF
        if _angle_mode:
            _chg_goal = _chg_expected
        _chg_last_frame = None

    # Advance the plan once per game frame; otherwise hold the last command.
    frame = game.frame()
    if _chg_last_frame is None or frame != _chg_last_frame:
        _chg_last_frame = frame

        # Charge axis. Dest mode: the OFFSET_DEG (90°) perpendicular arrow-swim axis applies
        # only when a tilt is dialed in; at 0 the axis is the dest bearing itself (straight
        # charge toward the dest, matching the projection). Angle mode: the captured facing.
        if _angle_mode:
            axis = _chg_goal
            dest_bearing = _chg_goal           # no drift in angle mode; placeholder
        else:
            dest_bearing = deg_to_halfword(
                wrap_deg(math.degrees(math.atan2(_dest_x - cur_x, _dest_z - cur_z)))) & 0xFFFF
            offset = (deg_to_halfword(OFFSET_DEG) & 0xFFFF) if arrow_on else 0
            axis = (dest_bearing + offset) & 0xFFFF
        e0, e1 = axis, (axis + 0x8000) & 0xFFFF

        # Turnaround target = axis end FARTHER from the modeled facing (the big-angle snap;
        # after snapping to one end the other becomes the far one -> auto 180° alternation).
        # Arrow tilt biases facing AWAY from the dest so the ⊥ drift heads toward it.
        far = e0 if abs(cam_sync.angdiff_hw(_chg_expected, e0)) > abs(cam_sync.angdiff_hw(_chg_expected, e1)) else e1
        drift = 0 if _angle_mode else (-_ARROW_HW if cam_sync.angdiff_hw(dest_bearing, far) >= 0 else _ARROW_HW)
        want = (far + drift) & 0xFFFF

        if abs(cam_sync.angdiff_hw(want, _chg_expected)) > _TURNAROUND_SNAP_HW:
            _chg_target, _chg_phase = want, "CHARGE"
        else:
            # Outside the snap window -> plan a snap chain onto the axis, take step 1.
            chain = cam_sync.reorient_targets(_chg_expected, axis)
            if chain:
                _chg_target, _chg_phase = chain[0], "REORIENT %d" % len(chain)
            else:
                # On-axis but the tilt pushed `want` under the 45° budget: snap the far end
                # untilted this frame to keep charging (drops the drift bias, not the snap).
                _chg_target, _chg_phase = far, "CHARGE*"

        _chg_expected = _chg_target            # the snap lands here by the next game frame

    # Every callback: de-rotate the held target for the live camera and issue it.
    (msx, msy), _pred = cam_sync.charge_stick(_chg_target, csx, csy, CAM_PREDICT_STEPS)
    _set_main(msx, msy)

# ── memory read (emu thread only) ───────────────────────────────────
@event.on_frameadvance
def _read_state() -> None:
    global _player, _cur_x, _cur_z, _facing_hw, _have_state, _current_stage

    try:
        _current_stage = current_stage()
    except Exception:
        _current_stage = ""

    # Rebuild every frame: the player actor (and its gptr) is recreated across
    # stage transitions, so a cached Player would read a stale base after e.g.
    # entering the sea. Construction is just a pointer read — cheap to redo.
    try:
        p = Player()
        _player = p if p._valid else None
    except Exception:
        _player = None
    if _player is None:
        _have_state = False
        _reset_charge()   # drop the plan so it re-latches fresh once Link is valid again
        return

    try:
        _cur_x = _player.debug_x()
        _cur_z = _player.debug_z()
        _facing_hw = _player.angle_y
        _have_state = True
    except Exception:
        _have_state = False

    # Drive the superswim charge here on the emu thread (memory + controller must live on
    # the emu thread, same as the standalone ss_charge_* scripts did via on_frameadvance).
    # Arrow-swim needs a destination; "swim at current angle" only needs Link on the sea.
    on_sea = _current_stage == SEA_STAGE
    if _have_state and _armed and on_sea and (_angle_mode or _dest_set):
        try:
            _run_charge(_cur_x, _cur_z)
        except Exception:
            _reset_charge()
    else:
        _reset_charge()

# ── input + draw (host thread; safe while paused) ───────────────────
@event.on_hostupdate
def update() -> None:
    global _dest_x, _dest_z, _dest_set, _cal_shown
    global _armed, _angle_mode, _dest_prev, _angle_prev, _ARROW_HW, _anim

    _anim += 1
    cur_x = _cur_x
    cur_z = _cur_z
    facing_hw: Optional[int] = _facing_hw
    on_sea = _current_stage == SEA_STAGE
    have = _have_state

    # Mutual exclusion: the two mode checkboxes can't both be on. If both read checked,
    # the one just turned on this frame (its prev state was off) wins; force the other
    # off in the widget too so the UI reflects it. Host thread owns the widgets.
    dest_chk, angle_chk = _chk_dest.checked, _chk_angle.checked
    if dest_chk and angle_chk:
        if not _dest_prev:           # "swim to destination" was just checked -> it wins
            angle_chk = False
            _chk_angle.checked = False
        else:                        # "swim at current angle" was just checked -> it wins
            dest_chk = False
            _chk_dest.checked = False
    _dest_prev, _angle_prev = dest_chk, angle_chk

    # Latch UI state for the emu-thread charge to read.
    _armed = dest_chk or angle_chk
    _angle_mode = angle_chk

    # Live arrow-swim modifier from the slider (deg -> halfword), with a signed readout.
    tilt_deg = _sld_arrow.value
    _ARROW_HW = int(round(tilt_deg * 65536 / 360.0))
    _txt_arrow.set("Modifier:  %+.1f°" % tilt_deg if abs(tilt_deg) >= 0.05
                   else "Modifier:  0.0°")

    show_proj = _chk_proj.checked
    g_left, g_right, g_top, g_bottom = _padding()

    # ── responsive: track the canvas's live size ──────────────────
    try:
        cw = float(_canvas.width)
        ch = float(_canvas.height)
    except (AttributeError, TypeError):
        cw, ch = float(CANVAS_W), float(MAP_ZONE_H)
    _view.set_viewport(cw, ch)

    # ── zoom (always available, even when the sea overlay is shown) ──
    wheel = _canvas.take_wheel()
    if wheel:
        mx, my, inside = _canvas.mouse_pos()
        if inside:
            _view.zoom(wheel, mx, my)

    # ── show/hide calibration widgets (only on change) ────────────
    if _chk_cal.checked != _cal_shown:
        _cal_shown = _chk_cal.checked
        _set_calibration_visible(_cal_shown)

    if have and on_sea:
        # ── button: snap destination to Link's current position ───
        if _btn_here.clicked:
            _dest_x, _dest_z = cur_x, cur_z
            _dest_set = True

        # ── click: set destination ────────────────────────────────
        click = _canvas.take_click()
        if click is not None:
            ix, iy = _view.canvas_to_img(click[0], click[1])
            _dest_x = WORLD_LEFT + (ix - g_left) / (g_right - g_left) * (WORLD_RIGHT - WORLD_LEFT)
            _dest_z = WORLD_TOP  + (iy - g_top)  / (g_bottom - g_top) * (WORLD_BOTTOM - WORLD_TOP)
            _dest_set = True
    else:
        # consume inputs so they don't queue up
        _canvas.take_click()

    # ── world → canvas helpers (live padding) ─────────────────────
    def w2c(wx: float, wz: float) -> Tuple[float, float]:
        px = g_left + (wx - WORLD_LEFT) / (WORLD_RIGHT - WORLD_LEFT) * (g_right - g_left)
        py = g_top  + (wz - WORLD_TOP)  / (WORLD_BOTTOM - WORLD_TOP) * (g_bottom - g_top)
        return _view.img_to_canvas(px, py)

    # Charging this frame? Mirror the emu-thread gate (in _read_state) so the on-canvas
    # swim indicator matches when the controller is actually being driven.
    charging = bool(_armed and have and on_sea and (_angle_mode or _dest_set))
    _reorienting = charging and _chg_phase.startswith("REORIENT")
    _swim_rgb = (C_REORI if _reorienting else C_SWIM) & 0x00FFFFFF

    # ── draw ──────────────────────────────────────────────────────
    _canvas.clear()

    # sea backdrop behind/around the chart, with a subtle vignette
    _canvas.rect_filled((0.0, 0.0), (cw, ch), C_SEA)
    _canvas.rect((6.0, 6.0), (cw - 6.0, ch - 6.0), C_SEA_DK, 0.0, 12.0)

    pos, size, src = _view.src_crop()
    if size[0] > 0 and size[1] > 0:
        _canvas.image(MAP_PATH, pos, size, src=src)
        # decorative double frame hugging the rendered chart
        x0, y0 = pos
        x1, y1 = pos[0] + size[0], pos[1] + size[1]
        _canvas.rect((x0 - 5, y0 - 5), (x1 + 5, y1 + 5), C_FRAME_O, 3.0, 6.0)
        _canvas.rect((x0 - 1, y0 - 1), (x1 + 1, y1 + 1), C_FRAME_I, 2.0, 2.0)
        # corner accents
        cl = 14.0
        for (ax, ay, sx, sy) in (
            (x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)
        ):
            _canvas.line((ax - 5 * sx, ay - 5 * sy), (ax - 5 * sx + cl * sx, ay - 5 * sy), C_CORNER, 2.5)
            _canvas.line((ax - 5 * sx, ay - 5 * sy), (ax - 5 * sx, ay - 5 * sy + cl * sy), C_CORNER, 2.5)

    if have and on_sea:
        # Link dot + facing arrow
        lx, ly = w2c(cur_x, cur_z)
        # swim indicator: two staggered sonar pings expanding from Link + a coloured ring,
        # so "charging" reads at a glance without any text (green = charge, amber = reorient).
        if charging:
            PING = 36.0
            for off in (0.0, 0.5):
                p = ((_anim + off * PING) % PING) / PING
                a = int(170 * (1.0 - p))
                if a > 0:
                    _canvas.circle((lx, ly), 7.0 + p * 24.0, (a << 24) | _swim_rgb, 2.0)
            _canvas.circle((lx, ly), 8.5, 0xFF000000 | _swim_rgb, 2.0)
        _canvas.circle_filled((lx, ly), 6, C_LINK)
        if facing_hw is not None:
            rad = math.radians(mathutils.halfword_to_deg(facing_hw))
            _canvas.line((lx, ly), (lx + 30 * math.sin(rad), ly + 30 * math.cos(rad)), C_TRAVEL, 2.0)

        # Destination + projection
        if _dest_set:
            dx, dy = w2c(_dest_x, _dest_z)
            R = 8.0
            _canvas.line((dx - R, dy - R), (dx + R, dy + R), C_DEST, 2.0)
            _canvas.line((dx + R, dy - R), (dx - R, dy + R), C_DEST, 2.0)
            _canvas.circle((dx, dy), R, C_DEST, 2.0)

            if show_proj and facing_hw is not None:
                need  = int(mathutils.dist2d(cur_x, cur_z, _dest_x, _dest_z) / SS_SPEED) + 2
                steps = max(1, min(PROJECTION_MAX_STEPS, need + PROJECTION_BUFFER_STEPS))
                pts   = ss_projection_points(cur_x, cur_z, facing_hw,
                                             _dest_x, _dest_z, steps, SS_SPEED,
                                             PROJECTION_BUFFER_STEPS)
                cpts = [w2c(wx, wz) for wx, wz in pts]
                for i in range(len(cpts) - 1):
                    _canvas.line(cpts[i], cpts[i + 1], C_PROJ, 1.5)
                for pt in cpts[1:]:
                    _canvas.circle_filled(pt, 2, C_PROJ)

    # ── "waiting for sea" overlay (drawn on top of the dimmed map) ──
    if not on_sea:
        _canvas.rect_filled((0.0, 0.0), (cw, ch), 0xBB000000)
        stage_label = _current_stage if _current_stage else "—"
        # canvas text() has no font-size; draw at multiple offsets for a bolder look
        label1 = "Waiting for Sea..."
        label2 = f"stage: {stage_label}"
        # ~7.5px per char at default font size; scale offsets to simulate larger text
        hw1 = len(label1) * 7.5 / 2.0
        hw2 = len(label2) * 7.5 / 2.0
        tx1 = cw / 2.0 - hw1
        ty1 = ch / 2.0 - 18.0
        tx2 = cw / 2.0 - hw2
        ty2 = ch / 2.0 + 10.0
        # draw heading text with 2-px shadow offsets to simulate larger/bolder
        for ox, oy in ((2, 2), (1, 2), (2, 1)):
            _canvas.text((tx1 + ox, ty1 + oy), 0xFF000000, label1)
        for ox, oy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            _canvas.text((tx1 + ox, ty1 + oy), 0xFFFFFFFF, label1)
        _canvas.text((tx2, ty2), 0xFF888899, label2)

    # ── swim status pill (top-left of the canvas; fixed size, never resizes the window) ──
    if charging:
        label = _chg_phase or "SWIM"
        pill_w = 30.0 + len(label) * 7.0
        _canvas.rect_filled((6, 6), (6 + pill_w, 25), 0xCC10101A, 5.0)
        _canvas.rect((6, 6), (6 + pill_w, 25), 0x66000000 | _swim_rgb, 5.0, 1.0)
        # blinking LED so it reads as "live"
        if (_anim // 12) % 2 == 0:
            _canvas.circle_filled((16, 15.5), 4.0, 0xFF000000 | _swim_rgb)
        else:
            _canvas.circle((16, 15.5), 4.0, 0xFF000000 | _swim_rgb, 1.5)
        _canvas.text((26, 10), 0xFFEEEEEE, label)

    _canvas.commit()

    # ── status line (native widget below the map) ─────────────────
    # NOTE: charge state is shown ON the canvas (pill + ping) on purpose -- appending it
    # here lengthened the QLabel and grew the whole window. Keep this line fixed-width-ish.
    if not on_sea:
        _status.set(f"Not on Great Sea  (stage: {_current_stage or '—'})")
    elif _dest_set and have:
        atd = mathutils.angle2d_hw(cur_x, cur_z, _dest_x, _dest_z)
        qd  = mathutils.dist2d(cur_x, cur_z, _dest_x, _dest_z) / 100_000.0
        _status.set(f"Dest  X={_dest_x:.0f}  Z={_dest_z:.0f}     "
                    f"Angle to dest={atd}     Quadrants={qd:.4f}")
    else:
        _status.set("Click the map to set a destination")
