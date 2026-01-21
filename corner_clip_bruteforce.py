# clip_bruteforce_per_frame_with_attempts.py
import math
from dolphin import event, controller, savestate, gui
import random, time
from ww.actors import Player
from ww import analog, camera, game, mathutils

# ── knobs ─────────────────────────────────────────────────────────────────────
RELOAD_SLOT             = 9
SUCCESS_SLOT            = 10

HW_MIN                  = 29500
HW_MAX                  = 32300
HW_RAND                 = 2000

ATTEMPT_MAX_FRAMES_INIT = 300      # initial cap; shrinks when we find a better success
MIN_SUCCESS_FRAME       = 17259   # ignore early noise until this frame

STICK_MIN               = 0.518 # min clip speed through testing
STICK_MAX               = 0.704 # max clip speed through testing

# Target seam (your inputs; logic left intact)
# SEAM_X = 199_664.171875
# SEAM_Z = -202_711.125
# SEAM_X = 202908.28125
# SEAM_Z = -209130.296875
SEAM_X = 202693.0
SEAM_Z = -209150.0

# ── state ─────────────────────────────────────────────────────────────────────
player = Player()
analog.load_table()

_initialized         = False
_last_seen_frame     = None
_trials              = 0
_attempt_start       = None         # game frame when current attempt started
attempt_max_frames   = ATTEMPT_MAX_FRAMES_INIT

# “best” result tracking
best_frame           = None         # earliest frame we achieved TRUE_SPEED_THRESH
best_trial_len       = None         # best_frame - _attempt_start captured for that attempt

# fastest_under_dist = math.inf # earliest frame below certain distance to seam
# dist_thresh = 3.0
longest_streak_of_fast_speed = 0

# ── helpers ───────────────────────────────────────────────────────────────────
def _frame_once_guard() -> bool:
    """True on first callback for a given game frame (Dolphin fires twice per frame)."""
    global _last_seen_frame
    f = game.frame()
    if _last_seen_frame == f:
        return False
    _last_seen_frame = f
    return True

def _hw_to_deg(hw: int) -> float:
    return (hw & 0xFFFF) * (360.0 / 65536.0)

def _neutral(inputs: dict):
    inputs["StickX"] = 128
    inputs["StickY"] = 128

def _start():
    global _attempt_start
    random.seed(int(time.time()))
    savestate.save_to_slot(RELOAD_SLOT)
    _attempt_start = game.frame()

def _reload_for_new_attempt():
    global player
    
    """Reload baseline and start a fresh attempt window."""
    global _trials, _attempt_start
    _trials += 1
    savestate.load_from_slot(RELOAD_SLOT)
    _attempt_start = game.frame()
    inputs = controller.get_gc_buttons(0)
    # inputs["StickX"] = 99
    # inputs["StickY"] = 164
    player_angle = player.angle_y_deg
    rand_dist = random.uniform(0.9,STICK_MAX)
    rand_angle = random.uniform(player_angle - 30, player_angle + 30)
    (x,y) = analog.stick_for_angle_deg(
        rand_angle,
        dist_min=rand_dist,
        dist_max=rand_dist + 0.05,
    )
    inputs["StickX"] = x
    inputs["StickY"] = y
    controller.set_gc_buttons(0, inputs)

def _record_success(cur: int, true_speed: float):
    """
    Save success state if it's an improvement and tighten the attempt window to that length.
    """
    global best_frame, best_trial_len, attempt_max_frames
    if cur < MIN_SUCCESS_FRAME:
        return
    improved = best_frame is None or cur < best_frame
    if improved:
        best_frame     = cur
        best_trial_len = (cur - _attempt_start) if _attempt_start is not None else None
        savestate.save_to_slot(SUCCESS_SLOT)
        print(f"[clip] NEW BEST at frame {cur} (true_speed={true_speed:.3f}); saved to slot {SUCCESS_SLOT}")
        if best_trial_len is not None and best_trial_len > 0:
            attempt_max_frames = min(attempt_max_frames, best_trial_len)

