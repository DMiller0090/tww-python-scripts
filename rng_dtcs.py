from __future__ import annotations
import math
import random
from typing import List, Optional
from dolphin import event, gui, savestate, controller, utils
from ww import actor, analog, game, mathutils
from ww.actors.player import Player
from ww.actors.chuchu import ChuChu
from ww.context.context import set_region
from ww.context.detect import detect_region

# ── knobs ─────────────────────────────────────────────────────────────────────
RELOAD_SLOT             = 9
SUCCESS_SLOT            = 10

MIDDLE_CORNER_X         = 2355.18335
MIDDLE_CORNER_Z         = 552.666321
LEFT_CORNER_X           = 2903.48
LEFT_CORNER_Z           = -368.07
RIGHT_CORNER_X          = 2368.98
RIGHT_CORNER_Z          = 2454.53
LEFT_CORNER_DIST_THRESH = 750   # min dist to left corner to confirm chu is going to left corner
RIGHT_CORNER_DIST_THRESH = 1300  # min dist to right corner to confirm chu is going to right corner
CLOSE_TO_WALL = 2175            # x values ABOVE this are past the lilly and moving towards the corner
FALLING_OOB_HEIGHT = 300        # min height to confirm chu is falling oob

# movement validation
PULL_WW_FRAME = 19354 # the frame to pull out wind waker
B_FRAME = 19361
ROLL_FRAME = 19367 # the frame to roll
BONK_FRAME = 19375 # the frame Link's state will be 31

analog.load_table()
set_region(detect_region())
_gate = game.FrameGate()
_player = Player()
_initialized         = False
_chus: List[ChuChu] = None
_trials = 0
_inputs = None
_chu_positions_dict = {}
_match_count = 0                # number of unique chu configurations found
_match_count_updated = False    # whether or not we've applied an update to the _match_count
_bonk_success_count = 0
_bonk_success_count_updated = False
_best_frame = math.inf
_within_threshold = False
_within_threshold_start_frame = 0
_best_oob_count = 0 # number of chus clipped oob
_best_oob_count_frame = math.inf

def init():
    global _initialized, _chus
    actor.ensure_proc_table_loaded()
    _chus = actor.get_actors_by_type(ChuChu)
    savestate.save_to_slot(RELOAD_SLOT)
    _initialized = True
 
def action_pull_ww():
    global _inputs
    _inputs["X"] = True

def action_press_b():
    global _inputs
    _inputs["B"] = True
    
def action_back_walk_down():
    global _inputs
    _inputs["L"] = True
    _inputs["TriggerLeft"] = 255
    rand_angle = random.uniform(100 - 10, 100 + 10)
    x,y = analog.stick_for_angle_deg(
        rand_angle
    )
    _inputs["StickX"] = int(max(0, min(255, x)))
    _inputs["StickY"] = int(max(0, min(255, y))) 
    
def action_walk_down():
    global _inputs  
    rand_angle = random.uniform(125 - 30, 125 + 30)
    x,y = analog.stick_for_angle_deg(
        rand_angle
    )
    _inputs["StickX"] = int(max(0, min(255, x)))
    _inputs["StickY"] = int(max(0, min(255, y))) 

def action_rand_c_stick():
    global _inputs
    x = random.uniform(0, 255)
    y = random.uniform(0, 255)#random.uniform(0, 70)
    _inputs["CStickX"] = int(max(0, min(255, x)))
    _inputs["CStickY"] = int(max(0, min(255, y)))

def action_bonk_roll():
    global _inputs
    rand_angle = random.uniform(294 - 5, 294 + 20)
    x,y = analog.stick_for_angle_deg(
        rand_angle
    )
    _inputs["StickX"] = int(max(0, min(255, x)))
    _inputs["StickY"] = int(max(0, min(255, y)))
    _inputs["A"] = True 
    
def reload_for_new_attempt():
    global _trials, _player, _inputs, _match_count_updated, _bonk_success_count_updated, _within_threshold,_within_threshold_start_frame
    _trials += 1
    _match_count_updated = False
    _bonk_success_count_updated = False
    _within_threshold = False
    _within_threshold_start_frame = 0
    action_walk_down()
    action_rand_c_stick()
    controller.set_gc_buttons(0, _inputs) # apply inputs   
    savestate.load_from_slot(RELOAD_SLOT)
    
def avg_position_chus():
    global _chus
    x_sum = 0
    z_sum = 0
    
    for a in _chus:
        x,z = a.pos2d()
        x_sum += x
        z_sum += z
    return (x_sum + z_sum) / 2
    
