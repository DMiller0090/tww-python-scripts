"""land_steer.py - LAND camera-relative stick chooser, backed by the tww_sim port.

`cam_sync` is the SWIM module: it predicts csangle from the live-captured subject-camera
`omega_table_full.csv` and inverts a world bearing through the advancewith-captured
`stick_angle_table.csv`. Both are the wrong tool on land, and both are wrong by a lot:

  * CAMERA. On land csangle is owned by `dCamera_c::manualCamera` (mode 12), whose yaw target
    moves at `styleParam[24]` deg/frame (MM03 = 8.0) through the `rationalBezierRatio` S-curve,
    and whose view globe chases that target at the `styleParam[21]` cushion (MM03 = 0.66). The
    swim subject cam runs ~3 deg/frame with a 0.5 chase, so a full C-stick pan predicted with the
    swim table comes out 2.67x too slow -- ~5 deg of aim error for every frame of camera motion.
  * STICK. The full-deflection cells of `stick_angle_table.csv` were captured through the
    advancewith pipe, which mis-injects near-full OFF-AXIS sticks; its off-axis angles carry a
    ~160 s16 artifact. The decomp-ported decode (PADClamp octagon + JUTGamePad::CStick::update,
    `tww_sim.core.mathlib.main_stick_decode`) is the clean-DTM truth. Measured over 1024 bearings,
    the table inverse lands p95 155 hw / max 278 hw off the bearing it was asked for; the ported
    inverse lands p95 75 hw / max 84 hw (that residue is the stick byte grid's own quantum).

So this module does the same job on the LAND engine, reusing tww_sim rather than re-deriving it:
the bezier target step and the s16 chase from `tww_sim.core.camera`, the clamp-aware bearing
inverse from `tww_sim.land.plan_land`. Every camera constant that CAN be read from the running
game is read from it (style index -> styleParam row), so the module stays stage-agnostic; the
baked MM03 row is only a fallback for when the live row fails its sanity check.

    cam = read_cam()
    (sx, sy), pred = stick_for_world_bearing(bearing_hw, csx, csy, cam=cam)

`AimMonitor` closes the loop: it remembers the bearing each stick was meant to hit and scores it
against the csangle actually in force `horizon` frames later, so the prediction horizon is
something you READ off the HUD instead of asserting.

Import is soft -- `loaded` / `load_error` mirror cam_sync, so a caller can fall back.
"""
from __future__ import annotations

import math
import os
import sys
from collections import deque

# -- locate + import tww_sim ------------------------------------------------
# The sibling repo is speedrunning/tww_sim/ and the package is speedrunning/tww_sim/tww_sim/, so
# the repo ROOT is what goes on sys.path. Walking up from this file is cwd-immune and is the one
# mechanism that works inside Dolphin's embedded interpreter (which cannot see a system-Python
# editable install). Same idea as cam_sync._candidate_table_dirs, one level up.
_HERE = os.path.dirname(os.path.abspath(__file__))

loaded = False
load_error = None


def _candidate_repo_roots():
    roots = []
    cur = _HERE
    for _ in range(6):
        roots.append(os.path.join(cur, "tww_sim"))       # sibling repo root
        cur = os.path.dirname(cur)
    return roots


def _import_sim():
    """Import the tww_sim pieces we need. Prefers an already-importable package (editable
    install), else puts the first sibling repo root that looks right onto sys.path."""
    try:
        import tww_sim  # noqa: F401
    except ImportError:
        for root in _candidate_repo_roots():
            if os.path.isfile(os.path.join(root, "tww_sim", "__init__.py")):
                if root not in sys.path:
                    sys.path.insert(0, root)
                break
        else:
            raise ImportError("tww_sim package not found; searched: "
                              + " | ".join(_candidate_repo_roots()))
    from tww_sim.core.camera import cam_angle, cam_bezier
    from tww_sim.core.mathlib import deg_to_s16, main_stick_decode
    from tww_sim.land.plan_land import stick_for_bearing
    return cam_bezier, cam_angle, deg_to_s16, main_stick_decode, stick_for_bearing


try:
    _CB, _CA, deg_to_s16, main_stick_decode, _stick_for_bearing = _import_sim()
    loaded = True