# ── main loop ─────────────────────────────────────────────────────────────────
@event.on_frameadvance
def update():
    global _initialized, _attempt_start, attempt_max_frames, longest_streak_of_fast_speed# fastest_under_dist, dist_thresh, 

    if not _initialized:
        _start()
        _initialized = True

    cur = game.frame()
    new_frame = _frame_once_guard()

    # Always fetch inputs (we will commit once per callback at the end)
    inputs = controller.get_gc_buttons(0)
    
    # ---- INPUTS: update EVERY callback ----
    # Angle selection (keep your logic): first ~6 frames of attempt: near 30000; then aim toward seam ± HW_RAND
    cur_x, cur_z = player.debug_pos2d()
    angle_to_destination = mathutils.angle2d_hw(cur_z, cur_x, SEAM_Z, SEAM_X)

    # if _attempt_start is not None and cur < _attempt_start + 5:
    #     angle_hw = mathutils.hw_wrap(41728 + random.randint(-HW_RAND * 1.35, HW_RAND))
    #     dist_min, dist_max = DIST_EARLY_MIN, DIST_EARLY_MAX
    # else:
    #     angle_hw = mathutils.hw_wrap(41728 + random.randint(-1000, 100))
    #     dist_min, dist_max = DIST_LATE_MIN,  DIST_LATE_MAX
    xy = None
    if cur <= 17533: # fast walk
        rand_angle_hw = mathutils.hw_wrap(random.randint(angle_to_destination - 1000, angle_to_destination - 500))
        #rand_angle_hw = mathutils.hw_wrap(random.randint(HW_MIN, HW_MAX))
        rand_angle_deg = mathutils.halfword_to_deg(rand_angle_hw)
        rand_dist = None
        if cur == 17523: # last frame or two slow down slightly before rolling, hoping for speed around 25
            rand_dist = random.uniform(0.88,0.88)
        else:
            rand_dist = 1.0

        xy = analog.stick_for_angle_deg(
            rand_angle_deg,
            flip=False,
            arrow_swim_deg=None,
            static_offset_deg=None,
            dist_min=rand_dist,
            dist_max=rand_dist + 0.02,
        )
        if cur == 17525:
            inputs["A"] = True # roll on this frame
    else:
        # rand_angle_hw = mathutils.hw_wrap(random.randint(angle_to_destination - 2000, angle_to_destination + 2000))
        # #rand_angle_hw = mathutils.hw_wrap(random.randint(HW_MIN, HW_MAX))
        # rand_angle_deg = mathutils.halfword_to_deg(rand_angle_hw)
        rand_dist = None
        if False:#cur < 17546: # first few frames after rolling, walk faster
            rand_dist = random.uniform(0.7,1.0)
        else:
            if cur == 17544:
                rand_dist = random.uniform(0.9,1.0)
            else:
                rand_dist = random.uniform(STICK_MIN,STICK_MAX)
            
        player_angle = player.angle_y_deg
        rand_angle = random.uniform(player_angle - 30, player_angle + 30)
        xy = analog.stick_for_angle_deg(
            rand_angle,
            dist_min=rand_dist,
            dist_max=rand_dist + 0.05,
        )
        # xy = analog.stick_for_angle_deg(
        #     rand_angle_deg,
        #     flip=False,
        #     arrow_swim_deg=None,
        #     static_offset_deg=None,
        #     dist_min=rand_dist,
        #     dist_max=rand_dist + 0.05,
        # )
    if cur >= 17540:
        inputs["CStickX"] = 128
        inputs["CStickY"] = 90
    if xy is None:
        _neutral(inputs)
    else:
        sx, sy = xy
        inputs["StickX"] = int(max(0, min(255, sx)))
        inputs["StickY"] = int(max(0, min(255, sy)))

    # ---- GAMEFLOW: only on first callback per frame ----
    if new_frame:
        true_speed = player.true_speed() or 0.0
        
        dist = mathutils.dist2d(cur_x,cur_z,SEAM_X,SEAM_Z)
        # Success check
        state = player.state()
        if state == 39 and cur >= MIN_SUCCESS_FRAME:
            _record_success(cur, true_speed)
            controller.set_gc_buttons(0, inputs)
            _reload_for_new_attempt()
            return
        if cur == 17527:
            if player.speed_f < 23:
                controller.set_gc_buttons(0, inputs)
                _reload_for_new_attempt()
        # if dist < dist_thresh and cur < fastest_under_dist:
        #     savestate.save_to_slot(8)
        #     fastest_under_dist = cur
        
        if cur >= 17546:
            true_speed = player.true_speed()
            if true_speed < 0.9:
                test = 5
                # controller.set_gc_buttons(0, inputs)
                # _reload_for_new_attempt()
            else:
                if cur > longest_streak_of_fast_speed:
                    savestate.save_to_slot(8) 
                    longest_streak_of_fast_speed = cur
                
        # Attempt window cap
        if _attempt_start is not None and (cur - _attempt_start) >= attempt_max_frames:
            print(f"[clip] ATTEMPT timeout ({attempt_max_frames} frames) → reload slot {RELOAD_SLOT}")
            controller.set_gc_buttons(0, inputs)
            _reload_for_new_attempt()
            return

        # Overlay
        try:
            best_str = f"{best_frame}" if best_frame is not None else "—"
            best_len = f"{best_trial_len}" if best_trial_len is not None else "—"
            gui.draw_text(
                (14, 280),
                0xFFFF8800,
                f"[clip brute]\n"
                f" frame: {cur}\n"
                f" trials: {_trials}\n"
                f" attempt_age: {cur - (_attempt_start or cur)} / {attempt_max_frames}\n"
                f" true_speed: {true_speed:.3f}\n"
                f" BEST: frame {best_str} (len {best_len})\n"
                f" longest_streak_of_fast_speed: {longest_streak_of_fast_speed}"
            )
        except Exception:
            pass

    # Commit inputs once per callback (every callback)
    controller.set_gc_buttons(0, inputs)
