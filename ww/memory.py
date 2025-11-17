"""
Thin, typed wrappers over Dolphin's memory API + safe pointer helpers.

Design goals:
- Keep EVERYTHING side-effect-free and importable outside Dolphin for type-checking.
- Prefer JP addresses by default, but transparently use `ww.version.detect_region`
- Fail “softly” for missing addresses (return 0/0.0) while logging a single warning
  per missing key so single-purpose scripts keep running and we get a TODO list.
"""

from __future__ import annotations

from typing import Optional, Tuple, TYPE_CHECKING, Set, AnyStr

from dolphin import memory as dm  # real runtime import

# Default to JP if no versioning module is present.
try:
    from .addresses import ww_jp as _addr_mod  # type: ignore
except Exception:  # pragma: no cover
    _addr_mod = None



# ──────────────────────────────────────────────────────────────────────────────
# Address resolution & “warn once” book-keeping
# ──────────────────────────────────────────────────────────────────────────────

_MISSING_SEEN : Set[str] = set()

def _warn_once(msg: str) -> None:
    if msg not in _MISSING_SEEN:
        _MISSING_SEEN.add(msg)
        print(f"[ww.memory] {msg}")

def missing_todos() -> Tuple[str, ...]:
    """Items we warned about already; handy to dump at script end."""
    return tuple(sorted(_MISSING_SEEN))


# ──────────────────────────────────────────────────────────────────────────────
# Low-level typed reads/writes
# ──────────────────────────────────────────────────────────────────────────────

def _require_dm() -> None:
    if dm is None:
        raise RuntimeError("dolphin.memory is not available (are you running inside Dolphin?).")

# GameCube RAM window
_RAM_MIN = 0x80000000
_RAM_MAX = 0x81800000

def is_valid_address(addr: int) -> bool:
    return _RAM_MIN <= int(addr) < _RAM_MAX


# --- Reads ---
def read_u8(addr: int) -> int:
    _require_dm()
    return int(dm.read_u8(addr))  # type: ignore[union-attr]

def read_s8(addr: int) -> int:
    _require_dm()
    return int(dm.read_s8(addr))  # type: ignore[union-attr]

def read_u16(addr: int) -> int:
    _require_dm()
    return int(dm.read_u16(addr))  # type: ignore[union-attr]

def read_s16(addr: int) -> int:
    _require_dm()
    return int(dm.read_s16(addr))  # type: ignore[union-attr]

def read_u32(addr: int) -> int:
    _require_dm()
    return int(dm.read_u32(addr))  # type: ignore[union-attr]

def read_s32(addr: int) -> int:
    _require_dm()
    return int(dm.read_s32(addr))  # type: ignore[union-attr]

def read_f32(addr: int) -> float:
    _require_dm()
    return float(dm.read_f32(addr))  # type: ignore[union-attr]

def read_bytes(addr: int, size: int) -> bytearray:
    _require_dm()
    return bytearray(dm.read_bytes(addr, size))
# --- Writes ---
def write_u8(addr: int, val: int) -> None:
    _require_dm()
    dm.write_u8(addr, val)  # type: ignore[union-attr]

def write_u16(addr: int, val: int) -> None:
    _require_dm()
    dm.write_u16(addr, val)  # type: ignore[union-attr]

def write_u32(addr: int, val: int) -> None:
    _require_dm()
    dm.write_u32(addr, val)  # type: ignore[union-attr]

def write_f32(addr: int, val: float) -> None:
    _require_dm()
    dm.write_f32(addr, val)  # type: ignore[union-attr]



# ──────────────────────────────────────────────────────────────────────────────
# Pointer helpers (read 32-bit value at the address)
# ──────────────────────────────────────────────────────────────────────────────

def read_pointer(addr: int) -> Optional[int]:
    """Return the u32 at `addr` if it looks like a valid RAM pointer; else None."""
    try:
        p = read_u32(addr)
    except Exception:
        return None
    return p if is_valid_address(p) else None


def deref_chain(base_addr: int, *offsets: int) -> Optional[int]:
    """
    Walk a pointer chain:
        p = read_pointer(base_addr)
        for off in offsets: p = read_pointer(p + off)
    Returns final address or None if any step is invalid.
    """
    p = read_pointer(base_addr)
    if p is None:
        return None
    for off in offsets:
        nxt = read_pointer(p + int(off))
        if nxt is None:
            return None
        p = nxt
    return p


# ──────────────────────────────────────────────────────────────────────────────
# JP-specific convenience (when you know you want the JP constants directly)
# ──────────────────────────────────────────────────────────────────────────────

def jp(key: str) -> Optional[int]:
    """Direct access to ww_jp.ADDR for quick tests."""
    if _addr_mod is None:
        return None
    return _addr_mod.ADDR.get(key)  # type: ignore[attr-defined]
