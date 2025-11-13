from __future__ import annotations
from . import memory as mem

# Symbolic keys
_CSANGLE_BASE       = "CSANGLE_BASE_PTR"
_CSANGLE_OFFPTR     = "CSANGLE_PTR_OFFSET"
_CSANGLE_OFFU16     = "CSANGLE_U16_OFFSET"
_ADDR_EVENT_MODE    = "EVENT_MODE"

def cs_angle_halfword(default: int = 0) -> int:
    """
    Read Camera Angle
    """
    base_addr = mem.resolve_address(_CSANGLE_BASE)
    off_ptr   = mem.resolve_address(_CSANGLE_OFFPTR)
    off_u16   = mem.resolve_address(_CSANGLE_OFFU16)

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
        return mem.read_u8(_ADDR_EVENT_MODE)
    except Exception:
        return default