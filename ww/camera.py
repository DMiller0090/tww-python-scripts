from __future__ import annotations
from . import memory as mem
from .addresses.address import Address

def cs_angle_halfword(default: int = 0) -> int:
    """
    Read Camera Angle
    """
    base_addr = Address.CSANGLE_BASE_PTR
    off_ptr   = Address.CSANGLE_PTR_OFFSET
    off_u16   = Address.CSANGLE_U16_OFFSET

    if base_addr is None or off_ptr is None or off_u16 is None:
        # Warn once for any missing piece; return default
        if base_addr is None: mem._warn_once(f"Missing address for key '{_CSANGLE_BASE}'")
        if off_ptr   is None: mem._warn_once(f"Missing address for key '{_CSANGLE_OFFPTR}'")
        if off_u16   is None: mem._warn_once(f"Missing address for key '{_CSANGLE_OFFU16}'")
        return default

    # p2 = deref_chain(base, +0x34)
    p2 = mem.deref_chain(base_addr, off_ptr)
    if p2 is None:
        return default

    try:
        return mem.read_u16(p2 + off_u16) & 0xFFFF
    except Exception:
        return default

def cs_angle_deg() -> float:
    # Optional helper
    return (cs_angle_halfword() * 360.0) / 65536.0

def event_mode(default: int = -1) -> int:
    """
    Camera event mode.
    """
    try:
        addr = Address.EVENT_MODE
        return mem.read_u8(addr)
    except Exception:
        return default