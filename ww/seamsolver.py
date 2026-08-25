"""Hand a selected seam to the host-side seam-clip solver window.

This is the Dolphin half, and it is deliberately thin: decide whether an attempt is even possible
from where Link stands, write one JSON, spawn the tool, forget about it. There is no server, no
request spool and no reply -- the window reads the file once and drives the game itself through
`dolphin_mem.py`, the same way every other host-side tool does.

Why a spawn instead of doing the work here: Dolphin's script runtime is Python 3.8 and the
simulation's fused Cython engine builds as a 3.7 extension, so `tww_sim` cannot be imported in this
process at all. Even if it could, a solver draw blocks for about 150 seconds, and this code runs on
the emulator thread.

The reach numbers are NOT owned here. They are derived from the cut model and exported to
`ww/data/seam_reach.json`; the fallbacks below exist only so a fresh clone without that file still
gates rather than silently allowing everything.
"""
import json
import os
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

REACH_PATH = os.path.join(_HERE, "data", "seam_reach.json")

FALLBACK = {
    "walk_lunge": 40.220246093674874,
    "roll_lunge": 49.22022573158013,
    "walk_cap": 17.0,
    "min_approach_walk": 79.19654394523742,
    "max_approach_walk": 782.0,
    "reach_rel_eps": 2.384185791015625e-07,
}

CONFIG_PATH = os.path.join(_HERE, "data", "seamui.local.json")

_DEFAULT_SIM_ROOT = os.path.join(os.path.dirname(_REPO), "tww_sim")


def reach_constants():
    """The exported cut-model numbers, or the pinned fallbacks with a flag saying so."""
    try:
        with open(REACH_PATH) as f:
            d = json.load(f)
        d["_source"] = "exported"
        return d
    except (IOError, OSError, ValueError):
        d = dict(FALLBACK)
        d["_source"] = "fallback"
        return d


def config():
    """Where the host tool lives and which interpreter runs it.

    `ww/data/seamui.local.json` overrides; the defaults assume the documented side-by-side layout
    (`speedrunning/tww_sim` next to `speedrunning/tww-python-scripts`). `python` is left None so
    the launcher can search, because the interpreter that owns the Cython build is not the one
    running this code and is not on any predictable path."""
    cfg = {"sim_root": _DEFAULT_SIM_ROOT, "python": None}
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (IOError, OSError, ValueError):
        pass
    return cfg


def gate(disp, dist_to_seam, const=None):
    """Can this seam be attempted from `dist_to_seam` away? -> dict(ok, blocked, warnings, ...).

    Mirrors `harness.seamui.reach.gate`. Kept in step by the exported constants rather than by
    discipline; the host tool re-runs the exact gate before it will solve, so this one only has to
    be right enough to stop a pointless launch."""
    c = const or reach_constants()
    reach = c["walk_lunge"]
    ceiling = reach * (1.0 + c["reach_rel_eps"])
    near, far = c["min_approach_walk"], c["max_approach_walk"]
    blocked, warn = [], []
    # The seam's own displacement NEVER blocks. The locator banks an existence pair, not the
    # minimum, so refusing on it would call a seam infeasible from a number that never claimed to be.
    if disp is not None and disp > ceiling:
        if disp <= c["roll_lunge"] * (1.0 + c["reach_rel_eps"]):
            warn.append(("roll_tier", "the banked pair needs %.3fu (> walk lunge %.3fu), roll-tier "
                         "depth" % (disp, reach)))
        else:
            warn.append(("deeper_than_lunge", "the banked pair needs %.3fu, past even the roll "
                         "lunge %.3fu -- but it is an existence pair, not the minimum"
                         % (disp, c["roll_lunge"])))
    if dist_to_seam is not None and dist_to_seam < near:
        blocked.append(("too_close", "Link is %.0fu away; needs %.0fu to reach the walk cap and "
                        "still fire the lunge" % (dist_to_seam, near)))
    if dist_to_seam is not None and dist_to_seam > far:
        blocked.append(("too_far", "Link is %.0fu away; the approach search stages at most %.0fu"
                        % (dist_to_seam, far)))
    if c.get("_source") == "fallback":
        warn.append(("stale_constants", "using pinned reach constants (seam_reach.json missing)"))
    return {"ok": not blocked, "blocked": blocked, "warnings": warn,
            "reach": reach, "near": near, "far": far, "disp": disp, "dist": dist_to_seam}


