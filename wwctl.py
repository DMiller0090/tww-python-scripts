#!/usr/bin/env python3
"""
wwctl.py — host-side client for the Dolphin command bridge.

Run from the repo root (so `from ww import bridge` resolves). It writes one
request, blocks until `ww_server.py` (running inside Dolphin) answers, and
prints the JSON result. Exit code is non-zero on error or timeout.

Examples
--------
  python wwctl.py ping
  python wwctl.py frame
  python wwctl.py savestate save 1
  python wwctl.py savestate load 1
  python wwctl.py savestate savefile "C:\\path\\state.sav"
  python wwctl.py pause | resume | toggle | status
  python wwctl.py advance 10
  python wwctl.py eval "game.frame()"
  python wwctl.py exec "_result = ww.some_fn()"

Note: memory reads/writes use dolphin_mem.py (ReadProcessMemory), not this bridge.
"""
from __future__ import annotations

import json
import os
import sys
import time

import importlib.util

# Load ww/bridge.py directly (not `from ww import bridge`): the ww package
# __init__ imports `dolphin`, which only exists inside the emulator runtime.
_BRIDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ww", "bridge.py")
_spec = importlib.util.spec_from_file_location("ww_bridge", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

# advance/eval/exec can legitimately take a while; give them more headroom.
DEFAULT_TIMEOUT = 10.0
LONG_TIMEOUT = 120.0


def _new_id() -> str:
    return "{}-{:06d}".format(time.time_ns(), os.getpid() % 1_000_000)


def _parse_num(s: str) -> int:
    return int(s, 0)  # honors 0x / 0o / 0b prefixes


def _send(op, args, timeout):
    req_id = _new_id()
    bridge.write_request(req_id, op, args)
    rp = bridge.resp_path(req_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = bridge.read_json(rp)
        if resp is not None:
            try:
                os.remove(rp)
            except OSError:
                pass
            return resp
        time.sleep(0.005)
    # clean up the unanswered request so the server doesn't act on it late
    try:
        os.remove(bridge.req_path(req_id))
    except OSError:
        pass
    return {"ok": False, "error": "timeout after {}s (is ww_server.py running in "
                                  "Dolphin, and emulation running for memory ops?)".format(timeout)}


def _build(argv):
    """Return (op, args, timeout) from CLI args."""
    cmd = argv[0]

    if cmd in ("ping", "frame", "pause", "resume", "toggle", "status"):
        return cmd, {}, DEFAULT_TIMEOUT

    if cmd == "savestate":
        action = argv[1]
        amap = {"save": "save_slot", "load": "load_slot",
                "savefile": "save_file", "loadfile": "load_file"}
        a = amap.get(action, action)
        args = {"action": a}
        if a.endswith("_slot"):
            args["slot"] = _parse_num(argv[2])
        else:
            args["path"] = argv[2]
        return "savestate", args, DEFAULT_TIMEOUT

    if cmd == "advance":
        n = _parse_num(argv[1]) if len(argv) > 1 else 1
        return "advance", {"frames": n}, LONG_TIMEOUT

    if cmd in ("eval", "exec"):
        return cmd, {"code": argv[1]}, LONG_TIMEOUT

    raise SystemExit("unknown command: " + cmd)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op, args, timeout = _build(sys.argv[1:])
    resp = _send(op, args, timeout)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
