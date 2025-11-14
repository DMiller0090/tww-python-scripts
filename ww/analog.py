"""
ww.analog
---------
CSV-backed angle → stick lookup with modulo-aware nearest search.

- Accepts an Actor/Player via `actor=...` for source position (preferred).
- If not provided, defaults to Player() instance.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import camera, config
from .actor import Actor
from .actors.player import Player
from .mathutils import deg_to_halfword, wrap_deg

# ──────────────────────────────────────────────────────────────────────────────
# Data model & loader
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AnalogRow:
    angle: int         # halfword 0..65535
    total_dist: float  # usually 1.0
    x: int             # 0..255
    y: int             # 0..255

class SortedAnalogTable:
    __slots__ = ("rows", "source")
    def __init__(self, rows: Sequence[AnalogRow], source: str = "<memory>") -> None:
        self.rows = list(rows)
        self.source = source

    @classmethod
    def from_csv(cls, path: str) -> "SortedAnalogTable":
        rows: List[AnalogRow] = []
        with open(path, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    x = int(row["input x"].strip())
                    y = int(row["input y"].strip())
                    total = float(row["total dist"])
                    angle = int(row["angle"]) & 0xFFFF
                except Exception:
                    continue
                rows.append(AnalogRow(angle=angle, total_dist=total, x=x, y=y))
        rows.sort(key=lambda a: a.angle)
        return cls(rows, source=path)

    def __len__(self) -> int:
        return len(self.rows)

_TABLE: Optional[SortedAnalogTable] = None
_TABLE_NAME: Optional[str] = None

def _default_path() -> str:
    if config and getattr(config, "INPUT_TABLE_PATH", None):
        return config.INPUT_TABLE_PATH  # type: ignore[attr-defined]
    return "ww/data/input_tables/superswim.csv"

def _resolve_path(path_or_key: Optional[str]) -> Tuple[str, Optional[str]]:
    if path_or_key is None:
        return (_default_path(), None)
    if config and hasattr(config, "INPUT_TABLES"):
        tables = getattr(config, "INPUT_TABLES")
        if isinstance(tables, dict) and path_or_key in tables:
            return (tables[path_or_key], path_or_key)  # type: ignore[index]
    return (path_or_key, None)

def load_table(path_or_key: Optional[str] = None, force_reload: bool = False) -> SortedAnalogTable:
    global _TABLE, _TABLE_NAME
    resolved, name = _resolve_path(path_or_key)
    if not force_reload and _TABLE is not None:
        same_name = (name is not None and name == _TABLE_NAME)
        same_path = (name is None and _TABLE.source == resolved)
        if same_name or same_path:
            return _TABLE
    try:
        _TABLE = SortedAnalogTable.from_csv(resolved)
        _TABLE_NAME = name if name is not None else resolved
    except Exception as e:
        print(f"[ww.analog] Failed to load analog CSV at '{resolved}': {e}")
        _TABLE = SortedAnalogTable([], source=resolved)
        _TABLE_NAME = name if name is not None else resolved
    return _TABLE

def current_table_info() -> Tuple[Optional[str], Optional[str]]:
    name = _TABLE_NAME
    src = _TABLE.source if _TABLE is not None else None
    return (name, src)

# ──────────────────────────────────────────────────────────────────────────────
# Nearest-angle search
# ──────────────────────────────────────────────────────────────────────────────

def _mod_diff(a: int, b: int) -> int:
    d = abs((a - b) & 0xFFFF)
    return d if d <= 0x8000 else 0x10000 - d

def _bisect_index(rows: Sequence[AnalogRow], target: int) -> int:
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid].angle < target:
            lo = mid + 1
        else:
            hi = mid
    return lo

def find_closest_xy(
    angle_halfword: int,
    table: Optional[SortedAnalogTable] = None,
    dist_min: float = 1.0,
    dist_max: float = 1.0,
    scan_window: int = 64,
) -> Optional[Tuple[int, int]]:
    tbl = table or load_table()
    if not tbl.rows:
        print("[ww.analog] Analog table is empty.")
        return None

    target = angle_halfword & 0xFFFF
    idx = _bisect_index(tbl.rows, target)

    best: Optional[AnalogRow] = None
    best_d = 0x10000

    left = max(0, idx - scan_window)
    right = min(len(tbl.rows) - 1, idx + scan_window)

    for i in range(left, right + 1):
        row = tbl.rows[i]
        if not (dist_min <= row.total_dist <= dist_max):
            continue
        d = _mod_diff(row.angle, target)
        if d < best_d:
            best_d, best = d, row
            if d == 0:
                break

    if best is None:
        # relax distance constraint locally
        for i in range(max(0, idx - 8), min(len(tbl.rows) - 1, idx + 8) + 1):
            row = tbl.rows[i]
            d = _mod_diff(row.angle, target)
            if d < best_d:
                best_d, best = d, row

    if best is None:
        return None
    return (best.x, best.y)

# ──────────────────────────────────────────────────────────────────────────────
# Destination helpers (camera-aware) — actor-driven
# ──────────────────────────────────────────────────────────────────────────────

def _apply_angle_corrections(
    angle_deg: float,
    *,
    flip: bool = False,
    arrow_swim_deg: Optional[float] = None,
    static_offset_deg: Optional[float] = None,
) -> float:
    if flip:
        angle_deg += 180.0
    if arrow_swim_deg is not None:
        angle_deg = angle_deg - arrow_swim_deg if flip else angle_deg + arrow_swim_deg
    if static_offset_deg is not None:
        angle_deg += float(static_offset_deg)
    return wrap_deg(angle_deg)

def _dest_angle_deg(dest_x: float, dest_z: float, src_x: float, src_z: float) -> float:
    return wrap_deg(math.degrees(math.atan2(dest_x - src_x, dest_z - src_z)))

def stick_for_destination(
    dest_x: float,
    dest_z: float,
    *,
    flip: bool = False,
    arrow_swim_deg: Optional[float] = None,
    static_offset_deg: Optional[float] = None,
    table: Optional[SortedAnalogTable] = None,
    dist_min: float = 1.0,
    dist_max: float = 1.0,
) -> Optional[Tuple[int, int]]:
    # pick the source actor (default: Link())
    a = Player()
    src = a.pos2d()
    if src is None:
        return None
    src_x, src_z = src

    angle_deg = _dest_angle_deg(dest_x, dest_z, src_x, src_z)
    angle_deg = _apply_angle_corrections(
        angle_deg,
        flip=flip,
        arrow_swim_deg=arrow_swim_deg,
        static_offset_deg=static_offset_deg,
    )

    angle_hw = deg_to_halfword(angle_deg)
    cs_hw = camera.cs_angle_halfword()
    stick_hw = (angle_hw - cs_hw - 0x8000) & 0xFFFF
    return find_closest_xy(stick_hw, table=table, dist_min=dist_min, dist_max=dist_max)

def stick_for_angle_deg(
    world_angle_deg: float,
    *,
    flip: bool = False,
    arrow_swim_deg: Optional[float] = None,
    static_offset_deg: Optional[float] = None,
    table: Optional[SortedAnalogTable] = None,
    dist_min: float = 1.0,
    dist_max: float = 1.0,
) -> Optional[Tuple[int, int]]:
    angle_deg = _apply_angle_corrections(
        world_angle_deg,
        flip=flip,
        arrow_swim_deg=arrow_swim_deg,
        static_offset_deg=static_offset_deg,
    )
    angle_hw = deg_to_halfword(angle_deg)
    cs_hw = camera.cs_angle_halfword()
    stick_hw = (angle_hw - cs_hw - 0x8000) & 0xFFFF
    return find_closest_xy(stick_hw, table=table, dist_min=dist_min, dist_max=dist_max)
