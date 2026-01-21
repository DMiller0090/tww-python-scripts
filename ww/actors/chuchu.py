# ww/actors/darknut.py
from __future__ import annotations
from typing import Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from ww.addresses.address import Address
from . import register

_PID = proc_id("PROC_CC")  # Register proc name

@register(_PID)
class ChuChu(Actor):
    __slots__ = ("_valid",)
    def __init__(self,p_addr) -> None:
        super().__init__(p_addr)
        self._valid = bool(p_addr)
        
    @property
    def action(self) -> Optional[int]:
        """
        Current action type
        """
        if not self._valid:
            return None
        off = Address.CHU_ACTION_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return mem.read_s8(self.base + off)
        except Exception:
            return None