from __future__ import annotations
import math
import random
from typing import Iterable, Tuple, Optional, List
from dolphin import event, gui, savestate, controller, utils
from ww import actor, analog, game, mathutils
from ww.actors.player import Player
from ww.actors.chuchu import ChuChu
from ww.context.context import set_region
from ww.context.detect import detect_region

# ── knobs ─────────────────────────────────────────────────────────────────────
RELOAD_SLOT             = 8
SUCCESS_SLOT            = 9
SUCCESS_SLOT_1_CHU      = 10

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
SUCCESS_COUNTER_SIZE = 100 # how large to store RNG variance checker
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
_smallest_spread = math.inf # projected best spread of chus
_match_success_counter = []
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

def add_to_success_counter(value):
    global _match_success_counter
    if len(_match_success_counter) == SUCCESS_COUNTER_SIZE:
        _match_success_counter.pop(0) # remove first element
    _match_success_counter.append(value)
    
def calc_match_prop():
    global _match_success_counter
    if len(_match_success_counter) == 0:
        return 0
    success_count = 0
    for match in _match_success_counter:
        if match:
            success_count += 1
            
    return success_count / len(_match_success_counter)

Point = Tuple[float, float]   # (x, z)
Vec   = Tuple[float, float]   # (vx, vz)

def cross2(a: Vec, b: Vec) -> float:
    return a[0] * b[1] - a[1] * b[0]

def dot2(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]

def sub2(a: Point, b: Point) -> Vec:
    return (a[0] - b[0], a[1] - b[1])

def add2(a: Point, b: Vec) -> Point:
    return (a[0] + b[0], a[1] + b[1])

def mul2(v: Vec, s: float) -> Vec:
    return (v[0] * s, v[1] * s)

WALL_X1 = 2368.13
WALL_Z1 = 521.625488
WALL_X2 = 2390.056885
WALL_Z2 = 386.095825

def ray_hits_infinite_line(
    p: Point,
    v: Vec,
    a: Point,
    b: Point,
    *,
    eps: float = 1e-12,
    require_forward: bool = True,
) -> Optional[Tuple[Point, float, float]]:
    """
    Intersect ray p + t*v (t>=0 if require_forward) with infinite line through a->b.

    Returns (hit_point, t, u) where:
      - hit_point = p + t*v
      - line is a + u*(b-a)
    Returns None if no intersection under the chosen rules.
    """
    r = sub2(b, a)              # line direction
    denom = cross2(v, r)

    ap = sub2(a, p)

    # Parallel (or nearly)
    if abs(denom) < eps:
        # If collinear: the ray lies on the line. Treat as "hit" at t=0.
        if abs(cross2(ap, r)) < eps:
            rr = dot2(r, r)
            if rr < eps:
                raise ValueError("Wall line is degenerate: a and b are the same point.")
            u = dot2(sub2(p, a), r) / rr
            return (p, 0.0, u)
        return None

    # Solve using 2D cross products
    t = cross2(ap, r) / denom
    if require_forward and t < -eps:
        return None

    hit = add2(p, mul2(v, t))

    rr = dot2(r, r)
    if rr < eps:
        raise ValueError("Wall line is degenerate: a and b are the same point.")
    u = dot2(sub2(hit, a), r) / rr

    return (hit, t, u)

