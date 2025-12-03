# ww/actors/__init__.py
from __future__ import annotations
from typing import Dict, Type, Optional
from ..actor import Actor

# proc-id -> wrapper class
_REGISTRY: Dict[int, Type[Actor]] = {}

def register(proc_id: int):
    """Decorator to register a typed Actor wrapper for a given ProcValue."""
    def _wrap(cls: Type[Actor]) -> Type[Actor]:
        _REGISTRY[int(proc_id)] = cls
        return cls
    return _wrap

def wrapper_for_pid(pid: Optional[int]) -> Type[Actor]:
    if pid is None:
        return Actor
    return _REGISTRY.get(int(pid), Actor)

# Convenience re-exports (so callers can do: from ww.actors import Player)
from .player import Player  # noqa: F401
from .darknut import DarkNut
from .bokobaba import BokoBaba
from .itemdrop import ItemDrop
from .tbox import TBox
from .ship import Ship
from .gba import GBA
__all__ = ["register", "wrapper_for_pid", "Player", "Ship", "DarkNut", "BokoBaba", "ItemDrop", "TBox", "GBA"]
