"""
ww_server.py — Dolphin-side command bridge.

Toggle this on from Dolphin's Scripts GUI. It drains a file-based request
queue and executes commands against the live emulator: savestates,
pause / resume / frame-advance, and arbitrary eval/exec.

It is the server half of the bridge; the host-side client is `wwctl.py`.

Memory reads/writes are handled externally via dolphin_mem.py
(ReadProcessMemory/WriteProcessMemory) — safe while paused, no race.

Threading model: all ops run on on_frameadvance (emu thread only).
on_hostupdate is intentionally NOT used — running Python on two threads
concurrently causes GIL/refcount corruption and crashes Dolphin after ~5s.
Consequence: all bridge ops require emulation to be running.
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict

from dolphin import event, savestate, utils, gui

from ww import bridge, game

# --- frame-advance state -----------------------------------------------------
_frames_to_run = 0           # >0 means "let this many frames pass, then pause"
_advance_req_id = None       # request id awaiting the advance to finish
_running = True              # our best guess at running/paused (we drive toggle)

try:
    _osd = gui.add_osd_message  # type: ignore[attr-defined]
except Exception:
    _osd = None


def _log(msg: str) -> None:
    print("[ww_server] " + msg)
    if _osd:
        try:
            _osd("[ww_server] " + msg, 3000)
        except Exception:
            pass


def _do_savestate(args: Dict[str, Any]) -> Any:
    action = args["action"]
    if action == "save_slot":
        savestate.save_to_slot(int(args["slot"])); return {"saved_slot": int(args["slot"])}
    if action == "load_slot":
        savestate.load_from_slot(int(args["slot"])); return {"loaded_slot": int(args["slot"])}
    if action == "save_file":
        savestate.save_to_file(args["path"]); return {"saved_file": args["path"]}
    if action == "load_file":
        savestate.load_from_file(args["path"]); return {"loaded_file": args["path"]}
    raise ValueError("unknown savestate action: " + str(action))


# Namespace exposed to eval/exec — full power, by request.
def _eval_ns() -> Dict[str, Any]:
    import ww
    from dolphin import memory as dm, controller, registers
    return {
        "dm": dm, "game": game, "ww": ww,
        "savestate": savestate, "utils": utils, "controller": controller,
        "registers": registers,
    }


def _dispatch(op: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Return a response dict. All ops run on the emu thread (on_frameadvance only)."""
    global _frames_to_run, _advance_req_id, _running

    if op == "ping":
        return {"ok": True, "code": "ok", "result": "pong", "version": bridge.PROTOCOL_VERSION}
    if op == "status":
        return {"ok": True, "code": "ok", "result": {"running": _running, "frames_to_run": _frames_to_run}}
    if op == "pause":
        if _running:
            utils.toggle_play(); _running = False
        return {"ok": True, "code": "ok", "result": {"running": _running}}
    if op == "resume":
        if not _running:
            utils.toggle_play(); _running = True
        return {"ok": True, "code": "ok", "result": {"running": _running}}
    if op == "toggle":
        utils.toggle_play(); _running = not _running
        return {"ok": True, "code": "ok", "result": {"running": _running}}
    if op == "advance":
        return {"_advance": int(args.get("frames", 1))}
    if op == "savestate":
        return {"ok": True, "code": "ok", "result": _do_savestate(args)}
    if op == "frame":
        return {"ok": True, "code": "ok", "result": game.frame()}
    if op == "eval":
        return {"ok": True, "code": "ok", "result": repr(eval(args["code"], _eval_ns()))}
    if op == "exec":
        ns = _eval_ns()
        exec(args["code"], ns)
        return {"ok": True, "code": "ok", "result": ns.get("_result")}
    return {"ok": False, "code": "unknown_op", "error": "unknown op: " + str(op)}


def _service() -> None:
    global _frames_to_run, _advance_req_id, _running

    for req_id in bridge.list_request_ids():
        if req_id == _advance_req_id:
            continue
        req = bridge.read_json(bridge.req_path(req_id))
        if req is None:
            continue  # half-written; pick it up next tick
        op = req.get("op", "")

        try:
            resp = _dispatch(op, req.get("args") or {})
        except Exception as e:
            code = "no_game" if isinstance(e, ValueError) else "exception"
            resp = {"ok": False, "code": code,
                    "error": "{}: {}".format(type(e).__name__, e),
                    "traceback": traceback.format_exc()}

        if "_advance" in resp:
            _frames_to_run = max(1, resp["_advance"])
            _advance_req_id = req_id
            if not _running:
                utils.toggle_play(); _running = True
            continue  # response written when the countdown reaches 0

        bridge.write_response(req_id, resp)
        try:
            os.remove(bridge.req_path(req_id))
        except OSError:
            pass


@event.on_frameadvance
def _on_frame() -> None:
    global _frames_to_run, _advance_req_id, _running
    try:
        if _advance_req_id is not None:
            _frames_to_run -= 1
            if _frames_to_run <= 0:
                utils.toggle_play(); _running = False
                try:
                    cur = game.frame()
                except Exception:
                    cur = None
                bridge.write_response(_advance_req_id,
                                      {"ok": True, "code": "ok", "result": {"paused_at_frame": cur}})
                try:
                    os.remove(bridge.req_path(_advance_req_id))
                except OSError:
                    pass
                _advance_req_id = None

        _service()
    except Exception:
        _log("frameadvance error: " + traceback.format_exc())


bridge.ensure_spool()
_log("bridge listening @ " + bridge.SPOOL_DIR)
