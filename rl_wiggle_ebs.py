"""TWW JP automatic right/left wiggle EBS builder.

The script measures each ESS + C-stick segment from a savestate, adjusts the
camera magnitude until the requested speed loss frame is reached, then replays
the segment and switches two frames before that loss.  The fixed pattern is
R2,L1,R1,L1, or L2,R1,L1,R1 when inverted.
Slot 4 is the last verified lead start checkpoint.  Slot 3 holds an
uncommitted follow-start staging state. slot 4 is replaced only after the full
pair passes the instant-turn check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from dolphin import controller, event, gui, memory, savestate


CONTROLLER_ID = 0
BASE_STATE_SLOT = 5
CHECKPOINT_STATE_SLOT = 4
STAGING_STATE_SLOT = 3

PLAYER_POINTER_ADDRESS = 0x803BD910
POTENTIAL_SPEED_POINTER_ADDRESS = 0x803AD860
FRAME_COUNTER_ADDRESS = 0x803E9D34
FACING_Y_ADDRESS = 0x803EA3D2

CURRENT_ANGLE_Y_OFFSET = 0x206
TARGET_FACING_OFFSET = 0x34E8
POTENTIAL_SPEED_OFFSET = 0x34E4

ESS_RIGHT_X = 146
ESS_LEFT_X = 110
STICK_Y = 128
CSTICK_CENTER_X = 128
CSTICK_Y = 128

SIDE_RIGHT = "right"
SIDE_LEFT = "left"

RIGHT_DEFAULT_MAGNITUDE = 30
RIGHT_SHORT_DEFAULT_MAGNITUDE = 34
LEFT_DEFAULT_MAGNITUDE = 34
RIGHT_LONG_DROP_FRAME = 4
RIGHT_SHORT_DROP_FRAME = 3
RIGHT_PATTERN_MODES = (False, True)
LEFT_DROP_FRAME = 3
SWITCH_FRAMES_BEFORE_DROP = 2
SPEED_DROP_THRESHOLD = 2.0
MAX_MEASURE_FRAMES = 12
SNAP_LIMIT_DEGREES = 120.0
SNAP_GATE_HALFWORD = 0x6000

SHOW_OVERLAY = True
WINDOW_STYLE = """
QWidget {
  background: #171a1f;
  color: #e8f3ff;
  font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
  font-size: 13px;
}
QLabel { color: #b9c7d9; padding: 3px 0; }
QLineEdit {
  background: #101318;
  color: #ffffff;
  border: 1px solid #4e5968;
  border-radius: 4px;
  padding: 5px 7px;
}
QPushButton {
  background: #2d5f9a;
  color: #ffffff;
  border: 0;
  border-radius: 4px;
  padding: 6px 10px;
}
QPushButton:hover { background: #3972b5; }
"""

_window = gui.window("TWW R/L Wiggle EBS", style=WINDOW_STYLE)
_enabled_control = _window.checkbox("Enabled", checked=True)
_invert_control = _window.checkbox(
    "Invert pattern (L2 R1 L1 R1)", checked=False
)
_right_magnitude_control = _window.input_text(
    "Two-frame lead-side C-stick magnitude", str(RIGHT_DEFAULT_MAGNITUDE)
)
_right_short_magnitude_control = _window.input_text(
    "One-frame lead-side C-stick magnitude", str(RIGHT_SHORT_DEFAULT_MAGNITUDE)
)
_left_magnitude_control = _window.input_text(
    "One-frame follow-side C-stick magnitude", str(LEFT_DEFAULT_MAGNITUDE)
)
_speed_cutoff_control = _window.input_text(
    "Speed-loss cutoff", str(SPEED_DROP_THRESHOLD)
)
_base_slot_control = _window.input_text("Base savestate slot", str(BASE_STATE_SLOT))
_checkpoint_slot_control = _window.input_text(
    "Verified lead checkpoint slot", str(CHECKPOINT_STATE_SLOT)
)
_staging_slot_control = _window.input_text(
    "Uncommitted follow-side staging slot", str(STAGING_STATE_SLOT)
)
_start_button = _window.button("Start / restart from base state")
_status_control = _window.text("Load the base state, then start the builder.")


@dataclass(frozen=True)
class PlayerReadings:
    potential_speed: Optional[float]
    facing_y: Optional[int]
    current_angle_y: Optional[int]
    target_facing: Optional[int]


PHASE_CALIBRATE = "calibrate"
PHASE_BUILD_SWITCH = "build_switch"
PHASE_VERIFY_TURN = "verify_turn"
PHASE_COMMIT_SWITCH = "commit_switch"
PHASE_STOPPED = "stopped"

ACTION_CALIBRATE = "calibrate"
ACTION_BUILD = "build"
ACTION_COMMIT = "commit"

_phase = PHASE_CALIBRATE
_active_side = SIDE_RIGHT
_active_drop_target = RIGHT_LONG_DROP_FRAME
_active_checkpoint_slot = BASE_STATE_SLOT
_right_long_stock_magnitude = RIGHT_DEFAULT_MAGNITUDE
_right_short_stock_magnitude = RIGHT_SHORT_DEFAULT_MAGNITUDE
_left_stock_magnitude = LEFT_DEFAULT_MAGNITUDE
_right_magnitude = RIGHT_DEFAULT_MAGNITUDE
_left_magnitude = LEFT_DEFAULT_MAGNITUDE
_right_mode_short = False
_right_schedule_step = 0
_next_right_schedule_step = 1
_right_segment_source_slot = BASE_STATE_SLOT
_inverted = False

_measure_frame = 1
_replay_frame = 1
_safe_hold_frames = 1
_snap_previous_facing: Optional[int] = None
_left_start_facing: Optional[int] = None

_enabled_cached = False
_enabled_was_checked = False
_session_started = False
_reload_requested = False
_waiting_for_state = False
_reload_slot = BASE_STATE_SLOT
_reload_action = ACTION_CALIBRATE
_last_game_frame: Optional[int] = None
_last_speed: Optional[float] = None
_last_readings: Optional[PlayerReadings] = None

_trials = 0
_switches = 0
_magnitude_changes = 0
_snap_recoveries = 0
_last_message = "ready"


def _parse_int(text: object, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(text).strip())
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _valid_ptr(value: int) -> bool:
    return (0x80000000 <= value < 0x81800000) or (0x90000000 <= value < 0x94000000)


def _read_ptr(address: int) -> Optional[int]:
    try:
        value = int(memory.read_u32(address))
    except Exception:
        return None
    return value if _valid_ptr(value) else None


def _read_u16_ptr(base_address: int, offset: int) -> Optional[int]:
    ptr = _read_ptr(base_address)
    if ptr is None:
        return None
    try:
        return int(memory.read_u16(ptr + offset)) & 0xFFFF
    except Exception:
        return None


def _read_u16(address: int) -> Optional[int]:
    try:
        return int(memory.read_u16(address)) & 0xFFFF
    except Exception:
        return None


def _read_f32_ptr(base_address: int, offset: int) -> Optional[float]:
    ptr = _read_ptr(base_address)
    if ptr is None:
        return None
    try:
        return float(memory.read_f32(ptr + offset))
    except Exception:
        return None


def _read_game_frame() -> Optional[int]:
    try:
        return int(memory.read_u32(FRAME_COUNTER_ADDRESS))
    except Exception:
        return None


def _read_player() -> PlayerReadings:
    return PlayerReadings(
        potential_speed=_read_f32_ptr(POTENTIAL_SPEED_POINTER_ADDRESS, POTENTIAL_SPEED_OFFSET),
        facing_y=_read_u16(FACING_Y_ADDRESS),
        current_angle_y=_read_u16_ptr(PLAYER_POINTER_ADDRESS, CURRENT_ANGLE_Y_OFFSET),
        target_facing=_read_u16_ptr(PLAYER_POINTER_ADDRESS, TARGET_FACING_OFFSET),
    )


def _s16(value: int) -> int:
    value = int(value) & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _angle_delta(current: Optional[int], previous: Optional[int]) -> Optional[int]:
    if current is None or previous is None:
        return None
    return _s16(current - previous)


def _angle_degrees(delta: Optional[int]) -> Optional[float]:
    if delta is None:
        return None
    return abs(float(delta)) * 360.0 / 65536.0


def _target_facing_gap(readings: PlayerReadings) -> Optional[int]:
    facing = _player_facing(readings)
    if readings.target_facing is None or facing is None:
        return None
    return _s16(readings.target_facing - facing)


def _player_facing(readings: PlayerReadings) -> Optional[int]:
    """Link's gameplay facing; actor current-angle is diagnostic fallback only."""
    return (
        readings.facing_y
        if readings.facing_y is not None
        else readings.current_angle_y
    )


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def _opposite(side: str) -> str:
    return SIDE_LEFT if side == SIDE_RIGHT else SIDE_RIGHT


def _physical_side(side: str) -> str:
    return _opposite(side) if _inverted else side


def _physical_input(side: str, magnitude: int) -> tuple[int, int]:
    """Return the fully mirrored main-stick and signed camera input.

    Inversion applies to the complete input, not only the ESS direction:
    right/+C becomes left/-C and left/-C becomes right/+C. Magnitude remains
    attached to the pulse role (long lead, short lead, or follow), so calibrated
    retries cannot leak a value from the opposite role.
    """
    physical = _physical_side(side)
    if physical == SIDE_RIGHT:
        return ESS_RIGHT_X, abs(int(magnitude))
    return ESS_LEFT_X, -abs(int(magnitude))


def _magnitude(side: str) -> int:
    return _right_magnitude if side == SIDE_RIGHT else _left_magnitude


def _stock_magnitude(side: str) -> int:
    if side == SIDE_LEFT:
        return _left_stock_magnitude
    return (
        _right_short_stock_magnitude
        if _right_mode_short
        else _right_long_stock_magnitude
    )


def _right_is_short(step: int) -> bool:
    step = max(0, int(step))
    return RIGHT_PATTERN_MODES[step % len(RIGHT_PATTERN_MODES)]


def _right_requires_turn_check(step: int) -> bool:
    """The short lead pulse is rewound to a long pulse if its pair turns."""
    return _right_is_short(step)


def _signed_magnitude(side: str, magnitude: int) -> int:
    return _physical_input(side, magnitude)[1]


def _signed_camera(side: str) -> int:
    return _signed_magnitude(side, _magnitude(side))


def _set_sticks(side: str, camera_signed: Optional[int] = None) -> None:
    stick_x, mirrored_camera = _physical_input(side, _magnitude(side))
    if camera_signed is None:
        camera_signed = mirrored_camera
    inputs = controller.get_gc_buttons(CONTROLLER_ID)
    inputs["Connected"] = True
    inputs["StickX"] = stick_x
    inputs["StickY"] = STICK_Y
    inputs["CStickX"] = _clamp_byte(CSTICK_CENTER_X + camera_signed)
    inputs["CStickY"] = CSTICK_Y
    controller.set_gc_buttons(CONTROLLER_ID, inputs)


def _set_neutral() -> None:
    inputs = controller.get_gc_buttons(CONTROLLER_ID)
    inputs["Connected"] = True
    inputs["StickX"] = 128
    inputs["StickY"] = 128
    inputs["CStickX"] = 128
    inputs["CStickY"] = 128
    controller.set_gc_buttons(CONTROLLER_ID, inputs)


def _set_next_right_probe() -> None:
    next_short = _right_is_short(_next_right_schedule_step)
    magnitude = (
        _right_short_stock_magnitude if next_short else _right_long_stock_magnitude
    )
    _set_sticks(SIDE_RIGHT, _signed_magnitude(SIDE_RIGHT, magnitude))


def _speed_drop(
    previous: Optional[float], current: Optional[float]
) -> tuple[bool, Optional[float]]:
    if previous is None or current is None:
        return False, None
    loss = abs(previous) - abs(current)
    return loss > SPEED_DROP_THRESHOLD, loss


def _request_load(slot: int, action: str, reason: str) -> None:
    global _reload_requested, _waiting_for_state, _reload_slot, _reload_action, _last_message
    _reload_requested = True
    _waiting_for_state = True
    _reload_slot = slot
    _reload_action = action
    _last_message = reason


def _reset_frame_tracking() -> None:
    global _last_game_frame, _last_speed, _last_readings
    _last_game_frame = None
    _last_speed = None
    _last_readings = None


def _begin_calibration() -> None:
    global _phase, _measure_frame, _waiting_for_state, _trials, _last_message
    global _snap_previous_facing
    _phase = PHASE_CALIBRATE
    _measure_frame = 1
    _waiting_for_state = False
    _trials += 1
    _reset_frame_tracking()
    if _right_mode_short:
        _snap_previous_facing = (
            _left_start_facing if _active_side == SIDE_LEFT else None
        )
    _set_sticks(_active_side)
    _last_message = (
        "MEASURE %s C%+d for loss frame %d from slot %d"
        % (
            _physical_side(_active_side),
            _signed_camera(_active_side),
            _active_drop_target,
            _active_checkpoint_slot,
        )
    )


def _begin_build_switch(commit_only: bool = False) -> None:
    global _phase, _replay_frame, _waiting_for_state, _last_message
    global _snap_previous_facing
    global _right_segment_source_slot
    _phase = PHASE_COMMIT_SWITCH if commit_only else PHASE_BUILD_SWITCH
    _replay_frame = 1
    _waiting_for_state = False
    _reset_frame_tracking()
    if _right_mode_short:
        _snap_previous_facing = (
            _left_start_facing if _active_side == SIDE_LEFT else None
        )
    _set_sticks(_active_side)
    if _active_side == SIDE_RIGHT and not commit_only:
        # Do not overwrite this source until R+L has passed the turn check.
        _right_segment_source_slot = _active_checkpoint_slot
    _last_message = (
        "%s %s for %d frame(s), then switch to %s"
        % (
            "COMMIT" if commit_only else "REPLAY",
            _physical_side(_active_side),
            _safe_hold_frames,
            _physical_side(_opposite(_active_side)),
        )
    )


def _adjust_magnitude(observed_frame: int) -> None:
    global _right_magnitude, _left_magnitude, _magnitude_changes, _last_message
    old = _magnitude(_active_side)
    # Earlier loss means the camera was too strong; later loss means it was too weak.
    new = old - 1 if observed_frame < _active_drop_target else old + 1
    new = max(0, min(127, new))
    if new == old:
        _stop("camera magnitude exhausted at %d" % old)
        return
    if _active_side == SIDE_RIGHT:
        _right_magnitude = new
    else:
        _left_magnitude = new
    _magnitude_changes += 1
    _last_message = (
        "ADJUST %s loss frame %d, target %d: C%d -> C%d"
        % (_physical_side(_active_side), observed_frame, _active_drop_target, old, new)
    )
    _request_load(_active_checkpoint_slot, ACTION_CALIBRATE, _last_message)


def _calibration_drop(observed_frame: int, speed_loss: Optional[float]) -> None:
    global _safe_hold_frames, _last_message
    if observed_frame != _active_drop_target:
        _adjust_magnitude(observed_frame)
        return
    _safe_hold_frames = max(1, observed_frame - SWITCH_FRAMES_BEFORE_DROP)
    _last_message = (
        "CONFIRMED %s C%+d loss on frame %d (loss=%s); replay %d frame(s)"
        % (
            _physical_side(_active_side),
            _signed_camera(_active_side),
            observed_frame,
            "n/a" if speed_loss is None else "%.6f" % speed_loss,
            _safe_hold_frames,
        )
    )
    _request_load(_active_checkpoint_slot, ACTION_BUILD, _last_message)


def _finish_r_to_l_switch(readings: PlayerReadings) -> None:
    global _active_side, _active_drop_target, _active_checkpoint_slot
    global _left_magnitude, _next_right_schedule_step
    global _left_start_facing, _snap_previous_facing
    global _switches
    _set_sticks(SIDE_LEFT)
    # This state is provisional. CHECKPOINT_STATE_SLOT remains the
    # verified pre-lead source, so a failed short pair can be rebuilt long.
    savestate.save_to_slot(STAGING_STATE_SLOT)
    _switches += 1
    # This remains idempotent when turnaround recovery rebuilds the lead segment.
    _next_right_schedule_step = _right_schedule_step + 1
    _left_start_facing = _player_facing(readings)
    _snap_previous_facing = _left_start_facing
    _active_side = SIDE_LEFT
    _active_drop_target = LEFT_DROP_FRAME
    _active_checkpoint_slot = STAGING_STATE_SLOT
    _left_magnitude = _stock_magnitude(SIDE_LEFT)
    # Continue from the staging frame while keeping its slot for replay.
    _begin_calibration()


def _recover_turnaround(delta: Optional[int], snap_gap: Optional[int] = None) -> None:
    global _active_side, _active_drop_target, _active_checkpoint_slot
    global _right_magnitude, _right_mode_short
    global _right_schedule_step, _next_right_schedule_step
    global _snap_recoveries, _last_message
    _snap_recoveries += 1
    short_name = "L1" if _inverted else "R1"
    long_name = "L2" if _inverted else "R2"
    old_mode = short_name if _right_mode_short else long_name
    old_magnitude = _right_magnitude
    # Replace the preceding short lead pulse with a two-frame lead pulse.
    _right_mode_short = False
    _right_magnitude = _right_long_stock_magnitude
    # Restart the 2,1 cycle after replacing the failed short lead pulse.
    _right_schedule_step = 0
    _next_right_schedule_step = 1
    _active_side = SIDE_RIGHT
    _active_drop_target = RIGHT_LONG_DROP_FRAME
    _active_checkpoint_slot = _right_segment_source_slot
    degrees = _angle_degrees(delta)
    gap_degrees = _angle_degrees(snap_gap)
    cause = (
        "pending snap gate %.3f deg"
        % (0.0 if gap_degrees is None else gap_degrees)
        if snap_gap is not None and abs(snap_gap) > SNAP_GATE_HALFWORD
        else "facing change %.3f deg/frame"
        % (0.0 if degrees is None else degrees)
    )
    _last_message = (
        "TURNAROUND %s: reload pre-lead slot %d, replace %s C%d with %s C%d"
        % (
            cause,
            _right_segment_source_slot,
            old_mode,
            old_magnitude,
            long_name,
            _right_magnitude,
        )
    )
    _request_load(_right_segment_source_slot, ACTION_CALIBRATE, _last_message)


def _begin_post_l1_turn_check(readings: PlayerReadings) -> None:
    global _phase, _snap_previous_facing
    global _last_speed, _last_readings, _last_message
    _set_next_right_probe()
    _phase = PHASE_VERIFY_TURN
    _snap_previous_facing = _player_facing(readings)
    _last_speed = readings.potential_speed
    _last_readings = readings
    _last_message = "VERIFY delayed L1 turn before committing slot %d" % CHECKPOINT_STATE_SLOT


def _finish_snap_check(readings: PlayerReadings) -> None:
    global _active_side, _active_drop_target, _active_checkpoint_slot, _last_message
    # Commit only the proven follow-to-lead switch frame.
    _active_side = SIDE_LEFT
    _active_drop_target = LEFT_DROP_FRAME
    _active_checkpoint_slot = STAGING_STATE_SLOT
    _last_message = "turn check passed; rebuild exact follow->lead switch for slot 4"
    _request_load(STAGING_STATE_SLOT, ACTION_COMMIT, _last_message)


def _finish_l_to_r_commit(readings: PlayerReadings) -> None:
    global _active_side, _active_drop_target, _active_checkpoint_slot
    global _right_magnitude, _right_mode_short, _right_schedule_step
    global _switches
    _set_sticks(SIDE_RIGHT)
    savestate.save_to_slot(CHECKPOINT_STATE_SLOT)
    _switches += 1
    _active_side = SIDE_RIGHT
    _right_schedule_step = _next_right_schedule_step
    _right_mode_short = _right_is_short(_right_schedule_step)
    _active_drop_target = (
        RIGHT_SHORT_DROP_FRAME if _right_mode_short else RIGHT_LONG_DROP_FRAME
    )
    _active_checkpoint_slot = CHECKPOINT_STATE_SLOT
    _right_magnitude = _stock_magnitude(SIDE_RIGHT)
    # Continue directly from the verified checkpoint frame.  Reloading the
    # state here only repeats the same frame and adds one load per pair.
    _begin_calibration()


def _stop(reason: str) -> None:
    global _phase, _last_message
    _phase = PHASE_STOPPED
    _last_message = "STOPPED: " + reason
    print("[R/L Wiggle EBS] " + _last_message)


def _sample_game_frame() -> None:
    global _measure_frame, _replay_frame
    global _snap_previous_facing
    global _last_speed, _last_readings

    readings = _read_player()
    dropped, speed_loss = _speed_drop(_last_speed, readings.potential_speed)

    # Normal mode exposes the bad turn after R1 -> L1. Inverted mode can expose
    # the mirrored turn one sample earlier while its L1 lead is still active.
    # Do not apply that early check to normal R1 calibration: its intentional
    # facing motion would rewrite every R1 as R2.
    follow_guard = (
        _active_side == SIDE_LEFT
        and _phase in (PHASE_BUILD_SWITCH, PHASE_COMMIT_SWITCH)
    )
    inverted_early_guard = (
        _inverted
        and _active_side == SIDE_RIGHT
        and _phase in (PHASE_CALIBRATE, PHASE_BUILD_SWITCH)
    )
    if (
        _right_requires_turn_check(_right_schedule_step)
        and (follow_guard or inverted_early_guard)
    ):
        short_pair_delta = _angle_delta(
            _player_facing(readings), _snap_previous_facing
        )
        short_pair_degrees = _angle_degrees(short_pair_delta)
        # The gap predicts the delayed snap only after the follow-side switch.
        snap_gap = _target_facing_gap(readings) if follow_guard else None
        if (
            (
                short_pair_degrees is not None
                and short_pair_degrees > SNAP_LIMIT_DEGREES
            )
            or (snap_gap is not None and abs(snap_gap) > SNAP_GATE_HALFWORD)
        ):
            _recover_turnaround(short_pair_delta, snap_gap)
            return
        _snap_previous_facing = _player_facing(readings)

    if _phase == PHASE_CALIBRATE:
        if dropped:
            _calibration_drop(_measure_frame, speed_loss)
        elif _measure_frame >= MAX_MEASURE_FRAMES:
            _adjust_magnitude(MAX_MEASURE_FRAMES + 1)
        else:
            _measure_frame += 1
            _last_speed = readings.potential_speed
            _last_readings = readings
        return

    if _phase in (PHASE_BUILD_SWITCH, PHASE_COMMIT_SWITCH):
        if dropped:
            _stop(
                "replayed %s prefix lost speed early on frame %d"
                % (_physical_side(_active_side), _replay_frame)
            )
            return
        if _replay_frame >= _safe_hold_frames:
            if _active_side == SIDE_RIGHT:
                _finish_r_to_l_switch(readings)
            elif _phase == PHASE_COMMIT_SWITCH:
                _finish_l_to_r_commit(readings)
            else:
                if _right_requires_turn_check(_right_schedule_step):
                    _begin_post_l1_turn_check(readings)
                else:
                    _finish_snap_check(readings)
        else:
            _replay_frame += 1
            _last_speed = readings.potential_speed
            _last_readings = readings
        return

    if _phase == PHASE_VERIFY_TURN:
        delta = _angle_delta(_player_facing(readings), _snap_previous_facing)
        degrees = _angle_degrees(delta)
        if degrees is not None and degrees > SNAP_LIMIT_DEGREES:
            _recover_turnaround(delta)
            return
        _finish_snap_check(readings)
        return

def _restart_from_base() -> None:
    global BASE_STATE_SLOT, CHECKPOINT_STATE_SLOT, STAGING_STATE_SLOT
    global SPEED_DROP_THRESHOLD
    global _right_long_stock_magnitude, _right_short_stock_magnitude
    global _left_stock_magnitude
    global _right_magnitude, _left_magnitude, _right_mode_short
    global _right_schedule_step, _next_right_schedule_step
    global _right_segment_source_slot
    global _inverted
    global _left_start_facing, _snap_previous_facing
    global _phase, _active_side, _active_drop_target, _active_checkpoint_slot
    global _session_started, _reload_requested, _waiting_for_state
    global _trials, _switches, _magnitude_changes, _snap_recoveries, _last_message

    BASE_STATE_SLOT = _parse_int(_base_slot_control.value, 5, 0, 99)
    CHECKPOINT_STATE_SLOT = _parse_int(_checkpoint_slot_control.value, 4, 0, 99)
    STAGING_STATE_SLOT = _parse_int(_staging_slot_control.value, 3, 0, 99)
    if len({BASE_STATE_SLOT, CHECKPOINT_STATE_SLOT, STAGING_STATE_SLOT}) != 3:
        _stop("base, verified checkpoint, and staging slots must be different")
        return
    _right_long_stock_magnitude = _parse_int(
        _right_magnitude_control.value, RIGHT_DEFAULT_MAGNITUDE, 0, 127
    )
    _right_short_stock_magnitude = _parse_int(
        _right_short_magnitude_control.value,
        RIGHT_SHORT_DEFAULT_MAGNITUDE,
        0,
        127,
    )
    _left_stock_magnitude = _parse_int(
        _left_magnitude_control.value, LEFT_DEFAULT_MAGNITUDE, 0, 127
    )
    _right_magnitude = _right_long_stock_magnitude
    _left_magnitude = _left_stock_magnitude
    _inverted = bool(_invert_control.checked)
    try:
        SPEED_DROP_THRESHOLD = max(
            0.01, min(20.0, float(str(_speed_cutoff_control.value).strip()))
        )
    except (TypeError, ValueError):
        SPEED_DROP_THRESHOLD = 2.0

    _phase = PHASE_CALIBRATE
    _active_side = SIDE_RIGHT
    _active_drop_target = RIGHT_LONG_DROP_FRAME
    _active_checkpoint_slot = BASE_STATE_SLOT
    _right_mode_short = False
    _right_schedule_step = 0
    _next_right_schedule_step = 1
    _right_segment_source_slot = BASE_STATE_SLOT
    _left_start_facing = None
    _snap_previous_facing = None
    _session_started = True
    _reload_requested = False
    _waiting_for_state = False
    _trials = 0
    _switches = 0
    _magnitude_changes = 0
    _snap_recoveries = 0
    _last_message = "restart %s from base slot %d" % (
        "L2 R1 L1 R1" if _inverted else "R2 L1 R1 L1",
        BASE_STATE_SLOT,
    )
    _request_load(BASE_STATE_SLOT, ACTION_CALIBRATE, _last_message)


def _draw_overlay() -> None:
    if not SHOW_OVERLAY:
        return
    readings = _last_readings
    gui.draw_text(
        (15, 500),
        0xFFFFFFFF,
        (
            "TWW R/L Wiggle EBS\n"
            "pattern=%s  phase=%s side=%s target=%d local=%d\n"
            "lead C%d(stock %d,%s step %d)  follow C%d(stock %d)  source=%d staging=%d\n"
            "speed=%s facing=%s actorAngle=%s gap=%s trials=%d switches=%d adjusts=%d recoveries=%d\n"
            "%s"
        )
        % (
            "L2 R1 L1 R1" if _inverted else "R2 L1 R1 L1",
            _phase,
            _physical_side(_active_side),
            _active_drop_target,
            _measure_frame if _phase == PHASE_CALIBRATE else _replay_frame,
            _right_magnitude,
            _stock_magnitude(SIDE_RIGHT),
            "short" if _right_mode_short else "long",
            _right_schedule_step,
            _left_magnitude,
            _left_stock_magnitude,
            _right_segment_source_slot,
            STAGING_STATE_SLOT,
            "n/a"
            if readings is None or readings.potential_speed is None
            else "%.6f" % readings.potential_speed,
            "n/a" if readings is None or readings.facing_y is None else str(readings.facing_y),
            "n/a"
            if readings is None or readings.current_angle_y is None
            else str(readings.current_angle_y),
            "n/a" if readings is None or _target_facing_gap(readings) is None
            else str(_target_facing_gap(readings)),
            _trials,
            _switches,
            _magnitude_changes,
            _snap_recoveries,
            _last_message,
        ),
    )


@event.on_hostupdate
def _update_window() -> None:
    global _enabled_cached, _enabled_was_checked, _session_started

    requested_enabled = bool(_enabled_control.checked)
    if _start_button.clicked:
        _restart_from_base()
        try:
            _enabled_control.checked = True
        except (AttributeError, TypeError):
            pass
        requested_enabled = True
    elif requested_enabled and not _enabled_was_checked:
        _restart_from_base()
    elif not requested_enabled:
        _session_started = False

    _enabled_cached = requested_enabled and _session_started
    _enabled_was_checked = requested_enabled

    if not _enabled_cached:
        _status_control.set("Paused. Settings apply when Start / restart is pressed.")
    else:
        _status_control.set(_last_message)


@event.on_savestateload
def _on_savestateload(from_slot: bool, slot: int) -> None:
    del from_slot, slot
    # Some Dolphin builds dispatch this before the next frame callback and
    # others do not.  The frame callback also starts the requested action, so
    # this hook intentionally only clears stale sampling state.
    if _waiting_for_state:
        _reset_frame_tracking()


@event.on_frameadvance
def update() -> None:
    global _reload_requested, _waiting_for_state, _last_game_frame, _last_message

    if not _enabled_cached:
        _draw_overlay()
        return

    if _phase == PHASE_STOPPED:
        _set_neutral()
        _draw_overlay()
        return

    if _reload_requested:
        _set_neutral()
        _reload_requested = False
        _waiting_for_state = True
        try:
            savestate.load_from_slot(_reload_slot)
            if _reload_action == ACTION_CALIBRATE:
                _begin_calibration()
            elif _reload_action == ACTION_BUILD:
                _begin_build_switch(False)
            elif _reload_action == ACTION_COMMIT:
                _begin_build_switch(True)
            else:
                _stop("unknown reload action %s" % _reload_action)
        except Exception as exc:
            _reload_requested = True
            _last_message = "LOAD_FAILED slot=%d: %s" % (_reload_slot, exc)
        _draw_overlay()
        return

    if _waiting_for_state:
        _set_neutral()
        _draw_overlay()
        return

    if _phase == PHASE_VERIFY_TURN:
        _set_next_right_probe()
    else:
        _set_sticks(_active_side)
    _draw_overlay()

    game_frame = _read_game_frame()
    if game_frame is None:
        _sample_game_frame()
        return
    if _last_game_frame is None:
        _last_game_frame = game_frame
        return
    if game_frame == _last_game_frame:
        return

    _last_game_frame = game_frame
    _sample_game_frame()


print("[R/L Wiggle EBS] loaded")