@event.on_frameadvance
def update():
    global _initialized, _trials, _chus, _inputs, _trials, _match_count, _match_count_updated, _bonk_success_count, _bonk_success_count_updated, _best_frame,_within_threshold,_within_threshold_start_frame, _best_oob_count,_best_oob_count_frame
    _inputs = controller.get_gc_buttons(0)  
    if not _initialized:
        init()
  
    frame = game.frame()    
    
    output_str = ""
    non_oob_chus = list(filter(lambda a: a.y > FALLING_OOB_HEIGHT, _chus))
    left_chus = list(filter(lambda a: mathutils.dist2d(LEFT_CORNER_X, LEFT_CORNER_Z, a.x, a.z) < LEFT_CORNER_DIST_THRESH, non_oob_chus))
    right_chus = list(filter(lambda a: mathutils.dist2d(RIGHT_CORNER_X, RIGHT_CORNER_Z, a.x, a.z) < RIGHT_CORNER_DIST_THRESH, non_oob_chus))
    past_lilly_chus = list(filter(lambda a: a.x > CLOSE_TO_WALL, _chus))

    # if len(left_chus) > 0:
    #     reload_for_new_attempt()
    #     return
    if len(right_chus) > 0:
        reload_for_new_attempt()
        return
    
    if _within_threshold == False and frame >= _best_frame + 30:
        if len(past_lilly_chus) != 10:
            reload_for_new_attempt()
            return
        else:
            _within_threshold = True
            _within_threshold_start_frame = frame
            
    # test to see how many chus clip oob
    if _within_threshold:
        if frame > _within_threshold_start_frame + 300:
            reload_for_new_attempt()
            return
        else:
            oob_chus_count = 10 - len(non_oob_chus)
            if oob_chus_count > _best_oob_count:
                _best_oob_count = oob_chus_count
                _best_oob_count_frame = frame
                savestate.save_to_slot(SUCCESS_SLOT)
            if oob_chus_count == _best_oob_count:
                if frame < _best_oob_count_frame:
                    _best_oob_count_frame = frame
                    savestate.save_to_slot(SUCCESS_SLOT)
    else:        
        if frame <= PULL_WW_FRAME:
            action_back_walk_down()
            if frame == PULL_WW_FRAME:
                action_pull_ww()
        elif frame == B_FRAME or frame == B_FRAME + 2:
            action_press_b()
        elif frame <  ROLL_FRAME:
            action_walk_down()
        elif frame == ROLL_FRAME:
            action_bonk_roll()
        elif frame == BONK_FRAME:
            if _player.state() != 31:
                reload_for_new_attempt()
                return
            else:
                if not _bonk_success_count_updated:
                    _bonk_success_count += 1
                    _bonk_success_count_updated = True
        elif len(past_lilly_chus) == 10:
            if frame < _best_frame:
                _best_frame = frame
                #savestate.save_to_slot(SUCCESS_SLOT)
        
    

    match = False
    if not _gate.gate():
        if frame > 19385:
            avg_pos = avg_position_chus()
            if frame not in _chu_positions_dict:
                _chu_positions_dict[frame] = []
            if avg_pos in _chu_positions_dict[frame]:
                match = True
            else:
                _chu_positions_dict[frame].append(avg_pos)
                if not _match_count_updated:
                    _match_count += 1
                    _match_count_updated = True
        
        if match:
            reload_for_new_attempt()
            return
        
        output_str += f"trials: {_trials}\n"
        output_str += f"left: {len(left_chus)}\n"
        output_str += f"right: {len(right_chus)}\n"
        output_str += f"past_lilly: {len(past_lilly_chus)}\n"
        output_str += f"non_oob_chus: {len(non_oob_chus)}\n"
        output_str += f"best_frame: {_best_frame}\n"
        output_str += f"best_oob_count: {_best_oob_count}\n"
        output_str += f"best_oob_count_frame: {_best_oob_count_frame}\n"
        output_str += f"match_count: {_match_count} / {_bonk_success_count}"
    
    if frame < BONK_FRAME + 10:
        action_rand_c_stick()
    elif frame < 19405:
         _inputs["CStickX"] = 180
         _inputs["CStickY"] = 128
    else:
        _inputs["CStickX"] = 128
        _inputs["CStickY"] = 128
    controller.set_gc_buttons(0, _inputs) # apply inputs    
    _, display_y = gui.get_display_size()
    gui.draw_text((15, display_y / 3), 0xffff0000, output_str)