def furthest_impacts_on_wall(
    pellets: Iterable[Tuple[Point, Vec]],
    wall_a: Point,
    wall_b: Point,
    *,
    eps: float = 1e-12,
    require_forward: bool = True,
) -> Tuple[Point, Point, float]:
    """
    Projects each pellet ray until it hits the infinite wall line,
    then returns the two furthest impact points and their distance.
    """
    r = sub2(wall_b, wall_a)
    r_len = math.hypot(r[0], r[1])
    if r_len < eps:
        raise ValueError("Wall line is degenerate: wall_a and wall_b are the same point.")

    min_u = None
    max_u = None
    min_pt = None
    max_pt = None

    hits_found = 0

    for p, v in pellets:
        if math.hypot(v[0], v[1]) < eps:
            continue  # pellet not moving -> ignore

        res = ray_hits_infinite_line(p, v, wall_a, wall_b, eps=eps, require_forward=require_forward)
        if res is None:
            continue

        hit, t, u = res
        hits_found += 1

        if (min_u is None) or (u < min_u):
            min_u = u
            min_pt = hit
        if (max_u is None) or (u > max_u):
            max_u = u
            max_pt = hit

    if hits_found < 2 or min_pt is None or max_pt is None:
        #print(f"Need at least 2 valid pellet impacts; got {hits_found}.")
        #raise ValueError(f"Need at least 2 valid pellet impacts; got {hits_found}.")
        return -1,-1,-1

    # Points are collinear on the wall; distance is |Δu| * |r|
    dist = abs(max_u - min_u) * r_len
    return min_pt, max_pt, dist
        
@event.on_frameadvance
def update():
    global _initialized, _trials, _chus, _inputs, _trials, _match_count, _match_count_updated, _bonk_success_count, _bonk_success_count_updated, _best_frame,_within_threshold,_within_threshold_start_frame, _best_oob_count,_best_oob_count_frame,_match_success_counter,_smallest_spread
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
    
    # if _within_threshold == False and frame >= _best_frame + 60:
    #     if len(past_lilly_chus) != 10:
    #         reload_for_new_attempt()
    #         return
    #     else:
    #         _within_threshold = True
    #         _within_threshold_start_frame = frame
            
    if frame > _best_frame and frame < _best_frame + 30:
        if len(past_lilly_chus) == 10:
            pellets = []
            for chu in non_oob_chus:
                pellets.append((chu.pos2d(),chu.speed2d()))
            #print(pellets)
            _,_, dist = furthest_impacts_on_wall(pellets=pellets,wall_a=(WALL_X1,WALL_Z1),wall_b=(WALL_X2,WALL_Z2),require_forward=False)
            
            if dist == -1: # chu has invalid points
                reload_for_new_attempt()
                return
            if dist < _smallest_spread:
                _smallest_spread = dist
                savestate.save_to_slot(SUCCESS_SLOT_1_CHU)
    
    if frame > _best_frame + 30:
        reload_for_new_attempt()
        return
                
                      
    # test to see how many chus clip oob
    if _within_threshold:
        if frame > _within_threshold_start_frame + 300:
            reload_for_new_attempt()
            return
        else:
            oob_chus_count = 10 - len(non_oob_chus)
            if oob_chus_count == 1 and frame < _best_oob_count_frame:
                _best_oob_count = oob_chus_count
                _best_oob_count_frame = frame
                savestate.save_to_slot(SUCCESS_SLOT_1_CHU)
            # if oob_chus_count == _best_oob_count:
            #     if frame < _best_oob_count_frame:
            #         _best_oob_count_frame = frame
            #         savestate.save_to_slot(SUCCESS_SLOT_1_CHU)
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
                savestate.save_to_slot(SUCCESS_SLOT)
        
    

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
                    add_to_success_counter(True)
        
        if match:
            add_to_success_counter(False)
            reload_for_new_attempt()
            return
        # else:
        #     if not _match_success_added:
        #         _match_success_added = True
        #         add_to_success_counter(True)
        
        match_prop = calc_match_prop() * 100
        output_str += f"trials: {_trials}\n"
        output_str += f"left: {len(left_chus)}\n"
        output_str += f"right: {len(right_chus)}\n"
        output_str += f"past_lilly: {len(past_lilly_chus)}\n"
        output_str += f"non_oob_chus: {len(non_oob_chus)}\n"
        output_str += f"best_frame: {_best_frame}\n"
        output_str += f"smallest_spread: {_smallest_spread}\n"
        output_str += f"best_oob_count: {_best_oob_count}\n"
        output_str += f"best_oob_count_frame: {_best_oob_count_frame}\n"
        output_str += f"match_count: {_match_count} / {_bonk_success_count}\n"
        output_str += f"match_prop: {match_prop:.2f}% ({len(_match_success_counter)})"
        
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