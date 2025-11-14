# rng_salvage_cruise.py
# SALVAGE SCRIPT
# BEFORE RUNNING THIS SCRIPT:
#   1. Have the correct angle to salvage spot so no additional stick inputs are required during cruising
#   2. Determine the earliest frame you can press the crane button and get a salvage

# WHILE SCRIPT IS RUNNING:
# Script will cruise to spot with random control stick inputs and find what results in the fastest salvage
# Script also outputs usueful information at the top bar of dolphin
#    - bestOpenChestFrame: current best found frame. If this value doesn't decrease after awhile you probably have a pretty good solution
#    - earliestPullCraneFrame: the earliest frame the script was able to press the crane to get a successful salvage.
#        - if this number is higher than `minPullCraneFrame` after running the script for awhile, 
#          it means you should increase `minPullCraneFrame` to that number to speed up search
from dolphin import event, controller, savestate, gui
import random
import time

from ww.actors import Player
from ww.actors.ship import Ship      
from ww import actor as actor_mod
from ww import camera, game

# ──────────────────────────────────────────────────────────────────────────────
# Tweakables
# ──────────────────────────────────────────────────────────────────────────────
minPullCraneFrame     = 24948   # the earlest frame you can press the crane button to get a successful salvage. 
                                # If unsure, have this number be a LOW estimate and increase it to match `earliestPullCraneFrame` once you've ran it for awhile    
pullCraneFrameRange   = 1       # the amount of variance for when to start salvaging. Higher number = more thorough search
reloadSaveStateSlot   = 9       # set this to the save state slot the script will use to reload and retry crusing
bestSaveStateSlot     = 10      # set this to the save state slot the script will save the current best found salvage
maxSalvageCruiseSpeed = 2.8     # max cruising speed while searching
autoReducePullCraneFrame = True # set this false if you don't want the script to automatically decrease 'minPullCraneFrame' in the event that 'earliestPullCraneFrame' matches 'minPullCraneFrame'
craneBtn              = "X"     # button grappline hook is on

# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────
player = Player()
ship   = Ship()

currX = 0
currY = 0
currConStickX = 128
currModulo = 4
randDiff = 0

waitFrame = None
startFrame = 0
loadStateFrame = 0
loadingState = False
saveStateWaitFrame = None

bestOpenChestFrame = float("inf")
earliestPullCraneFrame = float("inf")
pullCraneFrame = 0
eventStateStart = None     # frame when camera.event_mode() first == 2
demo_item_found = False    # whether demo item actor 259 exists

_gate = game.FrameGate()
# ──────────────────────────────────────────────────────────────────────────────
# Input helpers (mutate a single dict per frame)
# ──────────────────────────────────────────────────────────────────────────────
def _press_button(inputs: dict, btn: str):
    inputs[btn] = True
    if btn == "R":
        inputs["TriggerRight"] = 255

def _set_main_stick(inputs: dict, x: int = None, y: int = None):
    if x is not None:
        inputs["StickX"] = max(0, min(int(x), 255))
    if y is not None:
        inputs["StickY"] = max(0, min(int(y), 255))

def _set_c_stick(inputs: dict, x: int = None, y: int = None):
    if x is not None:
        inputs["CStickX"] = max(0, min(int(x), 255))
    if y is not None:
        inputs["CStickY"] = max(0, min(int(y), 255))

# ──────────────────────────────────────────────────────────────────────────────
# Other helpers
# ──────────────────────────────────────────────────────────────────────────────
def _reload_state():
    global loadStateFrame, loadingState, pullCraneFrame, eventStateStart, demo_item_found
    loadStateFrame = game.frame()
    savestate.load_from_slot(reloadSaveStateSlot)
    loadingState = True
    pullCraneFrame = minPullCraneFrame + random.randint(0, pullCraneFrameRange)
    eventStateStart = None
    demo_item_found = False

# ──────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────────────
def _init():
    global startFrame, currX, currY, pullCraneFrame, randDiff
    random.seed(int(time.time()))
    savestate.save_to_slot(reloadSaveStateSlot)
    startFrame = game.frame()
    currX = random.randint(0, 255)
    currY = random.randint(0, 255)
    pullCraneFrame = minPullCraneFrame + random.randint(0, pullCraneFrameRange)
    randDiff = random.randint(0, 15)

# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────
_initialized = False

