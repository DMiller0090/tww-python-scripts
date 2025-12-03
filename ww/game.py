"""
ww.game
-------
Global game/engine state helpers (frame counter, parity, simple gating).
"""

from __future__ import annotations
from typing import Dict, List, Tuple

from ww.addresses.address import Address

from . import memory as mem
# --- Add near the top with your imports ---


# Bit layout (base = Address.INPUT_BUFFER)
# byte 0 (base+0): A=0x01, B=0x02, X=0x04, Y=0x08, Start=0x10
# byte 1 (base+1): Left=0x01, Right=0x02, Down=0x04, Up=0x08, Z=0x10, R=0x20, L=0x40
_GC_BTN0 = {
    "A": 0x01,
    "B": 0x02,
    "X": 0x04,
    "Y": 0x08,
    "Start": 0x10,
}
_GC_BTN1 = {
    "Left": 0x01,
    "Right": 0x02,
    "Down": 0x04,
    "Up": 0x08,
    "Z": 0x10,
    "R": 0x20,
    "L": 0x40,
}

def read_gc_input(i: int = 0) -> Dict[str, int | bool]:
    """
    Read GameCube controller 'i' directly from RAM.

    Currently supports i == 0 mapped at Address.INPUT_BUFFER.
    Returns a dict with the same keys Dolphin uses (so playback can reuse it):
      booleans: A,B,X,Y,Start,L,R,Z,Left,Right,Down,Up
      bytes: StickX,StickY,CStickX,CStickY,TriggerLeft,TriggerRight,AnalogA,AnalogB
      bool: Connected
    """
    if i != 0:
        # Only GC pad #1 is mapped in the buffer you provided; everything else false/neutral.
        return {
            "Left": False, "Right": False, "Down": False, "Up": False,
            "Z": False, "R": False, "L": False, "A": False, "B": False,
            "X": False, "Y": False, "Start": False,
            "StickX": 128, "StickY": 128, "CStickX": 128, "CStickY": 128,
            "TriggerLeft": 0, "TriggerRight": 0, "AnalogA": 0, "AnalogB": 0,
            "Connected": False,
        }

    base = Address.INPUT_BUFFER
    b0 = mem.read_u8(base + 0)
    b1 = mem.read_u8(base + 1)

    stick_x   = mem.read_u8(base + 2)
    stick_y   = mem.read_u8(base + 3)
    cstick_x  = mem.read_u8(base + 4)
    cstick_y  = mem.read_u8(base + 5)
    trig_l    = mem.read_u8(base + 6)
    trig_r    = mem.read_u8(base + 7)

    # AnalogA/AnalogB aren't in your mapping; synthesize from digital for compatibility
    analog_a  = 255 if (b0 & _GC_BTN0["A"]) else 0
    analog_b  = 255 if (b0 & _GC_BTN0["B"]) else 0

    out: Dict[str, int | bool] = {
        # digital
        "A":     bool(b0 & _GC_BTN0["A"]),
        "B":     bool(b0 & _GC_BTN0["B"]),
        "X":     bool(b0 & _GC_BTN0["X"]),
        "Y":     bool(b0 & _GC_BTN0["Y"]),
        "Start": bool(b0 & _GC_BTN0["Start"]),
        "Left":  bool(b1 & _GC_BTN1["Left"]),
        "Right": bool(b1 & _GC_BTN1["Right"]),
        "Down":  bool(b1 & _GC_BTN1["Down"]),
        "Up":    bool(b1 & _GC_BTN1["Up"]),
        "Z":     bool(b1 & _GC_BTN1["Z"]),
        "R":     bool(b1 & _GC_BTN1["R"]),
        "L":     bool(b1 & _GC_BTN1["L"]),
        # analog
        "StickX":       int(stick_x),
        "StickY":       int(stick_y),
        "CStickX":      int(cstick_x),
        "CStickY":      int(cstick_y),
        "TriggerLeft":  int(trig_l),
        "TriggerRight": int(trig_r),
        "AnalogA":      int(analog_a),
        "AnalogB":      int(analog_b),
        # connection (assume pad 1 is present when we can read it)
        "Connected": True,
    }
    return out

# --- CSV helpers (same column order as your recorded logs) ---
def gc_csv_headers() -> List[str]:
    return [
        "frame","A","AnalogA","AnalogB","B",
        "CStickX","CStickY","Connected","Down","L","Left","R","Right",
        "Start","StickX","StickY","TriggerLeft","TriggerRight","Up","X","Y","Z",
    ]

def gc_csv_row(i: int = 0) -> List[int | bool]:
    s = read_gc_input(i)
    f = frame()
    return [
        f,
        bool(s["A"]), int(s["AnalogA"]), int(s["AnalogB"]), bool(s["B"]),
        int(s["CStickX"]), int(s["CStickY"]), bool(s["Connected"]),
        bool(s["Down"]), bool(s["L"]), bool(s["Left"]), bool(s["R"]), bool(s["Right"]),
        bool(s["Start"]), int(s["StickX"]), int(s["StickY"]),
        int(s["TriggerLeft"]), int(s["TriggerRight"]),
        bool(s["Up"]), bool(s["X"]), bool(s["Y"]), bool(s["Z"]),
    ]

# Symbolic key lives in addresses/<region>.py:
# FRAME_COUNTER_ADDRESS = 0x803E9D34

def frame() -> int:
    """
    Read the global frame counter.
    """
    return mem.read_u32(Address.FRAME_COUNTER_ADDRESS)

def parity() -> int:
    """0 for even frames, 1 for odd frames."""
    return frame() & 1

def is_new_frame(prev: int | None) -> Tuple[bool, int]:
    """
    Convenience: returns (is_new, current_frame).
    Treats None as "always new".
    """
    cur = frame()
    return (prev is None or cur != prev, cur)

class FrameGate:
    """
    Minimal helper to ensure logic runs once per game frame.
    gate() -> True exactly once for each new frame.
    """
    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last: int | None = None

    def gate(self) -> bool:
        is_new, cur = is_new_frame(self._last)
        if is_new:
            self._last = cur
        return is_new

    @property
    def last(self) -> int | None:
        return self._last
