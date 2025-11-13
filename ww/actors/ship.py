# ww/actors/ship.py
from __future__ import annotations
from typing import Optional, Tuple

from ww.actors import register

from .. import memory as mem
from ..actor import Actor, proc_id


# Address keys
_ADDR_SHIP_PTR            = "SHIP_POINTER"                 
_OFF_CRANE_POS_PTR        = "SHIP_CRANE_POS_PTR_OFFSET"    
_OFF_SHIP_MODE            = "SHIP_MODE_OFFSET"

_PID = proc_id("PROC_SHIP") # Register proc name

@register(_PID)
class Ship(Actor):
    """
    King of Red Lions wrapper.
    """
    __slots__ = ("_valid",)

    def __init__(self) -> None:
        p_addr = mem.resolve_address(_ADDR_SHIP_PTR)
        if p_addr is None:
            super().__init__(0); self._valid = False; return
        try:
            boat_base = mem.read_pointer(p_addr)  # u32
        except Exception:
            boat_base = 0
        super().__init__(boat_base)
        self._valid = bool(boat_base)

    def _crane_ptr(self) -> Optional[int]:
        """Return pointer to crane vec3 (x,y,z) or None if unavailable."""
        if not self._valid:
            return None
        off = mem.resolve_address(_OFF_CRANE_POS_PTR)
        if not isinstance(off, int):
            return None
        try:
            return mem.read_pointer(self.base + off)
        except Exception:
            return None

    # --- crane XYZ readers (float) ---
    def crane_x(self, default: float = float("inf")) -> float:
        p = self._crane_ptr()
        if p is None:
            return default
        try:
            return float(mem.read_f32(p + 0x0))
        except Exception:
            return default

    def crane_y(self, default: float = float("inf")) -> float:
        p = self._crane_ptr()
        if p is None:
            return default
        try:
            return float(mem.read_f32(p + 0x4))
        except Exception:
            return default

    def crane_z(self, default: float = float("inf")) -> float:
        p = self._crane_ptr()
        if p is None:
            return default
        try:
            return float(mem.read_f32(p + 0x8))
        except Exception:
            return default

    def crane_pos3d(self) -> Tuple[float, float, float]:
        """(x, y, z); returns (inf, inf, inf)"""
        return (self.crane_x(), self.crane_y(), self.crane_z())
    
    def mode(self) -> Optional[int]:
        """
        Current ship mode.
        """
        if not self._valid:
            return None
        off = mem.resolve_address(_OFF_SHIP_MODE)
        if not isinstance(off, int):
            return None
        try:
            return mem.read_s8(self.base + off)
        except Exception:
            return None
