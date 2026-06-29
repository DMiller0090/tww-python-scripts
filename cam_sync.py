"""cam_sync.py - shared camera-prediction + de-rotation for superswim charge scripts.

The control stick is camera-relative, so the stick that charges toward a WORLD direction depends on
csangle. Reading csangle live is a frame stale when the camera is rotating, so the charge drifts.
This module:
  - PREDICTS next-frame csangle from the live camera internals + the held C-stick (the bit-exact
    omega model: cam_target += omega_cmd(csx,csy); cam_yaw += int((s16)(target-yaw)/2);
    csangle = cam_yaw + 0x8000), and
  - inverse-looks-up the full-deflection main stick for a desired world angle from the COMPLETE
    live stick grid (stick_angle_full.csv), indexed by angle with a bisect.

charge_stick(world_angle_hw, csx, csy) returns the (sx,sy) that charges toward world_angle_hw given
where the camera WILL be, so the charge holds a fixed world axis no matter how the camera spins.

Data tables (omega_table_full.csv, stick_angle_table.csv) now ship inside the `superswim` pip
package, under superswim/superswim/tables/. They are resolved at import time by _resolve_tables()
below; see that function for the search order.
"""
import os
import csv
import bisect

from dolphin import memory
from ww import camera

_HERE = os.path.dirname(os.path.abspath(__file__))
FULL_DEFLECT_MIN = 0.98          # stick_dist threshold for "full deflection" (max-charge snap)


def _candidate_table_dirs():
    """Directories that might hold the superswim data tables, best first."""
    dirs = []
    # 1) the sibling `superswim` repo's package dir, found by walking up from this script.
    #    cwd-immune, and the one mechanism that works inside Dolphin's embedded interpreter
    #    (which can't see the system-Python editable install).
    cur = _HERE
    for _ in range(6):
        dirs.append(os.path.join(cur, "superswim", "superswim", "tables"))
        cur = os.path.dirname(cur)
    # 2) the installed `superswim` package, if the running interpreter can import it.
    try:
        import importlib.util
        spec = importlib.util.find_spec("superswim")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            dirs.append(os.path.join(loc, "tables"))
            dirs.append(os.path.join(loc, "superswim", "tables"))
    except Exception:
        pass
    # 3) legacy: the old drop in the project root (parent of the scripts dir).
    dirs.append(os.path.abspath(os.path.join(_HERE, "..")))
    return dirs


def _resolve_tables():
    """(omega_path, stick_path) for the first candidate dir holding both, else (None, None).
    The stick table was renamed package-side (stick_angle_table.csv); the old name is a fallback."""
    for d in _candidate_table_dirs():
        omega = os.path.join(d, "omega_table_full.csv")
        stick = os.path.join(d, "stick_angle_table.csv")
        if not os.path.exists(stick):
            stick = os.path.join(d, "stick_angle_full.csv")   # legacy name
        if os.path.exists(omega) and os.path.exists(stick):
            return omega, stick
    return None, None

# csangle pointer chain: [[0x803AD380]+0x34] = camera instance; cam_yaw @ +0x252, cam_target @ +0x5F2.
_CAM_ROOT = 0x803AD380
_YAW_OFF = 0x252
_TARGET_OFF = 0x5F2

_OMEGA = {}
_ANG, _STK, _N = [], [], 0
loaded = False
load_error = None


def _load():
    global _OMEGA, _ANG, _STK, _N, loaded, load_error
    omega_path, stick_path = _resolve_tables()
    if not omega_path:
        load_error = ("superswim tables not found (omega_table_full.csv / stick_angle_table.csv); "
                      "searched: " + " | ".join(_candidate_table_dirs()))
        loaded = False
        return
    try:
        with open(omega_path) as f:
            _OMEGA = {(int(r["csx"]), int(r["csy"])): int(r["omega"]) for r in csv.DictReader(f)}
        by_ang = {}
        with open(stick_path) as f:
            for r in csv.DictReader(f):
                if float(r["stick_dist"]) >= FULL_DEFLECT_MIN:
                    by_ang.setdefault(int(r["angle"]), (int(r["sx"]), int(r["sy"])))
        _ANG = sorted(by_ang)
        _STK = [by_ang[a] for a in _ANG]
        _N = len(_ANG)
        loaded = _N > 0 and len(_OMEGA) > 0
    except Exception as e:
        load_error = str(e)
        loaded = False


_load()


def s16(x):
    x &= 0xFFFF
    return x - 0x10000 if x >= 0x8000 else x


