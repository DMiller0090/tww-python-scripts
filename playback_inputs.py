# playback_inputs.py
# Plays back inputs from CSV logs, auto-reloading if the CSVs change on disk.

from dolphin import event, controller
import csv
import os

INPUT_DIR = "InputLogs"
# ---------- file paths ----------
_GC_CSV  = os.path.join(INPUT_DIR, "controller1_gc_inputs.csv")
_GBA_CSV = os.path.join(INPUT_DIR, "controller2_gba_inputs.csv")

# ---------- reload cadence ----------
# Check for file changes every N frames to reduce OS calls
_RELOAD_CHECK_INTERVAL = 15

# ---------- globals (loaded & updated as needed) ----------
_gc_by_frame = {}   # frame:int -> [rows...]
_gba_by_frame = {}  # frame:int -> [rows...]
_gc_use_count = {}  # frame:int -> count of variants used this frame
_gba_use_count = {} # frame:int -> count of variants used this frame
_last_frame_seen = {0: None, 1: None}  # per-controller last frame applied

_gc_mtime = None
_gba_mtime = None

def _as_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y")

def _as_int(v, default=0):
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default

def _load_csv(path):
    by_frame = {}
    if not os.path.exists(path):
        return by_frame
    with open(path, "r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            frame = _as_int(row.get("frame", 0))
            by_frame.setdefault(frame, []).append(row)
    return by_frame

def _pick_variant_for_frame(frame, variants, use_count_map):
    if not variants:
        return None
    count = use_count_map.get(frame, 0)
    if count >= len(variants):
        count = len(variants) - 1
    use_count_map[frame] = count + 1
    return variants[count]

def _apply_gc_row(i, row):
    inputs = controller.get_gc_buttons(i)

    inputs["A"]     = _as_bool(row.get("A", False))
    inputs["B"]     = _as_bool(row.get("B", False))
    inputs["X"]     = _as_bool(row.get("X", False))
    inputs["Y"]     = _as_bool(row.get("Y", False))
    inputs["Start"] = _as_bool(row.get("Start", False))
    inputs["L"]     = _as_bool(row.get("L", False))
    inputs["R"]     = _as_bool(row.get("R", False))
    inputs["Z"]     = _as_bool(row.get("Z", False))

    inputs["Left"]  = _as_bool(row.get("Left", False))
    inputs["Right"] = _as_bool(row.get("Right", False))
    inputs["Down"]  = _as_bool(row.get("Down", False))
    inputs["Up"]    = _as_bool(row.get("Up", False))

    inputs["StickX"]      = _as_int(row.get("StickX", 128), 128)
    inputs["StickY"]      = _as_int(row.get("StickY", 128), 128)
    inputs["CStickX"]     = _as_int(row.get("CStickX", 128), 128)
    inputs["CStickY"]     = _as_int(row.get("CStickY", 128), 128)
    inputs["TriggerLeft"] = _as_int(row.get("TriggerLeft", 0), 0)
    inputs["TriggerRight"]= _as_int(row.get("TriggerRight", 0), 0)

    controller.set_gc_buttons(i, inputs)

def _apply_gba_row(i, row):
    inputs = controller.get_gc_buttons(i)

    # Buttons
    inputs["A"]     = _as_bool(row.get("A", False))
    inputs["B"]     = _as_bool(row.get("B", False))
    inputs["L"]     = _as_bool(row.get("L", False))
    inputs["R"]     = _as_bool(row.get("R", False))
    inputs["Start"] = _as_bool(row.get("Start", False))
    inputs["Y"]     = _as_bool(row.get("Disconnect", False))
    # GBA Select → GC Z
    inputs["Z"]     = _as_bool(row.get("Select", False))

    # D-pad from CSV
    d_right = _as_bool(row.get("Right", False))
    d_left  = _as_bool(row.get("Left", False))
    d_up    = _as_bool(row.get("Up", False))
    d_down  = _as_bool(row.get("Down", False))

    # D-pad booleans
    inputs["Right"] = d_right
    inputs["Left"]  = d_left
    inputs["Up"]    = d_up
    inputs["Down"]  = d_down

    controller.set_gc_buttons(i, inputs)

def _stat_mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return None

def _reload_gc_csv_if_changed():
    global _gc_mtime, _gc_by_frame, _gc_use_count
    m = _stat_mtime(_GC_CSV)
    if _gc_mtime is None or (m is not None and m != _gc_mtime):
        # Try to load; if it fails mid-write, keep old data
        try:
            by_frame = _load_csv(_GC_CSV)
        except Exception:
            return
        _gc_by_frame = by_frame
        _gc_use_count = {}  # reset variant consumption (safer after topology changes)
        _gc_mtime = m

def _reload_gba_csv_if_changed():
    global _gba_mtime, _gba_by_frame, _gba_use_count
    m = _stat_mtime(_GBA_CSV)
    if _gba_mtime is None or (m is not None and m != _gba_mtime):
        try:
            by_frame = _load_csv(_GBA_CSV)
        except Exception:
            return
        _gba_by_frame = by_frame
        _gba_use_count = {}
        _gba_mtime = m

# Initial load
_reload_gc_csv_if_changed()
_reload_gba_csv_if_changed()

@event.on_frameadvance
def update():
    from ww import game
    cur = game.frame()
    
    # Debounced hot-reload
    if (cur % _RELOAD_CHECK_INTERVAL) == 0:
        _reload_gc_csv_if_changed()
        _reload_gba_csv_if_changed()

    # controller 0 → GC, controller 1 → GBA
    for idx, by_frame, use_map in (
        (0, _gc_by_frame,  _gc_use_count),
        (1, _gba_by_frame, _gba_use_count),
    ):
        if(idx == 0):
            cur = game.frame()
        elif(idx == 1):
            cur = game.frame() + 1
        last = _last_frame_seen.get(idx)
        # If frame changed since we last applied for this controller, reset variant count for this frame
        if last is None or cur != last:
            use_map[cur] = 0
            _last_frame_seen[idx] = cur

        variants = by_frame.get(cur)
        if not variants:
            continue

        row = _pick_variant_for_frame(cur, variants, use_map)
        if not row:
            continue

        if idx == 0:
            _apply_gc_row(0, row)
        else:
            _apply_gba_row(1, row)
