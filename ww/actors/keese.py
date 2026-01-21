# ww/actors/darknut.py
from __future__ import annotations
from typing import Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from ww.addresses.address import Address
from . import register

_PID = proc_id("PROC_KI")  # Register proc name

@register(_PID)
class Keese(Actor):
    __slots__ = ("_valid",)
    def __init__(self,p_addr) -> None:
        super().__init__(p_addr)
        self._valid = bool(p_addr)
        
    def action(self, default: int = 0) -> int:
        off = Address.KEESE_ACTION_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u8(self.base + off))
        except Exception: return default
        
    def behavior(self, default: int = 0) -> int:
        off = Address.KEESE_BEHAVIOR_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u8(self.base + off))
        except Exception: return default
        
    def pos_move_x(self, default: float = 0.0) -> float:
        off = Address.KEESE_POS_MOVE_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default
        
    def pos_move_y(self, default: float = 0.0) -> float:
        off = Address.KEESE_POS_MOVE_OFFSET + 4
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default
        
    def pos_move_z(self, default: float = 0.0) -> float:
        off = Address.KEESE_POS_MOVE_OFFSET + 8
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default
        
    def pos_move3d(self) -> Tuple[float, float, float]:
        return (self.pos_move_x(), self.pos_move_y(), self.pos_move_z())
    
    def check_player_dist_timer(self, default: int = 0) -> int:
        off = Address.KEESE_CHK_PLYR_DIST_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u16(self.base + off))
        except Exception: return default
        
    def update_pos_timer(self, default: int = 0) -> int:
        off = Address.KEESE_RND_UPDATE_POS_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u16(self.base + off))
        except Exception: return default

    def action_timer(self, default: int = 0) -> int:
        off = Address.KEESE_ACTION_TIMER_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u16(self.base + off))
        except Exception: return default