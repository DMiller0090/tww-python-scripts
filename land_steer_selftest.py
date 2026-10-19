"""land_steer_selftest.py - offline proof that land_steer's live path reproduces the console.

Runs OUTSIDE Dolphin (`python land_steer_selftest.py`). It stubs `dolphin.memory` with the camera
state from tww_sim's 0-ULP land-camera oracle (`fixtures/courtyard_cam_oracle.json`, a chained
120-frame live capture) and drives land_steer's REAL `read_cam()` / `predict_csangle()` -- live
style-row read included -- over every mode-12 frame in the window, then scores the bearing inverse
against the decomp-ported clamped decode. The same numbers are run for the SWIM model `cam_sync`
uses, so the comparison in land_steer's docstring is reproducible rather than asserted.

Needs the sibling tww_sim repo checked out (land_steer finds it the same way at runtime).
"""
from __future__ import annotations

import bisect
import csv
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ---- fake RAM, installed before land_steer imports `dolphin` --------------------------------
CAM_CHAIN = 0x803AD380          # must match land_steer.CAM_CHAIN
CAM_OBJ = 0x91000000
DCAM = CAM_OBJ + 0x244
STYLE_TABLE, STRIDE, PARAM0, NAME_OFF = 0x803485AC, 0x84, 0x04, 0x80
ROW = STYLE_TABLE + 57 * STRIDE                     # style 57 = MM03, the land walk style

_F32 = {ROW + PARAM0 + 4 * 21: 0.6600000262260437,  # styleParam[21] chase cushion
        ROW + PARAM0 + 4 * 24: 8.0,                 # styleParam[24] deg/frame
        DCAM + 0x16C: 0.0}
_U32 = {CAM_CHAIN: CAM_OBJ - 0x34, CAM_OBJ: CAM_OBJ, DCAM + 0x13C: 12, DCAM + 0x510: 57}
_U16 = {DCAM + 0x00E: 0, DCAM + 0x3AE: 0, DCAM + 0x06C: 0x8000}
_BYTES = {ROW + NAME_OFF: b"MM03\0\0\0\0"}

_memory = types.SimpleNamespace(
    read_f32=lambda a: _F32[a], read_u32=lambda a: _U32[a], read_u16=lambda a: _U16[a],
    read_bytes=lambda a, n: _BYTES[a][:n],
)
_dolphin = types.ModuleType("dolphin")
_dolphin.memory = _memory
sys.modules.setdefault("dolphin", _dolphin)
sys.modules.setdefault("dolphin.memory", _memory)

import land_steer as LS  # noqa: E402

if not LS.loaded:
    raise SystemExit("land_steer could not import tww_sim: %s" % LS.load_error)

from tww_sim.core.camera.cam_bezier import cstick_normalize  # noqa: E402


def _sim_root():
    for root in LS._candidate_repo_roots():
        if os.path.isfile(os.path.join(root, "fixtures", "courtyard_cam_oracle.json")):
            return root
    raise SystemExit("tww_sim fixtures not found next to this repo")


def _put(yaw, target, csangle):
    _U16[DCAM + 0x00E] = yaw & 0xFFFF
    _U16[DCAM + 0x3AE] = target & 0xFFFF
    _U16[DCAM + 0x06C] = csangle & 0xFFFF


_NORM2RAW = {}
for _b in range(256):
    _NORM2RAW.setdefault(round(cstick_normalize(_b, 128)[0], 6), _b)


def _raw_csx(cx):
    """The raw C-stick byte whose decode is exactly `cx` (the oracle logs the decoded float)."""
    return _NORM2RAW.get(round(cx, 6))


def check_camera(root):
    """One-step csangle prediction over every mode-12 frame pair in the oracle."""
    fr = json.load(open(os.path.join(root, "fixtures", "courtyard_cam_oracle.json")))["frames"]
    ours, swim = [], []
    omega = {}
    with open(os.path.join(root, "tww_sim", "core", "tables", "omega_table_full.csv")) as f:
        for r in csv.DictReader(f):
            omega[(int(r["csx"]), int(r["csy"]))] = int(r["omega"])

    for k in range(1, len(fr)):
        if fr[k]["expect"]["mode"] != 12 or fr[k - 1]["expect"]["mode"] != 12:
            continue
        if fr[k].get("dup"):              # a duplicated frame: the camera did not run
            continue
        prev, cur = fr[k - 1]["expect"], fr[k]["expect"]
        csx = _raw_csx(fr[k]["pad"]["cx"])
        if csx is None:
            continue
        _put(prev["vc_inc"], prev["glob"][2], prev["csangle"])
        cam = LS.read_cam()
        assert cam and not cam["dmc"] and cam["style_name"] == "MM03", cam
        assert (cam["cushion"], cam["scale"]) == (0.6600000262260437, 8.0), cam
        ours.append(abs(LS._s16(LS.predict_csangle(csx, 128, steps=1, cam=cam) - cur["csangle"])))
        # the SWIM model cam_sync steers with, for comparison
        t = (prev["glob"][2] + omega.get((csx, 128), 0)) & 0xFFFF
        y = (prev["vc_inc"] + int(LS._s16(t - prev["vc_inc"]) / 2)) & 0xFFFF
        swim.append(abs(LS._s16(((y + 0x8000) & 0xFFFF) - cur["csangle"])))
    return ours, swim


