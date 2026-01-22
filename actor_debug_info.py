# actor_debug_info.py
"""
Outputs general information about an actor.
"""
from typing import List
from dolphin import event, gui
from ww import actor, game, mathutils
from ww.actors import Player
from ww.actors.chuchu import ChuChu
from ww.context.context import set_region
from ww.context.detect import detect_region

set_region(detect_region())
_gate = game.FrameGate()
_player = Player()

@event.on_frameadvance
def update():
    if not _gate.gate():
        return  # only run once per game frame
    actor.ensure_proc_table_loaded()
    _, display_y = gui.get_display_size()
    output_str = ""
    count = 0
    
    player_x, player_z = _player.pos2d()
    
    # display info for actor
    for bb in actor.iter_actors(typed=True):
        if isinstance(bb, ChuChu):
            bb_x, bb_y, bb_z = bb.pos3d()
            bb_angle = bb.angle_y
            distance = mathutils.dist2d(player_x, player_z, bb_x, bb_z)
            count += 1
            output_str += f"======Actor {count}======\n"
            output_str += f"x: {bb_x}\n"
            output_str += f"y: {bb_y}\n"
            output_str += f"z: {bb_z}\n"
            output_str += f"angle: {bb_angle}\n"
            output_str += f"dist: {distance}\n"
            output_str += f"action: {bb.action}\n"

                     
    # If actor not registered, can also use proc name (lookup within /data/proc_name_structs.csv)
    # for bb in actor.iter_actors("PROC_BO",typed=False):
    #     bb_x, bb_y, bb_z = bb.pos3d()
    #     distance = mathutils.dist2d(player_x, player_z, bb_x, bb_z)
    #     count += 1
    #     output_str += f"======Actor {count}======\n"
    #     output_str += f"x: {bb_x}\n"
    #     output_str += f"y: {bb_y}\n"
    #     output_str += f"z: {bb_z}\n"
    #     output_str += f"distance: {distance}\n"
    
    # Can also find actors by proc name id (lookup within /data/proc_name_structs.csv)
    # for bb in actor.iter_actors(68,typed=False):
    #     bb_x, bb_y, bb_z = bb.pos3d()
    #     distance = mathutils.dist2d(player_x, player_z, bb_x, bb_z)
    #     count += 1
    #     output_str += f"======Actor {count}======\n"
    #     output_str += f"x: {bb_x}\n"
    #     output_str += f"y: {bb_y}\n"
    #     output_str += f"z: {bb_z}\n"
    #     output_str += f"action: {bb.action()}\n"
    #     output_str += f"behavior: {bb.behavior()}\n"
    #     output_str += f"pos_move3d: {bb.pos_move3d()}\n"
    #     output_str += f"check_player_dist_timer: {bb.check_player_dist_timer()}\n"
    #     output_str += f"update_pos_timer: {bb.update_pos_timer()}\n"
    #     output_str += f"action_timer: {bb.action_timer()}\n"
    
    if count > 0:
        gui.draw_text((15, display_y / 4), 0xffff0000, output_str)
    else:
        gui.draw_text((15, display_y / 4), 0xffff0000, "No actors of type found.")