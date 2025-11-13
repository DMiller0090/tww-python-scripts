"""
ww.game
-------
Global game/engine state helpers (frame counter, parity, simple gating).
"""

from __future__ import annotations
from typing import Tuple

from . import memory as mem

# Symbolic key lives in addresses/<region>.py:
# FRAME_COUNTER_ADDRESS = 0x803E9D34

def frame(default: int = 0, *, signed: bool = False) -> int:
    """
    Read the global frame counter. Use unsigned by default (parity identical).
    """
    key = "FRAME_COUNTER_ADDRESS"
    return (mem.read_s32_sym(key, default) if signed else mem.read_u32_sym(key, default))

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
