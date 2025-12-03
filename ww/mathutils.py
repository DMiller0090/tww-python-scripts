"""
ww.mathutils
------------
Small numeric helpers.

Notes
- Angles: degrees in [0,360) unless otherwise stated.
- "Halfword angles" are 0..65535 (inclusive wrapping at 65536).
"""

from __future__ import annotations
import math
from typing import Tuple

from ww import memory
from ww.addresses.address import Address

# ── Constants ─────────────────────────────────────────────────────────────────

PI  = math.pi
TAU = math.tau

DEG_PER_TURN = 360.0
HW_PER_TURN  = 65536  # halfword units per full turn

EPS = 1e-7  # general-purpose epsilon for float compares


# ── Generic math helpers ──────────────────────────────────────────────────────

def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

def feq(a: float, b: float, eps: float = EPS) -> bool:
    return abs(a - b) <= eps

def nfmod(a: float, m: float) -> float:
    """Normalized floating mod that always returns in [0, m)."""
    return (a % m + m) % m

def wrap_deg(deg: float) -> float:
    """Wrap degrees to [0, 360)."""
    return nfmod(deg, DEG_PER_TURN)

def wrap_rad(rad: float) -> float:
    """Wrap radians to [0, 2π)."""
    return nfmod(rad, TAU)


# ── Angle conversions (deg <-> halfword) ──────────────────────────────────────

def deg_to_halfword(deg: float) -> int:
    """
    Map degrees to halfword units in [0, 65535].
    Note: 360° maps back to 0 (i.e., wrap).
    """
    return int(wrap_deg(deg) * HW_PER_TURN / DEG_PER_TURN) & 0xFFFF

def halfword_to_deg(hw: int) -> float:
    """
    Map halfword 0..65535 to degrees in [0, 360).
    """
    return (int(hw) & 0xFFFF) * (DEG_PER_TURN / HW_PER_TURN)

def hw_wrap(hw: int) -> int:
    """Wrap any int to halfword domain [0, 65535]."""
    return int(hw) & 0xFFFF

def hw_mod_diff(a: int, b: int) -> int:
    """
    Minimal absolute difference between two halfword angles in [0..32768].
    """
    da = hw_wrap(a) - hw_wrap(b)
    da &= 0xFFFF
    return da if da <= 0x8000 else 0x10000 - da


# ── 2D geometry helpers (ground plane) ────────────────────────────────────────

def dist2d(x1: float, z1: float, x2: float, z2: float) -> float:
    dx, dz = x2 - x1, z2 - z1
    return math.hypot(dx, dz)


    
def angle2d_deg(x1: float, z1: float, x2: float, z2: float) -> float:
    """
    World-space angle from (x1,z1) to (x2,z2), in degrees [0,360).
    """
    return wrap_deg(math.degrees(math.atan2(z2 - z1, x2 - x1)))

def angle2d_hw(x1: float, z1: float, x2: float, z2: float) -> int:
    """
    World-space angle from (x1,z1) to (x2,z2) as halfword domain [0, 65535].
    """
    return deg_to_halfword(angle2d_deg(x1, z1, x2, z2))

def project2d(x: float, z: float, angle_deg: float, dist: float, lookup: bool = False) -> Tuple[float, float]:
    """
    Move (x,z) forward by `dist` along `angle_deg`.
    """
    print(angle_deg)
    rad = math.radians(wrap_deg(angle_deg))
    new_x = 0
    new_z = 0
    if lookup:  
        new_x = x + (dist * sin_lookup(rad))
        new_z = z + (dist * cos_lookup(rad))
    else:
        new_x = x + (dist * math.sin(rad))
        new_z = z + (dist * math.cos(rad))
    return (new_x, new_z)


# ── Degree offsets pipeline helpers (for 45°/arrow-swim) ──────────

