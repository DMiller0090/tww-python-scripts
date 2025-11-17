# ww/actors/tbox.py
from __future__ import annotations
from typing import Optional, Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from . import register


_PID = proc_id("PROC_TBOX")  # Register proc name

@register(_PID)
class TBox(Actor):
    __slots__ = ("_valid",)
    def __init__(self, base: int) -> None:
        super().__init__(base)
        self._valid = bool(base)
        
    @property
    def lighting(self) -> Optional[float]:
        """Raw m_itemNo (u8) or None if offset not available."""
        off = Address.TBOX_LIGHTING_OFFSET
        if not (self._valid and isinstance(off, int)):
            return None
        try:
            return float(mem.read_u8(self.base + off))  # relies on ww.memory → dolphin.memory.read_u8
        except Exception:
            return None
    
    def write_lighting(self, value: float) -> None:
        off = Address.TBOX_LIGHTING_OFFSET
        if not (self._valid and isinstance(off, int)):
            return
        mem.write_f32(self.base + off,value)