except Exception as _e:                      # a Dolphin script must never hard-fail on import
    load_error = "%s: %s" % (type(_e).__name__, _e)
    _CB = _CA = deg_to_s16 = main_stick_decode = _stick_for_bearing = None

try:
    from dolphin import memory
except ImportError:                          # importable outside Dolphin for offline checks
    memory = None


# -- live dCamera_c ---------------------------------------------------------
# camera_class = [[0x803AD380] + 0x34];  dCamera_c = camera_class + 0x244.
CAM_CHAIN = 0x803AD380
CAM_OBJ_OFF = 0x34
DCAM_OFF = 0x244

F_DIR_INC = 0x00E     # s16  mDirection.inc -- the COMMITTED view-globe yaw (csangle = inc - 0x8000)
F_ANGLE_Y = 0x06C     # u16  mAngleY == dCam_getControledAngleY() (what the stick decode adds to)
F_MODE = 0x13C        # s32  mCurMode (12 = manualCamera; 0 = the 1-frame followCamera blip)
F_CX = 0x16C          # f32  mStickCPosX -- last updatePad's decoded C-stick X (diagnostic only)
F_WGLOB_INC = 0x3AE   # s16  mWork globe inc -- the manual camera's yaw TARGET (cam_target)
F_STYLE = 0x510       # u32  mCurStyle (57 = MM03, the land walk style)

MODE_MANUAL = 12

# styles[] rows: 0x803485AC + idx*0x84; u32 alg, f32 styleParam[30] @+0x04, u16 flags @+0x7C,
# char name[8] @+0x80. Read from the live JP binary -- d_cam_style.cpp's source rows DO NOT match
# what shipped (knowledge/mechanics/land-camera.md, "The source style table lies").
STYLE_TABLE = 0x803485AC
STYLE_STRIDE = 0x84
STYLE_PARAM0 = 0x04
STYLE_NAME = 0x80
P_CUSHION = 21        # styleParam[21] - view-globe chase cushion (yaw/pitch/radius), MM03 0.66
P_YAW_SCALE = 24      # styleParam[24] - C-stick X -> cam_target degrees/frame, MM03 8.0

# Fallback when the live style row fails its sanity check: land_cam.STYLE_MM03.
FALLBACK_CUSHION = 0.66
FALLBACK_SCALE = 8.0

# The game acts on a stick INPUT_DELAY frames after delivery (tww_sim.land.state.INPUT_DELAY = 2),
# and setStickData reads csangle as of the START of that frame -- so the csangle a stick written
# now gets decoded against is the one committed 2 frames from now. ss_navigator's 1 is the SWIM
# convention and is a frame short here; at a full land pan that frame is worth 8 deg. Read the
# truth off AimMonitor rather than trusting this number.
PREDICT_STEPS = 2

# Which pad the CAMERA consumes. It runs one frame ahead of physics: land-camera.md has it reading
# "the polled g_mDoCPd pad, the attention's delay-1 stage", and the attention reads L one frame
# ahead of the delay-2 physics (tww_sim.land.state.step). So the camera advance that produces the
# csangle a stick written at frame t is decoded against consumes the pads from t-1 and t -- both
# already OBSERVED. That is what `push_cstick` exploits: with the defaults there is no extrapolation
# left in the prediction at all, so slamming the C-stick to the opposite deflection costs nothing.
# Without a pad history the module falls back to assuming the stick is HELD, which is exact while
# it is held and wrong for one frame per change (measured: a full reversal spikes the aim 14.6 deg
# for a single frame, against a 0.20 deg steady floor).
CAM_INPUT_DELAY = 1

# `stick_for_bearing` drops into a 529-cell clamped-decode scan whenever the octagon clamp moves
# the analytic byte (2.2 ms) -- which is most off-axis bearings, i.e. 13% of a 60 fps frame, every
# frame, on the emu thread. Quantizing the REQUEST to 16 hw caps the distinct cache keys at 4096
# (inside the inverse's own lru_cache of 16384), so a run warms up and then costs ~0.2 us. Measured
# price of the quantum over 262 bearings: mean error 35.5 -> 35.9 hw, max 91 -> 93 hw. Free.
BEARING_QUANTUM = 16
MSD_QUANTUM = 1.0 / 64.0


def _s16(x):
    x = int(x) & 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


