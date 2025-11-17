"""
ww
--
Wind Waker TAS utilities for Dolphin's Python runtime.

Typical usage:
    from ww import analog, camera, actors
    from ww.actors import Link
    L = Player()
    x, z = L.pos2d()
    sx, sy = analog.stick_for_destination(dest_x, dest_z, actor=L)
"""

from __future__ import annotations
import os

__version__ = "0.1.0"

PKG_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(PKG_DIR, "data")

def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)

# Optional config (ok if missing)
from . import config  # noqa: F401

# Re-export commonly used submodules/packages
from . import mathutils as mathutils
from . import memory as memory
from . import camera as camera
from . import collision as collision
from . import analog as analog
from . import actor as actor            # base Actor + traversal
from . import actors as actors          # package with typed actor subclasses
from . import game as game

__all__ = [
    "mathutils",
    "memory",
    "camera",
    "collision",
    "analog",
    "actor",
    "actors",
    "game",
    "config",
    "Player",
    "PKG_DIR",
    "DATA_DIR",
    "data_path",
    "__version__",
]