def check_inverse(root, step=64):
    """How far the stick each model picks is from the bearing it was asked for, under the
    decomp-ported clamped decode (`main_stick_decode`) -- the clean-DTM truth."""
    path = os.path.join(root, "tww_sim", "core", "tables", "stick_angle_table.csv")
    by_ang = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if float(r["stick_dist"]) >= 0.98:        # cam_sync's FULL_DEFLECT_MIN
                by_ang.setdefault(int(r["angle"]), (int(r["sx"]), int(r["sy"])))
    angs = sorted(by_ang)
    stks = [by_ang[a] for a in angs]
    n = len(angs)

    def table_lookup(h):
        i = bisect.bisect_left(angs, h & 0xFFFF)
        best, bd = (128, 128), 1 << 30
        for j in (i - 1, i):
            d = abs(LS._s16(angs[j % n] - h))
            if d < bd:
                bd, best = d, stks[j % n]
        return best

    tbl, sim = [], []
    for h in range(0, 65536, step):
        for series, byte in ((tbl, table_lookup(h)),
                             (sim, LS._stick_for_bearing((h + 0x8000) & 0xFFFF, 0, 1.0))):
            ang, _ = LS.main_stick_decode(*byte)
            series.append(0x8000 if ang is None else abs(LS._s16(ang - h)))
    return tbl, sim


def check_saturated(root, frames=20):
    """The oracle window only pans gently (|cx| <= 0.52), where the two models nearly agree. This
    is the case that actually bites: a saturated C-stick, integrated from rest for `frames`."""
    omega = {}
    with open(os.path.join(root, "tww_sim", "core", "tables", "omega_table_full.csv")) as f:
        for r in csv.DictReader(f):
            omega[(int(r["csx"]), int(r["csy"]))] = int(r["omega"])
    _put(0, 0, 0x8000)
    cam = LS.read_cam()
    land = LS._s16(LS.predict_csangle(255, 128, steps=frames, cam=cam) - 0x8000)
    yaw, tgt = 0, 0
    for _ in range(frames):
        tgt = (tgt + omega.get((255, 128), 0)) & 0xFFFF
        yaw = (yaw + int(LS._s16(tgt - yaw) / 2)) & 0xFFFF
    swim = LS._s16(yaw)
    deg = 360.0 / 65536.0
    print("  full C-stick, %d frames from rest: land %d hw (%.1f deg), swim model %d hw (%.1f deg)"
          % (frames, land, land * deg, swim, swim * deg))
    print("  -> the swim model is %.2fx short; at steady state that is %.1f deg of aim error for"
          % (land / float(swim), (1456 - 546) * deg))
    print("     every frame the camera is panning.")


def check_schedule():
    """With a warm pad history and the default constants, the prediction must consume only pads
    that were actually OBSERVED -- that is what makes a sudden C-stick reversal free. A regression
    here (a schedule that reaches for the current pad instead) costs one frame of styleParam[24]
    every time the C-stick changes."""
    LS.reset_cstick()
    for csx in (200, 210, 220, 230):                  # four distinct frames of history
        LS.push_cstick(csx, 128)
    want = [cstick_normalize(220, 128)[0],            # frame t-1  (step 0, camera frame t)
            cstick_normalize(230, 128)[0]]            # frame t    (step 1, camera frame t+1)
    got = LS._posx_schedule(230, 128, LS.PREDICT_STEPS)
    print("  PREDICT_STEPS=%d CAM_INPUT_DELAY=%d -> consumes frames t-%d..t-%d, extrapolated=%s"
          % (LS.PREDICT_STEPS, LS.CAM_INPUT_DELAY, LS.CAM_INPUT_DELAY,
             LS.CAM_INPUT_DELAY - LS.PREDICT_STEPS + 1, LS.last_extrapolated))
    assert got == want, (got, want)
    assert not LS.last_extrapolated, "the default horizon should need no unseen pad"

    LS.reset_cstick()                                  # cold history -> documented hold fallback
    LS._posx_schedule(230, 128, LS.PREDICT_STEPS)
    assert LS.last_extrapolated, "a cold history must flag that it assumed a hold"
    print("  cold history falls back to assuming a hold, and says so (cs:hold on the HUD)")


def _report(name, errs):
    e = sorted(errs)
    deg = 360.0 / 65536.0
    print("  %-28s n=%-5d exact=%-5d mean %6.1f hw (%.3f deg)  p95 %5d  max %5d hw (%.3f deg)"
          % (name, len(e), sum(1 for x in e if x == 0), sum(e) / len(e), sum(e) / len(e) * deg,
             e[int(len(e) * 0.95)], e[-1], e[-1] * deg))


def main():
    root = _sim_root()
    print("tww_sim repo: %s\n" % root)

    print("CAMERA -- 1-step csangle prediction vs the live land-camera oracle:")
    ours, swim = check_camera(root)
    _report("land_steer (manualCamera)", ours)
    _report("cam_sync (swim subject cam)", swim)
    assert max(ours) == 0, "land camera integrator is no longer bit-exact vs the oracle"

    print("\nCAMERA -- where the two models actually diverge:")
    check_saturated(root)

    print("\nC-STICK SCHEDULE -- what a sudden pan reversal costs:")
    check_schedule()

    print("\nSTICK INVERSE -- bearing asked for vs bearing the clamped decode actually gives:")
    tbl, sim = check_inverse(root)
    _report("land_steer (ported decode)", sim)
    _report("cam_sync (captured table)", tbl)
    assert max(sim) < max(tbl), "the ported inverse should beat the captured table's tail"

    print("\nOK")


if __name__ == "__main__":
    main()
