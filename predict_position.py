from __future__ import annotations
import math
from typing import Optional
from dolphin import event, gui
from ww import game, mathutils
from ww.camera import cs_angle_halfword
from ww.actors.player import Player
from ww.context.context import set_region
from ww.context.detect import detect_region

set_region(detect_region())

_gate = game.FrameGate()
_player = Player()

@event.on_frameadvance
def update():
    if not _gate.gate():
        return  # only run once per game frame
    

    stick_dist: float = _player.stick_distance()
    stick_dist *= stick_dist
    target_angle: int = mathutils.to_s16(_player.target_facing())
    angle_y: int = mathutils.to_s16(_player.angle_y)
    cs_angle: int = cs_angle_halfword()
    cs_angle_diff = 0x10000 - cs_angle
    stick_angle: int = _player.stick_angle()
    
    #(angle_hw - cs_hw - 0x8000) & 0xFFFF
    next_target_angle:int = mathutils.hw_wrap(stick_angle - cs_angle_diff - 0x8000)
    max: int = int(stick_dist * 3000)
    min: int = int(stick_dist * 100)
    
    # only do this if  target_angle - angle_y < 0x7801
    new_angle, _ = mathutils.cLib_addCalcAngleS(angle_y,next_target_angle,5,max,min)
    
    x,z = _player.debug_pos2d()
    new_x, new_z = mathutils.project2d(x,z,mathutils.halfword_to_deg(new_angle),17.0,lookup=True)
    
    print(
                (
                f"angle_y: {angle_y}\n"
                f"target: {target_angle}\n"
                f"new_target: {next_target_angle}\n"
                f"cs_angle: {cs_angle}\n"
                f"stick_angle: {stick_angle}\n"
                f"max: {max}\n"
                f"min: {min}\n"
                f"Stick distance: {stick_dist}\n"
                f"val: {new_angle}\n"
                f"old pos: {x}, {z}\n"
                f"new pos: {new_x}, {new_z}\n"
                "================================\n"
                )
    )