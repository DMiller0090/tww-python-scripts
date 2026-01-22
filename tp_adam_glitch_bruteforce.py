# Standalone (Dolphin-only) search loop:
#  - Save BASE at current frame N
#  - Create MID at N+1 (wait for frame to increment)
#  - Run window [MID .. MID+MAX_FRAMES_PER_ATTEMPT)
#  - If no success: reload MID (K), advance 1 frame to K+1, overwrite MID there; repeat

from dolphin import event, controller, savestate, gui, utils, memory as mem

# ── fixed addresses ───────────────────────────────────────────────────────────
FRAME_ADDR        = 0x80430CD8   # u32 current frame
CHECK_ADDR        = 0x80406221   # s8; success when equals 0
STAGE_SWITCH_ADDR = 0x8040AFC6   # s8; success requires value 0x42

# ── slots ─────────────────────────────────────────────────────────────────────
BASE_SLOT    = 7   # starting snapshot
MID_SLOT     = 8   # moving “reload here” anchor
SUCCESS_SLOT = 9   # saved when success is found

# ── tuning ────────────────────────────────────────────────────────────────────
MAX_FRAMES_PER_ATTEMPT = 130

# ── helpers ───────────────────────────────────────────────────────────────────
def _read_frame():
    try:
        return int(mem.read_u32(FRAME_ADDR))
    except Exception:
        return 0

def _read_check_s8():
    try:
        return int(mem.read_s8(CHECK_ADDR))
    except Exception:
        return -1

def _read_stage_s8():
    try:
        return int(mem.read_s8(STAGE_SWITCH_ADDR))
    except Exception:
        return -1

def _hold_a(inputs):
    inputs["A"] = True
    inputs["AnalogA"] = 255

# ── state ─────────────────────────────────────────────────────────────────────
# States:
#  0: INIT           → save BASE at N, set target (N+1)
#  1: MAKE_MID       → wait until frame >= target, save MID (mid_frame = current), begin window
#  2: RUN_WINDOW     → hold A, check success; on timeout → state 3
#  3: STEP_MID_LOAD  → load MID (at K), set target (K+1) → state 4
#  4: STEP_MID_WAIT  → wait until frame >= target, save MID (mid_frame = current) → state 2
_state = 0

_base_frame = None     # N
_mid_frame  = None     # K (current MID anchor)
_target     = None     # “wait until frame >= target” when making/stepping MID
_window_end = None     # mid_frame + MAX_FRAMES_PER_ATTEMPT

_attempts = 0
_done = False

_last_check_val = None
_last_stage_val = None

@event.on_frameadvance
def run():
    global _state, _base_frame, _mid_frame, _target, _window_end, _attempts, _done
    global _last_check_val, _last_stage_val

    if _done:
        return

    cur = _read_frame()
    inputs = controller.get_gc_buttons(0)


    # ── state machine ─────────────────────────────────────────────────────────
    if _state == 0:
        # INIT: save BASE at N; set target N+1, move to MAKE_MID
        savestate.save_to_slot(BASE_SLOT)
        _base_frame = cur
        _target = _base_frame + 1
        _state = 1

    elif _state == 1:
        # MAKE_MID: wait until we have actually advanced one frame from BASE, then save MID
        if _target is not None and cur >= _target:
            savestate.save_to_slot(MID_SLOT)
            _mid_frame = cur
            _window_end = _mid_frame + MAX_FRAMES_PER_ATTEMPT
            _attempts += 1  # new window starting at this MID
            _state = 2

    elif _state == 2:
        # RUN_WINDOW: check success or timeout within [mid_frame .. mid_frame+MAX)
        _hold_a(inputs)
        val   = _read_check_s8()
        stage = _read_stage_s8()
        _last_check_val = val
        _last_stage_val = stage

        # Success condition
        if stage == 0x42 and val == 0:
            savestate.save_to_slot(SUCCESS_SLOT)
            _done = True
            utils.toggle_play()

        # Timeout => step MID by +1 frame
        if not _done and _window_end is not None and cur >= _window_end:
            _state = 3

    elif _state == 3:
        # STEP_MID_LOAD: reload current MID (K), set target = K+1, move to STEP_MID_WAIT
        if _mid_frame is None:
            # Should not happen, but fall back to BASE refresh.
            savestate.load_from_slot(BASE_SLOT)
            _base_frame = _read_frame()
            _target = _base_frame + 1
            _state = 1
        else:
            savestate.load_from_slot(MID_SLOT)
            _target = _mid_frame + 1
            _state = 4

    elif _state == 4:
        # STEP_MID_WAIT: wait until frame >= K+1, then overwrite MID there and restart window
        if _target is not None and cur >= _target:
            savestate.save_to_slot(MID_SLOT)
            _mid_frame = cur
            _window_end = _mid_frame + MAX_FRAMES_PER_ATTEMPT
            _attempts += 1
            _state = 2

    # ── HUD ───────────────────────────────────────────────────────────────────
    state_name = {
        0: "INIT(save BASE)",
        1: "MAKE_MID(wait N+1)",
        2: "RUN_WINDOW",
        3: "STEP_MID_LOAD(load MID@K)",
        4: "STEP_MID_WAIT(wait K+1 → overwrite MID)",
    }.get(_state, str(_state))

    base_txt = "-" if _base_frame is None else str(_base_frame)
    mid_txt  = "-" if _mid_frame  is None else str(_mid_frame)
    tgt_txt  = "-" if _target     is None else str(_target)
    win_txt  = "-" if _window_end is None else f"{_mid_frame}..{_window_end-1}"
    chk_txt  = "None" if _last_check_val is None else str(_last_check_val)
    stg_txt  = "None" if _last_stage_val is None else f"0x{_last_stage_val:02X}"

    gui.draw_text(
        (14, 140),
        0xFFFFCC00,
        "MID-stepping search (standalone)\n"
        f" state: {state_name}\n"
        f" attempts: {_attempts}\n"
        f" cur_frame: {cur}\n"
        f" BASE: {base_txt}   MID: {mid_txt}\n"
        f" target(for MID): {tgt_txt}\n"
        f" window: {win_txt}  (len={MAX_FRAMES_PER_ATTEMPT})\n"
        f" check({hex(CHECK_ADDR)}): {chk_txt}   stage({hex(STAGE_SWITCH_ADDR)}): {stg_txt}\n"
        f" slots: BASE={BASE_SLOT} MID={MID_SLOT} OK={SUCCESS_SLOT}\n"
    )

    # Always commit inputs once per callback
    controller.set_gc_buttons(0, inputs)
