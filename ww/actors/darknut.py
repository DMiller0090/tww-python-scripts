# ww/actors/darknut.py
from __future__ import annotations
from typing import Tuple
from .. import memory as mem
from ..actor import Actor, proc_id
from ww.addresses.address import Address
from . import register

_PID = proc_id("PROC_TN")  # Register proc name

@register(_PID)
class DarkNut(Actor):
    __slots__ = ("_valid",)
    def __init__(self,p_addr) -> None:
        super().__init__(p_addr)
        self._valid = bool(p_addr)