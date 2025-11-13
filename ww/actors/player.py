# ww/actors/player.py
from __future__ import annotations
from typing import Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from . import register

# Region symbols
_ADDR_PLAYER_PTR       = "PLAYER_POINTER"
_OFF_PLAYER_STATE       = "PLAYER_STATE"
_OFF_ANIM_POS          = "ANIMATION_POS_OFFSET"
_OFF_ANIM_LEN          = "ANIMATION_LENGTH"
_OFF_ANIM_INC          = "ANIMATION_INCREMENT_OFFSET"
_ADDR_ACTUAL_SPEED_PTR = "ACTUAL_SPEED_POINTER"
_OFF_ACTUAL_SPEED      = "ACTUAL_SPEED_ADDRESS_OFFSET"
_ADDR_EQUIPPED_ITEM_Y  = "EQUIPPED_ITEM_Y"
_ADDR_BOMB_COUNT        = "BOMB_COUNT"

_ADDR_DEBUG_X          = "X_ADDRESS"
_ADDR_DEBUG_Y          = "Y_ADDRESS"
_ADDR_DEBUG_Z          = "Z_ADDRESS"

_PLAYER_PID = proc_id("PROC_PLAYER")  # Register player proc name

@register(_PLAYER_PID)
class Player(Actor):
    __slots__ = ("_valid",)

    def __init__(self) -> None:
        p_addr = mem.resolve_address(_ADDR_PLAYER_PTR)
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
        a = mem.resolve_address(_ADDR_DEBUG_X)
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_y(self, default: float = 0.0) -> float:
        a = mem.resolve_address(_ADDR_DEBUG_Y)
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_z(self, default: float = 0.0) -> float:
        a = mem.resolve_address(_ADDR_DEBUG_Z)
        if a is None: return default
        try: return float(mem.read_f32(a))
        except Exception: return default

    def debug_pos2d(self) -> Tuple[float, float]:
        return (self.debug_x(), self.debug_z())

    def debug_pos3d(self) -> Tuple[float, float, float]:
        return (self.debug_x(), self.debug_y(), self.debug_z())

    # ---- state / animation (relative to gptr) ----
    def state(self, default: int = 0) -> int:
        off = mem.resolve_address(_OFF_PLAYER_STATE)
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u16(self.base + off))
        except Exception: return default

    def anim_pos(self, default: float = 0.0) -> float:
        off = mem.resolve_address(_OFF_ANIM_POS)
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    def anim_len(self, default: float = 0.0) -> float:
        off = mem.resolve_address(_OFF_ANIM_LEN)
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    def anim_increment(self, default: float = 0.0) -> float:
        off = mem.resolve_address(_OFF_ANIM_INC)
        if not (self._valid and isinstance(off, int)): return default
        try: return float(mem.read_f32(self.base + off))
        except Exception: return default

    # ---- speeds ----
    def true_speed(self, default: float = 0.0) -> float:
        p_addr = mem.resolve_address(_ADDR_ACTUAL_SPEED_PTR)
        off = mem.resolve_address(_OFF_ACTUAL_SPEED)
        if p_addr is None or off is None: return default
        try:
            p = mem.read_pointer(p_addr)
            return float(mem.read_f32(p + int(off)))
        except Exception:
            return default

    # ---- equipment / misc ----
    def equipped_item_y(self, default: int = 0) -> int:
        a = mem.resolve_address(_ADDR_EQUIPPED_ITEM_Y)
        if a is None: return default
        try: return int(mem.read_u32(a))
        except Exception: return default
        
    def bomb_count(self, default: int = 0) -> int:
        a = mem.resolve_address(_ADDR_BOMB_COUNT)
        if a is None: return default
        try: return int(mem.read_u8(a))
        except Exception: return default
