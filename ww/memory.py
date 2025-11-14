"""
Thin, typed wrappers over Dolphin's memory API + safe pointer helpers.

Design goals:
- Keep EVERYTHING side-effect-free and importable outside Dolphin for type-checking.
- Prefer JP addresses by default, but transparently use `ww.versioning.resolve_address`
  if we later add a versioning module.
- Fail “softly” for missing addresses (return 0/0.0) while logging a single warning
  per missing key so single-purpose scripts keep running and we get a TODO list.
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple, Dict, Callable, Any

# These imports are available when running inside Dolphin’s Python runtime.
# Outside Dolphin (e.g., static analysis), they can be type-checked via the .pyi stubs.
try:
    from dolphin import memory as dm  # type: ignore
except Exception:  # pragma: no cover
    dm = None  # allows offline type checking / docs builds

# Default to JP if no versioning module is present.
try:
    from .addresses import ww_jp as _addr_mod  # type: ignore
except Exception:  # pragma: no cover
    _addr_mod = None

# Optional region/version resolver: if present, we’ll prefer it.
try:
    from . import versioning as _ver  # type: ignore
except Exception:  # pragma: no cover
    _ver = None

# ──────────────────────────────────────────────────────────────────────────────
# Address resolution & “warn once” book-keeping
# ──────────────────────────────────────────────────────────────────────────────

_MISSING_SEEN: set[str] = set()

def _warn_once(msg: str) -> None:
    if msg not in _MISSING_SEEN:
        _MISSING_SEEN.add(msg)
        print(f"[ww.memory] {msg}")

def missing_todos() -> Tuple[str, ...]:
    """Items we warned about already; handy to dump at script end."""
    return tuple(sorted(_MISSING_SEEN))


def resolve_address(key: str) -> Optional[int]:
    """
    Resolve an address/offset by symbolic key.

    Order:
      1) ww.versioning.resolve_address(key)  (if module exists)
      2) ww.addresses.ww_jp.ADDR[key]        (fallback)
    Returns None if not found.
    """
    # 1) Prefer an explicit versioning layer if present.
    if _ver is not None and hasattr(_ver, "resolve_address"):
        try:
            val = _ver.resolve_address(key)  # type: ignore[attr-defined]
            if isinstance(val, int):
                return val
            if val is None:
                return None
        except Exception:
            pass

    # 2) Fallback to JP constants
    if _addr_mod is not None and hasattr(_addr_mod, "ADDR"):
        val = _addr_mod.ADDR.get(key)  # type: ignore[attr-defined]
        if isinstance(val, int):
            return val

    return None


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
# “Symbolic” readers that tolerate missing addresses
# ──────────────────────────────────────────────────────────────────────────────

def read_f32_sym(key: str, default: float = 0.0) -> float:
    """
    Resolve key → address and read f32, warning once if the key is missing.
    Useful in early NTSC-U work where values aren’t known yet.
    """
    addr = resolve_address(key)
    if addr is None:
        _warn_once(f"Missing address for key '{key}'")
        return default
    try:
        return read_f32(addr)
    except Exception:
        _warn_once(f"Failed f32 read at 0x{addr:08X} for key '{key}'")
        return default


def read_u16_sym(key: str, default: int = 0) -> int:
    addr = resolve_address(key)
    if addr is None:
        _warn_once(f"Missing address for key '{key}'")
        return default
    try:
        return read_u16(addr)
    except Exception:
        _warn_once(f"Failed u16 read at 0x{addr:08X} for key '{key}'")
        return default


def read_u32_sym(key: str, default: int = 0) -> int:
    addr = resolve_address(key)
    if addr is None:
        _warn_once(f"Missing address for key '{key}'")
        return default
    try:
        return read_u32(addr)
    except Exception:
        _warn_once(f"Failed u32 read at 0x{addr:08X} for key '{key}'")
        return default


def write_u16_sym(key: str, value: int) -> bool:
    addr = resolve_address(key)
    if addr is None:
        _warn_once(f"Missing address for key '{key}' (write skipped)")
        return False
    try:
        write_u16(addr, value)
        return True
    except Exception:
        _warn_once(f"Failed u16 write at 0x{addr:08X} for key '{key}'")
        return False


def write_f32_sym(key: str, value: float) -> bool:
    addr = resolve_address(key)
    if addr is None:
        _warn_once(f"Missing address for key '{key}' (write skipped)")
        return False
    try:
        write_f32(addr, value)
        return True
    except Exception:
        _warn_once(f"Failed f32 write at 0x{addr:08X} for key '{key}'")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# JP-specific convenience (when you know you want the JP constants directly)
# ──────────────────────────────────────────────────────────────────────────────

def jp(key: str) -> Optional[int]:
    """Direct access to ww_jp.ADDR for quick tests."""
    if _addr_mod is None:
        return None
    return _addr_mod.ADDR.get(key)  # type: ignore[attr-defined]
