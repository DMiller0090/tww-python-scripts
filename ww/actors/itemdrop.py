# ww/actors/itemdrop.py
from __future__ import annotations
from typing import Optional
from enum import IntEnum

from .. import memory as mem
from ..actor import Actor, proc_id
from . import register

_OFF_ITEMDROP_TYPE = "ITEMDROP_TYPE_OFFSET"  # Register proc name

_PID = proc_id("PROC_ITEM")

class ItemDropType(IntEnum):
    UNKNOWN       = 0
    GREEN_RUPEE   = 1
    BLUE_RUPEE    = 2
    YELLOW_RUPEE  = 3
    RED_RUPEE     = 4
    SMALL_MAGIC   = 9
    BOMBS_5       = 11
    # TODO: document more drops

# Safe registration: no-op if _PID is None
@register(_PID)
class ItemDrop(Actor):
    """
    Typed wrapper for item drops.
    - Reads m_itemNo (u8) at gptr + ITEMDROP_TYPE_OFFSET (0x63A JP).
    - Exposes both the raw value and a mapped enum.
    """
    __slots__ = ("_valid",)

    def __init__(self, base: int) -> None:
        super().__init__(base)
        self._valid = bool(base)

    @property
    def item_no(self) -> Optional[int]:
        """Raw m_itemNo (u8) or None if offset not available."""
        off = mem.resolve_address(_OFF_ITEMDROP_TYPE)
        if not (self._valid and isinstance(off, int)):
            return None
        try:
            return int(mem.read_u8(self.base + off))
        except Exception:
            return None

    @property
    def item_type(self) -> ItemDropType:
        """Enum mapping for item_no; UNKNOWN if missing/unmapped."""
        v = self.item_no
        if v is None:
            return ItemDropType.UNKNOWN
        # Direct mapping for the values documented
        if v == 1:  return ItemDropType.GREEN_RUPEE
        if v == 2:  return ItemDropType.BLUE_RUPEE
        if v == 3:  return ItemDropType.YELLOW_RUPEE
        if v == 4:  return ItemDropType.RED_RUPEE
        if v == 9:  return ItemDropType.SMALL_MAGIC
        if v == 11: return ItemDropType.BOMBS_5
        return ItemDropType.UNKNOWN

    @property
    def item_name(self) -> str:
        """Human-readable name."""
        t = self.item_type
        if t is ItemDropType.UNKNOWN:
            v = self.item_no
            return f"UNKNOWN({v})" if v is not None else "UNKNOWN(?)"
        return t.name
