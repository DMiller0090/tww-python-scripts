"""
ww.collision
------------
Collision flag helpers.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from ww.addresses.address import Address

from . import memory as mem


# Bit positions
_BIT_ON_FLOOR         = 10
_BIT_AGAINST_WALL     = 11


@dataclass(frozen=True)
class CollisionFlags:
    raw: int

    @property
    def on_floor(self) -> bool:
        return bool(self.raw & (1 << _BIT_ON_FLOOR))

    @property
    def against_wall(self) -> bool:
        return bool(self.raw & (1 << _BIT_AGAINST_WALL))


def _flags_raw(default: int = 0) -> int:
    """
    Read u16 collision flags via: *(u32 at COLLISION_POINTER) + COLLISION_OFFSET.
    Returns `default` (0) if any step is missing/invalid for the active region.
    """
    ptr_addr = Address.COLLISION_POINTER
    off      = Address.COLLISION_OFFSET

    if ptr_addr is None or off is None:
        # Region not populated yet; warn once per missing symbol and return default.
        if ptr_addr is None:
            mem._warn_once(f"Missing address for Address.COLLISION_POINTER")
        if off is None:
            mem._warn_once(f"Missing address for Address.COLLISION_OFFSET'")
        return default

    base = mem.read_pointer(ptr_addr)
    if base is None:
        # Pointer read failed or wasn't a valid RAM address.
        return default

    try:
        return mem.read_u16(base + off)
    except Exception:
        return default


def flags() -> CollisionFlags:
    """Typed view over the raw u16 flags."""
    return CollisionFlags(raw=_flags_raw())


def is_on_floor() -> bool:
    return flags().on_floor


def is_against_wall() -> bool:
    return flags().against_wall