# -- C-stick history --------------------------------------------------------
# Newest last; entry -1 is the pad delivered on the frame `push_cstick` was last called.
_CS_HIST = deque(maxlen=8)
last_extrapolated = False       # diagnostic: the last prediction had to guess a pad it cannot see


def push_cstick(csx, csy):
    """Record the C-stick delivered THIS frame. Call once per frame, unconditionally, BEFORE
    predicting -- including on frames where no stick goes out, since a gap would silently shift
    every later entry onto the wrong frame."""
    if _CB is not None:
        _CS_HIST.append(_CB.cstick_normalize(int(csx), int(csy))[0])


def reset_cstick():
    """Drop the history (stage change, savestate load -- anything that breaks frame continuity)."""
    _CS_HIST.clear()


def _posx_schedule(csx, csy, steps):
    """The normalized C-stick X the camera will consume on each of the `steps` prediction steps.

    Step k advances the camera over frame t+k, which consumes the pad delivered at
    t + k - CAM_INPUT_DELAY. Relative to the newest history entry (frame t, index -1) that is
    index k - CAM_INPUT_DELAY - 1. A non-negative index is a pad the caller cannot have seen yet,
    so it falls back to the current one (assume the hold continues) and sets `last_extrapolated`;
    so does a history that has not warmed up, or a caller that never pushes."""
    global last_extrapolated
    cur = _CB.cstick_normalize(int(csx), int(csy))[0]
    out = []
    last_extrapolated = False
    for k in range(steps):
        idx = k - CAM_INPUT_DELAY - 1
        if idx < 0 and len(_CS_HIST) >= -idx:
            out.append(_CS_HIST[idx])
        else:
            out.append(cur)
            last_extrapolated = True
    return out


# -- style row --------------------------------------------------------------
_STYLE_CACHE = {}


def style_params(idx):
    """(cushion, yaw_scale, name) for style `idx`, read live and sanity-checked. Falls back to the
    baked MM03 row (name None) when the row is unreadable or out of range -- a wrong table base
    would otherwise feed the integrator garbage in silence."""
    hit = _STYLE_CACHE.get(idx)
    if hit is not None:
        return hit
    cush, scale, name = FALLBACK_CUSHION, FALLBACK_SCALE, None
    if memory is not None:
        try:
            row = STYLE_TABLE + int(idx) * STYLE_STRIDE
            c = memory.read_f32(row + STYLE_PARAM0 + 4 * P_CUSHION)
            s = memory.read_f32(row + STYLE_PARAM0 + 4 * P_YAW_SCALE)
            if 0.0 < c <= 1.0 and 0.0 < s <= 90.0:
                raw = memory.read_bytes(row + STYLE_NAME, 8)
                cush, scale = float(c), float(s)
                name = raw.split(b"\0")[0].decode("ascii", "replace") or None
        except Exception:
            pass
    _STYLE_CACHE[idx] = (cush, scale, name)
    return _STYLE_CACHE[idx]


def forget_styles():
    """Drop the style-row cache (call on a stage/room change if a style index is ever reused)."""
    _STYLE_CACHE.clear()


# -- live camera snapshot ---------------------------------------------------
def read_cam():
    """The land camera's steering state, or None if the pointer chain is not readable.

    `dmc` is set when the committed mAngleY disagrees with `mDirection.inc - 0x8000`: the DMC
    system (frozen-yaw main-stick mode) is holding csangle, so the integrator below does not
    describe it and the prediction degrades to "frozen". It is DETECTED, not modeled -- no
    validated capture has ever caught DMC firing (knowledge/mechanics/land-camera.md#open)."""
    if memory is None:
        return None
    try:
        base = memory.read_u32(memory.read_u32(CAM_CHAIN) + CAM_OBJ_OFF) + DCAM_OFF
        yaw = memory.read_u16(base + F_DIR_INC)
        csangle = memory.read_u16(base + F_ANGLE_Y)
        cam = {
            "base": base,
            "mode": memory.read_u32(base + F_MODE),
            "style": memory.read_u32(base + F_STYLE),
            "yaw": yaw,
            "target": memory.read_u16(base + F_WGLOB_INC),
            "csangle": csangle,
            "cx": memory.read_f32(base + F_CX),
        }
    except Exception:
        return None
    cam["dmc"] = ((yaw - 0x8000) & 0xFFFF) != csangle
    cam["cushion"], cam["scale"], cam["style_name"] = style_params(cam["style"])
    return cam


