# ww/actors/gba.py
from __future__ import annotations
from typing import Dict

from .. import memory as mem
from ..actor import Actor, proc_id
from ww.addresses.address import Address
from . import register

_PID = proc_id("PROC_AGB")  # GBA link cable actor

@register(_PID)
class GBA(Actor):
    """
    Reads GBA input flags from the GBA actor instance.

    Offsets (bytes) relative to actor base:
      - Address.DISCONNECT_FLAG_OFFSET (0x640): bit0 == 1 when Y is pressed (disconnect)
      - Address.GBA_INPUT_OFFSET (0x672):
            bit0 (1) -> R pressed
            bit1 (2) -> L pressed
        Address.GBA_INPUT_OFFSET + 1 (0x673):
            bit0 (1)   -> A
            bit1 (2)   -> B
            bit2 (4)   -> Select (we’ll expose as 'Select', and also map to 'Z' for parity)
            bit3 (8)   -> Start
            bit4 (16)  -> Right
            bit5 (32)  -> Left
            bit6 (64)  -> Up
            bit7 (128) -> Down
    """
    __slots__ = ("_valid",)

    def __init__(self, p_addr: int) -> None:
        super().__init__(p_addr)
        self._valid = bool(p_addr)

    def _u8(self, off: int) -> int:
        try:
            return mem.read_u8(self.base + off)
        except Exception:
            return 0

    # Raw flag bytes
    def _flags0(self) -> int:
        return self._u8(int(Address.GBA_INPUT_OFFSET))

    def _flags1(self) -> int:
        return self._u8(int(Address.GBA_INPUT_OFFSET) + 1)

    def disconnect_pressed(self) -> bool:
        # Y on GBA used as "disconnect"
        val = self._u8(int(Address.DISCONNECT_FLAG_OFFSET))
        return val == 0#(val & 0x01) != 0

    def buttons(self) -> Dict[str, bool]:
        """
        Return a GBA-specific dict of booleans:
          A,B,L,R,Start,Select,Right,Left,Up,Down,Disconnect
        We also include GC-ish aliases for convenience: Z (== Select).
        """
        f0 = self._flags0()
        f1 = self._flags1()
        d  = {
            "R":      bool(f0 & 0x01),
            "L":      bool(f0 & 0x02),
            "A":      bool(f1 & 0x01),
            "B":      bool(f1 & 0x02),
            "Select": bool(f1 & 0x04),
            "Start":  bool(f1 & 0x08),
            "Right":  bool(f1 & 0x10),
            "Left":   bool(f1 & 0x20),
            "Up":     bool(f1 & 0x40),
            "Down":   bool(f1 & 0x80),
            "Disconnect": self.disconnect_pressed(),
        }
        # GC alias: map GBA Select to 'Z'
        d["Z"] = d["Select"]
        return d

    def upload_action(self, default: int = -1) -> int:
        """
            1 = uploadInitCheck
            2 = uploadPortCheckWait
            3 = uploadSelect
            4 = uploadJoyboot1
            6 = uploadJoyboot2
            7 = uploadMessageLoad
            5 = uploadMessageLoad2
            8 = uploadConnect
            10 = uploadMessageSend
        """
        off = Address.GBA_UPLOAD_ACTION_OFFSET
        if not (self._valid and isinstance(off, int)): return default
        try: return int(mem.read_u8(self.base + off))
        except Exception: return default