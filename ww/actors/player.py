# ww/actors/player.py
from __future__ import annotations
from typing import Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from . import register
from ww.addresses.address import Address


_PLAYER_PID = proc_id("PROC_PLAYER")  # Register player proc name

@register(_PLAYER_PID)
class Player(Actor):
    __slots__ = ("_valid",)

    def __init__(self) -> None:
        p_addr = Address.PLAYER_POINTER
        if p_addr is None:
            super().__init__(0); self._valid = False; return
        try:
            gptr = mem.read_pointer(p_addr)
        except Exception:
            gptr = 0
        super().__init__(gptr)
        self._valid = bool(gptr)

    # ---- debug absolute coordinates ----
    def debug_x(self, default: float = 0.0) -> float:
        a = Address.X_ADDRESS
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_y(self, default: float = 0.0) -> float:
        a = Address.Y_ADDRESS
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_z(self, default: float = 0.0) -> float:
        a = Address.Z_ADDRESS
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_pos2d(self) -> Tuple[float, float]:
        return (self.debug_x(), self.debug_z())

    def debug_pos3d(self) -> Tuple[float, float, float]:
        return (self.debug_x(), self.debug_y(), self.debug_z())

    # ---- state / animation (relative to gptr) ----
    def state(self, default: int = 0) -> int:
        off = Address.PLAYER_STATE
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u32(self.base + off))
        except Exception: return default

    def anim_pos(self, default: float = 0.0) -> float:
        off = Address.ANIMATION_POS_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    def anim_len(self, default: float = 0.0) -> float:
        off = Address.ANIMATION_LENGTH
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    def anim_increment(self, default: float = 0.0) -> float:
        off = Address.ANIMATION_INCREMENT_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    # ---- speeds ----
    def true_speed(self, default: float = 0.0) -> float:
        p_addr = Address.ACTUAL_SPEED_POINTER
        off = Address.ACTUAL_SPEED_ADDRESS_OFFSET
        if p_addr is None or off is None: return default
        try:
            p = mem.read_pointer(p_addr)
            return float(mem.read_f32(p + int(off)))
        except Exception:
            return default

    # ---- equipment / misc ----
    def equipped_item_y(self, default: int = 0) -> int:
        a = Address.EQUIPPED_ITEM_Y
        if a is None: return default
        try: return int(mem.read_u32(a))
        except Exception: return default
        
    def bomb_count(self, default: int = 0) -> int:
        a = Address.BOMB_COUNT
        if a is None: return default
        try: return int(mem.read_u8(a))
        except Exception: return default