def predict_csangle(csx=128, csy=128, steps=None, cam=None):
    """csangle `steps` frames ahead (None = the module's PREDICT_STEPS, read at CALL time so the
    constant stays tunable from a driving script), holding the C-stick at (csx, csy).

    Each step consumes the C-stick the camera will actually have in hand for that frame
    (`_posx_schedule`), not the one held now. One frame of dCamera_c::manualCamera, as the port
    runs it:
        cam_target = trunc(182.0444 * (Degree(cam_target) + f32(ratio(cx) * styleParam[24])))
        view_yaw  += trunc((s16)(cam_target - view_yaw) * styleParam[21])
        csangle    = view_yaw - 0x8000
    Returns None when the camera is unreadable, and the live value unchanged when the camera is
    not in manualCamera (a followCamera blip -- 1 frame) or DMC has frozen the yaw."""
    if not loaded:
        return None
    if steps is None:
        steps = PREDICT_STEPS
    if cam is None:
        cam = read_cam()
    if cam is None:
        return None
    if cam["dmc"] or cam["mode"] != MODE_MANUAL or steps <= 0:
        return cam["csangle"]
    yaw, tgt = cam["yaw"], cam["target"]
    cush, scale = cam["cushion"], cam["scale"]
    for posx in _posx_schedule(csx, csy, int(steps)):
        tgt = _CB.step_cam_target(tgt, posx, scale)
        yaw = (yaw + _CA.sang_mul(_s16(tgt - yaw), cush)) & 0xFFFF
    return (yaw - 0x8000) & 0xFFFF


# -- the inverse ------------------------------------------------------------
def stick_for_world_bearing(world_hw, csx=128, csy=128, steps=None, msd=1.0, cam=None):
    """((sx, sy), predicted_csangle) -- the stick whose decoded walk target `m34E8 = m34DC + csangle`
    points at world bearing `world_hw`, against the csangle that will be in force when the game
    reads it. `msd` (0..1) is the requested mStickDistance: 1.0 = full deflection (walk cap 17 u/f),
    lower creeps at 17*msd^2. Returns ((128, 128), None) if the sim or the camera is unavailable."""
    if not loaded:
        return (128, 128), None
    pred = predict_csangle(csx, csy, steps, cam)
    if pred is None:
        return (128, 128), None
    m34dc = (int(world_hw) - pred) & 0xFFFF
    q = (int(round(m34dc / float(BEARING_QUANTUM))) * BEARING_QUANTUM) & 0xFFFF
    qmsd = min(1.0, max(0.0, round(float(msd) / MSD_QUANTUM) * MSD_QUANTUM))
    # csangle=0: the inverse's own coordinate IS the camera-relative want, which is what `q` holds.
    return _stick_for_bearing(q, 0, qmsd), pred


def achieved_bearing(sx, sy, csangle):
    """The world bearing a stick ACTUALLY asks for against `csangle`: m34E8 = (angle + 0x8000) +
    csangle, through the decomp-ported clamped decode. None for a neutral stick."""
    if not loaded:
        return None
    ang, _msd = main_stick_decode(int(sx), int(sy))
    if ang is None:
        return None
    return (ang + 0x8000 + int(csangle)) & 0xFFFF


