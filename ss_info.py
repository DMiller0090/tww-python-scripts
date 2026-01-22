# ss_info.py
"""
Provides general purpose information about superswim.
Set DEST_X/DEST_Z knobs to specify destination
Outputs:
    Travel Direction: The direction Link is moving
    Angle to Dest: The direction Link needs to move at to go to destination
    Quadrants to Dest: The number of quadrants Link is away from the destination
"""
from __future__ import annotations
from typing import Optional
from dolphin import event, gui
from ww import game, mathutils
from ww.actors.player import Player
from ww.context.context import set_region
from ww.context.detect import detect_region

# --- knobs ---
#BPR
# DEST_X = 295727.281
# DEST_Z = 95239.7344
#FH
# DEST_X = 214790.39
# DEST_Z = 202194.14
DEST_X = 216786.39
DEST_Z = 203203.14
set_region(detect_region())
_player = Player()
_gate = game.FrameGate()
_pos_by_frame = {} # dict[frame:int] = (x:float,z:float)
@event.on_frameadvance
def update():
    if not _gate.gate():
        return  # only run once per game frame
    
    cur_frame = game.frame()
    cur_x, cur_y, cur_z = _player.debug_pos3d()
    _pos_by_frame[cur_frame] = _player.debug_x(),_player.debug_y(),_player.debug_z()
    if((cur_frame - 1) in _pos_by_frame):
        (prev_x, prev_y, prev_z) = _pos_by_frame[cur_frame - 1]
        hw_angle = mathutils.angle2d_hw(prev_z, prev_x, cur_z, cur_x)
        
        
        angle_to_destination = mathutils.angle2d_hw(cur_z, cur_x, DEST_Z, DEST_X)
        distance_quandrants = mathutils.dist2d(cur_x,cur_z,DEST_Z,DEST_X) / 100000
        
        y_dist = cur_y - prev_y
        gui.draw_text((15, 450), 0xffff0000, 
                      (
                        f"Travel Direction: {hw_angle}\n"
                        f"Angle to dest: {angle_to_destination}\n"
                        f"Quadrants to dest: {distance_quandrants:.8f}\n"
                        f"y_dist = {y_dist}"
                        )
                    )