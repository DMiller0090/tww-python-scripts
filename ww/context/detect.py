# ww/context/detect.py
from __future__ import annotations

from typing import Optional

def get_region_string() -> Optional[str]:
    """
    Read the  (Game ID + region) and returns a cleaned ASCII string.
    Returns None if the read fails or the bytes are not decodable.
    """
    try:
        from ww.memory import read_bytes
    except Exception:
        return None

    try:
        raw = read_bytes(0x80000000, 8)   # bytearray
        s = bytes(raw).decode("ascii", errors="ignore").replace("\x00", "")
        return s if s else None
    except Exception:
        return None


def detect_region():
    """
    Map Game ID to GameRegion enum.
    GZLJ01 -> JAPAN, GZLE01 -> NORTH_AMERICA, GZLP01 -> EUROPE.
    Raises ValueError if the ID is unsupported or unreadable.
    """
    # Lazy import to avoid ww.context <-> ww.memory cycles
    from .context import GameRegion

    game_id = get_region_string()
    if not game_id:
        raise ValueError("Unable to read game ID at 0x80000000.")

    # Debug prints if you want (comment out in production):
    print(f"[ww.detect] game_id={game_id!r}")

    if game_id.startswith("GZLJ01"):
        return GameRegion.JAPAN
    if game_id.startswith("GZLE01"):
        return GameRegion.NORTH_AMERICA
    if game_id.startswith("GZLP01"):
        return GameRegion.EUROPE

    raise ValueError(f"Game ID {game_id} is not supported.")