# -- closing the loop -------------------------------------------------------
class AimMonitor:
    """Scores the whole chain against reality, so the horizon is measured and not assumed.

    Per frame: `tick(csangle)` first, then `submit(...)` if a stick went out. `submit` records the
    bearing a stick was meant to hit; `horizon` frames later `tick` scores it against the csangle
    actually in force by then (AIM error) and scores that frame's csangle prediction against the
    csangle that actually arrived (MODEL error).

    A third number, TRACK, is the only end-to-end one: the angle between the direction Link
    actually moved and the bearing to the target. Feed it with `track()`.

    Reading them, and what each does NOT prove:
      * MODEL ~0 says the camera port is tracking the live camera -- the style row, the bezier and
        the cushion are all right. It says nothing about the horizon: it scores each prediction
        against its own horizon, so it reads ~0 for a wrong `PREDICT_STEPS` too.
      * AIM ~0.2 deg (the stick byte grid's own quantum) says the inverse round-trips: the stick
        sent does ask for the bearing wanted, against the csangle predicted. Same blind spot.
      * TRACK is what catches a wrong horizon, and only ACROSS runs: hold the C-stick through a
        long walk and note TRACK, then change `PREDICT_STEPS` and walk it again. The right value
        leaves TRACK at the aim floor; one frame out costs a full styleParam[24] of heading (8 deg
        on MM03) and visibly bends the path. Scoring the alternatives from a single run does not
        work -- a wrong horizon makes Link walk a pursuit curve, and re-attributing that curve to a
        neighbouring frame's bearing barely moves the number (tried; every candidate scored alike).
        With the C-stick centred TRACK is ~0 at any horizon, so probe it while panning or not at all.

    Entries are frame-INDEXED, not positional: a frame that sends no stick (Link at the target, or
    the camera briefly unreadable) simply leaves a gap, instead of shifting every later score onto
    the wrong frame."""

    def __init__(self, horizon=None, window=120):
        self.horizon = PREDICT_STEPS if horizon is None else int(horizon)
        self.window = int(window)
        self._frame = 0
        self._pending = deque()
        self._wants = {}          # frame -> wanted bearing, for track()'s horizon-aligned lookup
        self.aim = deque(maxlen=self.window)
        self.model = deque(maxlen=self.window)
        self.trk = deque(maxlen=self.window)

    def tick(self, csangle_now):
        """Advance one frame and score whatever was submitted `horizon` frames ago. Call once per
        frame, BEFORE this frame's submit()."""
        self._frame += 1
        if csangle_now is None:
            self._pending.clear()            # a gap in the csangle series invalidates the ages
            self._wants.clear()
            return
        while self._pending and (self._frame - self._pending[0][0]) > self.horizon:
            self._pending.popleft()
        if not self._pending or (self._frame - self._pending[0][0]) != self.horizon:
            return
        _f, want, stick, pred = self._pending.popleft()
        got = achieved_bearing(stick[0], stick[1], csangle_now)
        if got is not None:
            self.aim.append(abs(_s16(got - want)))
        if pred is not None:
            self.model.append(abs(_s16(int(csangle_now) - int(pred))))

    def submit(self, want_hw, stick, predicted_csangle):
        self._pending.append((self._frame, int(want_hw), tuple(stick), predicted_csangle))
        self._wants[self._frame] = int(want_hw)
        for f in [f for f in self._wants if f < self._frame - self.horizon - 2]:
            del self._wants[f]

    def track(self, dx, dz, dist=None, min_step=1.0):
        """Score the direction Link ACTUALLY moved (`dx`, `dz` since last frame) against the bearing
        the stick responsible for that motion was aiming at -- the one submitted `horizon` frames
        ago, not the bearing from where he now stands.

        Two things are dropped rather than averaged in, because neither carries a heading:
          * a step under `min_step` units (stopped, or gliding in) -- its direction is quantization
            noise;
          * the endgame, where the remaining `dist` is under four steps. That close, the bearing to
            the target swings further per frame than a walk can turn, so every sample is large no
            matter how good the aim was."""
        step = math.hypot(dx, dz)
        if deg_to_s16 is None or step < min_step:
            return
        if dist is not None and dist < 4.0 * step:
            return
        want = self._wants.get(self._frame - self.horizon)
        if want is None:
            return
        got = deg_to_s16(math.degrees(math.atan2(dx, dz)) % 360.0)
        self.trk.append(abs(_s16(got - want)))

    def reset(self):
        self._pending.clear()
        self._wants.clear()
        self.aim.clear()
        self.model.clear()
        self.trk.clear()

    @staticmethod
    def _summary(samples):
        if not samples:
            return None, None
        return sum(samples) / float(len(samples)), max(samples)

    def text(self):
        """One-line HUD summary: mean/max aim error, then mean camera-model error, in degrees."""
        a_mean, _ = self._summary(self.aim)
        m_mean, _ = self._summary(self.model)
        t_mean, _ = self._summary(self.trk)
        deg = 360.0 / 65536.0
        parts = ["h%d" % self.horizon]
        parts.append("aim %.2f" % (a_mean * deg) if a_mean is not None else "aim --")
        parts.append("cam %.2f" % (m_mean * deg) if m_mean is not None else "cam --")
        parts.append("track %.2f" % (t_mean * deg) if t_mean is not None else "track --")
        return "  ".join(parts) + " deg"
