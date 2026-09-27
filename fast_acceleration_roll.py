"""Live-verified fast-acceleration roll search for TWW JP.

Slot 5 is the starting state. Simulator ranked SIDE gates and L
timings are replayed in Dolphin. The four frame acceleration result is measured
directly and every improvement is saved to slot 4. The script never presses A.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import reload
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dolphin import controller, event, gui, memory, savestate
import fast_acceleration_roll_core

reload(fast_acceleration_roll_core)
from fast_acceleration_roll_core import ranked_gate_candidates, stick_for_world_target


CONTROLLER = 0
PLAYER_POINTER = 0x803AD860
CAMERA_POINTER = 0x803AD380
GAME_FRAME = 0x803E9D34
LIFE = 0x803B810B

ATN_MOVE = 7
DIR_LEFT = 2
DIR_RIGHT = 3
ACTION_KEYS = ("A", "B", "X", "Y", "Z", "Start", "R")
ACCELERATION_AGE = 3
MAX_ACCELERATION_SPEED = 17.0
TRIAL_TIMEOUT = 12


@dataclass(frozen=True)
class Trial:
    target: int
    offset: int
    potential: float
    l_rows: int


def _pointer(address: int) -> int:
    value = int(memory.read_u32(address))
    if not 0x80000000 <= value < 0x81800000:
        raise ValueError("Link/camera is not loaded")
    return value


def _camera_angle() -> int:
    root = _pointer(CAMERA_POINTER)
    camera = _pointer(root + 0x34)
    return int(memory.read_u16(camera + 0x2B0)) & 0xFFFF


def _snapshot() -> dict:
    if int(memory.read_u32(0x80000000)) != 0x475A4C4A:
        raise ValueError("Only the JP GZLJ memory layout is supported")
    player = _pointer(PLAYER_POINTER)
    return {
        "frame": int(memory.read_u32(GAME_FRAME)),
        "state": int(memory.read_u32(player + 0x3100)),
        "speed_f": float(memory.read_f32(player + 0x17C)),
        "normal_speed": float(memory.read_f32(player + 0x34E4)),
        "direction": int(memory.read_u32(player + 0x34B8)),
        "facing": int(memory.read_u16(player + 0x136)) & 0xFFFF,
        "camera": _camera_angle(),
        "life": int(memory.read_u8(LIFE)),
    }


def _parse_slot(value, default: int) -> int:
    try:
        return max(0, min(99, int(str(value).strip())))
    except Exception:
        return default


def _parse_trials(value) -> int:
    try:
        return max(1, min(4096, int(str(value).strip())))
    except Exception:
        return 128


def _set_input(target: int | None, l_held=False) -> None:
    pad = controller.get_gc_buttons(CONTROLLER)
    pad["Connected"] = True
    for key in ACTION_KEYS:
        pad[key] = False
    pad["L"] = bool(l_held)
    pad["A"] = False
    pad["TriggerLeft"] = 255 if l_held else 0
    pad["TriggerRight"] = 0
    if target is None:
        pad["StickX"], pad["StickY"] = 128, 128
    else:
        x, y, _ = stick_for_world_target(target, _camera_angle())
        pad["StickX"], pad["StickY"] = x, y
    pad["CStickX"], pad["CStickY"] = 128, 100
    controller.set_gc_buttons(CONTROLLER, pad)


_window = gui.window("Fast Acceleration Roll Brute Force")
_enabled_control = _window.checkbox("Enabled", checked=False)
_base_slot_control = _window.input_text("Base state slot", "5")
_best_slot_control = _window.input_text("Best result slot", "4")
_max_trials_control = _window.input_text("Maximum live trials", "128")
_left_gate_control = _window.checkbox("Target-walk left SIDE gate", checked=True)
_right_gate_control = _window.checkbox("Target-walk right SIDE gate", checked=True)
_start_button = _window.button("Start / restart search")
_status = _window.text("Save the starting state to slot 5, then start.")

_enabled = False
_running = False
_reload_requested = False
_base_slot = 5
_best_slot = 4
_max_trials = 128
_search_left_gate = True
_search_right_gate = True
_trials: list[Trial] = []
_trial_index = 0
_trial: Trial | None = None
_trial_frame = 0
_atn_age: int | None = None
_last_frame = None
_best_speed = -1.0
_best_trial: Trial | None = None
_message = "Ready"
_held_target: int | None = None
_held_l = False


def _hold_input(target: int | None, l_held=False) -> None:
    global _held_target, _held_l
    _held_target = target
    _held_l = bool(l_held)
    _set_input(_held_target, l_held=_held_l)


def _reissue_held_input() -> None:
    _set_input(_held_target, l_held=_held_l)


def _make_trials(state: dict) -> list[Trial]:
    gates = ranked_gate_candidates(
        state["facing"], state["camera"], max(64, _max_trials),
    )
    gates = [
        gate for gate in gates
        # setBlendAtnMoveAnime maps positive sin(delta) to DIR_LEFT and
        # negative sin(delta) to DIR_RIGHT.
        if (gate.offset > 0 and _search_left_gate)
        or (gate.offset < 0 and _search_right_gate)
    ]
    return [Trial(gate.target, gate.offset, gate.potential_roll_speed, l_rows)
            for gate in gates for l_rows in (1, 2)]


def _restart() -> None:
    global _running, _reload_requested, _base_slot, _best_slot, _max_trials
    global _search_left_gate, _search_right_gate
    global _trials, _trial_index, _trial, _best_speed, _best_trial, _message
    _base_slot = _parse_slot(_base_slot_control.value, 5)
    _best_slot = _parse_slot(_best_slot_control.value, 4)
    _max_trials = _parse_trials(_max_trials_control.value)
    _search_left_gate = bool(_left_gate_control.checked)
    _search_right_gate = bool(_right_gate_control.checked)
    if not _search_left_gate and not _search_right_gate:
        raise ValueError("select at least one target-walk SIDE gate")
    _trials = []
    _trial_index = 0
    _trial = None
    _best_speed = -1.0
    _best_trial = None
    _running = True
    _reload_requested = True
    direction = (
        "left and right" if _search_left_gate and _search_right_gate
        else "left" if _search_left_gate else "right"
    )
    _message = "Loading immutable base slot %d; searching %s SIDE gate(s)" % (
        _base_slot, direction,
    )


def _begin_trial() -> None:
    global _trials, _trial, _trial_frame, _atn_age
    global _last_frame, _message, _running
    state = _snapshot()
    if not _trials:
        _trials = _make_trials(state)[:_max_trials]
        if not _trials:
            raise RuntimeError("the simulator produced no SIDE-gate candidates")
    if _trial_index >= len(_trials):
        _running = False
        _hold_input(None)
        if _best_trial is None:
            _message = "Search exhausted: no complete acceleration window was produced"
        else:
            _message = "Search exhausted: best %.9f saved in slot %d" % (
                _best_speed, _best_slot,
            )
        return
    _trial = _trials[_trial_index]
    _trial_frame = 0
    _atn_age = None
    _last_frame = state["frame"]
    _hold_input(_trial.target, l_held=True)
    direction = "left" if _trial.offset > 0 else "right"
    _message = ("Trial %d/%d: %s offset=%+d L=%d potential=%.9f") % (
        _trial_index + 1, len(_trials), direction, _trial.offset, _trial.l_rows,
        _trial.potential,
    )


def _next_trial(reason: str) -> None:
    global _trial_index, _reload_requested, _message
    _trial_index += 1
    _reload_requested = True
    _hold_input(None)
    _message = "%s; reloading slot %d for trial %d" % (
        reason, _base_slot, _trial_index + 1,
    )


def _accept_acceleration(state: dict) -> None:
    global _best_speed, _best_trial, _running, _message
    speed = state["normal_speed"]
    if speed > _best_speed:
        _best_speed = speed
        _best_trial = _trial
        savestate.save_to_slot(_best_slot)
    if speed >= MAX_ACCELERATION_SPEED - 0.000001:
        _running = False
        _hold_input(None)
        _message = ("Complete: measured 17 acceleration speed on trial %d; "
                    "saved state to slot %d\noffset=%+d L=%d") % (
            _trial_index + 1, _best_slot, _trial.offset, _trial.l_rows,
        )
    else:
        _next_trial("measured acceleration %.9f (best %.9f in slot %d)" % (
            speed, _best_speed, _best_slot,
        ))


@event.on_hostupdate
def host_update() -> None:
    global _enabled, _running
    requested = bool(_enabled_control.checked)
    if _start_button.clicked or (requested and not _enabled):
        try:
            _enabled_control.checked = True
        except Exception:
            pass
        requested = True
        _restart()
    elif not requested and _enabled:
        _running = False
        _hold_input(None)
    _enabled = requested
    _status.set(_message if requested else "Disabled")


@event.on_savestateload
def state_loaded(from_slot, slot) -> None:
    del from_slot, slot
    global _last_frame
    _last_frame = None


@event.on_frameadvance
def update() -> None:
    global _reload_requested, _trial_frame, _last_frame, _atn_age
    global _message, _running
    if not _enabled or not _running:
        return
    try:
        if _reload_requested:
            _reload_requested = False
            _hold_input(None)
            savestate.load_from_slot(_base_slot)
            _begin_trial()
            return

        state = _snapshot()
        if state["frame"] == _last_frame:
            _reissue_held_input()
            return
        _last_frame = state["frame"]
        _trial_frame += 1

        if state["state"] == ATN_MOVE and state["normal_speed"] > 0.001:
            _atn_age = 0 if _atn_age is None else _atn_age + 1
            if _atn_age == 0:
                expected_direction = DIR_LEFT if _trial.offset > 0 else DIR_RIGHT
                if state.get("direction") in (DIR_LEFT, DIR_RIGHT) \
                        and state["direction"] != expected_direction:
                    _next_trial(
                        "target-walk direction mismatch (wanted %s, got mDirection=%d)"
                        % ("left" if expected_direction == DIR_LEFT else "right",
                           state["direction"])
                    )
                    return
        elif _atn_age is not None:
            _atn_age += 1

        if _atn_age is not None and _atn_age >= ACCELERATION_AGE:
            _hold_input(_trial.target)
            _accept_acceleration(state)
            return

        l_held = _trial_frame < _trial.l_rows
        _hold_input(_trial.target, l_held=l_held)

        if _trial_frame >= TRIAL_TIMEOUT:
            _next_trial("timeout before the four-frame acceleration window")
    except Exception as exc:
        _running = False
        _hold_input(None)
        _message = "Stopped: %s" % exc


print("[Fast Acceleration Roll Brute Force] loaded; disabled by default")
