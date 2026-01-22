# ss_charge_destination.py
"""
Charges a superswim towards destination, independent of camera angle.
Outputs number of inputs dropped.
There might be a small bug where it sometimes detects a dropped input when you load a savestate.
"""
from typing import Optional
from dolphin import event, controller, gui
from ww.actors import Player
from ww import analog, mathutils, game
from ww.context.context import set_region
from ww.context.detect import detect_region

# --- knobs ---
#Top of pillar
# DEST_X = 296177.25
# DEST_Z = 96816.1172

DEST_X = 202126.766
DEST_Z = -185459.266
ARROW_SWIM_DEG = 5.7  # angle to arrow swim at (keep at 0 for most use cases)
OFFSET_DEG = 90          # offset to swim at, for arrow swims, this should be 90 to swim towards destination
START_FACING_DEST = True # set this to True if starting swim facing destination

set_region(detect_region())

analog.load_table()

# state
_initialized = False
_start_frame = 0

player = Player()

# speed tracking
_last_frame = None            # last seen game frame (for duplicate callback detection)
_last_logged_frame = None     # last frame we actually logged
_prev_speed = None            # previous logged speed (baseline for delta check)
_speed_by_frame = {}          # dict[frame:int] = speed:float
_frame_drop_count = 0         # accumulated "drops" per rule

def _set_stick_unsigned(x255: int, y255: int) -> None:
    inputs = controller.get_gc_buttons(0)
    inputs["StickX"] = max(0, min(int(x255), 255))
    inputs["StickY"] = max(0, min(int(y255), 255))
    controller.set_gc_buttons(0, inputs)

def _neutral():
    _set_stick_unsigned(128, 128)

def _recompute_from_log(limit_frame: Optional[int] = None) -> None:
    """
    Recompute counters from _speed_by_frame without mutating the log.
    Only considers frames in [_start_frame .. limit_frame] if limit_frame is given;
    otherwise considers all frames >= _start_frame.
    Uses consecutive-frame segments (f == prev_f + 1) to count drops.
    """
    global _frame_drop_count, _prev_speed, _last_logged_frame

    if limit_frame is None:
        frames = sorted(f for f in _speed_by_frame if f >= _start_frame)
    else:
        frames = sorted(f for f in _speed_by_frame if _start_frame <= f <= limit_frame)

    drops = 0
    prev_f = None
    prev_s = None

    for f in frames:
        s = _speed_by_frame[f]
        if prev_f is None or prev_s is None or f != prev_f + 1:
            prev_f, prev_s = f, s
        else:
            # Good if current became >= 2.0 more negative: s <= prev_s - 2.0
            if not (s <= prev_s - 2.0):
                drops += 1
            prev_f, prev_s = f, s

    _frame_drop_count = drops
    _prev_speed = prev_s
    _last_logged_frame = prev_f

@event.on_frameadvance
def update():
    global _initialized, _start_frame
    global _last_frame, _last_logged_frame, _prev_speed, _frame_drop_count, _speed_by_frame

    cur_frame = game.frame()

    # Classify relative to last callback
    same_frame = (_last_frame is not None and cur_frame == _last_frame)
    went_back  = (_last_frame is not None and cur_frame <  _last_frame)      # rewind/load
    jumped     = (_last_frame is not None and cur_frame >  _last_frame + 1)  # skip forward
    sequential = (_last_frame is None) or (cur_frame == _last_frame + 1)

    # If we rewound, DO NOT truncate the log—just recompute up to cur_frame.
    if went_back:
        _recompute_from_log(limit_frame=cur_frame)

    # If we jumped forward, treat as discontinuity for *new* logging (keep history).
    if jumped:
        _prev_speed = None
        _last_logged_frame = None

    # initialize once
    if not _initialized:
        _start_frame = cur_frame

        pos = player.pos2d()
        if pos is None:
            _neutral(); _last_frame = cur_frame; return
        _initialized = True

    # flip 180° each game frame
    num_frames = cur_frame - _start_frame
    flip = (not START_FACING_DEST) if (num_frames % 2) == 0 else START_FACING_DEST

    xy = analog.stick_for_destination(
        DEST_X, DEST_Z,
        flip=flip,
        arrow_swim_deg=ARROW_SWIM_DEG,
        static_offset_deg=OFFSET_DEG
    )
    if xy is None:
        _neutral()
    else:
        sx, sy = xy
        _set_stick_unsigned(sx, sy)

    # ---- speed logging & drop detection ----
    # Log at most once per unique frame, and only if sequential.
    if sequential and not same_frame and _last_logged_frame != cur_frame:
        speed = player.speed_f
        if speed is not None:
            _speed_by_frame[cur_frame] = float(speed)

            if _prev_speed is None or _last_logged_frame is None or cur_frame != _last_logged_frame + 1:
                _prev_speed = float(speed)  # seed baseline
            else:
                if not (speed <= _prev_speed - 2.0):
                    _frame_drop_count += 1
                _prev_speed = float(speed)

            _last_logged_frame = cur_frame

        # GUI: show running stats (recomputed value if we just rewound)
        _, display_y = gui.get_display_size()
        gui.draw_text((15, display_y - 40), 0xffff0000,
                    f"Input Drops: {_frame_drop_count}")

    # Advance seen-frame marker
    _last_frame = cur_frame