def omega_cmd(csx, csy):
    o = _OMEGA.get((csx & 0xFF, csy & 0xFF))
    if o is not None:
        return o
    return _OMEGA.get((csx & 0xFF, 128), 0)      # csy fallback to the csx-only column


def _cam_instance():
    return memory.read_u32(memory.read_u32(_CAM_ROOT) + 0x34)


def predict_csangle(csx, csy, steps=1):
    """csangle `steps` frames ahead given the C-stick currently held (which drives the rotation).
    Falls back to the live read if the camera internals aren't readable."""
    try:
        inst = _cam_instance()
        yaw = memory.read_u16(inst + _YAW_OFF)
        target = memory.read_u16(inst + _TARGET_OFF)
    except Exception:
        return camera.cs_angle_halfword()
    if steps <= 0:
        return (yaw + 0x8000) & 0xFFFF
    om = omega_cmd(csx, csy)
    for _ in range(steps):
        target = (target + om) & 0xFFFF
        yaw = (yaw + int(s16(target - yaw) / 2)) & 0xFFFF
    return (yaw + 0x8000) & 0xFFFF


def stick_for_angle_hw(stick_hw):
    """Nearest full-deflection (sx,sy) whose mMainStickAngle == stick_hw (circular bisect)."""
    if _N == 0:
        return 128, 128
    stick_hw &= 0xFFFF
    i = bisect.bisect_left(_ANG, stick_hw)
    best, bd = (128, 128), 1 << 30
    for j in (i - 1, i):
        a = _ANG[j % _N]
        d = abs(s16(a - stick_hw))
        if d < bd:
            bd, best = d, _STK[j % _N]
    return best


def charge_stick(world_angle_hw, csx, csy, steps=1):
    """Full-deflection (sx,sy) that charges toward world_angle_hw given the PREDICTED camera.
    A snap sets facing := m34E8 == world_angle_hw, so pass the world facing you want to snap to.
    Returns ((sx,sy), predicted_csangle)."""
    pred = predict_csangle(csx, csy, steps)
    stick_hw = (world_angle_hw - pred - 0x8000) & 0xFFFF
    return stick_for_angle_hw(stick_hw), pred


# ---- instant-turnaround facing reorientation (arrow-swim philosophy, KNOWLEDGE §5.3) -------------
# Arrow drift is PERPENDICULAR to the charge axis (= Link's facing line), so to arrow-swim a world
# direction the facing must sit ON that axis. You get there with instant-turnaround SNAPS, never a
# gradual turn: a snap fires iff |m34E8 - facing| > 0x6000 (135 deg) and sets facing := m34E8 (and
# it charges). A target <135 deg away can't be reached in one snap, so BFS walks facing through
# intermediate >135 deg snaps. Pure world-angle math (camera-independent); charge_stick() de-rotates
# each chosen target into the actual stick.
SNAP_HW = 0x6000                 # 135 deg backward cone (DIR_BACKWARD instant snap fires iff >this)
_SNAP_MARGIN = 0x600             # require snaps comfortably past 135 deg (~8.4) so they fire even if
                                 # the live facing drifts a hair off the planned gate
_GATE_HW = 2731                  # facing-graph resolution (~15 deg)


def angdiff_hw(a, b):
    return s16(a - b)


def reorient_targets(facing_hw, axis_hw, tol_hw=1820, max_depth=6):
    """Snap-chain (list of WORLD facing targets, hw) that walks `facing_hw` onto the `axis_hw` LINE
    (either end) using only >135 deg snaps. [] if already on-axis, None if unreachable. The live
    caller snaps to chain[0] each frame (via charge_stick) and re-plans from the new facing."""
    facing_hw &= 0xFFFF
    e0, e1 = axis_hw & 0xFFFF, (axis_hw + 0x8000) & 0xFFFF
    on_axis = lambda f: abs(angdiff_hw(f, e0)) <= tol_hw or abs(angdiff_hw(f, e1)) <= tol_hw
    if on_axis(facing_hw):
        return []
    from collections import deque
    n_gates = 0x10000 // _GATE_HW
    gates = [(g * _GATE_HW) & 0xFFFF for g in range(n_gates)]
    start = (round(facing_hw / _GATE_HW) * _GATE_HW) & 0xFFFF
    seen = {start: []}
    q = deque([start])
    while q:
        f = q.popleft()
        path = seen[f]
        if len(path) >= max_depth:
            continue
        for g in gates:
            if abs(angdiff_hw(g, f)) <= SNAP_HW + _SNAP_MARGIN:   # must be a comfortable >135 snap
                continue
            if g in seen:
                continue
            seen[g] = path + [g]
            if on_axis(g):
                return seen[g]
            q.append(g)
    return None
