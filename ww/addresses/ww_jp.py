"""
Wind Waker (JP) – base addresses and offsets.

Keep names stable so other modules (link.py, camera.py, collision.py) don’t break if we swap regions.
"""
# Game / engine timing
FRAME_COUNTER_ADDRESS          = 0x803E9D34  # s32/u32 frame counter used for parity/once-per-frame gates
# ── Scalars (absolute addresses) ──────────────────────────────────────────────
X_ADDRESS                      = 0x803D78FC
Y_ADDRESS                      = 0x803D7900
Z_ADDRESS                      = 0x803D7904

# In-game “actual speed” (pointer + offset pattern used historically)
ACTUAL_SPEED_POINTER           = 0x803B02E4
ACTUAL_SPEED_ADDRESS_OFFSET    = 0x00000444  # +0x444 from the dereferenced base

# Player “data”/actor base pointer (u32 at this address → Player struct)
PLAYER_POINTER                    = 0x803BD910

# Animation fields (relative to Link base)
ANIMATION_LENGTH               = 0x00003034
ANIMATION_INCREMENT_OFFSET     = 0x00003038
ANIMATION_POS_OFFSET           = 0x0000303C

# Link’s state field (relative to Link base)
PLAYER_STATE                    = 0x000031D8

# Equipent
EQUIPPED_ITEM_Y                = 0x803BDCD0
BOMB_COUNT                     = 0x803B8172

# Controller inputs (raw, absolute)
MAIN_STICK_X                   = 0x803E4412  # int8
MAIN_STICK_Y                   = 0x803E4413  # int8
CONTROLLER_INPUT               = 0x803E0D2A  # u16 buttons bitfield

# Camera/CS angle pointer chain
CSANGLE_BASE_PTR       = 0x803AD380  # u32 *
CSANGLE_PTR_OFFSET     = 0x00000034  # +0x34, u32 *
CSANGLE_U16_OFFSET     = 0x000002B0  # +0x2B0, final u16 angle

EVENT_MODE             = 0x803BD3A2

# camera-stick derived float helpers (absolute, float layout)
MAIN_STICK_X_FLOAT             = 0x80398308
MAIN_STICK_Y_FLOAT             = 0x8039830C
MAIN_STICK_VALUE_FLOAT         = 0x80398310
MAIN_STICK_ANGLE               = 0x80398314  # (halfword representation lives elsewhere)

# Collision
COLLISION_POINTER              = 0x803BDC40  # u32 pointer to collision block
COLLISION_OFFSET               = 0x00000496  # +0x496 → u16 flags

# Actor list
ACTOR_LIST_HEAD         = 0x803654CC  # pointer to head of zelda heap
# Node layout
ACTOR_NODE_NEXT_OFFSET  = 0x00
ACTOR_NODE_GPTR_OFFSET  = 0x0C
# fopACTg layout
ACTOR_GPROC_ID_OFFSET   = 0x08

# Actor offsets
ACTOR_XYZ_OFFSET        = 0x1F8
ACTOR_SPEED_OFFSET      = 0x254
ACTOR_XYZ_ANGLE_OFFSET  = 0x20C
ACTOR_GRAVITY_OFFSET    = 0x600

# ItemDrop offsets
ITEMDROP_TYPE_OFFSET    = 0x63A

# TBox offsets
TBOX_LIGHTING_OFFSET    = 0x3E8

# Boat/ship & crane
SHIP_POINTER                  = 0x803BDC50     
SHIP_CRANE_POS_PTR_OFFSET     = 0x434    
SHIP_MODE_OFFSET              = 0x34D     
# ── A convenience mapping so generic code can do: ADDR["X_ADDRESS"] → int ─────
ADDR = {name: value for name, value in globals().items() if name.isupper() and isinstance(value, int)}