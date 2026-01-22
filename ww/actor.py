"""
ww.actor
--------
Actor (proc) helpers:
- Load ProcName/ProcValue mapping from CSV (ProcName,ProcValue,...).
- Resolve proc by name or value.
- Traverse the actor intrusive list and filter by proc.
- Return Actor objects with common accessors.

Expected addresses (region file):
  ACTOR_LIST_HEAD          : u32* head pointer (start of list)
  ACTOR_NODE_NEXT_OFFSET   : offset to next  (default 0x00)
  ACTOR_NODE_GPTR_OFFSET   : offset to gptr  (default 0x0C)
  ACTOR_GPROC_ID_OFFSET    : offset to u16 proc id inside gptr (default 0x08)

Optional (for Actor accessors):
  ACTOR_XYZ_OFFSET         : base offset to X (float); Y = +4, Z = +8
  ACTOR_SPEED_OFFSET       : float speed at gptr + offset
"""

from __future__ import annotations

import csv
from typing import Dict, Iterator, List, Optional, Type, Union, Set, Tuple

from . import memory as mem
from . import config, data_path
from . import mathutils as utils
from .addresses.address import Address

# ── Address keys & defaults ───────────────────────────────────────────────────

_DEF_NEXT = 0x00
_DEF_GPTR = 0x0C
_DEF_GPID = 0x08

def _off(v: Optional[int], fallback: int) -> int:
    return v if v is not None else fallback

# ── ProcName/ProcValue table ──────────────────────────────────────────────────
_NAME_TO_ID: Dict[str, int] = {}
_ID_TO_NAME: Dict[int, str] = {}
_LOADED = False

def _wrap_with_registered(gptr: int, pid: Optional[int]) -> "Actor":
    try:
        from .actors import wrapper_for_pid
        cls = wrapper_for_pid(pid)
        return cls(gptr)
    except Exception:
        return Actor(gptr)
def _default_csv_path() -> str:
    if config and getattr(config, "PROC_NAME_TABLE_PATH", None):
        return config.PROC_NAME_TABLE_PATH  # type: ignore[attr-defined]
    return data_path("proc_name_structs.csv")

