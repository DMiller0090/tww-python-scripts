# actor_debug_info.py
"""
Outputs general information about an actor.
"""
from typing import List
from dolphin import event, gui
from ww import actor, game, mathutils
from ww.actors import Player, GBA
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
        if isinstance(bb, GBA):
            bb_x, bb_y, bb_z = bb.pos3d()
            distance = mathutils.dist2d(player_x, player_z, bb_x, bb_z)
            count += 1
            output_str += f"======Actor {count}======\n"
            output_str += f"x: {bb_x}\n"
            output_str += f"y: {bb_y}\n"
            output_str += f"z: {bb_z}\n"
            output_str += f"distance: {distance}\n"
            output_str += f"upload action: {bb.upload_action()}\n"
                     
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
    for bb in actor.iter_actors(68,typed=False):
        bb_x, bb_y, bb_z = bb.pos3d()
        distance = mathutils.dist2d(player_x, player_z, bb_x, bb_z)
        count += 1
        output_str += f"======Actor {count}======\n"
        output_str += f"x: {bb_x}\n"
        output_str += f"y: {bb_y}\n"
        output_str += f"z: {bb_z}\n"
        output_str += f"distance: {distance}\n"
    
    if count > 0:
        gui.draw_text((15, display_y / 4), 0xffff0000, output_str)
    else:
        gui.draw_text((15, display_y / 4), 0xffff0000, "No actors of type found.")