@event.on_frameadvance
def update():
    global _initialized, waitFrame, saveStateWaitFrame, loadingState, loadStateFrame
    global bestOpenChestFrame, earliestPullCraneFrame, minPullCraneFrame, pullCraneFrameRange
    global currX, currY, currConStickX, currModulo, randDiff, eventStateStart, demo_item_found

    if not _initialized:
        _init()
        _initialized = True

    currentFrame = game.frame()
    inputs = controller.get_gc_buttons(0)  # ← fetch ONCE

    # Handle delayed save → reload
    if saveStateWaitFrame is not None:
        if currentFrame >= saveStateWaitFrame:
            saveStateWaitFrame = None
            controller.set_gc_buttons(0, inputs)  # commit whatever we have this frame
            _reload_state()
        else:
            controller.set_gc_buttons(0, inputs)
        return

    # Handle load in progress (ensure we land exactly on startFrame/startFrame+1)
    if loadingState:
        if currentFrame in (startFrame, startFrame + 1):
            loadingState = False
            loadStateFrame = 0
        if currentFrame > loadStateFrame + 1:
            controller.set_gc_buttons(0, inputs)
            savestate.load_from_slot(reloadSaveStateSlot)
            return
        controller.set_gc_buttons(0, inputs)
        return

    # #- Read world/game state #-
    craneY     = ship.crane_y(default=float("inf"))
    eventState = camera.event_mode()   # 2 = cutscene start detection
    korlSpeed  = ship.speed_f
    korlState  = ship.mode()                    # u8 state/mode

    if _gate.gate(): # draw once per frame
        gui.draw_text((15, 250), 0xffff0000, f"[salvage]\n frame={currentFrame}\n craneY={craneY:.3f}\n pull={pullCraneFrame}\n "
            f"bestOpen={bestOpenChestFrame}\n earliestPull={earliestPullCraneFrame}\n "
            f"min={minPullCraneFrame}\n KORLspd={korlSpeed:.2f}")

    # #- Cruising until pullCraneFrame #-
    if currentFrame < pullCraneFrame:
        _press_button(inputs, "R")  # hold cruise

    else:
        # press grapple (crane) on/after the chosen frame
        _press_button(inputs, craneBtn)

        # bail if we dipped too low or taking too long relative to best
        if craneY < -1500.0 or currentFrame > bestOpenChestFrame + 10:
            controller.set_gc_buttons(0, inputs)
            _reload_state()
            return
        
        # track when event/cutscene transitions begin
        if eventState == 2 and eventStateStart is None:
            eventStateStart = currentFrame

        if eventStateStart is not None:
            linkState = player.state()

            # After some frames into the event, confirm “demo item” actor (ProcValue 259) exists.
            if not demo_item_found and currentFrame > eventStateStart + 15:
                found_any = False
                for _a in actor_mod.iter_actors(259):  # demo item proc
                    found_any = True
                    break
                if not found_any:
                    controller.set_gc_buttons(0, inputs)
                    _reload_state()
                    return
                demo_item_found = True
                # Auto-update earliestPullCraneFrame/minPullCraneFrame
                if earliestPullCraneFrame > pullCraneFrame:
                    earliestPullCraneFrame = pullCraneFrame
                    if autoReducePullCraneFrame and (minPullCraneFrame == earliestPullCraneFrame):
                        minPullCraneFrame -= 1
                        pullCraneFrameRange += 1  # widen search range

            # Link opening chest state (211)
            if linkState == 211:
                if currentFrame < bestOpenChestFrame:
                    bestOpenChestFrame = currentFrame
                    savestate.save_to_slot(bestSaveStateSlot)
                    # small delay before reloading the search slot, not sure if necessary
                    saveStateWaitFrame = currentFrame + 5
                else:
                    controller.set_gc_buttons(0, inputs)
                    _reload_state()
                controller.set_gc_buttons(0, inputs)
                return

    # #- C-stick randomization #-
    if currentFrame % currModulo != 0:
        _set_c_stick(inputs, currX, currY)
    else:
        currModulo = random.randint(3, 30)
        currX = random.randint(76, 178)
        currY = random.randint(55, 200)
        currConStickX = random.randint(60, 128)
        _set_c_stick(inputs, currX, currY)

    # #- Minor main-stick tweaks near salvage #-
    if currentFrame > minPullCraneFrame - 30:
        _set_main_stick(inputs, x=currConStickX)
    else:
        if (currentFrame % 2) == 0:
            randDiff = random.randint(0, 15)
            _set_main_stick(inputs, x=146 + randDiff)
        else:
            _set_main_stick(inputs, x=110 - randDiff)

    # #- Random cruising assistance during salvage #-
    # korlState == 10, eventState == 0, speed below threshold
    if korlState == 10 and eventState == 0 and korlSpeed < maxSalvageCruiseSpeed:
        if korlSpeed < (maxSalvageCruiseSpeed - 0.2):
            _press_button(inputs, "R")
        elif random.random() > 0.3:
            _press_button(inputs, "R")

    # Failsafe guard
    if currentFrame > pullCraneFrame + 30 and korlSpeed > 5.0:
        controller.set_gc_buttons(0, inputs)
        _reload_state()
        return

    # Commit inputs once for the frame
    controller.set_gc_buttons(0, inputs)