def ensure_proc_table_loaded(path: Optional[str] = None) -> None:
    global _LOADED, _NAME_TO_ID, _ID_TO_NAME
    if _LOADED:
        return
    p = path or _default_csv_path()
    try:
        with open(p, "r", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                name = (row.get("ProcName") or "").strip()
                val  = (row.get("ProcValue") or "").strip()
                if not name or not val:
                    continue
                try:
                    pid = int(val, 0)
                except Exception:
                    continue
                _NAME_TO_ID[name] = pid
                _ID_TO_NAME.setdefault(pid, name)
        _LOADED = True
    except Exception as e:
        print(f"[ww.actor] Failed to load proc table '{p}': {e}")
        _LOADED = True  # avoid retry spam

def proc_id(proc: Union[str, int]) -> Optional[int]:
    """Resolve a proc (name or numeric) to its int id."""
    ensure_proc_table_loaded()
    if isinstance(proc, int):
        return proc
    pid = _NAME_TO_ID.get(proc)
    if pid is not None:
        return pid
    # case-insensitive fallback
    low = proc.lower()
    for k, v in _NAME_TO_ID.items():
        if k.lower() == low:
            return v
    return None

def proc_name(pid: int, default: str = "?") -> str:
    ensure_proc_table_loaded()
    return _ID_TO_NAME.get(int(pid), default)


class Actor:
    """
    Wraps a fopACTg* pointer (gptr) and exposes common fields.
    All reads are direct; memory validity is owned by ww.memory.
    """
    __slots__ = ("base",)

    def __init__(self, base: int) -> None:
        self.base: int = int(base)

    # --- identity ---
    @property
    def pid(self) -> Optional[int]:
        """u16 proc id at gptr + ACTOR_GPROC_ID_OFFSET."""
        id_off = _off(Address.ACTOR_GPROC_ID_OFFSET, _DEF_GPID)
        try:
            return int(mem.read_u16(self.base + id_off))
        except Exception:
            return None

    @property
    def name(self) -> str:
        p = self.pid
        return proc_name(p, "?") if p is not None else "?"

    # --- position (floats) ---
    def _xyz_base_off(self) -> Optional[int]:
        off = Address.ACTOR_XYZ_OFFSET
        return int(off) if isinstance(off, int) else None

    # --- angle ---
    def _angle_base_off(self) -> Optional[int]:
        off = Address.ACTOR_XYZ_ANGLE_OFFSET
        return int(off) if isinstance(off, int) else None
    
    @property
    def x(self) -> Optional[float]:
        off = self._xyz_base_off()
        if off is None:
            return None
        try:
            return float(mem.read_f32(self.base + off))
        except Exception:
            return None

    @property
    def y(self) -> Optional[float]:
        off = self._xyz_base_off()
        if off is None:
            return None
        try:
            return float(mem.read_f32(self.base + off + 4))
        except Exception:
            return None

    @property
    def z(self) -> Optional[float]:
        off = self._xyz_base_off()
        if off is None:
            return None
        try:
            return float(mem.read_f32(self.base + off + 8))
        except Exception:
            return None

    @property
    def angle_x(self) -> Optional[int]:
        off = self._angle_base_off()
        if off is None:
            return None
        try:
            return int(mem.read_u16(self.base + off))
        except Exception:
            return None
    @property
    def angle_y(self) -> Optional[int]:
        """
        Actor facing direction.
        """
        off = self._angle_base_off()
        if off is None:
            return None
        try:
            return int(mem.read_u16(self.base + off + 2))
        except Exception:
            return None
        
    @property
    def angle_y_deg(self) -> float:
        """
        Actor facing direction in degrees.
        """
        return utils.halfword_to_deg(self.angle_y)
    
    @property
    def angle_z(self) -> Optional[int]:
        off = self._angle_base_off()
        if off is None:
            return None
        try:
            return int(mem.read_u16(self.base + off + 4))
        except Exception:
            return None
        
    def pos3d(self) -> Optional[Tuple[float, float, float]]:
        x, y, z = self.x, self.y, self.z
        if x is None or y is None or z is None:
            return None
        return (x, y, z)

    def pos2d(self) -> Optional[Tuple[float, float]]:
        x, z = self.x, self.z
        if x is None or z is None:
            return None
        return (x, z)

    # --- speed (float) ---
    @property
    def speed_f(self) -> Optional[float]:
        off = Address.ACTOR_SPEED_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return float(mem.read_f32(self.base + int(off)))
        except Exception:
            return None
    @property
    def speed_x(self) -> Optional[float]:
        off = Address.ACTOR_XYZ_SPEED_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return float(mem.read_f32(self.base + int(off)))
        except Exception:
            return None
        
    @property
    def speed_y(self) -> Optional[float]:
        off = Address.ACTOR_XYZ_SPEED_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return float(mem.read_f32(self.base + int(off) + 4))
        except Exception:
            return None
        
    @property
    def speed_z(self) -> Optional[float]:
        off = Address.ACTOR_XYZ_SPEED_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return float(mem.read_f32(self.base + int(off) + 8))
        except Exception:
            return None
        
    def speed3d(self) -> Optional[Tuple[float, float, float]]:
        x, y, z = self.speed_x, self.speed_y, self.speed_z
        if x is None or y is None or z is None:
            return None
        return (x, y, z)
    
    def speed2d(self) -> Optional[Tuple[float, float]]:
        x, z = self.speed_x, self.speed_z
        if x is None or z is None:
            return None
        return (x, z)
    
    @property
    def gravity(self) -> Optional[float]:
        off = Address.ACTOR_GRAVITY_OFFSET
        if not isinstance(off, int):
            return None
        try:
            return float(mem.read_f32(self.base + int(off)))
        except Exception:
            return None
        
    # raw pointer if callers need deeper fields
    def ptr(self) -> int:
        return self.base

# ── Actor intrusive list traversal ────────────────────────────────────────────
def _is_valid(addr: Optional[int]) -> bool:
    if addr is None:
        return False
    try:
        return mem.is_valid_address(addr)
    except Exception:
        return 0x80000000 <= int(addr) < 0x81800000

def _head_ptr() -> Optional[int]:
    head_sym = Address.ACTOR_LIST_HEAD
    if head_sym is None:
        try:
            mem._warn_once(f"Missing address for key '{_K_HEAD}'")
        except Exception:
            pass
        return None
    try:
        return mem.read_pointer(head_sym)
    except Exception:
        return None

def _read_u32(addr: int) -> Optional[int]:
    try:
        return mem.read_u32(addr)
    except Exception:
        return None

def _read_u16(addr: int) -> Optional[int]:
    try:
        return mem.read_u16(addr)
    except Exception:
        return None

def iter_actor_nodes(start: Optional[int] = None, *, max_nodes: int = 10000) -> Iterator[int]:
    """
    Yield raw node pointers following next@+0x00 until end/loop/max.
    """
    if start is None:
        start = _head_ptr()
    if not _is_valid(start):
        return
    next_off = _off(Address.ACTOR_NODE_NEXT_OFFSET, _DEF_NEXT)

    seen: Set[int] = set()
    node = start
    n = 0
    while _is_valid(node) and node not in seen and n < max_nodes:
        yield node
        seen.add(node)
        n += 1
        nxt = _read_u32(node + next_off)
        if not _is_valid(nxt):
            break
        node = nxt

def iter_actors(
    proc: Optional[Union[str, int]] = None,
    *,
    start: Optional[int] = None,
    typed: bool = False,            # <— new
) -> Iterator[Actor]:
    want: Optional[int] = proc_id(proc) if isinstance(proc, str) else (proc if proc is None else int(proc))
    g_off = _off(Address.ACTOR_NODE_GPTR_OFFSET, _DEF_GPTR)
    id_off = _off(Address.ACTOR_GPROC_ID_OFFSET, _DEF_GPID)

    for node in iter_actor_nodes(start=start):
        gptr = _read_u32(node + g_off)
        if not _is_valid(gptr):
            continue
        pid = _read_u16(gptr + id_off)
        if want is not None and (pid is None or int(pid) != int(want)):
            continue
        yield _wrap_with_registered(gptr, pid) if typed else Actor(gptr)

def get_actors_by_type(obj: Type[Actor]) -> List[Actor]:
    return list(filter(lambda a: isinstance(a, obj), iter_actors(typed=True)))

def get_actors_by_proc(proc: Union[str, int], *, typed: bool = False) -> List[Actor]:
    return list(iter_actors(proc, typed=typed))
