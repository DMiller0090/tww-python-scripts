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
from typing import Optional, Tuple
from dolphin import event, gui
from ww import mathutils
from ww.actors.player import Player
from ww.context.context import set_region
from ww.context.detect import detect_region
from ww.game import current_stage

# ── configuration ───────────────────────────────────────────────────
PROJECTION_MAX_STEPS    = 4000   # safety cap; actual length tracks distance-to-dest
PROJECTION_BUFFER_STEPS = 6      # extra steps carried straight through the destination
SS_SPEED                = 7000.0

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

SEA_STAGE = "sea"

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
        return

    try:
        _cur_x = _player.debug_x()
        _cur_z = _player.debug_z()
        _facing_hw = _player.angle_y
        _have_state = True
    except Exception:
        _have_state = False

# ── input + draw (host thread; safe while paused) ───────────────────
@event.on_hostupdate
def update() -> None:
    global _dest_x, _dest_z, _dest_set, _cal_shown

    cur_x = _cur_x
    cur_z = _cur_z
    facing_hw: Optional[int] = _facing_hw
    on_sea = _current_stage == SEA_STAGE
    have = _have_state

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

    _canvas.commit()

    # ── status line (native widget below the map) ─────────────────

    if not on_sea:
        _status.set(f"Not on Great Sea  (stage: {_current_stage or '—'})")
    elif _dest_set and have:
        atd = mathutils.angle2d_hw(cur_x, cur_z, _dest_x, _dest_z)
        qd  = mathutils.dist2d(cur_x, cur_z, _dest_x, _dest_z) / 100_000.0
        _status.set(f"Dest  X={_dest_x:.0f}  Z={_dest_z:.0f}     "
                    f"Angle to dest={atd}     Quadrants={qd:.4f}")
    else:
        _status.set("Click the map to set a destination")