def summary(verdict):
    """One line for the viewer's HUD. Notes are shown even when nothing blocks, because the ones
    this gate raises are about the seam being unusual rather than about it being unusable."""
    if verdict["ok"]:
        line = "in range: %.2fu pair at %.0fu away" % (verdict["disp"] or 0.0,
                                                       verdict["dist"] or 0.0)
        if verdict["warnings"]:
            line += "  (note: " + "; ".join(m for _c, m in verdict["warnings"]) + ")"
        return line
    return "; ".join(m for _c, m in verdict["blocked"])


def _slug(stage, room, seam):
    """Must match `harness.seamui.handoff.slug` exactly, or the host tool files this seam's
    artefacts under a key the viewer never asks for again.

    XZ only: a seam is a vertical edge and the two sources name different points on it -- this CSV
    carries the standable floor Y, the DZB enumeration the wall-base vertex."""
    import hashlib
    raw = json.dumps([float(seam[0]), float(seam[2])], sort_keys=True).encode()
    return "%s_r%d_%s" % (str(stage).replace(os.sep, "_"), int(room),
                          hashlib.sha1(raw).hexdigest()[:8])


def build_handoff(stage, room, seam, initial, destination, angle_deg=None, link=None, gate_result=None):
    """The JSON contract. Coordinates stay full-precision Python floats -- a seam clip is a
    sub-ULP razor, and the same position rounded to three decimals blocks where the exact one
    clips, so nothing here may be formatted through a string."""
    return {"version": 1, "created_ms": int(time.time() * 1000), "source": "collision_viewer",
            "stage": str(stage), "room": int(room),
            "seam": [float(c) for c in seam],
            "initial": [float(c) for c in initial],
            "destination": [float(c) for c in destination],
            "angle_deg": None if angle_deg is None else float(angle_deg),
            "link": link or None, "gate": gate_result or None}


def _find_python(cfg):
    """The interpreter that owns the Cython build. Configured first, then the usual Windows
    launcher, then PATH -- and the host tool re-checks the build itself, so a wrong guess here
    surfaces as a readable 'not built for this interpreter' rather than a crash."""
    if cfg.get("python"):
        return _windowless(cfg["python"])
    for cand in (os.environ.get("TWW_SIM_PYTHON"), "py", "python"):
        if cand:
            return _windowless(cand)
    return "python"


def _windowless(exe):
    """`pythonw.exe` beside a given `python.exe`, when there is one.

    The window is a GUI and must not drag a console along behind it. DETACHED_PROCESS already
    denies it one, but a console-subsystem host still owns a console handle the moment anything
    asks for it, so the windowless build is the honest host. Stages the window runs go the other
    way on purpose -- they talk on stdout -- and suppress their console with a flag instead."""
    if os.name != "nt" or not exe:
        return exe
    base = os.path.basename(exe).lower()
    if base in ("py", "py.exe"):
        return exe                       # the launcher picks the host; -3.7 is added by the caller
    if base == "python.exe" and os.path.isabs(exe):
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def launch(payload, cfg=None):
    """Write the handoff and spawn the window, detached. Returns (path, pid) or raises."""
    cfg = cfg or config()
    root = cfg["sim_root"]
    if not os.path.isdir(root):
        raise RuntimeError("tww_sim not found at %s -- set sim_root in %s" % (root, CONFIG_PATH))
    d = os.path.join(root, "_generated", "seamui", "_handoff")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _slug(payload["stage"], payload["room"], payload["seam"]) + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=1)
    os.replace(tmp, path)
    exe = _find_python(cfg)
    argv = [exe, "-m", "harness.seamui", path]
    if os.path.basename(exe).lower() in ("py", "py.exe"):
        argv = [exe, "-3.7", "-m", "harness.seamui", path]
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(argv, cwd=root, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)
    return path, proc.pid
