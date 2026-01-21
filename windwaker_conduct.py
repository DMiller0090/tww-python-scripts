# windwaker_conduct.py
from dolphin import event,controller
from ww import analog, game, mathutils
from ww.actors.player import Player

analog.load_table()
_gate = game.FrameGate()
_player = Player()

curr_angle = 0
song_index = 0
song = ["U","D","R"]
morse_code_list = "^^^vvv^v^^^vvv^v^v^v"
morse_code_start = 16133
morse_code_end = 16200

_initialized = False
start_frame = 0
curr_frame = 0
start_angle = 0
group = 0
speed = 0
def is_in_group(num, group_size):
    # Calculate the modulo based on the group size * 2 (true + false cycle)
    cycle = group_size * 2
    position = (num - 1) % cycle + 1
    #  Return true if the number falls within the "true" group, otherwise false
    return position <= group_size

    
def set_direction(dir):
    if(dir == "D"):
        return 128,0
    elif(dir == "L"):
        return 0,128
    elif(dir == "U"):
        return 128,255
    elif(dir == "R"):
        return 255,128
    elif(dir == "C"):
        return 128,128
    else:
        print("ERROR")
        
def _init():
    global start_frame, curr_frame, start_angle, group, speed
    start_frame = game.frame()
    curr_frame = start_frame
    start_angle = 315
    group = 6
    speed = (180 / group)

    
@event.on_frameadvance
def update():
    global _initialized, start_frame, curr_frame, start_angle, group, speed, curr_angle, song_index, song, morse_code_list, morse_code_start, morse_code_end
    if not _initialized:
        _init()
        _initialized = True
    
    curr_frame = game.frame()
    frames = curr_frame - start_frame
    # angle = mathutils.nfmod(start_angle + (speed * frames), 360)
    beat_frame = mathutils.nfmod(_player.ww_beat_frame() - 0, 29)
    curr_beat = _player.ww_curr_beat()
    
    info = ""
    if(beat_frame >= 4 and beat_frame < 5):
        song_index = curr_beat
    
    min_beat = 29.9
    max_beat = 0.5
    
    if((beat_frame >= min_beat or beat_frame < max_beat) and song_index < len(song)):
        info = "PLAY SONG"
        x,y = set_direction(song[song_index])
        inputs = controller.get_gc_buttons(0)
        inputs["CStickX"] = x
        inputs["CStickY"] = y
        controller.set_gc_buttons(0, inputs)
    else:  
        # circles
        info = "CIRCLES"
        curr_angle = mathutils.nfmod(start_angle + (speed * frames), 360)
        x,y = analog.stick_for_angle_deg(world_angle_deg=curr_angle, flip=False,dist_min=0.8, dist_max=1.0)
        inputs = controller.get_gc_buttons(0)
        inputs["CStickX"] = x
        inputs["CStickY"] = y
        #print(f"x: {x}, y: {y}")
        controller.set_gc_buttons(0, inputs)
        
    #in_group = is_in_group(frames,1)

    
    if(curr_frame < morse_code_start):
        inputs = controller.get_gc_buttons(0)
        inputs["StickX"] = 128
        inputs["StickY"] = 255
        controller.set_gc_buttons(0, inputs)
    elif(curr_frame >= morse_code_end):
        inputs = controller.get_gc_buttons(0)
        inputs["StickX"] = 128
        inputs["StickY"] = 0
        controller.set_gc_buttons(0, inputs)
    elif(curr_frame >= morse_code_start and curr_frame < morse_code_end):
        morse_code_len = int((morse_code_end - morse_code_start) / (len(morse_code_list) + 1))
        elapsed_frames = int(curr_frame - morse_code_start)
        curr_morse_code_idx = int(elapsed_frames / morse_code_len)
        if(curr_morse_code_idx < len(morse_code_list)):
            curr_char = morse_code_list[curr_morse_code_idx]
            
            if(curr_char == '^'):
                inputs = controller.get_gc_buttons(0)
                inputs["StickX"] = 128
                inputs["StickY"] = 255
                controller.set_gc_buttons(0, inputs)
            else:
                inputs = controller.get_gc_buttons(0)
                inputs["StickX"] = 128
                inputs["StickY"] = 0
                controller.set_gc_buttons(0, inputs)
            print(f"{curr_char} -> index: {curr_morse_code_idx}")
    
    
    #print(f"dir: {song[song_index]}, beat_frame: {beat_frame}, song_index: {song_index}, curr_beat: {curr_beat}, max_beat: {max_beat}, curr_angle: {curr_angle}, group: {group}, info: {info}") 