def apply_angle_offsets_deg(
    base_deg: float,
    *,
    flip_180: bool = False,
    arrow_swim_deg: float = 0.0,
    static_offset_deg: float = 0.0,
) -> float:
    """
    Apply (optional) 180° flip, arrow-swim correction (+offset normally, -offset when flipped),
    and static/global offset. Returns wrapped degrees in [0,360).
    """
    deg = base_deg + (180.0 if flip_180 else 0.0)
    if arrow_swim_deg:
        deg = deg - arrow_swim_deg if flip_180 else deg + arrow_swim_deg
    deg += static_offset_deg
    return wrap_deg(deg)


# ── Utility: safe integer coercions for controller ranges ─────────────────────

def to_int8(n: float) -> int:
    """
    Clamp and round to signed int8 (-128..127). Many controller apis accept this.
    """
    n = clamp(round(n), -128, 127)
    return int(n)

def to_uint8(n: float) -> int:
    """
    Clamp and round to unsigned int8 (0..255). Some CSVs encode stick values like this.
    """
    n = clamp(round(n), 0, 255)
    return int(n)

def to_s16(x: int) -> int:
        return ((x + 0x8000) & 0xFFFF) - 0x8000  # signed 16-bit
    
def cos_lookup(value: float) -> float:
    value = nfmod(value, 6.283185482025146)
    index: int = int(value * 10430.3779296875)
    if index < -32768:
        index += 65536
    else:
        if 32767 < index:
            index += -65536
    index = index >> 4
    if index == 0:
        return 1
    if index > 4096:
        return math.nan
    if index < 0:
        index = 4096 + index
    off: int = index * 4
    
    cos_ptr: int = memory.read_u32(Address.COS_TABLE_PTR)
    addr: int = cos_ptr + off
    return memory.read_f32(addr)

def sin_lookup(value: float) -> float:
    # Normalize to [0, 2π) using the same constant your cos function uses
    value = nfmod(value, 6.283185482025146)

    # Convert radians → table index space (65536 / 2π ≈ 10430.3779)
    index: int = int(value * 10430.3779296875)

    # Wrap to signed 16-bit range like the original
    if index < -32768:
        index += 65536
    elif index > 32767:
        index -= 65536

    # The table uses 12-bit indices (divide by 16)
    index = index >> 4

    # Fast path & bounds (mirror your cos logic)
    if index == 0:
        return 0.0
    if index > 4096:
        return math.nan
    if index < 0:
        index = 4096 + index

    off: int = index * 4
    sin_ptr: int = memory.read_u32(Address.SIN_TABLE_PTR)
    addr: int = sin_ptr + off
    return memory.read_f32(addr)

def cLib_addCalcAngleS(value: int, target: int, scale: int, max_step: int, min_step: int, *, wrap16: bool = True) -> int:
    """
    Adds angle at a determined rate. 
    Based on decomp:
    https://github.com/zeldaret/tww/blob/c1d05201b6ace320f48727676eb3c09aa150798b/src/SSystem/SComponent/c_lib.cpp#L160
    """
    # Optionally coerce inputs to s16 first (to match C parameter types)
    if wrap16:
        value  = to_s16(value)
        target = to_s16(target)

    def diff_of(a: int, b: int) -> int:
        d = b - a
        return to_s16(d) if wrap16 else d

    diff = diff_of(value, target)

    if value != target:
        # step = (diff / scale) with C-like truncation toward zero
        if scale == 0:
            step = diff
        else:
            step = int(diff / scale)

        if step > min_step or step < -min_step:
            if step > max_step:
                step = max_step
            if step < -max_step:
                step = -max_step
            value = value + step
        else:
            if diff >= 0:
                value = value + min_step
                diff = diff_of(value, target)
                if diff <= 0:
                    value = target
            else:
                value = value - min_step
                diff = diff_of(value, target)
                if diff >= 0:
                    value = target

    if wrap16:
        value = to_s16(value)

    remaining = diff_of(value, target)  # returns (target - value) with same s16 policy
    # The C function returns (target - *pValue), so `remaining` matches that.
    return value, remaining