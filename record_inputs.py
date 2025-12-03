# record_inputs.py
# Logs inputs from RAM each on_frameadvance.
# - Controller 1 (GC pad #1): read from ww.game.read_gc_input()
# - Controller 2 (GBA): read from ww.actors.gba.GBA actor buttons()
#
# Produces two CSVs next to the script:
#   controller1_gc_inputs.csv
#   controller2_gba_inputs.csv
#
# Columns are simplified to just relevant fields.

from dolphin import event
import csv
import os

from ww import game, actor as actor_mod
from ww.actors.gba import GBA

INPUT_DIR = "InputLogs"
# ---------- file paths ----------
_GC_CSV  = os.path.join(INPUT_DIR, "controller1_gc_inputs.csv")
_GBA_CSV = os.path.join(INPUT_DIR, "controller2_gba_inputs.csv")

# ---------- CSV headers ----------
_GC_HEADERS  = [
    "frame",
    "A","B","X","Y","Start","L","R","Z",
    "Left","Right","Down","Up",
    "StickX","StickY","CStickX","CStickY",
    "TriggerLeft","TriggerRight",
    "Connected",
]

_GBA_HEADERS = [
    "frame",
    "A","B","L","R","Start","Select",
    "Right","Left","Up","Down",
    "Disconnect",
]

# ---------- cached handles ----------
_gc_writer = None
_gba_writer = None
_gc_file = None
_gba_file = None

# cache a GBA instance if present
_gba_cached = None

def _ensure_writer(path, headers):
    """Open CSV in append mode and write header if file is new/empty."""
    need_header = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if need_header:
        w.writerow(headers)
    return f, w

def _get_gba():
    """Find/refresh the connected GBA actor instance (PROC_AGB)."""
    global _gba_cached
    if _gba_cached and getattr(_gba_cached, "base", 0):
        return _gba_cached
    # iter_actors returns the registered subclass if available
    for a in actor_mod.iter_actors("PROC_AGB"):
        if isinstance(a, GBA):
            _gba_cached = a
            return _gba_cached
        # Fallback: wrap base pointer into a GBA instance
        try:
            _gba_cached = GBA(a.base)  # 'a' might be a generic Actor
            return _gba_cached
        except Exception:
            pass
    _gba_cached = None
    return None

def _gc_row():
    s = game.read_gc_input(0)  # GC pad #1
    f = game.frame() - 1
    return [
        f,
        bool(s["A"]), bool(s["B"]), bool(s["X"]), bool(s["Y"]), bool(s["Start"]),
        bool(s["L"]), bool(s["R"]), bool(s["Z"]),
        bool(s["Left"]), bool(s["Right"]), bool(s["Down"]), bool(s["Up"]),
        int(s["StickX"]), int(s["StickY"]), int(s["CStickX"]), int(s["CStickY"]),
        int(s["TriggerLeft"]), int(s["TriggerRight"]),
        bool(s["Connected"]),
    ]

def _gba_row():
    f = game.frame() - 1
    gba = _get_gba()
    if gba is None:
        # Not connected/visible this frame; record all-false line for alignment.
        return [
            f,
            False, False, False, False, False, False,  # A,B,L,R,Start,Select
            False, False, False, False,                # Right,Left,Up,Down
            False,                                     # Disconnect
        ]
    b = gba.buttons()
    return [
        f,
        bool(b.get("A", False)), bool(b.get("B", False)),
        bool(b.get("L", False)), bool(b.get("R", False)),
        bool(b.get("Start", False)), bool(b.get("Select", False)),
        bool(b.get("Right", False)), bool(b.get("Left", False)),
        bool(b.get("Up", False)), bool(b.get("Down", False)),
        bool(b.get("Disconnect", False)),
    ]

@event.on_frameadvance
def update():
    global _gc_writer, _gba_writer, _gc_file, _gba_file

    # Lazily open writers
    if _gc_writer is None:
        _gc_file, _gc_writer = _ensure_writer(_GC_CSV, _GC_HEADERS)
    if _gba_writer is None:
        _gba_file, _gba_writer = _ensure_writer(_GBA_CSV, _GBA_HEADERS)

    try:
        _gc_writer.writerow(_gc_row())
    except Exception as e:
        # Fail-soft: don't crash logging if something transient goes wrong
        # You can `print` if you want a log line:
        print(f"[record_inputs] GC write error: {e}")
        pass

    try:
        _gba_writer.writerow(_gba_row())
    except Exception as e:
        print(f"[record_inputs] GBA write error: {e}")
        pass
