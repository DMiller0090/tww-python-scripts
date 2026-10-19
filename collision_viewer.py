"""GPU collision viewer for The Wind Waker JP (GZLJ01).
"""
from __future__ import annotations

import atexit
import csv
import json
import math
import os
import struct
import sys
import textwrap
from array import array

# On Windows the fork adds this Scripts folder to Python's module path; the macOS/Linux build does not,
# so `import ww` fails there. Put the folder this script lives in on sys.path ourselves.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from dolphin import debug, event, gui, memory, registers

from ww.actor import proc_name
from ww.addresses.address import Address
from ww.collider import (ColliderDecoder, attack_info as _collider_attack_info,
                         attack_type_names as _attack_type_names, finite_vec as _finite_vec,
                         target_type_name as _target_type_names, u16 as _u16, u32 as _u32,
                         physical_collision_name as _physical_collision_name,
                         valid_ptr as _valid_ptr, vec3 as _vec3)
from ww.collision_geo import read_collision
from ww.collision_tools import (FIELDS as TRIANGLE_FIELDS, decode_properties, inspect_triangle,
                                write_triangle_property, read_link_ground_query, ground_candidates,
                                exit_projection_cells, exit_slice_polygons, height as _plane_height)
from ww.collider_edit import ColliderEdits, ray_hit as _ray_collider_hit
from ww.cull import read_camera, full_snapshot as read_cull_snapshot
from ww.zone_info import read_zone_tables
from ww.actor_switches import actor_switches
from ww.viewer_math import (ViewerCamera, add_scaled as _add_scaled,
                            angles_from_forward as _angles_from_forward, cross as _cross,
                            dot as _dot, forward_from_angles as _forward_from_angles,
                            normalize as _norm, screen_ray as _screen_ray, sub as _sub)
from ww.soft_canvas import SoftCanvas

# "auto": hardware (GPU) canvas on Windows, CPU-canvas software renderer on macOS/other.
# The hardware canvas is Direct3D 11 on Windows builds of the fork. Use "hardware" to try the
# fork's OpenGL path on a Mac, or "software" to force the CPU renderer anywhere.
RENDER_MODE = "auto"


class DolphinReader:
    def read_bytes(self, address, size):
        return bytes(memory.read_bytes(address, size))


RD = DolphinReader()
COLLIDER_DECODER = ColliderDecoder(RD, memory.read_u32)
COLLIDER_EDITS = ColliderEdits(memory, COLLIDER_DECODER, allow_ownerless=True)


def _decode_collider(address, data=None):
    data = RD.read_bytes(address, 0x150) if data is None else data
    return COLLIDER_EDITS.track(address, data, COLLIDER_DECODER.decode(data))


def _address(name, jp_fallback):
    """Use the shared address table when available, otherwise TWW JP."""
    value = getattr(Address, name, None)
    return jp_fallback if value is None else value


LINK_X = _address("X_ADDRESS", 0x803D78FC)
ROOM_NO_ADDR = _address("ROOM_NO", 0x803E9F48)
PLAYER_PTR = _address("PLAYER_POINTER", 0x803BD910)
GAME_FRAME_COUNTER = _address("FRAME_COUNTER_ADDRESS", 0x803E9D34)
POS_OFFSETS = (_address("ACTOR_OLD_XYZ_OFFSET", 0x1E4),
               _address("ACTOR_XYZ_OFFSET", 0x1F8))
GAME_INFO = _address("GAME_INFO", 0x803B8108)
CCS_BASE = GAME_INFO + 0x12A0 + 0x1404
CCS_ATTACK = 0x0000
CCS_TARGET = 0x0400
CCS_CONTACT = 0x1000
CCS_SLOT_COUNT = 0x100
CCS_TARGET_SLOT_COUNT = 0x300
CCS_ATTACK_COUNT = 0x2800
CCS_TARGET_COUNT = 0x2804
CCS_CONTACT_COUNT = 0x2808
COLLIDER_SNAPSHOT_WATCH = CCS_BASE + CCS_ATTACK_COUNT
COLLIDER_ATTACK_SPRM = 0x000
COLLIDER_TARGET_SPRM = 0x018
COLLIDER_CONTACT_SPRM = 0x02C
CCS_TARGET_TYPE_OFFSET = COLLIDER_TARGET_SPRM + 0x10
ACTOR_QUEUE = _address("ACTOR_QUEUE_BASE", 0x803654C8)
ACTOR_PROC = _address("ACTOR_GPROC_ID_OFFSET", 0x008)
ACTOR_CURRENT = _address("ACTOR_XYZ_OFFSET", 0x1F8)
ACTOR_OLD = _address("ACTOR_OLD_XYZ_OFFSET", 0x1E4)
ACTOR_CURRENT_ANGLE = ACTOR_CURRENT + _address("ACTOR_PLACE_ANGLE_OFFSET", 0x0C)
ACTOR_SPEED = _address("ACTOR_XYZ_SPEED_OFFSET", 0x220)
ACTOR_SCALE = _address("ACTOR_SCALE_OFFSET", 0x214)
ACTOR_MAX_HEALTH = 0x284
ACTOR_HEALTH = 0x285
ACTOR_SWITCH_CYLINDER = 0x2D8
ACTOR_REFRESH_INTERVAL = 3
PROC_OBJ_FTREE = 0x0085
PROC_OBJ_PLANT = 0x0114
FOLIAGE_COLLIDER_OFFSETS = {
    PROC_OBJ_FTREE: (0x398, 0x508),
}
FOLIAGE_PROBE_BYTES = 0x900
FOLIAGE_PROBE_INTERVAL = 120
FOLIAGE_REFRESH_INTERVAL = 6
ACTOR_COLLIDER_HEADER_BYTES = ACTOR_CURRENT + 12
ACTOR_COLLIDER_SCAN_PADDING = 1100.0
# Used only while a savestate is paused. TWW rebuilds dCcS on the next game
# frame, so restoring a frame boundary state otherwise leaves no live list.
STATE_COLLIDER_RECOVERY_BYTES = 0x1400
PLAYER_ATTACK_COLLIDER_OFFSETS = (0x435C, 0x45BC, 0x46EC, 0x481C)
PLAYER_STATE_COLLIDER_OFFSETS = (0x40FC, 0x422C, 0x435C, 0x448C,
                                  0x45BC, 0x46EC, 0x481C)
STATE_OVERLAY_HISTORY_LIMIT = 96

# Trigger actor process IDs from d_procname.h.
PROC_TAG_EVSW = 0x001C
PROC_TAG_SO = 0x0025
PROC_SCENECHG = 0x002B
PROC_TAG_EVENT = 0x019C
PROC_TAG_HINT = 0x019D
PROC_TAG_MSG = 0x019E
PROC_TAG_ETC = 0x019F
PROC_TAG_ISLAND = 0x01A0
PROC_TAG_KF1 = 0x01A1
PROC_TAG_KK1 = 0x01A2
PROC_TAG_PHOTO = 0x01A3
PROC_TAG_KB_ITEM = 0x01A4
PROC_TAG_MK = 0x01A5
PROC_TAG_MDCB = 0x01A6
PROC_TAG_BA1 = 0x01A7
PROC_TAG_GSHIP = 0x01CA
EVENT_TRIGGER_PROCS = {
    PROC_TAG_EVENT, PROC_TAG_HINT, PROC_TAG_MSG, PROC_TAG_ETC, PROC_TAG_KF1,
    PROC_TAG_KK1, PROC_TAG_PHOTO, PROC_TAG_KB_ITEM, PROC_TAG_MK, PROC_TAG_MDCB,
    PROC_TAG_BA1, PROC_TAG_GSHIP,
}
OTHER_TRIGGER_PROCS = {PROC_TAG_SO, PROC_TAG_ISLAND}
SEAM_DIR = os.path.join(os.path.dirname(__file__), "ww", "data", "seam_clips")
ROLL_STAB_MAX = 49.2202

W, H = 480, 340
DEFAULT_FOVY = 50.0
KEY_W, KEY_A, KEY_S, KEY_D, KEY_SPACE, KEY_SHIFT = (1, 2, 4, 8, 16, 32)


C_GROUND = 0xFFFFFFFF
C_WALL = 0xFF45D76B
C_WALL_VERTICAL = 0xFF1E6B36
C_SLOPE = 0xFFFF4D4D
C_ROOF = 0xFF4F91FF
C_LINK = 0xFF33E6FF
C_SEAM_ROLL = 0xFF4CE07A
C_SEAM_PUSH = 0xFFFFA83C
C_SEAM_SELECTED = 0xFFFFFFFF
C_LOAD = 0xFFFF4DFF
C_EVENT = 0xFFFFD33D
C_SWITCH = 0xFF438CFF
C_OTHER = 0xFF9BFF5A
C_PUSH = 0xFF65E6C3
C_PUSH_LINK = 0xFF8FD3FF
LOAD_ZONE_HEIGHT = 400.0
C_ATTACK = 0xFFFFA347
C_ATTACK_INFO = 0xFFF56827
C_TARGET = 0xFF5CA8FF
C_TARGET_INFO = 0xFF3C78FF  # deeper blue than C_TARGET, same relationship as C_ATTACK -> C_ATTACK_INFO
C_COORDINATE = 0xFF47DFFF
C_COORDINATE_OUTLINE = 0xFF000000
C_WIREFRAME = 0xFFFFFFFF
C_VERTEX_FACE = 0xFF3C9CFF
C_VERTEX_POINT = 0xFF33E6FF
VOLUME_CYLINDER_SEGMENTS = 16
SPHERE_DETAIL_FAR_PIXELS = 22.0
SPHERE_DETAIL_MEDIUM_PIXELS = 72.0
COORDINATE_DOT_OUTER_RADIUS = 10.0
COORDINATE_DOT_INNER_RADIUS = 6.5
MAX_ACTOR_LABELS = 256
HP_BAR_WIDTH = 52.0
HP_BAR_HEIGHT = 7.0
MAX_HP_BARS = 96
ATTACK_LABEL_MARGIN = 20.0
# Separates actor names from collider info at the same anchor.
ACTOR_LABEL_EXTRA_MARGIN = 20.0
MOVE_ACTOR_AXIS_PIXELS = 78.0
MOVE_ACTOR_AXIS_PICK_RADIUS = 13.0
MOVE_ACTOR_CENTER_RADIUS = 19.0
C_MOVE_X = 0xFFFF5A5A
C_MOVE_Y = 0xFF62E986
C_MOVE_Z = 0xFF5A9DFF
C_MOVE_CENTER = 0xFFFFFFFF

# dStage_roomControl_c::mStatus and dSv_info_c::mZone.
ROOM_STATUS_BASE = _address("ROOM_STATUS_BASE", 0x803B1188)
ROOM_STATUS_STRIDE = 0x114
ROOM_STATUS_COUNT = 64
ROOM_STATUS_ZONE_NO_OFFSET = 0x107
ZONE_ARRAY_BASE = _address("ZONE_ARRAY_BASE", 0x803B88B0)
ZONE_STRIDE = 0x4C
ZONE_COUNT = 32

SETTINGS_PATH = os.path.splitext(os.path.abspath(__file__))[0] + ".settings.json"
WINDOW_TITLE = "TWW Collision Viewer"


def _read_settings_document():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
            document = json.load(settings_file)
        return document if isinstance(document, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_initial_settings = _read_settings_document()

# Group order selects the left column in Dolphin's grouped controls layout.
# Collision/View and Culling share the left column. Leave its height flexible
# so Qt fills the column and keeps its contents aligned with the Tools group.
WINDOW_STYLE = ('ScriptCanvasWidget { min-width: 480px; min-height: 340px; } '
                'QScrollArea { min-height: 420px; max-height: 900px; } '
                'QGroupBox[title="Collision and View"] { '
                'min-width: 240px; max-width: 360px; }')
panel = gui.window(WINDOW_TITLE, style=WINDOW_STYLE)
SOFTWARE_RENDER = RENDER_MODE == "software" or (RENDER_MODE == "auto" and sys.platform != "win32")
if not SOFTWARE_RENDER:
    try:
        panel.enable_hardware_canvas()
    except Exception:
        SOFTWARE_RENDER = True  # this build has no hardware canvas; fall back to the CPU canvas
canvas = panel.canvas(W, H)
if SOFTWARE_RENDER:
    canvas = SoftCanvas(canvas, wire_color=C_WIREFRAME)
cb_enabled = panel.checkbox("Collision mesh", True, group="Collision and View")
cb_movebg = panel.checkbox("Movable BG", True, group="Collision and View")
cb_follow_cam = panel.checkbox("Follow Cam", True, group="Collision and View")
cb_ground = panel.checkbox("Ground", True, group="Collision and View")
cb_slope = panel.checkbox("Slopes", True, group="Collision and View")
cb_wall = panel.checkbox("Walls", True, group="Collision and View")
cb_roof = panel.checkbox("Roofs", False, group="Collision and View")
cb_filled = panel.checkbox("Filled", True, group="Collision and View")
cb_wire = panel.checkbox("Wireframe", False, group="Collision and View")
cb_xray = panel.checkbox("X-ray", True, group="Collision and View")
cb_view_cull = panel.checkbox("Cull outside view", True, group="Collision and View")
move_speed_slider = panel.slider_float("Freecam speed", 10.0, 500.0, group="Collision and View")
move_speed_slider.value = 50.0
wire_opacity_slider = panel.slider_float("Wireframe opacity", 0.0, 1.0, group="Collision and View")
wire_opacity_slider.value = 1.0
opacity_slider = panel.slider_float("Collision opacity", 0.0, 1.0, group="Collision and View")
opacity_slider.value = 0.54
radius_slider = panel.slider_float("Draw radius", 0.0, 12000.0, group="Collision and View")
# Dolphin exposes one group on the left; a subsection keeps both tools there.
cull_heading = panel.text("Culling", group="Collision and View",
                          style="QLabel { font-weight: bold; border-top: 1px solid #657080; "
                                "margin-top: 10px; padding-top: 8px; }")
cb_culling = panel.checkbox("Culling overlay", False, group="Collision and View")
cb_cull_visible = panel.checkbox("Visible cull boxes (green)", True, group="Collision and View")
cb_cull_hidden = panel.checkbox("Culled boxes (red)", True, group="Collision and View")
cb_cull_frustum = panel.checkbox("Game camera frustum", True, group="Collision and View")
cb_cull_labels = panel.checkbox("Cull box names", False, group="Collision and View")
cb_cull_follow_link = panel.checkbox("Follow Link in culling view", True, group="Collision and View")
cull_frame_button = panel.button("Frame culling view", group="Collision and View")
cull_help = panel.text("JP/GZLJ01. Magenta = draw-state mismatch.", group="Collision and View")
cull_status = panel.text("Culling overlay disabled.\n\n", group="Collision and View",
                         style="QLabel { min-width: 280px; max-width: 280px; "
                               "min-height: 64px; max-height: 64px; font-family: monospace; font-size: 12px; }")
reset_button = panel.button("Reset view / refresh static cache", group="Tools")
cb_vertex_select = panel.checkbox("Vertex Select", False, group="Tools")
cb_floor_ray = panel.checkbox("Link floor ray (engine result)", False, group="Tools")
cb_floor_inspector = panel.checkbox("Triangle Inspector", False, group="Tools")
triangle_info = panel.text("Enable inspector, then click a collision triangle.", group="Tools")
TRIANGLE_HELP_STYLE = 'QLabel { qproperty-wordWrap: true; min-width: 0px; }'
# Let Qt wrap to the actual Tools width instead of clipping a capped-width label.
TRIANGLE_FIELDS_TEXT = "Fields: " + ", ".join(TRIANGLE_FIELDS)
TRIANGLE_FIELDS_STYLE = 'QLabel { qproperty-wordWrap: true; min-width: 0px; font-size: 11px; }'
triangle_fields_help = panel.text(TRIANGLE_FIELDS_TEXT, group="Tools", style=TRIANGLE_FIELDS_STYLE)
triangle_field = panel.input_text("Property name", "exit_id", group="Tools")
triangle_value = panel.input_text("Property value", "", group="Tools")
triangle_read = panel.button("Read property", group="Tools")
triangle_apply = panel.button("Apply triangle property", group="Tools")
triangle_shared = panel.checkbox("Edit shared record (affects every user)", False, group="Tools")
triangle_edit_help = panel.text("Unchecked: if Ti is shared, copy it to an unused property slot and update only this triangle's Ti index. Requires an unused slot. Checked: edit the existing record for every triangle using it. exit_id selects an SCLS entry; the destination is stored in SCLS.", group="Tools", style=TRIANGLE_HELP_STYLE)
cb_filled_triggers = panel.checkbox("Filled triggers", False, group="Triggers")
trigger_opacity_slider = panel.slider_float("Trigger opacity", 0.0, 1.0, group="Triggers")
trigger_opacity_slider.value = 0.54
cb_on_top_triggers = panel.checkbox("On Top Triggers", False, group="Triggers")
cb_load = panel.checkbox("Load zones", True, group="Triggers")
cb_leaf_exits = panel.checkbox("Leaf exit projection (nearest floor)", False, group="Triggers")
cb_leaf_columns = panel.checkbox("Show tall exit columns", False, group="Triggers")
LEAF_EXIT_HELP = "Filled areas show usable exits at Link's height. Exit geometry is cached; only changed exits or nearby blockers rebuild it. Trigger opacity controls the fill. Tall columns are optional."
leaf_exit_help = panel.text(LEAF_EXIT_HELP, group="Triggers")
cb_event = panel.checkbox("Event areas", True, group="Triggers")
cb_switch = panel.checkbox("Switch areas", False, group="Triggers")
cb_other = panel.checkbox("Other Triggers", True, group="Triggers")
cb_push = panel.checkbox("Push colliders", True, group="Colliders")
cb_attack = panel.checkbox("Attack colliders", True, group="Colliders")
cb_target = panel.checkbox("Target colliders", True, group="Colliders")
cb_enemy_hp = panel.checkbox("Enemy HP bars", False, group="Colliders")
cb_attack_info = panel.checkbox("Attack Info", True, group="Colliders")
cb_target_info = panel.checkbox("Target Info", False, group="Colliders")
cb_attack_actor = panel.checkbox("Attack Names", True, group="Colliders")
cb_target_actor = panel.checkbox("Target Names", True, group="Colliders")
cb_contact_actor = panel.checkbox("Push Names", False, group="Colliders")
cb_coordinate_dot = panel.checkbox("Coordinate Dot", True, group="Colliders")
cb_actor_names = panel.checkbox("Actor Names", False, group="Colliders")
cb_move_actor = panel.checkbox("Actor Selector", False, group="Tools")
cb_move_collider = panel.checkbox("Collider Selector", False, group="Tools")
collider_move_info = panel.text("Enable Collider Selector, click a shape, then drag its center or an axis.", group="Tools")
collider_restore = panel.button("Reset selected collider offset", group="Tools")
collider_restore_all = panel.button("Restore all", group="Tools")
cb_seams = panel.checkbox("Show Seam Clips", False, group="Seam Clips")
seam_initial_button = panel.button("Teleport seam initial", group="Seam Clips")
seam_clip_button = panel.button("Teleport seam clip", group="Seam Clips")
status = panel.text("Waiting for TWW collision...", group="Tools")
renderer_note = panel.text("Renderer: %s" % ("software (CPU canvas)" if SOFTWARE_RENDER else "hardware"),
                           group="Tools")
cb_zone_info = panel.checkbox("Zone flags", False, group="Tools")
save_settings_button = panel.button("Save Settings", group="Settings")

_CHECKBOX_SETTINGS = {
    "collision_mesh": cb_enabled,
    "movable_bg": cb_movebg,
    "follow_cam": cb_follow_cam,
    "ground": cb_ground,
    "slopes": cb_slope,
    "walls": cb_wall,
    "roofs": cb_roof,
    "filled": cb_filled,
    "wireframe": cb_wire,
    "xray": cb_xray,
    "cull_outside_view": cb_view_cull,
    "vertex_select": cb_vertex_select,
    "floor_ray": cb_floor_ray,
    "floor_inspector": cb_floor_inspector,
    "leaf_exit_projection": cb_leaf_exits,
    "leaf_exit_columns": cb_leaf_columns,
    "move_collider": cb_move_collider,
    "filled_triggers": cb_filled_triggers,
    "on_top_triggers": cb_on_top_triggers,
    "load_zones": cb_load,
    "event_areas": cb_event,
    "switch_areas": cb_switch,
    "other_triggers": cb_other,
    "push_colliders": cb_push,
    "attack_colliders": cb_attack,
    "target_colliders": cb_target,
    "enemy_hp_bars": cb_enemy_hp,
    "attack_info": cb_attack_info,
    "target_info": cb_target_info,
    "attack_names": cb_attack_actor,
    "target_names": cb_target_actor,
    "push_names": cb_contact_actor,
    "coordinate_dot": cb_coordinate_dot,
    "actor_names": cb_actor_names,
    "culling_overlay": cb_culling,
    "cull_visible": cb_cull_visible,
    "cull_hidden": cb_cull_hidden,
    "cull_frustum": cb_cull_frustum,
    "cull_labels": cb_cull_labels,
    "cull_follow_link": cb_cull_follow_link,
    "move_actor": cb_move_actor,
    "zone_room_info": cb_zone_info,
    "show_seam_clips": cb_seams,
}
_SLIDER_SETTINGS = {
    "freecam_speed": move_speed_slider,
    "wireframe_opacity": wire_opacity_slider,
    "collision_opacity": opacity_slider,
    "draw_radius": radius_slider,
    "trigger_opacity": trigger_opacity_slider,
}


def _restore_saved_options():
    options = _initial_settings.get("options", {})
    if not isinstance(options, dict):
        return
    for name, control in _CHECKBOX_SETTINGS.items():
        value = options.get(name)
        if isinstance(value, bool):
            control.checked = value
    for name, control in _SLIDER_SETTINGS.items():
        value = options.get(name)
        if isinstance(value, (int, float)) and math.isfinite(value):
            control.value = float(value)


def _saved_geometry(document):
    geometry = document.get("window_geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        values = tuple(int(geometry[name]) for name in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if values[2] <= 0 or values[3] <= 0:
        return None
    return values


def _restore_saved_geometry():
    geometry = _saved_geometry(_initial_settings)
    set_geometry = getattr(panel, "set_geometry", None)
    if geometry is not None and set_geometry is not None:
        set_geometry(*geometry)


def _save_settings():
    document = _read_settings_document()
    geometry = getattr(panel, "geometry", None)
    if (isinstance(geometry, tuple) and len(geometry) == 4 and
            geometry[2] > 0 and geometry[3] > 0):
        document["window_geometry"] = dict(zip(
            ("x", "y", "width", "height"), (int(value) for value in geometry)))
    document["options"] = {
        **{name: bool(control.checked) for name, control in _CHECKBOX_SETTINGS.items()},
        **{name: float(control.value) for name, control in _SLIDER_SETTINGS.items()},
    }
    temporary_path = SETTINGS_PATH + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8", newline="\n") as settings_file:
            json.dump(document, settings_file, indent=2, sort_keys=True)
            settings_file.write("\n")
        os.replace(temporary_path, SETTINGS_PATH)
        if "window_geometry" in document:
            status.set("Settings and exact window rectangle saved")
        else:
            status.set("Options saved; window rectangle is not available yet")
    except OSError as exc:
        status.set("Could not save settings: %s" % exc)
        try:
            os.remove(temporary_path)
        except OSError:
            pass


_restore_saved_options()
_restore_saved_geometry()

_cache = None
_snapshot = None
_link = None
_link_actor = 0
_game_camera = None
_cull_snapshot = None
_cull_revision = 0
_cull_status_text = "Waiting for culling data..."
_cull_follow_anchor = None
_frame = 0
_host_ticks = 0
_state_load_pending = False
_state_load_wait_ticks = 0
_state_load_signature = None
_state_load_refreshes = 0
_state_load_recover_registry = False
_state_overlay_history = {}
_state_load_overlay_snapshot = None
_last_error = ""

_freecam = {"pos": [0.0, 0.0, 0.0], "az": 38.0, "el": 29.0, "ready": False}
_mouse_look = {"active": False, "x": 0.0, "y": 0.0}
_viewer_fullscreen = False
_clean_capture = False
_fullscreen_before_capture = False

_hardware_mesh_key = None
_hardware_dynamic_key = None
_hardware_state_key = None
_hardware_line_key = None
_hardware_on_top_line_key = None
_hardware_marker_key = None
_hardware_overlay_key = None
_stage_visible = None
_static_wire_positions = b""
_static_wire_colors = b""
_dynamic_wire_positions = b""
_dynamic_wire_colors = b""

_line_positions = None
_line_colors = None
_marker_positions = None
_marker_colors = None
_volume_positions = None
_volume_colors = None
_hardware_volume_key = None

_triggers = []
_push_colliders = []
_attack_colliders = []
_target_colliders = []
_enemy_health = []
_actor_labels = []
_attack_labels = []
_attack_actor_labels = []
_target_info_labels = []
_target_actor_labels = []
_contact_actor_labels = []
_actor_positions = {}
_actor_records = {}
_room_zones = {}
_zones = {}
_room_status_flags = {}
_zone_actor_records = {}
_zone_label_revision = 0
_move_actor_selected = None
_move_actor_drag = None
_move_actor_locks = {}
_move_actor_mode_active = False
_actor_fallback_colliders = {"attack": [], "target": [], "push": []}
_state_recovery_colliders = {"attack": [], "target": [], "push": []}
_player_attack_colliders = []
_foliage_probe_offsets = {}
_trigger_filter_key = None
_collider_watch_ready = False
_runtime_diag = {
    "actor_queue": 0,
    "tagged": 0,
    "attack_slots": 0,
    "attack_enabled": 0,
    "attack_decoded": 0,
    "push_slots": 0,
    "push_enabled": 0,
    "push_decoded": 0,
}
_status_summary_last = None

_seams = {"stage": None, "room": None, "clips": [], "selected": None}
_selected_face = None
_selected_point = None
_floor_query = None
_triangle_selection = None
_triangle_ui_key = None
_exit_projection_key = None
_exit_projection = []
_exit_projection_error = ""
_exit_triangle_cache = {}
_exit_geometry_revision = 0
_exit_render_key = None
_exit_render_positions = array('f')
_exit_render_colors = array('I')
_edit_mode_previous = {}
_move_collider_watch_ready = False
_move_collider_watch_key = None
_collider_flag_watches = set()
_applying_collider_flags = False
_collider_registration_watch = None
_last_stage_context = None


def _sync_canvas_size():
    global W, H
    width, height = int(canvas.width), int(canvas.height)
    if width >= 320 and height >= 240 and (width, height) != (W, H):
        W, H = width, height


def _active_focal():
    fovy = _game_camera[3] if cb_follow_cam.checked and _game_camera is not None else DEFAULT_FOVY
    height = min(float(H), float(W) * 0.75) if _clean_capture else float(H)
    return (height * 0.5) / math.tan(math.radians(fovy) * 0.5)


def _read_link():
    return struct.unpack(">3f", RD.read_bytes(LINK_X, 12))


def _read_room():
    return memory.read_bytes(ROOM_NO_ADDR, 1)[0]


def _state_ram_signature():
    try:
        return (memory.read_u32(GAME_FRAME_COUNTER) & 0xFFFFFFFF,
                bytes(memory.read_bytes(LINK_X, 12)), _read_room(),
                memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF)
    except Exception:
        return None


def _cache_state_overlay_snapshot():
    """Remember the exact live dCcS snapshot for a future paused state load (Temporary solution for now)."""
    signature = _state_ram_signature()
    if signature is None:
        return
    snapshot = {
        "attack": tuple(_attack_colliders) if cb_attack.checked else None,
        "target": tuple(_target_colliders) if cb_target.checked else None,
        "push": tuple(_push_colliders) if cb_push.checked else None,
    }
    for key in (signature, ("context",) + signature[1:]):
        _state_overlay_history.pop(key, None)
        _state_overlay_history[key] = snapshot
    while len(_state_overlay_history) > STATE_OVERLAY_HISTORY_LIMIT:
        _state_overlay_history.pop(next(iter(_state_overlay_history)))


def _read_collider_registry(offset, count_offset, sprm_offset, label, slot_count=CCS_SLOT_COUNT,
                            allow_stale=False, label_sinks=()):
    """Read active dCcD objects from one bounded dCcS registry.

    label_sinks is an iterable of (labels_list, builder) pairs -- e.g.
    (attack_labels, _append_attack_label) or
    (target_info_labels, _append_target_info_label). Each collider that
    successfully decodes in the loop below is passed to every builder in
    the same pass, so a label only ever exists for a collider that is
    actually about to be drawn, instead of coming from a second,
    separately-timed scan of the same slots.
    """
    colliders, seen = [], set()
    slots = enabled = 0
    try:
        active_count = min(memory.read_u32(CCS_BASE + count_offset), slot_count)
    except Exception:
        _runtime_diag[label + "_slots"] = 0
        _runtime_diag[label + "_enabled"] = 0
        _runtime_diag[label + "_decoded"] = 0
        return colliders
    if not active_count and _collider_watch_ready and not allow_stale:
        _runtime_diag[label + "_slots"] = 0
        _runtime_diag[label + "_enabled"] = 0
        _runtime_diag[label + "_decoded"] = 0
        return None
    try:
        pointers = RD.read_bytes(CCS_BASE + offset, slot_count * 4)
    except Exception:
        _runtime_diag[label + "_slots"] = 0
        _runtime_diag[label + "_enabled"] = 0
        _runtime_diag[label + "_decoded"] = 0
        return colliders
    limit = active_count if active_count else slot_count
    for index in range(limit):
        address = _u32(pointers, index * 4)
        if address in seen or not _valid_ptr(address):
            continue
        seen.add(address)
        slots += 1
        try:
            data = RD.read_bytes(address, 0x150)
        except Exception:
            continue
        if not active_count and not (_u32(data, sprm_offset) & 1):
            continue
        enabled += 1
        collider = _decode_collider(address, data)
        if collider is not None:
            colliders.append(collider)
            for labels_list, builder in label_sinks:
                builder(labels_list, collider, data)
    # Preserve a live count sampled before dCcS clears it.
    _runtime_diag[label + "_slots"] = active_count if active_count else slots
    _runtime_diag[label + "_enabled"] = enabled
    _runtime_diag[label + "_decoded"] = len(colliders)
    return colliders


def _collider_label_anchor(collider):
    """A point a little above the collider's own drawn shape, for anchoring
    its attack-info label. Each kind stores its geometry differently (see
    ColliderDecoder / _draw_collider), so the "top" is computed per kind
    rather than assumed to be a plain center + margin.
    """
    kind, _owner, *shape = collider
    if kind == "box":
        center, half = shape[0], shape[1]
        return (center[0], center[1] + half[1] + ATTACK_LABEL_MARGIN, center[2])
    if kind == "cylinder":
        bottom, _radius, height = shape[0], shape[1], shape[2]
        return (bottom[0], bottom[1] + height + ATTACK_LABEL_MARGIN, bottom[2])
    if kind == "sphere":
        center, radius = shape[0], shape[1]
        return (center[0], center[1] + radius + ATTACK_LABEL_MARGIN, center[2])
    if kind == "capsule":
        start, end, radius = shape[0], shape[1], shape[2]
        top_y = max(start[1], end[1]) + radius
        return ((start[0] + end[0]) * 0.5, top_y + ATTACK_LABEL_MARGIN, (start[2] + end[2]) * 0.5)
    p0, p1, p2 = shape[0], shape[1], shape[2]
    return (sum(p[0] for p in (p0, p1, p2)) / 3.0,
            sum(p[1] for p in (p0, p1, p2)) / 3.0 + ATTACK_LABEL_MARGIN,
            sum(p[2] for p in (p0, p1, p2)) / 3.0)


def _append_attack_label(labels, collider, data):
    """Add attack type and damage above a decoded collider."""
    if len(labels) >= MAX_ACTOR_LABELS:
        return
    info = _collider_attack_info(data)
    if info is None:
        return
    damage_type, damage_value = info
    position = _collider_label_anchor(collider)
    if not _finite_vec(position):
        return
    names = _attack_type_names(damage_type)
    label = "%s  DMG %d" % ("/".join(names) if names else "NONE", damage_value)
    labels.append((position, label))


def _collider_actor_label_anchor(collider):
    x, y, z = _collider_label_anchor(collider)
    return (x, y + ACTOR_LABEL_EXTRA_MARGIN, z)


def _append_target_info_label(labels, collider, data):
    """Add the accepted attack types above a target collider."""
    if len(labels) >= MAX_ACTOR_LABELS:
        return
    try:
        target_type = _u32(data, CCS_TARGET_TYPE_OFFSET)
    except (struct.error, IndexError):
        return
    position = _collider_label_anchor(collider)
    if not _finite_vec(position):
        return
    labels.append((position, _target_type_names(target_type)))


def _append_actor_label(labels, collider, data):
    """Add the collider owner's procedure name."""
    if len(labels) >= MAX_ACTOR_LABELS:
        return
    owner = collider[1]
    name='No actor owner'
    if owner:
        try:
            procedure = memory.read_u16(owner + ACTOR_PROC)
            name=proc_name(procedure, "Proc 0x%03X" % procedure)
        except Exception:return
    position = _collider_actor_label_anchor(collider)
    if not _finite_vec(position):
        return
    labels.append((position,name))


def _append_contact_label(labels,collider,data):
    if collider[1]:
        _append_actor_label(labels,collider,data)
    elif len(labels)<MAX_ACTOR_LABELS:
        position=_collider_actor_label_anchor(collider)
        if _finite_vec(position):
            labels.append((position,'No actor owner | '+_physical_collision_name(_u32(data,COLLIDER_CONTACT_SPRM))))




def _collider_label_sinks():
    labels = {
        "attack_info": [] if cb_attack.checked and cb_attack_info.checked else None,
        "attack_actor": [] if cb_attack.checked and cb_attack_actor.checked else None,
        "target_info": [] if cb_target.checked and cb_target_info.checked else None,
        "target_actor": [] if cb_target.checked and cb_target_actor.checked else None,
        "contact_actor": [] if cb_push.checked and cb_contact_actor.checked else None,
    }
    sinks = {"attack": [], "target": [], "push": []}
    if labels["attack_info"] is not None:
        sinks["attack"].append((labels["attack_info"], _append_attack_label))
    if labels["attack_actor"] is not None:
        sinks["attack"].append((labels["attack_actor"], _append_actor_label))
    if labels["target_info"] is not None:
        sinks["target"].append((labels["target_info"], _append_target_info_label))
    if labels["target_actor"] is not None:
        sinks["target"].append((labels["target_actor"], _append_actor_label))
    if labels["contact_actor"] is not None:
        sinks["push"].append((labels["contact_actor"], _append_contact_label))
    return labels, sinks


def _apply_live_colliders(attack, target, push, labels):
    global _push_colliders, _attack_colliders, _target_colliders
    global _attack_labels, _attack_actor_labels
    global _target_info_labels, _target_actor_labels, _contact_actor_labels

    if attack is not None:
        _attack_colliders = attack
        if labels["attack_info"] is not None:
            _attack_labels = labels["attack_info"]
        if labels["attack_actor"] is not None:
            _attack_actor_labels = labels["attack_actor"]
    if target is not None:
        _target_colliders = target
        if labels["target_info"] is not None:
            _target_info_labels = labels["target_info"]
        if labels["target_actor"] is not None:
            _target_actor_labels = labels["target_actor"]
    if push is not None:
        _push_colliders = push
        if labels["contact_actor"] is not None:
            _contact_actor_labels = labels["contact_actor"]

    if labels["attack_info"] is None:
        _attack_labels = []
    if labels["attack_actor"] is None:
        _attack_actor_labels = []
    if labels["target_info"] is None:
        _target_info_labels = []
    if labels["target_actor"] is None:
        _target_actor_labels = []
    if labels["contact_actor"] is None:
        _contact_actor_labels = []

    if not cb_attack.checked:
        _runtime_diag.update(attack_slots=0, attack_enabled=0, attack_decoded=0)
    if not cb_push.checked:
        _runtime_diag.update(push_slots=0, push_enabled=0, push_decoded=0)
    if not cb_target.checked:
        _runtime_diag.update(target_slots=0, target_enabled=0, target_decoded=0)


def _sample_live_colliders(allow_stale=False):
    labels, sinks = _collider_label_sinks()
    attack = (_read_collider_registry(CCS_ATTACK, CCS_ATTACK_COUNT,
                                      COLLIDER_ATTACK_SPRM, "attack", allow_stale=allow_stale,
                                      label_sinks=sinks["attack"])
              if cb_attack.checked else [])
    target = (_read_collider_registry(CCS_TARGET, CCS_TARGET_COUNT,
                                      COLLIDER_TARGET_SPRM, "target", CCS_TARGET_SLOT_COUNT,
                                      allow_stale, label_sinks=sinks["target"])
              if cb_target.checked else [])
    push = (_read_collider_registry(CCS_CONTACT, CCS_CONTACT_COUNT,
                                    COLLIDER_CONTACT_SPRM, "push", allow_stale=allow_stale,
                                    label_sinks=sinks["push"])
            if cb_push.checked else [])
    return attack, target, push, labels


def _read_live_colliders(allow_stale=False):
    attack, target, push, labels = _sample_live_colliders(allow_stale)
    _apply_live_colliders(attack, target, push, labels)


def _capture_live_colliders():
    """Snapshot dCcS while MoveAfterCheck still owns this frame's entries."""
    try:
        attack, target, push, labels = _sample_live_colliders()
        _apply_live_colliders(attack, target, push, labels)
        _cache_state_overlay_snapshot()
    except Exception as exc:
        _runtime_diag["capture_error"] = str(exc)


def _trigger_enabled(category):
    return ((category == "load" and cb_load.checked) or
            (category == "event" and cb_event.checked) or
            (category == "switch" and cb_switch.checked) or
            (category == "other" and cb_other.checked))


def _actor_trigger_category(proc):
    if proc == PROC_SCENECHG:
        return "load"
    if proc == PROC_TAG_EVSW:
        return "switch"
    if proc in EVENT_TRIGGER_PROCS:
        return "event"
    if proc in OTHER_TRIGGER_PROCS:
        return "other"
    return None


def _read_actor_triggers():
    """Decode authored tag actors from TWW's actor queue into visible volumes."""
    triggers = []
    try:
        queue = RD.read_bytes(ACTOR_QUEUE, 12)
        node, count = _u32(queue, 0), min(_u32(queue, 8), 2048)
    except Exception:
        _runtime_diag.update(actor_queue=0, tagged=0)
        return triggers
    _runtime_diag["actor_queue"] = count
    tagged = 0
    seen = set()
    for _ in range(count):
        if not _valid_ptr(node) or node in seen:
            break
        seen.add(node)
        try:
            tag = RD.read_bytes(node, 0x14)
            actor = _u32(tag, 0x0C)
            node = _u32(tag, 8)
            if not _valid_ptr(actor):
                continue
            data = RD.read_bytes(actor, 0x220)
        except Exception:
            continue
        category = _actor_trigger_category(_u16(data, ACTOR_PROC))
        if category is None or not _trigger_enabled(category):
            continue
        tagged += 1
        position, scale = _vec3(data, ACTOR_CURRENT), _vec3(data, ACTOR_SCALE)
        if not _finite_vec(position) or not _finite_vec(scale):
            continue
        proc = _u16(data, ACTOR_PROC)
        if proc == PROC_SCENECHG:
            half = tuple(max(25.0, abs(value) * 100.0) for value in scale)
            triggers.append(("box", category, position, half, 0.0))
        elif proc == PROC_TAG_EVSW:
            try:
                collider_address = actor + ACTOR_SWITCH_CYLINDER
                collider = _decode_collider(collider_address)
            except Exception:
                collider = None
            if collider is not None and collider[0] == "cylinder":
                _, _, center, radius, height = collider
                triggers.append(("cylinder", category, center, radius, height))
            else:
                radius, half_height = abs(scale[0]) * 100.0, abs(scale[1]) * 100.0
                if radius >= 1.0 and half_height >= 1.0:
                    triggers.append(("cylinder", category,
                                     (position[0], position[1] - half_height, position[2]),
                                     radius, half_height * 2.0))
        elif proc == PROC_TAG_ISLAND:
            radius, half_height = abs(scale[0]) * 10000.0, abs(scale[1]) * 10000.0
            if radius >= 1.0 and half_height >= 1.0:
                triggers.append(("cylinder", category,
                                 (position[0], position[1] - half_height, position[2]),
                                 radius, half_height * 2.0))
        else:
            radius, half_height = abs(scale[0]) * 100.0, abs(scale[1]) * 100.0
            if radius >= 1.0 and half_height >= 1.0:
                triggers.append(("cylinder", category,
                                 (position[0], position[1] - half_height, position[2]),
                                 radius, half_height * 2.0))
    _runtime_diag["tagged"] = tagged
    return triggers


def _read_zone_info():
    global _room_status_flags
    try:
        rooms, zones, _room_status_flags = read_zone_tables(RD, ROOM_STATUS_BASE, ZONE_ARRAY_BASE)
        return rooms, zones
    except Exception:
        _room_status_flags = {}
        return {}, {}


def _actor_room_no(header, procedure, actor):
    try:
        room = struct.unpack_from('>b', header, ACTOR_CURRENT + 0x12)[0]
        return room if 0 <= room < ROOM_STATUS_COUNT else None
    except Exception:
        return None


def _refresh_actor_records(camera):
    """Scan actor queue once for labels, manipulation, and room/zone data."""
    global _actor_positions, _actor_records, _actor_labels, _room_zones, _zones, _zone_actor_records, _zone_label_revision
    labels, positions, records = [], {}, {}
    _zone_actor_records = {}
    _zone_label_revision += 1
    _room_zones, _zones = _read_zone_info()
    try:
        queue = RD.read_bytes(ACTOR_QUEUE, 12)
        node, count = _u32(queue, 0), min(_u32(queue, 8), 2048)
    except Exception:
        _actor_positions, _actor_records, _actor_labels = positions, records, labels
        return

    seen = set()
    for _ in range(count):
        if not _valid_ptr(node) or node in seen:
            break
        seen.add(node)
        try:
            entry = RD.read_bytes(node, 0x14)
            actor, node = _u32(entry, 0x0C), _u32(entry, 8)
            if not _valid_ptr(actor):
                continue
            header = RD.read_bytes(actor, ACTOR_CURRENT + 0x14)
            position = _vec3(header, ACTOR_CURRENT)
            if not _finite_vec(position):
                continue
            procedure = _u16(header, ACTOR_PROC)
            room_no = _actor_room_no(header, procedure, actor)
        except Exception:
            continue
        zone_no = _room_zones.get(room_no)
        positions[actor] = position
        records[actor] = (procedure, position, room_no, zone_no)
        if cb_zone_info.checked:
            _zone_actor_records[actor] = actor_switches(RD,actor,procedure,header,_room_zones,_zones,
                                                       stay_room=_read_room())
        zone_name=cb_zone_info.checked and any(t['overlay'] for t in _zone_actor_records.get(actor,()))
        if (cb_actor_names.checked and not zone_name and len(labels) < MAX_ACTOR_LABELS and
                _overlay_visible(camera, position, 160.0)):
            label = proc_name(procedure, "Proc 0x%03X" % procedure)
            labels.append((position, label))
    _actor_positions, _actor_records, _actor_labels = positions, records, labels


def _append_embedded_collider(found, actor, collider, data):
    if collider[1] != actor:
        return
    if cb_attack.checked and _u32(data, COLLIDER_ATTACK_SPRM) & 1:
        found["attack"].append(collider)
    if cb_target.checked and _u32(data, COLLIDER_TARGET_SPRM) & 1:
        found["target"].append(collider)
    if cb_push.checked and _u32(data, COLLIDER_CONTACT_SPRM) & 1:
        found["push"].append(collider)


def _probe_plant_collider_offsets(actor):
    try:
        data = RD.read_bytes(actor, FOLIAGE_PROBE_BYTES)
    except Exception:
        return ()
    offsets = []
    for offset in range(0x290, len(data) - 0x150 + 1, 4):
        shape_vptr = _u32(data, offset + 0x114)
        if not _valid_ptr(shape_vptr):
            continue
        collider_data = data[offset:offset + 0x150]
        collider = _decode_collider(actor + offset, collider_data)
        if collider is None or collider[1] != actor:
            continue
        if collider[0] not in ("box", "cylinder", "sphere", "capsule", "triangle"):
            continue
        offsets.append(offset)
    return tuple(offsets)


def _read_foliage_colliders(camera, force=False):
    found = {"attack": [], "target": [], "push": []}
    seen_colliders = set()
    try:
        queue = RD.read_bytes(ACTOR_QUEUE, 12)
        node, count = _u32(queue, 0), min(_u32(queue, 8), 2048)
    except Exception:
        return found
    seen_nodes = set()
    for _ in range(count):
        if not _valid_ptr(node) or node in seen_nodes:
            break
        seen_nodes.add(node)
        try:
            tag = RD.read_bytes(node, 0x14)
            actor, node = _u32(tag, 0x0C), _u32(tag, 8)
            if not _valid_ptr(actor):
                continue
            header = RD.read_bytes(actor, ACTOR_COLLIDER_HEADER_BYTES)
            proc = _u16(header, ACTOR_PROC)
            if proc not in (PROC_OBJ_FTREE, PROC_OBJ_PLANT):
                continue
            position = _vec3(header, ACTOR_CURRENT)
            if not (_finite_vec(position) and
                    _overlay_visible(camera, position, ACTOR_COLLIDER_SCAN_PADDING)):
                continue
        except Exception:
            continue
        offsets = FOLIAGE_COLLIDER_OFFSETS.get(proc)
        if offsets is None:
            if actor not in _foliage_probe_offsets:
                if not (force or _frame % FOLIAGE_PROBE_INTERVAL == 0):
                    continue
                _foliage_probe_offsets[actor] = _probe_plant_collider_offsets(actor)
            offsets = _foliage_probe_offsets[actor]
        for offset in offsets:
            address = actor + offset
            if address in seen_colliders:
                continue
            try:
                collider_data = RD.read_bytes(address, 0x150)
            except Exception:
                continue
            collider = _decode_collider(actor + offset, collider_data)
            if collider is None or collider[1] != actor:
                continue
            seen_colliders.add(address)
            _append_embedded_collider(found, actor, collider, collider_data)
    return found


def _read_state_recovery_colliders():
    """Read only validated embedded dCcD objects from a loaded paused state."""
    found = {"attack": [], "target": [], "push": []}
    seen_nodes, seen_colliders = set(), set()
    try:
        queue = RD.read_bytes(ACTOR_QUEUE, 12)
        node, count = _u32(queue, 0), min(_u32(queue, 8), 2048)
    except Exception:
        return found
    for _ in range(count):
        if not _valid_ptr(node) or node in seen_nodes:
            break
        seen_nodes.add(node)
        try:
            tag = RD.read_bytes(node, 0x14)
            actor, node = _u32(tag, 0x0C), _u32(tag, 8)
            if not _valid_ptr(actor) or actor == _link_actor:
                continue
            data = RD.read_bytes(actor, STATE_COLLIDER_RECOVERY_BYTES)
        except Exception:
            continue
        for offset in range(0x290, len(data) - 0x150 + 1, 4):
            address = actor + offset
            if address in seen_colliders:
                continue
            collider_data = data[offset:offset + 0x150]
            status_ptr = _u32(collider_data, 0x44)
            if not actor + 0x290 <= status_ptr < actor + len(data):
                continue
            if COLLIDER_DECODER.shape_kind(collider_data) is None:
                continue
            collider = _decode_collider(actor + offset, collider_data)
            if collider is None or collider[1] != actor:
                continue
            seen_colliders.add(address)
            _append_embedded_collider(found, actor, collider, collider_data)
    return found


def _read_player_state_colliders():
    """Read Link's saved collision members for a paused state recovery."""
    found = {"attack": [], "target": [], "push": []}
    try:
        actor = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    except Exception:
        return found
    if not _valid_ptr(actor):
        return found
    for offset in PLAYER_STATE_COLLIDER_OFFSETS:
        try:
            data = RD.read_bytes(actor + offset, 0x150)
            collider = _decode_collider(actor + offset, data)
        except Exception:
            continue
        if collider is not None and collider[1] == actor:
            _append_embedded_collider(found, actor, collider, data)
    return found


def _read_player_attack_colliders():
    """Read Link's sword hitboxes from daPy_lk_c directly."""
    colliders = []
    try:
        actor = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    except Exception:
        return colliders
    if not _valid_ptr(actor):
        return colliders
    for offset in PLAYER_ATTACK_COLLIDER_OFFSETS:
        try:
            collider = _decode_collider(actor + offset)
        except Exception:
            continue
        if collider is None or collider[1] != actor:
            continue
        try:
            data = RD.read_bytes(actor + offset, 0x30)
        except Exception:
            continue
        if _u32(data, COLLIDER_ATTACK_SPRM) & 1:
            colliders.append(collider)
    return colliders


def _merge_actor_fallbacks():
    """Add embedded actors once without replacing the live dCcS snapshot."""
    global _attack_colliders, _target_colliders, _push_colliders
    for source in (_actor_fallback_colliders, _state_recovery_colliders):
        for label, colliders in (("attack", _attack_colliders), ("target", _target_colliders),
                                 ("push", _push_colliders)):
            known = set(colliders)
            colliders.extend(collider for collider in source[label] if collider not in known)
    known = set(_attack_colliders)
    _attack_colliders.extend(collider for collider in _player_attack_colliders
                             if collider not in known)


def _refresh_runtime_overlays(force=False, recover_registry=False):
    global _triggers, _trigger_filter_key, _push_colliders, _attack_colliders, _target_colliders
    global _actor_fallback_colliders, _state_recovery_colliders, _player_attack_colliders, _enemy_health
    global _actor_labels, _attack_labels, _attack_actor_labels
    global _target_info_labels, _target_actor_labels, _contact_actor_labels
    filter_key = (cb_load.checked, cb_event.checked, cb_switch.checked, cb_other.checked)
    if force or filter_key != _trigger_filter_key or _frame % ACTOR_REFRESH_INTERVAL == 0:
        _triggers = _read_actor_triggers()
        _trigger_filter_key = filter_key
    if _collider_watch_ready:
        if recover_registry:
            if _state_load_overlay_snapshot is not None:
                # A saved dCcS snapshot is exact for this state. TWW clears
                # its public registry counts before the next frame, so do not
                # replace this with the stale backing arrays below.
                snapshot = _state_load_overlay_snapshot
                _attack_colliders = (list(snapshot["attack"])
                                     if cb_attack.checked and snapshot["attack"] is not None else [])
                _target_colliders = (list(snapshot["target"])
                                     if cb_target.checked and snapshot["target"] is not None else [])
                _push_colliders = (list(snapshot["push"])
                                  if cb_push.checked and snapshot["push"] is not None else [])
                _state_recovery_colliders = {"attack": [], "target": [], "push": []}
                _player_attack_colliders = []
                _actor_fallback_colliders = {"attack": [], "target": [], "push": []}
                _enemy_health = _read_enemy_health(_viewer_camera())
                if cb_actor_names.checked or cb_move_actor.checked or cb_zone_info.checked:
                    _refresh_actor_records(_viewer_camera())
                # Info labels require raw collider bytes; actor labels only need decoded owners.
                _attack_labels = []
                _target_info_labels = []
                _attack_actor_labels = []
                if cb_attack.checked and cb_attack_actor.checked:
                    for collider in _attack_colliders:
                        _append_actor_label(_attack_actor_labels, collider, None)
                _target_actor_labels = []
                if cb_target.checked and cb_target_actor.checked:
                    for collider in _target_colliders:
                        _append_actor_label(_target_actor_labels, collider, None)
                _contact_actor_labels = []
                if cb_push.checked and cb_contact_actor.checked:
                    for collider in _push_colliders:
                        _append_actor_label(_contact_actor_labels, collider, None)
                return
            _read_live_colliders(allow_stale=True)
            if _valid_ptr(_link_actor):
                _push_colliders[:] = [item for item in _push_colliders if item[1] != _link_actor]
                _attack_colliders[:] = [item for item in _attack_colliders if item[1] != _link_actor]
                _target_colliders[:] = [item for item in _target_colliders if item[1] != _link_actor]
            _state_recovery_colliders = _read_state_recovery_colliders()
            player_colliders = _read_player_state_colliders()
            for label, colliders in player_colliders.items():
                _state_recovery_colliders[label].extend(colliders)
        # _capture_live_colliders owns the current snapshot. Do not clear it
        # here: dCcS has already cleared its public registries by this point.
        if not cb_push.checked:
            _push_colliders = []
        if not cb_attack.checked:
            _attack_colliders = []
        if not cb_target.checked:
            _target_colliders = []
    elif cb_push.checked or cb_attack.checked or cb_target.checked:
        _read_live_colliders()
    else:
        _push_colliders, _attack_colliders, _target_colliders = [], [], []
    _player_attack_colliders = _read_player_attack_colliders() if cb_attack.checked else []
    if cb_push.checked or cb_attack.checked or cb_target.checked:
        if force or _frame % FOLIAGE_REFRESH_INTERVAL == 0:
            _actor_fallback_colliders = _read_foliage_colliders(_viewer_camera(), force)
    else:
        _actor_fallback_colliders = {"attack": [], "target": [], "push": []}
    _merge_actor_fallbacks()
    _enemy_health = _read_enemy_health(_viewer_camera())
    if cb_actor_names.checked or cb_move_actor.checked or cb_zone_info.checked:
        if force or not _actor_records or _frame % ACTOR_REFRESH_INTERVAL == 0:
            _refresh_actor_records(_viewer_camera())
    else:
        _actor_labels = []
    if not (cb_attack.checked and cb_attack_info.checked):
        _attack_labels = []
    if not (cb_attack.checked and cb_attack_actor.checked):
        _attack_actor_labels = []
    if not (cb_target.checked and cb_target_info.checked):
        _target_info_labels = []
    if not (cb_target.checked and cb_target_actor.checked):
        _target_actor_labels = []
    if not (cb_push.checked and cb_contact_actor.checked):
        _contact_actor_labels = []


def _refresh_game_camera():
    global _game_camera
    try:
        camera = read_camera(RD)
        eye, target, up = tuple(camera["eye"]), tuple(camera["center"]), tuple(camera["up"])
        fovy = float(camera["fovy"])
        forward = _sub(target, eye)
        if not (all(math.isfinite(value) for value in eye + target + up) and
                _dot(forward, forward) > 1.0 and 5.0 <= fovy <= 175.0):
            raise ValueError("invalid camera")
        _game_camera = (eye, target, up, fovy)
        if cb_follow_cam.checked:
            _freecam["pos"][:] = eye
            _freecam["az"], _freecam["el"] = _angles_from_forward(forward)
            _freecam["ready"] = True
    except Exception:
        _game_camera = None


def _ensure_freecam():
    if _freecam["ready"]:
        return
    target = _link or (_game_camera[1] if _game_camera is not None else (0.0, 0.0, 0.0))
    forward = _forward_from_angles(_freecam["az"], _freecam["el"])
    _freecam["pos"][:] = [target[0] - forward[0] * 2600.0,
                            target[1] - forward[1] * 2600.0,
                            target[2] - forward[2] * 2600.0]
    _freecam["ready"] = True


def _viewer_camera():
    camera_args = {"width": W, "height": H, "focal": _active_focal()}
    if cb_follow_cam.checked and _game_camera is not None:
        eye, target, up, _ = _game_camera
        return ViewerCamera(eye, _sub(target, eye), up, **camera_args)
    _ensure_freecam()
    return ViewerCamera(_freecam["pos"], _forward_from_angles(_freecam["az"], _freecam["el"]),
                        **camera_args)


def _load_seams(stage, room):
    directory = os.path.join(SEAM_DIR, stage)
    if not os.path.isdir(directory):
        return []
    path = os.path.join(directory, "Room%d__room.csv" % room)
    if not os.path.exists(path):
        candidates = [name for name in os.listdir(directory) if name.endswith("__room.csv")]
        if len(candidates) != 1:
            return []
        path = os.path.join(directory, candidates[0])
    clips = []
    try:
        with open(path, newline="") as source:
            for row in csv.DictReader(source):
                initial = (float(row["init_x"]), float(row["init_y"]), float(row["init_z"]))
                destination = (float(row["dest_x"]), float(row["dest_y"]), float(row["dest_z"]))
                displacement = math.sqrt(sum((a - b) ** 2 for a, b in zip(initial, destination)))
                clips.append({"point": (float(row["seam_x"]), float(row["seam_y"]),
                                        float(row["seam_z"])),
                              "initial": initial, "destination": destination,
                              "angle": float(row["angle_deg"]),
                              "rollstab": displacement <= ROLL_STAB_MAX})
    except Exception:
        return []
    return clips


def _write_position(address, point):
    for axis, value in enumerate(point):
        memory.write_f32(address + axis * 4, value)


def _teleport_initial(clip):
    base = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    if not 0x80000000 <= base < 0x81800000:
        return
    for offset in POS_OFFSETS:
        _write_position(base + offset, clip["initial"])
    _write_position(LINK_X, clip["initial"])


def _teleport_destination(clip):
    _write_position(LINK_X, clip["destination"])


def _seam_selection():
    index = _seams["selected"]
    clips = _seams["clips"]
    return clips[index] if index is not None and 0 <= index < len(clips) else None


def _apply_input():
    global _cache, _hardware_mesh_key, _hardware_dynamic_key, _hardware_state_key
    global _viewer_fullscreen, _clean_capture, _fullscreen_before_capture
    global _selected_face
    changed = False
    try:
        toggle = canvas.take_capture_toggle()
    except AttributeError:
        toggle = False
    if toggle:
        if _clean_capture:
            _clean_capture = False
            _viewer_fullscreen = _fullscreen_before_capture
        else:
            _fullscreen_before_capture = _viewer_fullscreen
            _viewer_fullscreen = True
            _clean_capture = True
        changed = True
    _sync_edit_modes()
    if cull_frame_button.clicked:
        cb_culling.checked = True
        cb_follow_cam.checked = False
        _freecam.update(pos=[0.0, 0.0, 0.0], az=38.0, el=29.0, ready=False)
        _mouse_look["active"] = False
        _ensure_freecam()
        changed = True
    if reset_button.clicked:
        _cache = None
        _hardware_mesh_key = _hardware_dynamic_key = _hardware_state_key = None
        _freecam.update(pos=[0.0, 0.0, 0.0], az=38.0, el=29.0, ready=False)
        _mouse_look["active"] = False
        _selected_face = None
        _set_selected_point(None)
        changed = True
    clip = _seam_selection()
    if clip is not None and seam_initial_button.clicked:
        _teleport_initial(clip)
    if clip is not None and seam_clip_button.clicked:
        _teleport_destination(clip)
    if cb_follow_cam.checked:
        _mouse_look["active"] = False
        return changed
    _ensure_freecam()
    wheel = canvas.take_wheel()
    mouse_x, mouse_y, _ = canvas.mouse_pos()
    try:
        right_down, keys = canvas.right_down(), canvas.key_mask()
    except AttributeError:
        right_down, keys = False, 0
    if right_down:
        if _mouse_look["active"]:
            _freecam["az"] -= (mouse_x - _mouse_look["x"]) * 0.28
            _freecam["el"] = max(-85.0, min(85.0, _freecam["el"] +
                                               (mouse_y - _mouse_look["y"]) * 0.28))
            changed = True
        _mouse_look.update(active=True, x=mouse_x, y=mouse_y)
    else:
        _mouse_look["active"] = False
    camera = _viewer_camera()
    movement = [0.0, 0.0, 0.0]
    if keys & KEY_W: movement = [movement[i] + camera.forward[i] for i in range(3)]
    if keys & KEY_S: movement = [movement[i] - camera.forward[i] for i in range(3)]
    if keys & KEY_D: movement = [movement[i] + camera.right[i] for i in range(3)]
    if keys & KEY_A: movement = [movement[i] - camera.right[i] for i in range(3)]
    if keys & KEY_SPACE: movement[1] += 1.0
    if keys & KEY_SHIFT: movement[1] -= 1.0
    if wheel:
        for axis in range(3):
            movement[axis] += camera.forward[axis] * wheel * 4.0
    length = math.sqrt(_dot(movement, movement))
    if length:
        speed = move_speed_slider.value / length
        for axis in range(3):
            _freecam["pos"][axis] += movement[axis] * speed
        changed = True
    return changed


def _static_signature():
    if _snapshot is None:
        return ()
    return (_snapshot["stage"],
            tuple((slot, mesh["bgw"], mesh["pm_bgd"], mesh["v_tbl"], mesh["v_num"], mesh["t_num"])
                  for slot, mesh in sorted(_snapshot["meshes"].items()) if not mesh["is_movebg"]))


def _upload_static_mesh():
    global _hardware_mesh_key, _stage_visible, _static_wire_positions, _static_wire_colors
    enabled = (cb_ground.checked, cb_slope.checked, cb_wall.checked, cb_roof.checked)
    signature = (enabled, _static_signature())
    if signature == _hardware_mesh_key:
        return
    positions = [array("f") for _ in range(3)]
    colors = [array("I") for _ in range(3)]
    wire_positions, wire_colors = array("f"), array("I")
    class_index = {"ground": 0, "slope": 0, "wall": 1, "roof": 2}
    palette = (C_GROUND, C_WALL, C_ROOF)
    for mesh in _snapshot["meshes"].values():
        if mesh["is_movebg"]:
            continue
        for index, triangle in enumerate(mesh["tris"]):
            surface_class = _surface_class(mesh, index)
            group = class_index[surface_class]
            if ((surface_class == "ground" and not cb_ground.checked) or
                    (surface_class == "slope" and not cb_slope.checked) or
                    (surface_class == "wall" and not cb_wall.checked) or
                    (surface_class == "roof" and not cb_roof.checked)):
                continue
            color = _surface_color(mesh, index, C_SLOPE if surface_class == "slope" else palette[group])
            for vertex in triangle[:3]:
                positions[group].extend(mesh["verts"][vertex])
            colors[group].extend((color, color, color))
            for first, second in ((0, 1), (1, 2), (2, 0)):
                wire_positions.extend(mesh["verts"][triangle[first]])
                wire_positions.extend(mesh["verts"][triangle[second]])
                wire_colors.extend((C_WIREFRAME, C_WIREFRAME))
    for group in range(3):
        canvas.hardware_mesh(group, positions[group].tobytes(), colors[group].tobytes())
    _static_wire_positions, _static_wire_colors = wire_positions.tobytes(), wire_colors.tobytes()
    _hardware_mesh_key = signature
    _stage_visible = True


def _surface_class(mesh, triangle_index):
    """Expose only the DZB ground surfaces that Link's slide code accepts."""
    surface_class = mesh["classes"][triangle_index]
    if surface_class != "ground":
        return surface_class
    special_codes = mesh.get("special_codes", ())
    ground_codes = mesh.get("ground_codes", ())
    special = special_codes[triangle_index] if triangle_index < len(special_codes) else 0
    ground = ground_codes[triangle_index] if triangle_index < len(ground_codes) else 0
    return "slope" if special == 1 and ground != 8 else "ground"


def _surface_color(mesh, triangle_index, default_color):
    """Return TWW's live surface color from the triangle's current normal."""
    if mesh["classes"][triangle_index] != "wall":
        return default_color
    triangle = mesh["tris"][triangle_index]
    a, b, c = (mesh["verts"][triangle[0]], mesh["verts"][triangle[1]],
               mesh["verts"][triangle[2]])
    normal_y = _norm(_cross(_sub(b, a), _sub(c, a)))[1]
    return C_WALL_VERTICAL if abs(normal_y) <= 0.001 else default_color


def _clear_static_mesh():
    global _hardware_mesh_key, _stage_visible, _static_wire_positions, _static_wire_colors
    if _stage_visible is False:
        return
    empty_positions, empty_colors = array("f").tobytes(), array("I").tobytes()
    for group in range(3):
        canvas.hardware_mesh(group, empty_positions, empty_colors)
    _static_wire_positions = _static_wire_colors = b""
    _hardware_mesh_key = None
    _stage_visible = False


def _upload_dynamic_mesh():
    global _hardware_dynamic_key, _dynamic_wire_positions, _dynamic_wire_colors
    dynamic_meshes = tuple((slot, mesh["bgw"], mesh["v_tbl"], mesh["t_num"], mesh["vertex_probe"])
                           for slot, mesh in sorted(_snapshot["meshes"].items())
                           if mesh["is_movebg"])
    # Reupload moveable geometry only when its sampled world pose changes.
    # Empty and disabled dynamic layers are retained instead of recreating an
    # empty GPU buffer every update, which matters in rooms with no moving collision.
    enabled = {"ground": cb_ground.checked, "slope": cb_slope.checked,
               "wall": cb_wall.checked, "roof": cb_roof.checked}
    palette = {"ground": C_GROUND, "slope": C_SLOPE, "wall": C_WALL, "roof": C_ROOF}
    signature = (_snapshot["stage"], cb_movebg.checked, tuple(sorted(enabled.items())), dynamic_meshes)
    if signature == _hardware_dynamic_key:
        return
    positions, colors = array("f"), array("I")
    wire_positions, wire_colors = array("f"), array("I")
    if cb_movebg.checked:
        for mesh in _snapshot["meshes"].values():
            if not mesh["is_movebg"]:
                continue
            for index, triangle in enumerate(mesh["tris"]):
                surface_class = _surface_class(mesh, index)
                if not enabled[surface_class]:
                    continue
                for vertex in triangle[:3]:
                    positions.extend(mesh["verts"][vertex])
                color = _surface_color(mesh, index, palette[surface_class])
                colors.extend((color, color, color))
                for first, second in ((0, 1), (1, 2), (2, 0)):
                    wire_positions.extend(mesh["verts"][triangle[first]])
                    wire_positions.extend(mesh["verts"][triangle[second]])
                    wire_colors.extend((C_WIREFRAME, C_WIREFRAME))
    # Groups 0-4 are used in the collision depth prepass. Keep movable
    # BG in the final collision slot so it layers with static room geometry.
    canvas.hardware_mesh(4, positions.tobytes(), colors.tobytes())
    _dynamic_wire_positions, _dynamic_wire_colors = wire_positions.tobytes(), wire_colors.tobytes()
    _hardware_dynamic_key = signature


def _queue_line(a, b, color):
    _line_positions.extend(a)
    _line_positions.extend(b)
    _line_colors.extend((color, color))


def _queue_marker(point, radius, color):
    x, y, z = point
    vertices = ((x, y + radius, z), (x + radius, y, z), (x, y, z + radius),
                (x - radius, y, z), (x, y, z - radius), (x, y - radius, z))
    for triangle in ((0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1),
                     (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4)):
        for vertex in triangle:
            _marker_positions.extend(vertices[vertex])
        _marker_colors.extend((color, color, color))


def _queue_volume_triangle(a, b, c, color):
    if not cb_filled_triggers.checked:
        return
    _volume_positions.extend(a)
    _volume_positions.extend(b)
    _volume_positions.extend(c)
    _volume_colors.extend((color, color, color))


def _queue_volume_box(points, color):
    for a, b, c, d in ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
                       (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)):
        _queue_volume_triangle(points[a], points[b], points[c], color)
        _queue_volume_triangle(points[a], points[c], points[d], color)


def _draw_box(center, half, color):
    hx, hy, hz = half
    x, y, z = center
    points = ((x - hx, y - hy, z - hz), (x + hx, y - hy, z - hz),
              (x + hx, y - hy, z + hz), (x - hx, y - hy, z + hz),
              (x - hx, y + hy, z - hz), (x + hx, y + hy, z - hz),
              (x + hx, y + hy, z + hz), (x - hx, y + hy, z + hz))
    _queue_volume_box(points, color)
    for first, second in ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6),
                          (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
        _queue_line(points[first], points[second], color)


def _draw_cylinder(bottom, radius, height, color):
    rings = []
    for y in (0.0, height):
        rings.append([(bottom[0] + math.cos(index * math.pi * 2.0 / VOLUME_CYLINDER_SEGMENTS) * radius,
                       bottom[1] + y,
                       bottom[2] + math.sin(index * math.pi * 2.0 / VOLUME_CYLINDER_SEGMENTS) * radius)
                      for index in range(VOLUME_CYLINDER_SEGMENTS)])
    top = (bottom[0], bottom[1] + height, bottom[2])
    for index in range(VOLUME_CYLINDER_SEGMENTS):
        nxt = (index + 1) % VOLUME_CYLINDER_SEGMENTS
        _queue_volume_triangle(rings[0][index], rings[0][nxt], rings[1][nxt], color)
        _queue_volume_triangle(rings[0][index], rings[1][nxt], rings[1][index], color)
        _queue_volume_triangle(bottom, rings[0][nxt], rings[0][index], color)
        _queue_volume_triangle(top, rings[1][index], rings[1][nxt], color)
        _queue_line(rings[0][index], rings[0][nxt], color)
        _queue_line(rings[1][index], rings[1][nxt], color)
        if index % 2 == 0:
            _queue_line(rings[0][index], rings[1][index], color)


def _sphere_points(center, radius, rows=3, segments=8):
    return [[(center[0] + math.sin(math.pi * row / rows) * math.cos(col * math.pi * 2.0 / segments) * radius,
              center[1] + math.cos(math.pi * row / rows) * radius,
              center[2] + math.sin(math.pi * row / rows) * math.sin(col * math.pi * 2.0 / segments) * radius)
             for col in range(segments)] for row in range(rows + 1)]


def _sphere_detail(camera, center, radius):
    """Choose sphere detail from projected size, matching the TP viewer."""
    depth = max(camera.near, _dot(_sub(center, camera.position), camera.forward))
    projected_radius = radius * _active_focal() / depth
    if projected_radius <= SPHERE_DETAIL_FAR_PIXELS:
        return 3, 8, 2
    if projected_radius <= SPHERE_DETAIL_MEDIUM_PIXELS:
        return 5, 12, 3
    return 8, 20, 4


def _draw_sphere(camera, center, radius, color):
    rows, segments, meridian_step = _sphere_detail(camera, center, radius)
    points = _sphere_points(center, radius, rows, segments)
    for row in range(rows):
        for col in range(segments):
            nxt = (col + 1) % segments
            _queue_volume_triangle(points[row][col], points[row + 1][col], points[row + 1][nxt], color)
            _queue_volume_triangle(points[row][col], points[row + 1][nxt], points[row][nxt], color)
    # Keep the wire mesh proportionate to the fill. Near spheres retain the
    # high quality rings while distant ones avoid needless overlay work.
    for row in range(1, rows):
        for col in range(segments):
            _queue_line(points[row][col], points[row][(col + 1) % segments], color)
    for col in range(0, segments, meridian_step):
        for row in range(rows):
            _queue_line(points[row][col], points[row + 1][col], color)


def _draw_capsule(camera, start, end, radius, color):
    _draw_sphere(camera, start, radius, color)
    _draw_sphere(camera, end, radius, color)
    direction = _sub(end, start)
    length = math.sqrt(_dot(direction, direction))
    if length <= 0.001:
        return
    direction = tuple(component / length for component in direction)
    side = _cross(direction, (0.0, 1.0, 0.0))
    if _dot(side, side) < 0.0001:
        side = (1.0, 0.0, 0.0)
    else:
        side = _norm(side)
    up = _norm(_cross(side, direction))
    rings = []
    for center in (start, end):
        rings.append([tuple(center[axis] +
                           (side[axis] * math.cos(index * math.pi * 2.0 / 8.0) +
                            up[axis] * math.sin(index * math.pi * 2.0 / 8.0)) * radius
                           for axis in range(3)) for index in range(8)])
    for index in range(8):
        nxt = (index + 1) % 8
        _queue_volume_triangle(rings[0][index], rings[0][nxt], rings[1][nxt], color)
        _queue_volume_triangle(rings[0][index], rings[1][nxt], rings[1][index], color)
    for vector in (side, tuple(-value for value in side), up, tuple(-value for value in up)):
        _queue_line(tuple(start[axis] + vector[axis] * radius for axis in range(3)),
                    tuple(end[axis] + vector[axis] * radius for axis in range(3)), color)


def _draw_collider(camera, collider, color):
    kind, _owner, *shape = collider
    if kind == "box":
        _draw_box(shape[0], shape[1], color)
    elif kind == "cylinder":
        _draw_cylinder(shape[0], shape[1], shape[2], color)
    elif kind == "sphere":
        _draw_sphere(camera, shape[0], shape[1], color)
    elif kind == "capsule":
        _draw_capsule(camera, shape[0], shape[1], shape[2], color)
    else:
        _queue_volume_triangle(shape[0], shape[1], shape[2], color)
        _queue_line(shape[0], shape[1], color)
        _queue_line(shape[1], shape[2], color)
        _queue_line(shape[2], shape[0], color)


def _within_draw_radius(camera, point, bounds_radius=0.0):
    """Keep a dynamic volume when its bounding sphere intersects draw range."""
    radius = radius_slider.value
    if radius <= 0.0:
        return True
    dx = point[0] - camera.position[0]
    dy = point[1] - camera.position[1]
    dz = point[2] - camera.position[2]
    limit = radius + max(0.0, bounds_radius)
    return dx * dx + dy * dy + dz * dz <= limit * limit


def _within_view(camera, point, bounds_radius=0.0):
    """Keep a live volume when its bounding sphere intersects the view bounds.

    The retained stage mesh stays entirely on the GPU, where normal hardware
    clipping is substantially cheaper than rebuilding it whenever the camera
    moves. Dynamic actor and trigger overlays are generated in Python, so
    culling them here avoids geometry generation.
    """
    if not cb_view_cull.checked:
        return True
    bounds_radius = max(0.0, bounds_radius)
    relative = _sub(point, camera.position)
    depth = _dot(relative, camera.forward)
    if depth + bounds_radius <= camera.near:
        return False
    # Use signed distance to the four side planes. A center only test drops
    # large triggers & colliders that overlap the viewport from just offscreen;
    # the sphere/plane test avoids that while still removing off camera work.
    focal = max(1.0, _active_focal())
    horizontal_slope = W * 0.5 / focal
    vertical_slope = H * 0.5 / focal
    horizontal_limit = bounds_radius * math.sqrt(1.0 + horizontal_slope * horizontal_slope)
    vertical_limit = bounds_radius * math.sqrt(1.0 + vertical_slope * vertical_slope)
    horizontal = abs(_dot(relative, camera.right))
    vertical = abs(_dot(relative, camera.up))
    return (horizontal - horizontal_slope * depth <= horizontal_limit and
            vertical - vertical_slope * depth <= vertical_limit)


def _overlay_visible(camera, point, bounds_radius=0.0):
    return (_within_draw_radius(camera, point, bounds_radius) and
            _within_view(camera, point, bounds_radius))


def _collider_center(collider):
    """Return a conservative center and extent for radius culling a collider."""
    kind, _owner, *shape = collider
    if kind == "box":
        return shape[0], math.sqrt(_dot(shape[1], shape[1]))
    if kind == "cylinder":
        return (shape[0][0], shape[0][1] + shape[2] * 0.5, shape[0][2]), math.hypot(shape[1], shape[2] * 0.5)
    if kind == "sphere":
        return shape[0], shape[1]
    if kind == "capsule":
        start, end, radius = shape
        center = tuple((start[index] + end[index]) * 0.5 for index in range(3))
        return center, math.sqrt(_dot(_sub(end, start), _sub(end, start))) * 0.5 + radius
    center = tuple(sum(point[index] for point in shape) / 3.0 for index in range(3))
    extent = max(math.sqrt(_dot(_sub(point, center), _sub(point, center))) for point in shape)
    return center, extent


def _collider_top(collider):
    """Return an owner collider's topmost point and a useful screen space bound."""
    kind, _owner, *shape = collider
    if kind == "box":
        center, half = shape
        return (center[0], center[1] + half[1], center[2]), max(half)
    if kind == "cylinder":
        bottom, radius, height = shape
        return (bottom[0], bottom[1] + height, bottom[2]), max(radius, height * 0.5)
    if kind == "sphere":
        center, radius = shape
        return (center[0], center[1] + radius, center[2]), radius
    if kind == "capsule":
        start, end, radius = shape
        top = start if start[1] >= end[1] else end
        return (top[0], top[1] + radius, top[2]), max(radius, abs(end[1] - start[1]) * 0.5)
    points = shape
    top = max(points, key=lambda point: point[1])
    center, extent = _collider_center(collider)
    return top, extent


def _read_enemy_health(camera):
    """Read base-actor HP for owners of visible collider volumes only.

    fopAc_ac_c stores signed `max_health`/`health` bytes at +0x284/+0x285.
    Restricting reads to decoded collider owners avoids treating unrelated actor
    fields as enemy HP and avoids another full actor queue walk every frame.
    """
    if not cb_enemy_hp.checked:
        return []

    anchors = {}
    for colliders in (_target_colliders, _push_colliders, _attack_colliders):
        for collider in colliders:
            owner = collider[1]
            if not _valid_ptr(owner) or owner == _link_actor:
                continue
            top, extent = _collider_top(collider)
            if not _overlay_visible(camera, top, extent):
                continue
            previous = anchors.get(owner)
            if previous is None or top[1] > previous[0][1]:
                anchors[owner] = (top, extent)

    bars = []
    for owner, (top, extent) in anchors.items():
        try:
            max_health, health = struct.unpack(">bb", RD.read_bytes(owner + ACTOR_MAX_HEALTH, 2))
        except Exception:
            continue
        # A positive max and a current value within that range is the base
        # actor convention used by standard enemies. Some non enemy actors
        # reuse these bytes for parameters, so reject unrealistic values.
        if not (0 < max_health <= 127 and 0 <= health <= max_health):
            continue
        lift = max(18.0, min(180.0, extent * 0.25))
        bars.append((owner, (top[0], top[1] + lift, top[2]), health, max_health))

    # Prefer the nearest readable bars if a room exceeds the HUD limit.
    bars.sort(key=lambda item: _dot(_sub(item[1], camera.position), camera.forward))
    return bars[:MAX_HP_BARS]


def _screen_marker_radius(camera, point, pixels, minimum, maximum):
    """Return a world radius that keeps a debug marker readable at any depth."""
    depth = max(camera.near, _dot(_sub(point, camera.position), camera.forward))
    return max(minimum, min(maximum, pixels * depth / max(1.0, _active_focal())))


def _queue_face_outline_edge(camera, first, second, color):
    """Draw a face edge as a camera facing ribbon without moving its endpoints."""
    direction = _sub(second, first)
    side = _cross(camera.forward, direction)
    if _dot(side, side) < 0.000001:
        side = camera.right
    else:
        side = _norm(side)
    center = tuple((first[axis] + second[axis]) * 0.5 for axis in range(3))
    depth = max(camera.near, _dot(_sub(center, camera.position), camera.forward))
    pixel_width = min(14.0, 3.5 + depth / 1800.0)
    half_width = pixel_width * 0.5 * depth / max(1.0, _active_focal())
    a0 = tuple(first[axis] + side[axis] * half_width for axis in range(3))
    a1 = tuple(first[axis] - side[axis] * half_width for axis in range(3))
    b0 = tuple(second[axis] + side[axis] * half_width for axis in range(3))
    b1 = tuple(second[axis] - side[axis] * half_width for axis in range(3))
    for vertex in (a0, b0, b1, a0, b1, a1):
        _marker_positions.extend(vertex)
    _marker_colors.extend((color,) * 6)


def _vertex_hud_layout():
    """Return shared vertex HUD geometry for drawing and hit testing."""
    x1 = W - 10
    x0 = max(8, x1 - min(440, W - 18))
    return x0, x1, 194


def _pick_collision_face(camera, screen_point):
    """Return the nearest enabled collision face below a canvas click."""
    if _snapshot is None:
        return None
    sx, sy = screen_point
    focal = _active_focal()
    ray = _norm(tuple(
        camera.forward[axis] + camera.right[axis] * (sx - W * 0.5) / focal -
        camera.up[axis] * (sy - H * 0.5) / focal
        for axis in range(3)))
    enabled_classes = {
        "ground": cb_ground.checked,
        "slope": cb_slope.checked,
        "wall": cb_wall.checked,
        "roof": cb_roof.checked,
    }
    nearest = None
    nearest_distance = float("inf")
    origin = camera.position
    for slot, mesh in _snapshot["meshes"].items():
        if mesh["is_movebg"]:
            if not cb_movebg.checked:
                continue
        elif not cb_enabled.checked:
            continue
        for index, triangle in enumerate(mesh["tris"]):
            surface_class = _surface_class(mesh, index)
            if not enabled_classes.get(surface_class, False):
                continue
            a, b, c = (mesh["verts"][triangle[0]], mesh["verts"][triangle[1]],
                       mesh["verts"][triangle[2]])
            edge1, edge2 = _sub(b, a), _sub(c, a)
            cross = _cross(ray, edge2)
            determinant = _dot(edge1, cross)
            if abs(determinant) < 0.000001:
                continue
            inverse = 1.0 / determinant
            offset = _sub(origin, a)
            u = _dot(offset, cross) * inverse
            if u < 0.0 or u > 1.0:
                continue
            cross2 = _cross(offset, edge1)
            v = _dot(ray, cross2) * inverse
            if v < 0.0 or u + v > 1.0:
                continue
            distance = _dot(edge2, cross2) * inverse
            hit = tuple(origin[axis] + ray[axis] * distance for axis in range(3))
            if (_dot(_sub(hit, origin), camera.forward) <= camera.near or
                    distance > nearest_distance + 0.01):
                continue
            # Prefer static collision on an effectively identical surface.
            if (abs(distance - nearest_distance) <= 0.01 and nearest is not None and
                    not mesh["is_movebg"] and nearest["move"]):
                continue
            # The visible part can be hit even when original corners are behind
            # the near plane (common on large terrain faces).
            nearest_distance = distance
            nearest = {
                "hit": hit,
                "world": (a, b, c),
                "class": surface_class,
                "move": mesh["is_movebg"],
                "slot": slot, "index": index, "bgw": mesh["bgw"], "pm_bgd": mesh["pm_bgd"],
            }
    return nearest


def _project_face_edges(camera, world):
    """Project original face edges, clipping those that cross the near plane."""
    edges = []
    near = camera.near + 0.001
    for first, second in ((0, 1), (1, 2), (2, 0)):
        a, b = world[first], world[second]
        depth_a = _dot(_sub(a, camera.position), camera.forward)
        depth_b = _dot(_sub(b, camera.position), camera.forward)
        if depth_a < near and depth_b < near:
            continue
        if depth_a < near:
            t = (near - depth_a) / (depth_b - depth_a)
            a = tuple(a[axis] + (b[axis] - a[axis]) * t for axis in range(3))
            depth_a = near
        elif depth_b < near:
            t = (near - depth_b) / (depth_a - depth_b)
            b = tuple(b[axis] + (a[axis] - b[axis]) * t for axis in range(3))
            depth_b = near
        projected_a, projected_b = camera.project(a), camera.project(b)
        if projected_a is not None and projected_b is not None:
            edges.append((projected_a[0], projected_b[0], a, b, depth_a, depth_b))
    return edges


def _edge_point(mouse, first, second):
    dx, dy = second[0] - first[0], second[1] - first[1]
    length2 = dx * dx + dy * dy
    if length2 < 0.000001:
        return 0.0, (mouse[0] - first[0]) ** 2 + (mouse[1] - first[1]) ** 2
    t = max(0.0, min(1.0, ((mouse[0] - first[0]) * dx +
                           (mouse[1] - first[1]) * dy) / length2))
    x, y = first[0] + dx * t, first[1] + dy * t
    return t, (mouse[0] - x) ** 2 + (mouse[1] - y) ** 2


def _set_selected_point(point, triangle=None):
    global _selected_point
    _selected_point = None if point is None else {"point": point, "triangle": triangle}


def _selected_xyz_text():
    return "" if _selected_point is None else "%.6f, %.6f, %.6f" % _selected_point["point"]


def _face_normal(record):
    a, b, c = record["world"]
    return _norm(_cross(_sub(b, a), _sub(c, a)))


def _normal_xyz_text(record):
    normal = _face_normal(record)
    return "N: X=%.7f  Y=%.7f  Z=%.7f" % normal


def _normal_angle_lines(record):
    normal = _face_normal(record)
    vertical = math.degrees(math.acos(max(-1.0, min(1.0, abs(normal[1])))))
    if math.hypot(normal[0], normal[2]) < 0.00001:
        return "Surface angles: N/A (vertical normal)", "Vertical: %.1f deg" % vertical
    perpendicular = int(round(math.atan2(normal[0], normal[2]) * 32768.0 / math.pi)) & 0xFFFF
    parallel = (perpendicular + 0x4000) & 0xFFFF
    return ("Parallel: %d/%d" % (parallel, (parallel + 0x8000) & 0xFFFF),
            "Perp: %d/%d  Vertical: %.1f deg" %
            (perpendicular, (perpendicular + 0x8000) & 0xFFFF, vertical))


def _teleport_point(point):
    try:
        actor = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
        if _valid_ptr(actor):
            for offset in POS_OFFSETS:
                _write_position(actor + offset, point)
        _write_position(LINK_X, point)
        return True
    except Exception:
        return False


def _queue_seams(camera):
    if not cb_seams.checked:
        return
    selected = _seams["selected"]
    for index, clip in enumerate(_seams["clips"]):
        point = clip["point"]
        if not _overlay_visible(camera, point, 320.0):
            continue
        color = C_SEAM_SELECTED if index == selected else (C_SEAM_ROLL if clip["rollstab"] else C_SEAM_PUSH)
        pixels = 12.0 if index == selected else 8.0
        radius = _screen_marker_radius(camera, point, pixels, 8.0, 320.0)
        # Group 6 renders after scene depth, so these black rimmed markers
        # stay visible over collision instead of disappearing into a seam.
        _queue_marker(point, radius * 1.28, 0xFF101820)
        _queue_marker(point, radius, color)


def _ordered_boundary(edges):
    """Return one closed boundary loop, or None for disjoint/branched geometry."""
    neighbors = {}
    for first, second in edges:
        neighbors.setdefault(first, []).append(second)
        neighbors.setdefault(second, []).append(first)
    if not neighbors or any(len(values) != 2 for values in neighbors.values()):
        return None
    start = next(iter(neighbors))
    loop, previous, current = [start], None, start
    while True:
        choices = [value for value in neighbors[current] if value != previous]
        if not choices:
            return None
        nxt = choices[0]
        if nxt == start:
            return loop if len(loop) == len(neighbors) else None
        if nxt in loop:
            return None
        loop.append(nxt)
        previous, current = current, nxt


def _queue_load_zone_outline(lower, upper):
    for index in range(len(lower)):
        nxt = (index + 1) % len(lower)
        _queue_line(lower[index], lower[nxt], C_LOAD)
        _queue_line(upper[index], upper[nxt], C_LOAD)
        _queue_line(lower[index], upper[index], C_LOAD)


def _triangulate_boundary(points):
    if len(points) < 3:
        return ()
    normal = (0.0, 0.0, 0.0)
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        normal = (normal[0] + (point[1] - nxt[1]) * (point[2] + nxt[2]),
                  normal[1] + (point[2] - nxt[2]) * (point[0] + nxt[0]),
                  normal[2] + (point[0] - nxt[0]) * (point[1] + nxt[1]))
    axis = max(range(3), key=lambda index: abs(normal[index]))
    if abs(normal[axis]) < 0.0001:
        return ()
    axes = tuple(index for index in range(3) if index != axis)
    projected = [(point[axes[0]], point[axes[1]]) for point in points]
    area = sum(projected[index][0] * projected[(index + 1) % len(projected)][1] -
               projected[(index + 1) % len(projected)][0] * projected[index][1]
               for index in range(len(projected)))
    if abs(area) < 0.0001:
        return ()
    winding = 1.0 if area > 0.0 else -1.0

    def cross2(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1]) -
                (b[1] - a[1]) * (c[0] - a[0]))

    def inside(a, b, c, point):
        ab, bc, ca = cross2(a, b, point), cross2(b, c, point), cross2(c, a, point)
        epsilon = 0.00001
        return (ab >= -epsilon and bc >= -epsilon and ca >= -epsilon if winding > 0.0 else
                ab <= epsilon and bc <= epsilon and ca <= epsilon)

    remaining = list(range(len(points)))
    triangles = []
    while len(remaining) > 3:
        clipped = False
        for cursor, current in enumerate(remaining):
            previous = remaining[cursor - 1]
            following = remaining[(cursor + 1) % len(remaining)]
            a, b, c = projected[previous], projected[current], projected[following]
            if winding * cross2(a, b, c) <= 0.00001:
                continue
            if any(inside(a, b, c, projected[other]) for other in remaining
                   if other not in (previous, current, following)):
                continue
            triangles.append((previous, current, following))
            del remaining[cursor]
            clipped = True
            break
        if not clipped:
            return ()
    triangles.append(tuple(remaining))
    return tuple(triangles)


def _queue_load_zone_volume(lower, upper):
    for first, second, third in _triangulate_boundary(lower):
        _queue_volume_triangle(lower[first], lower[second], lower[third], C_LOAD)
        _queue_volume_triangle(upper[third], upper[second], upper[first], C_LOAD)
    for index in range(len(lower)):
        nxt = (index + 1) % len(lower)
        _queue_volume_triangle(lower[index], lower[nxt], upper[nxt], C_LOAD)
        _queue_volume_triangle(lower[index], upper[nxt], upper[index], C_LOAD)


def _queue_load_zones(camera):
    """Raw authored exit volumes, or nearest-floor projected gliding regions."""
    leaf_exit_help.visible = cb_leaf_exits.checked
    cb_leaf_columns.visible = cb_leaf_exits.checked
    if cb_leaf_exits.checked:
        _queue_leaf_exit_projection(camera)
        return
    if not cb_load.checked or _snapshot is None:
        return
    for mesh in _snapshot["meshes"].values():
        exits = {}
        for index, triangle in enumerate(mesh["tris"]):
            if index >= len(mesh.get("exit_ids", ())) or mesh["exit_ids"][index] == 0x3F:
                continue
            exits.setdefault(mesh["exit_ids"][index], []).append(triangle[:3])
        for triangles in exits.values():
            boundary = set()
            for triangle in triangles:
                for first, second in ((triangle[0], triangle[1]), (triangle[1], triangle[2]),
                                      (triangle[2], triangle[0])):
                    edge = tuple(sorted((first, second)))
                    if edge in boundary:
                        boundary.remove(edge)
                    else:
                        boundary.add(edge)
            loop = _ordered_boundary(boundary)
            if loop is None:
                continue
            lower = tuple(mesh["verts"][vertex] for vertex in loop)
            upper = tuple((point[0], point[1] + LOAD_ZONE_HEIGHT, point[2]) for point in lower)
            center = tuple(sum(point[axis] for point in lower) / len(lower) for axis in range(3))
            extent = max(math.sqrt(_dot(_sub(point, center), _sub(point, center))) for point in lower)
            if not _overlay_visible(camera, center, extent + LOAD_ZONE_HEIGHT):
                continue
            _queue_load_zone_volume(lower, upper)
            _queue_load_zone_outline(lower, upper)


def _commit_overlays():
    global _hardware_line_key, _hardware_on_top_line_key, _hardware_marker_key, _hardware_volume_key
    overlay_line_positions = _line_positions.tobytes()
    overlay_line_colors = _line_colors.tobytes()
    line_positions = b""
    line_colors = b""
    if cb_wire.checked and not SOFTWARE_RENDER:  # software mode wires the triangles directly
        if cb_enabled.checked:
            line_positions += _static_wire_positions
            line_colors += _static_wire_colors
        line_positions += _dynamic_wire_positions
        line_colors += _dynamic_wire_colors
    if cb_on_top_triggers.checked:
        on_top_positions, on_top_colors = overlay_line_positions, overlay_line_colors
    else:
        line_positions += overlay_line_positions
        line_colors += overlay_line_colors
        on_top_positions = on_top_colors = b""
    key = (line_positions, line_colors)
    if key != _hardware_line_key:
        canvas.hardware_mesh(5, line_positions, line_colors)
        _hardware_line_key = key
    key = (on_top_positions, on_top_colors)
    if key != _hardware_on_top_line_key:
        canvas.hardware_mesh(8, on_top_positions, on_top_colors)
        _hardware_on_top_line_key = key
    marker_positions, marker_colors = _marker_positions.tobytes(), _marker_colors.tobytes()
    key = (marker_positions, marker_colors)
    if key != _hardware_marker_key:
        canvas.hardware_mesh(6, marker_positions, marker_colors)
        _hardware_marker_key = key
    volume_positions, volume_colors = _volume_positions.tobytes(), _volume_colors.tobytes()
    key = (volume_positions, volume_colors)
    if key != _hardware_volume_key:
        canvas.hardware_mesh(7, volume_positions, volume_colors)
        _hardware_volume_key = key


def _set_hardware_state(camera):
    global _hardware_state_key
    state = (camera.position, camera.right, camera.up, camera.forward, _active_focal(),
             radius_slider.value, opacity_slider.value, wire_opacity_slider.value,
             trigger_opacity_slider.value, cb_filled.checked, cb_wire.checked, True,
             cb_xray.checked, cb_on_top_triggers.checked, _viewer_fullscreen, _clean_capture,
             cb_enemy_hp.checked, cb_actor_names.checked, cb_coordinate_dot.checked,
             cb_culling.checked, cb_cull_labels.checked, cb_zone_info.checked)
    if state == _hardware_state_key:
        return
    canvas.hardware_state(camera.position, camera.right, camera.up, camera.forward, _active_focal(),
                          radius_slider.value, opacity_slider.value, wire_opacity_slider.value,
                          cb_filled.checked, cb_wire.checked, True, cb_xray.checked,
                          cb_on_top_triggers.checked, _viewer_fullscreen, _clean_capture,
                          hud_visible=(not _clean_capture or cb_enemy_hp.checked or
                                       cb_actor_names.checked or cb_coordinate_dot.checked or cb_zone_info.checked or
                                       (cb_culling.checked and cb_cull_labels.checked)),
                          overlay_opacity=trigger_opacity_slider.value)
    _hardware_state_key = state


def _draw_enemy_hp_hud(hud, camera):
    """Draw health bars above enemies"""
    if not cb_enemy_hp.checked:
        return
    for _owner, anchor, health, max_health in _enemy_health:
        projected = camera.project(anchor)
        if projected is None:
            continue
        point, _depth = projected
        center_x, top = point[0], point[1] - 26.0
        left, right = center_x - HP_BAR_WIDTH * 0.5, center_x + HP_BAR_WIDTH * 0.5
        if right < 0.0 or left > W or top + HP_BAR_HEIGHT < 0.0 or top > H:
            continue
        ratio = health / max_health
        hud.rect_filled((left - 1.0, top - 1.0), (right + 1.0, top + HP_BAR_HEIGHT + 1.0),
                        0xE6101018, 1)
        hud.rect_filled((left, top), (right, top + HP_BAR_HEIGHT), 0xFF3B1010, 1)
        if ratio > 0.0:
            hud.rect_filled((left, top), (left + HP_BAR_WIDTH * ratio, top + HP_BAR_HEIGHT),
                            0xFFFF3B30, 1)
        hud.text((left + 1.0, top - 14.0), 0xFFFFFFFF, "HP %d/%d" % (health, max_health))


def _draw_actor_names_hud(hud, camera):
    """Draw procedure names above actors after scene geometry has rendered."""
    if not cb_actor_names.checked:
        return
    labels = []
    for position, label in _actor_labels:
        projected = camera.project((position[0], position[1] + 120.0, position[2]))
        if projected is None:
            continue
        point, depth = projected
        if point[0] < -160.0 or point[0] > W + 160.0 or point[1] < -24.0 or point[1] > H + 24.0:
            continue
        labels.append((depth, point, label))
    labels.sort(reverse=True)
    for _depth, point, label in labels:
        x, y = point[0] - len(label) * 3.5, point[1] - 12.0
        hud.text((x + 1.0, y + 1.0), 0xE6000000, label)
        hud.text((x, y), 0xFFFFFFFF, label)


def _project_labels(camera, labels):
    """Project (position, text) labels to screen space, culling anything
    off-canvas, and depth-sort back-to-front (nearest drawn last, so it
    ends up on top of anything it overlaps). Shared by every *_hud drawer
    below whose labels already carry their own on-screen anchor point --
    _draw_attack_info_hud and the Target Info / Attack Actor / Target
    Actor / Contact Actor drawers.
    """
    projected_labels = []
    for position, label in labels:
        projected = camera.project(position)
        if projected is None:
            continue
        point, depth = projected
        if point[0] < -160.0 or point[0] > W + 160.0 or point[1] < -24.0 or point[1] > H + 24.0:
            continue
        projected_labels.append((depth, point, label))
    projected_labels.sort(reverse=True)
    return projected_labels


def _draw_label_set(hud, camera, labels, color):
    """Draw a projected (position, text) label list with the standard drop
    shadow, in `color`. Shared body for every simple info/actor-name HUD
    drawer -- callers just supply their own labels list, checkbox gate and
    color.
    """
    for _depth, point, label in _project_labels(camera, labels):
        x, y = point[0] - len(label) * 3.5, point[1] - 12.0
        hud.text((x + 1.0, y + 1.0), 0xE6000000, label)
        hud.text((x, y), color, label)


def _draw_attack_info_hud(hud, camera):
    if cb_attack.checked and cb_attack_info.checked:
        _draw_label_set(hud, camera, _attack_labels, C_ATTACK_INFO)


def _draw_attack_actor_hud(hud, camera):
    if cb_attack.checked and cb_attack_actor.checked:
        _draw_label_set(hud, camera, _attack_actor_labels, C_ATTACK)


def _draw_target_info_hud(hud, camera):
    if cb_target.checked and cb_target_info.checked:
        _draw_label_set(hud, camera, _target_info_labels, C_TARGET_INFO)


def _draw_target_actor_hud(hud, camera):
    if cb_target.checked and cb_target_actor.checked:
        _draw_label_set(hud, camera, _target_actor_labels, C_TARGET)


def _draw_contact_actor_hud(hud, camera):
    if cb_push.checked and cb_contact_actor.checked:
        _draw_label_set(hud, camera, _contact_actor_labels, C_PUSH)




def _draw_zone_flags_hud(hud, camera):
    if not cb_zone_info.checked:
        return
    for address, targets in _zone_actor_records.items():
        record = _actor_records.get(address)
        if record is None:
            continue
        position = record[1]
        projected = camera.project((position[0], position[1]+120.0, position[2]))
        if projected is None:
            continue
        (x,y), _depth = projected
        if not (0 <= x <= W and 0 <= y <= H):
            continue
        labels=[]
        for target in targets:
            if not target['overlay']:continue
            room='-' if target['room'] is None else str(target['room'])
            label='Room %s switch %d: %s' % (room,target['number'],target['state'])
            if label not in labels:labels.append(label)
        for index,label in enumerate(labels):
            hud.text((x+4,y-16-index*16),0xFFE6EDF3,label)
        if labels:
            name=proc_name(record[0], "Proc 0x%03X" % record[0])
            hud.text((x+4,y-16-len(labels)*16),0xFFE6EDF3,name)


def _draw_move_actor_hud(hud):
    if not cb_move_actor.checked or _clean_capture:
        return
    x0, y0, x1, y1 = _move_actor_hud_layout()
    outline, panel_bg, text, button = 0xFF3C9CFF, 0xE610315D, 0xFFF2F7FF, 0xFF1B568D
    hud.rect_filled((x0, y0), (x1, y1), panel_bg, 2).rect((x0, y0), (x1, y1), outline, 2, 2)
    info = _selected_actor_hud_info()
    if info is None:
        hud.text((x0 + 12, y0 + 12), text, "Actor Selector")
        hud.text((x0 + 12, y0 + 34), text, "Click a dynamic actor or collider")
        return
    procedure, position, rotation = info
    label = proc_name(procedure, "Proc 0x%03X" % procedure)
    hud.text((x0 + 12, y0 + 10), text, "Actor Selector: %s" % label)
    hud.text((x0 + 12, y0 + 34), text, "Address: 0x%08X   Proc ID: 0x%03X" % (_move_actor_selected, procedure))
    hud.text((x0 + 12, y0 + 58), text, "Position: X=%.3f  Y=%.3f  Z=%.3f" % position)
    hud.text((x0 + 12, y0 + 80), text, "Rotation: X=%d  Y=%d  Z=%d" % rotation)
    freeze, actor_to_link, link_to_actor, copy_watches = _move_actor_button_rects()
    locked = _move_actor_selected in _move_actor_locks
    for rect, title in ((freeze, "Unfreeze Actor" if locked else "Freeze Actor"),
                        (actor_to_link, "Teleport to Link"), (link_to_actor, "Teleport to Actor"),
                        (copy_watches, "Copy XYZ Watch Addresses")):
        hud.rect_filled((rect[0], rect[1]), (rect[2], rect[3]), button, 2)
        hud.text((rect[0] + 100, rect[1] + 5), text, title)
    projected = _viewer_camera().project(position)
    if projected is not None:
        hud.circle(projected[0], MOVE_ACTOR_CENTER_RADIUS, C_MOVE_CENTER, 2.5)


def _draw_coordinate_dot_hud(hud, camera):
    """Draw Link's coordinate point over collision, independent of depth."""
    if not cb_coordinate_dot.checked or _link is None:
        return
    projected = camera.project(_link)
    if projected is None:
        return
    point, _depth = projected
    if point[0] < -COORDINATE_DOT_OUTER_RADIUS or point[0] > W + COORDINATE_DOT_OUTER_RADIUS:
        return
    if point[1] < -COORDINATE_DOT_OUTER_RADIUS or point[1] > H + COORDINATE_DOT_OUTER_RADIUS:
        return
    hud.circle_filled(point, COORDINATE_DOT_OUTER_RADIUS, C_COORDINATE_OUTLINE)
    hud.circle_filled(point, COORDINATE_DOT_INNER_RADIUS, C_COORDINATE)


def _update_overlay_text(camera):
    global _hardware_overlay_key
    clip = _seam_selection()
    selected_point = _selected_xyz_text() if _selected_point is not None else None
    collider_info = _selected_collider_hud_info() if cb_move_collider.checked else None
    key = (cb_zone_info.checked, _zone_label_revision if cb_zone_info.checked else None,
           _cull_revision, cb_culling.checked, cb_cull_labels.checked,
           cb_cull_visible.checked, cb_cull_hidden.checked,
           collider_info, cb_floor_ray.checked, repr(_floor_query) if cb_floor_ray.checked else None, cb_move_collider.checked, COLLIDER_EDITS.selected, W, H, _clean_capture, cb_seams.checked, cb_vertex_select.checked, len(_seams["clips"]),
           cb_enemy_hp.checked, tuple(_enemy_health), cb_actor_names.checked, tuple(_actor_labels),
           cb_attack_info.checked, tuple(_attack_labels),
           cb_attack_actor.checked, tuple(_attack_actor_labels),
           cb_target_info.checked, tuple(_target_info_labels),
           cb_target_actor.checked, tuple(_target_actor_labels),
           cb_contact_actor.checked, tuple(_contact_actor_labels),
           cb_move_actor.checked, _move_actor_selected, _selected_actor_hud_info(),
           tuple(sorted((actor, lock["position"]) for actor, lock in _move_actor_locks.items())),
           cb_coordinate_dot.checked, _link, camera.position, camera.right, camera.up, camera.forward,
           None if clip is None else (clip["point"], clip["angle"], clip["rollstab"]),
           _normal_xyz_text(_selected_face) if _selected_face is not None else None,
           _normal_angle_lines(_selected_face) if _selected_face is not None else None,
           selected_point)
    if key == _hardware_overlay_key:
        return

    hud = canvas.hud()
    if not _clean_capture:
        outline, panel_bg, text = 0xFF3C9CFF, 0xE610315D, 0xFFF2F7FF
        hud.rect_filled((8, 8), (42, 42), panel_bg, 2).rect((8, 8), (42, 42), outline, 2, 2)
        hud.line((15, 22), (15, 15), text, 2).line((15, 15), (22, 15), text, 2)
        hud.line((35, 22), (35, 15), text, 2).line((35, 15), (28, 15), text, 2)
        hud.line((15, 28), (15, 35), text, 2).line((15, 35), (22, 35), text, 2)
        hud.line((35, 28), (35, 35), text, 2).line((35, 35), (28, 35), text, 2)

        # The seam controls intentionally exist only while the Qt checkbox is enabled
        if cb_seams.checked:
            button_bg = 0xFF1B568D if clip is not None else 0x99314C68
            hud.rect_filled((12, 50), (244, 82), panel_bg, 2).rect((12, 50), (244, 82), outline, 2, 2)
            hud.circle_filled((28, 66), 7, C_SEAM_ROLL)
            hud.text((42, 57), text, "Show Seam Clips (%d)" % len(_seams["clips"]))
            hud.rect_filled((256, 50), (422, 82), button_bg, 2).rect((256, 50), (422, 82), outline, 2, 1)
            hud.rect_filled((434, 50), (600, 82), button_bg, 2).rect((434, 50), (600, 82), outline, 2, 1)
            hud.text((287, 57), text, "Initial Position")
            hud.text((477, 57), text, "Clip Position")
            if clip is None:
                hud.text((18, 94), text, "Click a seam dot to select it")
            else:
                hud.text((18, 94), text, "Seam: %.2f, %.2f, %.2f" % clip["point"])
                hud.text((18, 116), text, "Angle: %.2f deg  %s" %
                         (clip["angle"], "roll-stab reachable" if clip["rollstab"] else "needs push"))

        # Vertex Select is a Qt only mode switch. Its canvas HUD is drawn only
        # while that switch is enabled, keeping the normal TWW view uncluttered.
        if cb_vertex_select.checked:
            x0, x1, bottom = _vertex_hud_layout()
            button_bg = 0xFF1B568D
            hud.rect_filled((x0, 8), (x1, bottom), panel_bg, 2).rect((x0, 8), (x1, bottom), outline, 2, 2)
            hud.text((x0 + 12, 14), text, "Vertex select: ON")
            if _selected_face is None:
                hud.text((x0 + 12, 44), text, "Click a collision face")
            else:
                parallel, perpendicular = _normal_angle_lines(_selected_face)
                hud.text((x0 + 12, 40), text, _normal_xyz_text(_selected_face))
                hud.text((x0 + 12, 62), text, parallel)
                hud.text((x0 + 12, 82), text, perpendicular)
                hud.rect_filled((x0 + 10, 100), (x0 + 142, 126), button_bg, 2)
                hud.text((x0 + 34, 104), text, "Copy Normal")
                if selected_point is None:
                    hud.text((x0 + 12, 140), text, "Click selected face again for point")
                else:
                    hud.text((x0 + 12, 140), text, selected_point)
                    hud.rect_filled((x0 + 10, 156), (x0 + 122, 182), button_bg, 2)
                    hud.rect_filled((x0 + 132, 156), (x1 - 10, 182), button_bg, 2)
                    hud.text((x0 + 47, 160), text, "Copy")
                    hud.text((x0 + 178, 160), text, "Teleport")
    # These scene annotations are intentionally retained in clean capture
    # mode when enabled; normal canvas controls stay hidden there.
    _draw_culling_hud(hud, camera)
    _draw_enemy_hp_hud(hud, camera)
    _draw_actor_names_hud(hud, camera)
    _draw_zone_flags_hud(hud, camera)
    _draw_attack_info_hud(hud, camera)
    _draw_attack_actor_hud(hud, camera)
    _draw_target_info_hud(hud, camera)
    _draw_target_actor_hud(hud, camera)
    _draw_contact_actor_hud(hud, camera)
    _draw_coordinate_dot_hud(hud, camera)
    _draw_floor_ray_hud(hud)
    _draw_move_actor_hud(hud)
    _draw_move_collider_hud(hud,camera,collider_info)
    hud.commit()
    _hardware_overlay_key = key


def _handle_vertex_select(camera, click):
    global _selected_face
    if not cb_vertex_select.checked:
        if _selected_face is not None or _selected_point is not None:
            _selected_face = None
            _set_selected_point(None)
            return True, False
        return False, False
    if _clean_capture:
        return False, False
    # Let the shared fullscreen button and seam HUD keep their existing
    # behavior instead of interpreting those controls as collision clicks.
    if click is not None and 8 <= click[0] <= 42 and 8 <= click[1] <= 42:
        return False, False
    if click is not None and cb_seams.checked and 8 <= click[0] <= 610 and 44 <= click[1] <= 140:
        return False, False
    x0, x1, bottom = _vertex_hud_layout()
    if click is not None and x0 <= click[0] <= x1 and 8 <= click[1] <= bottom:
        if _selected_face is not None and 100 <= click[1] <= 126 and x0 + 10 <= click[0] <= x0 + 142:
            gui.set_clipboard(_normal_xyz_text(_selected_face))
        elif _selected_point is not None and 156 <= click[1] <= 182:
            if x0 + 10 <= click[0] <= x0 + 122:
                gui.set_clipboard(_selected_xyz_text())
            elif x0 + 132 <= click[0] <= x1 - 10:
                _teleport_point(_selected_point["point"])
        # Reserve the complete HUD so controls never select underlying faces.
        return True, True
    if click is not None and click[0] >= W - 450 and 0 <= click[1] <= 210:
        return False, True

    changed = False
    if click is not None:
        record = _pick_collision_face(camera, click)
        if record is not None:
            if _selected_face is not None and _selected_face["world"] == record["world"]:
                _set_selected_point(record["hit"], record)
            else:
                _selected_face = record
                _set_selected_point(None)
            changed = True
        return changed, changed

    try:
        left_down = canvas.left_down()
    except AttributeError:
        left_down = False
    if left_down and _selected_point is not None and _selected_point["triangle"] is not None:
        mouse_x, mouse_y, inside = canvas.mouse_pos()
        if inside:
            record = _selected_point["triangle"]
            edges = _project_face_edges(camera, record["world"])
            if edges:
                (t, _distance2), edge = min(
                    ((_edge_point((mouse_x, mouse_y), edge[0], edge[1]), edge)
                     for edge in edges), key=lambda item: item[0][1])
                screen_a, screen_b, a, b, depth_a, depth_b = edge
                # Convert the screen fraction back to a world-space fraction.
                t = t * depth_a / ((1.0 - t) * depth_b + t * depth_a)
                _set_selected_point(tuple(a[axis] + (b[axis] - a[axis]) * t
                                          for axis in range(3)), record)
                changed = True
    return changed, False


def _pick_seam(camera, click):
    global _viewer_fullscreen
    if click is None:
        return False
    if _clean_capture:
        return False
    if 8 <= click[0] <= 42 and 8 <= click[1] <= 42:
        _viewer_fullscreen = not _viewer_fullscreen
        return True
    if cb_seams.checked and 256 <= click[0] <= 422 and 50 <= click[1] <= 82:
        clip = _seam_selection()
        if clip is not None:
            _teleport_initial(clip)
        return True
    if cb_seams.checked and 434 <= click[0] <= 600 and 50 <= click[1] <= 82:
        clip = _seam_selection()
        if clip is not None:
            _teleport_destination(clip)
        return True
    if cb_seams.checked and 8 <= click[0] <= 610 and 44 <= click[1] <= 140:
        return True
    if not cb_seams.checked:
        return False
    best_index, best_distance = None, 14.0 * 14.0
    for index, clip in enumerate(_seams["clips"]):
        projected = camera.project(clip["point"])
        if projected is None:
            continue
        point, _ = projected
        distance = (point[0] - click[0]) ** 2 + (point[1] - click[1]) ** 2
        if distance < best_distance:
            best_index, best_distance = index, distance
    if best_index is None:
        return False
    _seams["selected"] = best_index
    return True


def _actor_procedure(actor):
    record = _actor_records.get(actor)
    if record is not None:
        return record[0]
    try:
        return _u16(RD.read_bytes(actor + ACTOR_PROC, 2))
    except Exception:
        return None


def _selected_actor_position():
    if _move_actor_selected is None:
        return None
    lock = _move_actor_locks.get(_move_actor_selected)
    if lock is not None:
        return lock["position"]
    position = _actor_positions.get(_move_actor_selected)
    if position is not None:
        return position
    try:
        position = _vec3(RD.read_bytes(_move_actor_selected + ACTOR_CURRENT, 12))
        return position if _finite_vec(position) else None
    except Exception:
        return None


def _write_actor_position(actor, position):
    """Write both fopAc transforms and clear velocity for one generic actor."""
    if not _valid_ptr(actor) or not _finite_vec(position):
        return False
    try:
        for offset in (ACTOR_CURRENT, ACTOR_OLD):
            for axis, value in enumerate(position):
                memory.write_f32(actor + offset + axis * 4, float(value))
        for axis in range(3):
            memory.write_f32(actor + ACTOR_SPEED + axis * 4, 0.0)
        _actor_positions[actor] = tuple(position)
        record = _actor_records.get(actor)
        if record is not None:
            _actor_records[actor] = (record[0], tuple(position), record[2], record[3])
        return True
    except Exception:
        return False


def _clear_move_actor_state():
    global _move_actor_selected, _move_actor_drag
    _move_actor_locks.clear()
    _move_actor_selected = None
    _move_actor_drag = None


def _maintain_actor_locks():
    global _move_actor_selected
    for actor, lock in list(_move_actor_locks.items()):
        if _actor_procedure(actor) != lock["procedure"] or not _write_actor_position(actor, lock["position"]):
            _move_actor_locks.pop(actor, None)
            if _move_actor_selected == actor:
                _move_actor_selected = None


def _selected_actor_hud_info():
    if _move_actor_selected is None:
        return None
    position = _selected_actor_position()
    procedure = _actor_procedure(_move_actor_selected)
    if position is None or procedure is None:
        return None
    try:
        rotation = struct.unpack(">3h", RD.read_bytes(_move_actor_selected + ACTOR_CURRENT_ANGLE, 6))
    except Exception:
        rotation = (0, 0, 0)
    return procedure, position, rotation


def _move_actor_hud_layout():
    height=66.0 if _move_actor_selected is None else 244.0
    top=min(150.0,max(10.0,H-height-10.0))
    return (10.0,top,420.0 if _move_actor_selected is None else 430.0,top+height)


def _move_actor_button_rects(layout=None):
    x0, y0, x1, _y1 = layout or _move_actor_hud_layout()
    return ((x0 + 10.0, y0 + 104.0, x1 - 10.0, y0 + 132.0),
            (x0 + 10.0, y0 + 138.0, x1 - 10.0, y0 + 166.0),
            (x0 + 10.0, y0 + 172.0, x1 - 10.0, y0 + 200.0),
            (x0 + 10.0, y0 + 206.0, x1 - 10.0, y0 + 234.0))


def _selected_actor_watch_text():
    """Return the three direct Free Look coordinate addresses as one clipboard block."""
    actor = _move_actor_selected
    if actor is None or not _valid_ptr(actor):
        return None
    x_address = actor + ACTOR_CURRENT
    return "0x%08X\n0x%08X\n0x%08X" % (x_address, x_address + 4, x_address + 8)


def _toggle_selected_actor_lock():
    if _move_actor_selected is None:
        return False
    actor = _move_actor_selected
    if actor in _move_actor_locks:
        _move_actor_locks.pop(actor, None)
        return True
    procedure, position = _actor_procedure(actor), _selected_actor_position()
    if procedure is None or position is None:
        return False
    _move_actor_locks[actor] = {"procedure": procedure, "position": tuple(position)}
    return _write_actor_position(actor, position)


def _teleport_selected_actor_to_link():
    if _move_actor_selected is None or _link is None:
        return False
    position = tuple(_link)
    if not _write_actor_position(_move_actor_selected, position):
        return False
    if _move_actor_selected in _move_actor_locks:
        _move_actor_locks[_move_actor_selected]["position"] = position
    return True


def _teleport_link_to_selected_actor():
    position = _selected_actor_position()
    return position is not None and _teleport_point(position)


def _ray_sphere_distance(origin, direction, center, radius):
    offset = _sub(center, origin)
    along = _dot(offset, direction)
    if along <= 0.0:
        return None
    closest2 = _dot(offset, offset) - along * along
    if closest2 > radius * radius:
        return None
    return max(0.0, along - math.sqrt(max(0.0, radius * radius - closest2)))


def _pick_move_actor(camera, screen_point):
    """Return the nearest collision owner, falling back to a small actor pivot."""
    ray, best_distance, best_actor = _screen_ray(camera, screen_point), float("inf"), None
    for collider in _push_colliders + _attack_colliders + _target_colliders:
        owner = collider[1]
        if owner == _link_actor or owner not in _actor_records:
            continue
        center, extent = _collider_center(collider)
        distance = _ray_sphere_distance(camera.position, ray, center, max(40.0, min(900.0, extent)))
        if distance is not None and distance < best_distance:
            best_distance, best_actor = distance, owner
    if best_actor is not None:
        return best_actor
    for actor, (_proc, position, _room, _zone) in _actor_records.items():
        if actor == _link_actor:
            continue
        distance = _ray_sphere_distance(camera.position, ray, position, 70.0)
        if distance is not None and distance < best_distance:
            best_distance, best_actor = distance, actor
    return best_actor


def _select_move_actor(actor):
    global _move_actor_selected, _move_actor_drag
    if actor is None or actor == _link_actor or actor not in _actor_records:
        return False
    position = _actor_positions.get(actor)
    if position is None:
        return False
    _move_actor_selected, _move_actor_drag = actor, None
    return True


def _move_actor_axis_geometry(camera, position, vector):
    origin = camera.project(position)
    if origin is None:
        return None
    length = _screen_marker_radius(camera, position, MOVE_ACTOR_AXIS_PIXELS, 70.0, 4200.0)
    end_world = _add_scaled(position, vector, length)
    projected = camera.project(end_world)
    if projected is None:
        return None
    start, end = origin[0], projected[0]
    screen_length = math.hypot(end[0] - start[0], end[1] - start[1])
    if 0.001 < screen_length < MOVE_ACTOR_AXIS_PIXELS:
        end_world = _add_scaled(position, vector, length * min(12.0, MOVE_ACTOR_AXIS_PIXELS / screen_length))
        projected = camera.project(end_world)
        if projected is None:
            return None
        end = projected[0]
    return start, end, end_world


def _move_actor_axis_handle(camera, point):
    position, best = _selected_actor_position(), None
    if position is None:
        return None
    for axis, vector in enumerate(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))):
        geometry = _move_actor_axis_geometry(camera, position, vector)
        if geometry is None:
            continue
        start, end, _end_world = geometry
        t, distance2 = _edge_point(point, start, end)
        if t >= 0.18 and distance2 <= MOVE_ACTOR_AXIS_PICK_RADIUS ** 2 and (best is None or distance2 < best[0]):
            best = (distance2, vector)
    return best


def _camera_depth_hit(camera, point, depth):
    ray = _screen_ray(camera, point)
    denominator = _dot(ray, camera.forward)
    if abs(denominator) < 0.000001:
        return None
    distance = depth / denominator
    return _add_scaled(camera.position, ray, distance) if distance >= camera.near else None


def _ray_axis_parameter(camera, point, origin, axis):
    ray, axis = _screen_ray(camera, point), _norm(axis)
    offset = _sub(camera.position, origin)
    ray_axis = _dot(ray, axis)
    denominator = 1.0 - ray_axis * ray_axis
    if denominator <= 0.0001:
        return None
    return (_dot(axis, offset) - ray_axis * _dot(ray, offset)) / denominator


def _handle_move_actor(camera, click):
    global _move_actor_mode_active, _move_actor_drag, _selected_face, _viewer_fullscreen
    if not cb_move_actor.checked:
        if _move_actor_mode_active:
            _clear_move_actor_state()
            _move_actor_mode_active = False
            return True, False
        return False, False
    if not _move_actor_mode_active:
        _move_actor_mode_active = True
        _selected_face = None
        _set_selected_point(None)
    _maintain_actor_locks()
    if _clean_capture:
        return False, False
    if click is not None and 8 <= click[0] <= 42 and 8 <= click[1] <= 42:
        _viewer_fullscreen = not _viewer_fullscreen
        return True, True
    x0, y0, x1, y1 = _move_actor_hud_layout()
    if click is not None and x0 <= click[0] <= x1 and y0 <= click[1] <= y1:
        freeze, actor_to_link, link_to_actor, copy_watches = _move_actor_button_rects()
        if _move_actor_selected is not None and freeze[0] <= click[0] <= freeze[2] and freeze[1] <= click[1] <= freeze[3]:
            return _toggle_selected_actor_lock(), True
        if _move_actor_selected is not None and actor_to_link[0] <= click[0] <= actor_to_link[2] and actor_to_link[1] <= click[1] <= actor_to_link[3]:
            return _teleport_selected_actor_to_link(), True
        if _move_actor_selected is not None and link_to_actor[0] <= click[0] <= link_to_actor[2] and link_to_actor[1] <= click[1] <= link_to_actor[3]:
            return _teleport_link_to_selected_actor(), True
        if _move_actor_selected is not None and copy_watches[0] <= click[0] <= copy_watches[2] and copy_watches[1] <= click[1] <= copy_watches[3]:
            watch_text = _selected_actor_watch_text()
            if watch_text is not None:
                gui.set_clipboard(watch_text)
                return True, True
        return True, True
    if click is not None:
        if _move_actor_selected is not None:
            position = _selected_actor_position()
            projected = camera.project(position) if position is not None else None
            if projected is not None and math.hypot(click[0] - projected[0][0], click[1] - projected[0][1]) <= MOVE_ACTOR_CENTER_RADIUS + 7.0:
                depth = max(camera.near, _dot(_sub(position, camera.position), camera.forward))
                hit = _camera_depth_hit(camera, click, depth)
                if hit is not None:
                    offset = _sub(position, hit)
                    _move_actor_drag = {"kind": "plane", "actor": _move_actor_selected, "depth": depth,
                                        "offset": (_dot(offset, camera.right), _dot(offset, camera.up), _dot(offset, camera.forward))}
                return True, True
            handle = _move_actor_axis_handle(camera, click)
            if handle is not None:
                _distance2, vector = handle
                parameter = _ray_axis_parameter(camera, click, position, vector)
                if parameter is not None:
                    _move_actor_drag = {"kind": "axis", "actor": _move_actor_selected, "origin": tuple(position),
                                        "vector": vector, "parameter": parameter}
                return True, True
        return _select_move_actor(_pick_move_actor(camera, click)), True
    try:
        left_down = canvas.left_down()
        mouse_x, mouse_y, inside = canvas.mouse_pos()
    except AttributeError:
        left_down, inside = False, False
    if _move_actor_drag is None:
        return False, False
    if not left_down or not inside:
        _move_actor_drag = None
        return True, False
    drag = _move_actor_drag
    if drag["kind"] == "plane":
        hit = _camera_depth_hit(camera, (mouse_x, mouse_y), drag["depth"])
        if hit is None:
            return False, False
        offset = drag["offset"]
        position = tuple(hit[index] + camera.right[index] * offset[0] + camera.up[index] * offset[1] +
                         camera.forward[index] * offset[2] for index in range(3))
    else:
        parameter = _ray_axis_parameter(camera, (mouse_x, mouse_y), drag["origin"], drag["vector"])
        if parameter is None:
            return False, False
        position = _add_scaled(drag["origin"], drag["vector"], parameter - drag["parameter"])
    changed = _write_actor_position(drag["actor"], position)
    if changed and drag["actor"] in _move_actor_locks:
        _move_actor_locks[drag["actor"]]["position"] = position
    return changed, False


def _draw_move_actor_gizmo(camera):
    if not cb_move_actor.checked or _clean_capture:
        return
    position = _selected_actor_position()
    if position is None:
        return
    radius = _screen_marker_radius(camera, position, 8.0, 8.0, 100.0)
    for vector, color in (((1.0, 0.0, 0.0), C_MOVE_X), ((0.0, 1.0, 0.0), C_MOVE_Y), ((0.0, 0.0, 1.0), C_MOVE_Z)):
        geometry = _move_actor_axis_geometry(camera, position, vector)
        if geometry is not None:
            _start, _end, end_world = geometry
            _queue_line(position, end_world, color)
            _queue_marker(end_world, radius, color)



_CULL_BOX_EDGES = ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),
                   (0,4),(1,5),(2,6),(3,7))
C_CULL_VISIBLE, C_CULL_HIDDEN, C_CULL_MISMATCH = 0xFF37DD6A, 0x99FF5A5A, 0xFFFF00FF
C_CULL_FRUSTUM, C_CULL_FAR, C_CULL_EYE = 0xFF39C6FF, 0x6639C6FF, 0xFFFFC83C


def _set_cull_status(text):
    """Keep live text within a fixed label; updates cannot change Qt layout bounds."""
    global _cull_status_text
    lines = []
    for line in str(text).splitlines():
        lines.extend(textwrap.wrap(line, width=36) or [""])
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:35] + "~"
    lines.extend([""] * (3-len(lines)))
    bounded = "\n".join(lines)
    if bounded != _cull_status_text:
        _cull_status_text = bounded
        cull_status.set(bounded)


def _refresh_culling():
    """Read only while enabled; failures never interrupt the collision viewer."""
    global _cull_snapshot, _cull_revision, _cull_follow_anchor, _cull_status_text
    if not cb_culling.checked:
        if _cull_snapshot is not None:
            _cull_revision += 1
        _set_cull_status("Culling overlay disabled.")
        _cull_snapshot = _cull_follow_anchor = None
        return
    _cull_revision += 1
    try:
        if bytes(RD.read_bytes(0x80000000, 6)) != b"GZLJ01":
            raise ValueError("Culling overlay supports JP/GZLJ01 only")
        _cull_snapshot = read_cull_snapshot(RD)
        counts = _cull_snapshot["counts"]
        boxed = [actor for actor in _cull_snapshot["actors"] if actor["our_culled"] is not None]
        hidden = sum(bool(actor["our_culled"]) for actor in boxed)
        _set_cull_status("Boxes: %d   Visible: %d   Culled: %d\nDraw-state differences: %d\nFar: %.0f   FOV: %.0f" %
                         (len(boxed), len(boxed)-hidden, hidden, counts["mismatch"],
                          _cull_snapshot["cull_far"], _cull_snapshot["camera"]["fovy"]))
        anchor = tuple(_cull_snapshot["link"])
        if cb_cull_follow_link.checked and not cb_follow_cam.checked:
            _ensure_freecam()
            if _cull_follow_anchor is not None:
                for axis in range(3):
                    _freecam["pos"][axis] += anchor[axis] - _cull_follow_anchor[axis]
            _cull_follow_anchor = anchor
        else:
            _cull_follow_anchor = None
    except Exception as exc:
        _cull_snapshot = _cull_follow_anchor = None
        _set_cull_status("Culling overlay: " + str(exc))


def _visible_cull_actors():
    if not cb_culling.checked or _cull_snapshot is None:
        return
    for actor in _cull_snapshot["actors"]:
        if actor["our_culled"] is None or actor["corners"] is None:
            continue
        mismatch = actor["agree"] is False
        if not mismatch and not (cb_cull_hidden.checked if actor["our_culled"] else cb_cull_visible.checked):
            continue
        color = (C_CULL_MISMATCH if mismatch else
                 C_CULL_HIDDEN if actor["our_culled"] else C_CULL_VISIBLE)
        yield actor, color


def _queue_culling_overlay(camera):
    if not cb_culling.checked or _cull_snapshot is None:
        return
    if cb_cull_frustum.checked:
        corners = _cull_snapshot["frustum_corners"]
        for first, second in _CULL_BOX_EDGES:
            color = C_CULL_FAR if first >= 4 and second >= 4 else C_CULL_FRUSTUM
            _queue_line(corners[first], corners[second], color)
    for actor, color in _visible_cull_actors():
        corners = actor["corners"]
        for first, second in _CULL_BOX_EDGES:
            _queue_line(corners[first], corners[second], color)
    game_camera = _cull_snapshot["camera"]
    eye, target, link = game_camera["eye"], game_camera["center"], _cull_snapshot["link"]
    _queue_line(eye, target, C_CULL_EYE)
    _queue_marker(eye, _screen_marker_radius(camera, eye, 6.0, 5.0, 250.0), C_CULL_EYE)
    _queue_marker(link, _screen_marker_radius(camera, link, 4.0, 4.0, 180.0), 0xFFFFFFFF)


def _draw_culling_hud(hud, camera):
    if not cb_culling.checked:
        return
    if not cb_cull_labels.checked:
        return
    for actor, color in _visible_cull_actors():
        projected = camera.project(actor["pos"])
        if projected is not None:
            (x, y), _depth = projected
            if -200 <= x <= W and 0 <= y <= H:
                hud.text((x+4, y-6), color, actor["name"])


def _draw_scene():
    global _line_positions, _line_colors, _marker_positions, _marker_colors
    global _volume_positions, _volume_colors
    if save_settings_button.clicked:
        _save_settings()
    _update_triangle_inspector()
    _update_collider_tools()
    _sync_canvas_size()
    if _snapshot is None:
        return
    camera = _viewer_camera()
    if cb_enabled.checked:
        _upload_static_mesh()
    else:
        _clear_static_mesh()
    _upload_dynamic_mesh()
    _set_hardware_state(camera)
    _line_positions, _line_colors = array("f"), array("I")
    _marker_positions, _marker_colors = array("f"), array("I")
    _volume_positions, _volume_colors = array("f"), array("I")
    if cb_vertex_select.checked and _selected_face is not None:
        a, b, c = _selected_face["world"]
        _queue_face_outline_edge(camera, a, b, C_VERTEX_FACE)
        _queue_face_outline_edge(camera, b, c, C_VERTEX_FACE)
        _queue_face_outline_edge(camera, c, a, C_VERTEX_FACE)
    if cb_vertex_select.checked and _selected_point is not None:
        point = _selected_point["point"]
        radius = _screen_marker_radius(camera, point, 9.0, 5.0, 250.0)
        _queue_marker(point, radius * 1.55, 0xFF101820)
        _queue_marker(point, radius, C_VERTEX_POINT)
    trigger_colors = {"load": C_LOAD, "event": C_EVENT, "switch": C_SWITCH, "other": C_OTHER}
    for kind, category, position, value, extra in _triggers:
        trigger_extent = (math.sqrt(_dot(value, value)) if kind == "box"
                          else math.hypot(value, extra * 0.5))
        if not _overlay_visible(camera, position, trigger_extent):
            continue
        color = trigger_colors[category]
        if kind == "box":
            _draw_box(position, value, color)
        else:
            _draw_cylinder(position, value, extra, color)
    if cb_push.checked:
        for collider in _push_colliders:
            center, extent = _collider_center(collider)
            if not _overlay_visible(camera, center, extent):
                continue
            _draw_collider(camera, collider, C_PUSH_LINK if collider[1] == _link_actor else C_PUSH)
    if cb_attack.checked:
        for collider in _attack_colliders:
            center, extent = _collider_center(collider)
            if not _overlay_visible(camera, center, extent):
                continue
            _draw_collider(camera, collider, C_ATTACK)
    if cb_target.checked:
        for collider in _target_colliders:
            center, extent = _collider_center(collider)
            if not _overlay_visible(camera, center, extent):
                continue
            _draw_collider(camera, collider, C_TARGET)
    _queue_load_zones(camera)
    _queue_floor_tools(camera)
    _draw_move_collider_gizmo(camera)
    _queue_seams(camera)
    _queue_culling_overlay(camera)
    _draw_move_actor_gizmo(camera)
    _commit_overlays()
    _update_overlay_text(camera)
    if SOFTWARE_RENDER:
        canvas.flush()


def _refresh_live(force=False, recover_registry=False):
    global _cache, _snapshot, _link, _link_actor, _selected_face, _floor_query, _last_stage_context
    _snapshot = read_collision(RD, cache=_cache)
    _cache = _snapshot
    _link = _read_link()
    try:
        _link_actor = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    except Exception:
        _link_actor = 0
    context = (_snapshot["stage"], _link_actor)
    if _last_stage_context is not None and context != _last_stage_context:
        COLLIDER_EDITS.clear(restore=False)
    _last_stage_context = context
    _refresh_game_camera()
    _refresh_culling()
    _refresh_runtime_overlays(force, recover_registry)
    _sync_moved_collider_shapes()
    try:
        _floor_query = (read_link_ground_query(RD, _link_actor)
                        if _valid_ptr(_link_actor) and (cb_floor_ray.checked or cb_leaf_exits.checked) else None)
    except Exception:
        _floor_query = None
    stage, room = _snapshot["stage"], _read_room()
    if force or (stage, room) != (_seams["stage"], _seams["room"]):
        _seams.update(stage=stage, room=room, clips=_load_seams(stage, room), selected=None)
        _selected_face = None
        _set_selected_point(None)


def _update_status():
    global _status_summary_last
    if _snapshot is None:
        return
    mesh_count = len(_snapshot["meshes"])
    triangles = sum(mesh["t_num"] for mesh in _snapshot["meshes"].values())
    floor = _snapshot.get("floor")
    summary = "%s: %d meshes, %d triangles, floor=%s, seam clips=%d" % (
        _snapshot["stage"], mesh_count, triangles, floor, len(_seams["clips"]))
    if summary != _status_summary_last:
        status.set(summary)
        _status_summary_last = summary


def _report_error(prefix, exc):
    global _last_error
    _last_error = prefix + str(exc)
    status.set(_last_error)


def _update(force=False, recover_registry=False):
    try:
        changed = _apply_input()
        _refresh_live(force=force, recover_registry=recover_registry)
        camera = _viewer_camera()
        click = canvas.take_click()
        changed = _handle_edit_tools(camera, click) or changed
        _update_status()
        _draw_scene()
    except Exception as exc:
        _report_error("update: ", exc)


@event.on_frameadvance
def on_frameadvance():
    global _frame, _state_recovery_colliders, _state_load_overlay_snapshot
    _frame += 1
    _state_recovery_colliders = {"attack": [], "target": [], "push": []}
    _state_load_overlay_snapshot = None
    _update()
    _cache_state_overlay_snapshot()


def on_memorywatch(is_write, address, value):
    global _applying_collider_flags
    if not is_write and address==_collider_registration_watch:
        _register_locked_colliders()
    if address in _collider_flag_watches and not _applying_collider_flags:
        _apply_collider_flag_locks()
    if is_write and address == CCS_BASE + 0x280C and value > 0 and COLLIDER_EDITS.offsets:
        try:
            if value <= 0x500:
                collider_address = memory.read_u32(CCS_BASE + 0x1400 + (value - 1) * 4) & 0xFFFFFFFF
                COLLIDER_EDITS.apply_registration(collider_address)
        except Exception as exc:
            COLLIDER_EDITS.last_error = str(exc)
    if is_write and address == COLLIDER_SNAPSHOT_WATCH and value == 0:
        if cb_attack.checked or cb_target.checked or cb_push.checked:
            _capture_live_colliders()


@event.on_savestatesave
def on_savestatesave(*_):
    _cache_state_overlay_snapshot()


@event.on_hostupdate
def on_hostupdate():
    global _host_ticks, _state_load_pending, _state_load_wait_ticks, _state_load_signature
    global _state_load_refreshes, _state_load_recover_registry
    global _state_load_overlay_snapshot
    _host_ticks += 1
    try:
        changed = _apply_input()
        if _snapshot is not None:
            camera = _viewer_camera()
            click = canvas.take_click()
            changed = _handle_edit_tools(camera, click) or changed
        if _state_load_pending:
            signature = _state_ram_signature()
            _state_load_wait_ticks += 1
            # The savestate callback can arrive while RAM is being restored.
            # Two matching host samples are stable without advancing a frame.
            if (signature is None or signature != _state_load_signature) and _state_load_wait_ticks < 8:
                _state_load_signature = signature
                return
            _state_load_pending = False
            _state_load_overlay_snapshot = _state_overlay_history.get(signature)
            if _state_load_overlay_snapshot is None and signature is not None:
                _state_load_overlay_snapshot = _state_overlay_history.get(
                    ("context",) + signature[1:])
            _refresh_live(force=True, recover_registry=_state_load_recover_registry)
            _state_load_recover_registry = False
            _state_load_refreshes = 2
            _update_status()
            _draw_scene()
            return
        if _state_load_refreshes:
            _state_load_refreshes -= 1
            _refresh_live(force=True)
            _update_status()
            _draw_scene()
            return
        if _snapshot is None or changed or _host_ticks % 6 == 0:
            _refresh_live(force=False)
            _update_status()
            _draw_scene()
    except Exception as exc:
        _report_error("paused update: ", exc)


@event.on_savestateload
def on_savestateload(*_):
    global _cull_snapshot, _cull_revision, _cull_follow_anchor, _cull_status_text
    _cull_snapshot = _cull_follow_anchor = None
    _set_cull_status("Waiting for culling data...")
    _cull_revision += 1
    global _triangle_selection, _floor_query, _exit_projection_key
    COLLIDER_EDITS.clear(restore=False)
    _triangle_selection = _floor_query = _exit_projection_key = None
    global _cache, _snapshot, _hardware_mesh_key, _hardware_dynamic_key, _hardware_state_key
    global _hardware_line_key, _hardware_on_top_line_key, _hardware_marker_key, _hardware_volume_key
    global _hardware_overlay_key, _stage_visible
    global _triggers, _push_colliders, _attack_colliders, _target_colliders, _player_attack_colliders
    global _enemy_health, _actor_labels, _attack_labels, _attack_actor_labels
    global _target_info_labels, _target_actor_labels, _contact_actor_labels
    global _actor_positions, _actor_records, _room_zones, _zones
    global _room_status_flags, _zone_actor_records, _zone_label_revision
    global _trigger_filter_key, _link_actor, _selected_face
    global _actor_fallback_colliders, _state_recovery_colliders, _foliage_probe_offsets
    global _state_load_pending, _state_load_wait_ticks, _state_load_signature
    global _state_load_refreshes, _state_load_recover_registry
    global _state_load_overlay_snapshot, _move_actor_selected, _move_actor_drag, _move_actor_locks
    _cache = _snapshot = None
    _link_actor = 0
    _triggers = []
    _push_colliders = []
    _attack_colliders = []
    _target_colliders = []
    _player_attack_colliders = []
    _enemy_health = []
    _actor_labels = []
    _attack_labels = []
    _attack_actor_labels = []
    _target_info_labels = []
    _target_actor_labels = []
    _contact_actor_labels = []
    _actor_positions = {}
    _actor_records = {}
    _room_zones = {}
    _zones = {}
    _room_status_flags = {}
    _zone_actor_records = {}
    _zone_label_revision += 1
    _move_actor_selected = None
    _move_actor_drag = None
    _move_actor_locks = {}
    _actor_fallback_colliders = {"attack": [], "target": [], "push": []}
    _state_recovery_colliders = {"attack": [], "target": [], "push": []}
    _foliage_probe_offsets = {}
    _trigger_filter_key = None
    _seams.update(stage=None, room=None, clips=[], selected=None)
    _selected_face = None
    _set_selected_point(None)
    _hardware_mesh_key = _hardware_dynamic_key = _hardware_state_key = None
    _hardware_line_key = _hardware_on_top_line_key = _hardware_marker_key = None
    _hardware_volume_key = _hardware_overlay_key = None
    _stage_visible = None
    # This event can run while Dolphin is still writing state RAM. The host
    # loop waits for a stable memory signature, then refreshes while paused.
    _state_load_pending = True
    _state_load_wait_ticks = 0
    _state_load_signature = None
    _state_load_refreshes = 0
    _state_load_recover_registry = True
    _state_load_overlay_snapshot = None


def _remove_collider_watch():
    """Remove the write watch when the script interpreter shuts down."""
    for remove in (getattr(memory, "remove_memcheck", None),
                   getattr(debug, "remove_memory_breakpoint", None)):
        if remove is None:
            continue
        try:
            for address in _collider_flag_watches:remove(address)
            _collider_flag_watches.clear()
            if _collider_registration_watch is not None:remove(_collider_registration_watch)
            remove(COLLIDER_SNAPSHOT_WATCH)
            if _move_collider_watch_ready:
                remove(CCS_BASE + 0x280C)
            return
        except Exception:
            continue


def _sync_edit_modes():
    """The most recently enabled picking tool owns canvas clicks."""
    global _move_actor_mode_active
    modes = {'collider': cb_move_collider, 'floor': cb_floor_inspector,
             'vertex': cb_vertex_select, 'actor': cb_move_actor}
    newly = [name for name, control in modes.items()
             if control.checked and not _edit_mode_previous.get(name, False)]
    if newly:
        selected = newly[0]
        for name, control in modes.items():
            if name != selected:
                control.checked = False
    _edit_mode_previous.update({name: control.checked for name, control in modes.items()})
    if not cb_move_collider.checked:
        COLLIDER_EDITS.drag = None
    if not cb_move_actor.checked and _move_actor_mode_active:
        _clear_move_actor_state()
        _move_actor_mode_active = False


def _selected_triangle():
    if not _triangle_selection or not _snapshot:
        return None
    record = _triangle_selection
    mesh = _snapshot['meshes'].get(record['slot'])
    if (mesh is None or mesh['bgw'] != record['bgw'] or mesh['pm_bgd'] != record['pm_bgd'] or
            not 0 <= record['index'] < mesh['t_num']):
        return None
    return mesh, record['index']


def _update_triangle_inspector():
    global _triangle_ui_key, _cache, _exit_projection_key, _hardware_mesh_key, _hardware_dynamic_key
    controls = (triangle_info, triangle_field, triangle_value, triangle_read,
                triangle_shared, triangle_edit_help, triangle_apply, triangle_fields_help)
    for control in controls:
        control.visible = cb_floor_inspector.checked
    if not cb_floor_inspector.checked:
        return
    selected = _selected_triangle()
    if selected is None:
        triangle_info.set('Click a collision triangle.')
        # Consume old button presses even when the selected scene vanished.
        _ = triangle_apply.clicked
        _ = triangle_read.clicked
        return
    mesh, index = selected
    try:
        tri, words, users = inspect_triangle(RD, mesh, index)
        values = decode_properties(words)
        key = (_triangle_selection['slot'], index, tri, words, users)
        if key != _triangle_ui_key:
            normal = mesh['normals'][index]
            text = ('BG %d / triangle %d / Ti %d / group %d / %d triangles share Ti\n'
                    'Normal: %.5f, %.5f, %.5f\n'
                    'exit=%d  ground=%d  special=%d  attribute=%d  material=%d  link-through=%d\n'
                    'info0=%08X  info1=%08X  info2=%08X  info3=%08X') % (
                        _triangle_selection['slot'], index, tri[3], tri[4], users, *normal,
                        values['exit_id'], values['ground_code'], values['special_code'],
                        values['attribute_code'], values['material_sound'], values['link_through'], *words)
            triangle_info.set(text)
            if _triangle_ui_key is None:
                field_name = triangle_field.value.strip().lower()
                if field_name in values:
                    triangle_value.value = str(values[field_name])
            _triangle_ui_key = key
        field = triangle_field.value.strip().lower()
        if triangle_read.clicked:
            if field not in values:
                raise ValueError('Unknown property: ' + field)
            triangle_value.value = ('0x%08X' if field.startswith('info') else '%d') % values[field]
        if triangle_apply.clicked:
            tid, affected = write_triangle_property(memory, mesh, _triangle_selection['slot'], index,
                                                    field, triangle_value.value.strip(), triangle_shared.checked, _snapshot["meshes"].values())
            _cache = None
            _hardware_mesh_key = _hardware_dynamic_key = None
            _exit_projection_key = None
            _triangle_ui_key = None
            _refresh_live(force=True)
            if tid!=tri[3]:
                status.set('Copied Ti %d to Ti %d; triangle %d now references Ti %d; updated %s'%
                           (tri[3],tid,index,tid,field))
            else:status.set('Updated triangle property %s; Ti=%d; %d physical triangles affected' % (field, tid, affected))
    except Exception as exc:
        status.set('Triangle inspector: ' + str(exc))


def _queue_floor_tools(camera):
    if cb_floor_ray.checked and _floor_query and _floor_query['valid']:
        origin = _floor_query['origin']
        if _floor_query['floor']:
            hit = (origin[0], _floor_query['height'], origin[2])
            _queue_line(origin, hit, C_LINK)
            _queue_marker(hit, _screen_marker_radius(camera, hit, 5, 2, 60), C_LINK)
            _queue_marker(origin, _screen_marker_radius(camera, origin, 4, 2, 40), 0xFFFFFF33)
            slot, index = _floor_query['floor']
            mesh = _snapshot['meshes'].get(slot)
            if mesh and index < mesh['t_num']:
                points = [mesh['verts'][v] for v in mesh['tris'][index][:3]]
                for i in range(3):
                    _queue_face_outline_edge(camera, points[i], points[(i+1)%3], C_LINK)
        else:
            # No finite game floor: draw a dashed, visibly truncated diagnostic ray.
            length = max(1500.0, radius_slider.value or 12000.0)
            for i in range(16):
                _queue_line(_add_scaled(origin, (0,-1,0), i*length/16),
                            _add_scaled(origin, (0,-1,0), (i+0.5)*length/16), C_LINK)
    selected = _selected_triangle() if cb_floor_inspector.checked else None
    if selected:
        mesh, index = selected
        points = [mesh['verts'][v] for v in mesh['tris'][index][:3]]
        for i in range(3):
            _queue_face_outline_edge(camera, points[i], points[(i+1)%3], C_VERTEX_FACE)


def _draw_floor_ray_hud(hud):
    if not cb_floor_ray.checked or _clean_capture:
        return
    y = 146 if cb_seams.checked else 54
    if not _floor_query or not _floor_query['valid']:
        hud.text((14,y), C_LINK, 'Floor query unavailable')
        return
    q = _floor_query
    text = ('Leaf glide' if q['gliding'] else 'Floor query')
    text += ' | BG %d / triangle %d | Y %.3f' % (*q['floor'], q['height']) if q['floor'] else ' | no accepted floor'
    hud.text((14,y), C_LINK, text)
    hud.text((14,y+18), C_LINK, 'Engine origin: %.2f, %.2f, %.2f | roof undersides skipped' % q['origin'])


def _queue_leaf_exit_projection(camera):
    global _exit_projection_key, _exit_projection, _exit_projection_error
    global _exit_render_key, _exit_render_positions, _exit_render_colors
    global _exit_geometry_revision
    if not cb_load.checked or not _snapshot:
        return
    q = _floor_query or {}
    filters = (q.get('flags',3), q.get('group_mask',1), q.get('link_pass',True), q.get('actor_pid'))
    key = (tuple((slot,id(mesh)) for slot,mesh in sorted(_snapshot['meshes'].items())), filters)
    if key != _exit_projection_key:
        previous_projection = _exit_projection
        try:
            candidates = ground_candidates(_snapshot['meshes'], *filters)
            _exit_projection = exit_projection_cells(candidates, cache=_exit_triangle_cache)
            _exit_projection_error = ''
        except Exception as exc:
            _exit_projection = []
            _exit_projection_error = str(exc)
            status.set('Leaf exit projection: ' + str(exc))
        if (len(previous_projection)!=len(_exit_projection) or
                any(a is not b for a,b in zip(previous_projection,_exit_projection))):
            _exit_geometry_revision += 1
        leaf_exit_help.set(('Projection unavailable: ' + _exit_projection_error)
                           if _exit_projection_error else LEAF_EXIT_HELP)
        _exit_projection_key = key
    origin_offset = q.get('origin_offset',60.0)
    query_y = q.get('origin', (0, (_link or camera.position)[1]+origin_offset, 0))[1]
    columns = cb_leaf_columns.checked
    extent = max(2000.0, radius_slider.value or 12000.0)
    # A stable display range avoids rebuilding open columns on every camera step.
    clip_top = (math.floor(max(camera.position[1],query_y)/extent)+2)*extent
    render_key = (_exit_geometry_revision,origin_offset,columns,clip_top if columns else query_y)
    if render_key != _exit_render_key:
        positions,colors = array('f'),array('I')
        def triangle(a,b,c):
            positions.extend(a); positions.extend(b); positions.extend(c)
            colors.extend((C_LOAD,C_LOAD,C_LOAD))
        if not columns:
            # The expensive exit/blocker partition is independent of Link's
            # movement. Only take a cheap cross-section when his query Y changes.
            y = query_y-origin_offset
            for poly in exit_slice_polygons(_exit_projection,query_y):
                points = [(p[0],y,p[1]) for p in poly]
                for i in range(1,len(points)-1):
                    triangle(points[0],points[i+1],points[i])
        else:
            for cell in _exit_projection:
                base,poly,ceiling = cell['base'],cell['polygon'],cell['ceiling']
                lower = tuple((p[0],_plane_height(base['plane'],p)-origin_offset,p[1]) for p in poly)
                upper = tuple((p[0],min(clip_top,_plane_height(ceiling,p)-origin_offset)
                               if ceiling is not None else clip_top,p[1]) for p in poly)
                if all(upper[i][1]<=lower[i][1]+0.001 for i in range(len(poly))):
                    continue
                for i in range(1,len(poly)-1):
                    triangle(lower[0],lower[i+1],lower[i])
                    if ceiling is not None:
                        triangle(upper[0],upper[i],upper[i+1])
                for i in range(len(poly)):
                    j=(i+1)%len(poly)
                    triangle(lower[i],lower[j],upper[j])
                    triangle(lower[i],upper[j],upper[i])
        _exit_render_positions,_exit_render_colors = positions,colors
        _exit_render_key = render_key
    # Always filled, independently of the other trigger-volume toggle. Retain
    # the world geometry and let the renderer handle camera projection/culling.
    _volume_positions.extend(_exit_render_positions)
    _volume_colors.extend(_exit_render_colors)


def _ensure_collider_move_watch(address=None):
    global _move_collider_watch_ready, _move_collider_watch_key
    addresses = set(COLLIDER_EDITS.offsets)
    if address is not None:
        addresses.add(address)
    key = tuple(sorted(addresses))
    if _move_collider_watch_ready and key == _move_collider_watch_key:
        return True
    if not _collider_watch_ready:
        status.set('Collider movement requires this Dolphin build\'s memory-watch support')
        return False
    try:
        # This fork evaluates conditions before the count store. Set has already
        # written mpObj[old_count], so filter here before entering Python.
        count = 'read_u32(0x%X)' % (CCS_BASE+0x280C)
        pointer = 'read_u32(0x%X+4*%s)' % (CCS_BASE+0x1400,count)
        matches = ' || '.join('%s == 0x%X' % (pointer,item) for item in key) or '0'
        condition = '%s < 1280 && (%s)' % (count,matches)
        debug.set_memory_watch({'At': CCS_BASE+0x280C, 'WatchOnRead': False,
                                'WatchOnWrite': True, 'Condition': condition})
        _move_collider_watch_ready = True
        _move_collider_watch_key = key
        return True
    except Exception as exc:
        status.set('Could not hook collider registration: ' + str(exc))
        return False


def _sync_moved_collider_shapes():
    if not COLLIDER_EDITS.offsets:
        return
    fresh = {}
    for address in list(COLLIDER_EDITS.offsets):
        try:
            COLLIDER_EDITS.apply_registration(address)
            _raw, shape, _identity = COLLIDER_EDITS.read(address)
            fresh[address] = shape
        except Exception:
            continue
    for shapes in (_push_colliders, _attack_colliders, _target_colliders):
        for i, shape in enumerate(shapes):
            address = COLLIDER_EDITS.addresses.get(shape)
            if address in fresh:
                shapes[i] = fresh[address]
    for address, shape in fresh.items():
        COLLIDER_EDITS.track(address, b'', shape)


def _selected_collider():
    address = COLLIDER_EDITS.selected
    if address is None:
        return None
    try:
        _raw, shape, identity = COLLIDER_EDITS.read(address)
        if COLLIDER_EDITS.selected_identity is not None and identity != COLLIDER_EDITS.selected_identity:
            raise ValueError('selected collider was replaced')
        return shape
    except Exception:
        COLLIDER_EDITS.selected = COLLIDER_EDITS.drag = None
        return None


def _remove_move_collider_watch():
    global _move_collider_watch_ready, _move_collider_watch_key
    if not _move_collider_watch_ready:
        return
    for remove in (getattr(memory, 'remove_memcheck', None), getattr(debug, 'remove_memory_breakpoint', None)):
        if remove is not None:
            try:
                remove(CCS_BASE+0x280C)
                _move_collider_watch_ready = False
                _move_collider_watch_key = None
                return
            except Exception:
                pass


def _apply_collider_flag_locks():
    global _applying_collider_flags
    if _applying_collider_flags:return
    _applying_collider_flags=True
    try:
        for address in list(COLLIDER_EDITS.flag_locks):COLLIDER_EDITS.apply_flag_locks(address)
    finally:_applying_collider_flags=False


def _sync_registration_watch():
    global _collider_registration_watch
    needed=(any(r.get('registration') for r in COLLIDER_EDITS.flag_locks.values()) or
            any(r.get('follow') for r in COLLIDER_EDITS.offsets.values()))
    if not needed:
        if _collider_registration_watch is not None:
            debug.remove_memory_breakpoint(_collider_registration_watch)
            _collider_registration_watch=None
        return
    if _collider_registration_watch is not None:return
    # GZLJ01 CalcArea: first lfs initializes the local AABB before either loop.
    # Reading mObjCount would be too late: MMU::Read captures its return value
    # before notifying a watch. This constant's captured value stays unchanged.
    if bytes(memory.read_bytes(0x80000000,6))!=b'GZLJ01' or memory.read_u32(0x80241F04)!=0xC002BEC8:
        raise ValueError('Registration hook requires the verified GZLJ01 CalcArea code')
    site=(registers.read_gpr(2)-0x4138)&0xFFFFFFFF
    if not _valid_ptr(site):raise ValueError('Invalid SDA2 address for registration hook')
    debug.set_memory_watch({'At':site,'WatchOnRead':True,'WatchOnWrite':False,
                            'Condition':'pc == 0x80241F04 && r31 == 0x%X'%CCS_BASE})
    _collider_registration_watch=site


def _register_locked_colliders():
    try:
        # Queue membership is checked on the CPU thread, not from cached labels.
        owners={};head=bytes(memory.read_bytes(ACTOR_QUEUE,12))
        node,count=_u32(head,0),_u32(head,8)
        if count>4096:raise ValueError('Invalid actor queue count')
        seen=set()
        for _ in range(count):
            if not _valid_ptr(node) or node in seen:raise ValueError('Invalid actor queue')
            seen.add(node);entry=bytes(memory.read_bytes(node,0x14))
            owner,node=_u32(entry,0xC),_u32(entry,8)
            if _valid_ptr(owner):owners[owner]=memory.read_u32(owner+4)
        for address,record in list(COLLIDER_EDITS.offsets.items()):
            following=record.get('follow')
            if following is None:continue
            owner,pid=record['identity'][:2]
            if owners.get(following['actor'])!=following['pid'] or (owner and owners.get(owner)!=pid):
                COLLIDER_EDITS.offsets.pop(address,None);continue
            COLLIDER_EDITS.apply_registration(address)
        COLLIDER_EDITS.apply_registrations(CCS_BASE,owners)
    except Exception as exc:
        COLLIDER_EDITS.last_error=str(exc)
        for address,record in list(COLLIDER_EDITS.flag_locks.items()):
            if record.get('registration'):COLLIDER_EDITS.flag_locks.pop(address,None)
        for address,record in list(COLLIDER_EDITS.offsets.items()):
            if record.get('follow'):COLLIDER_EDITS.offsets.pop(address,None)


def _sync_collider_flag_watches():
    wanted=COLLIDER_EDITS.flag_watch_addresses()
    for address in _collider_flag_watches-wanted:
        debug.remove_memory_breakpoint(address)
    for address in wanted-_collider_flag_watches:
        debug.set_memory_watch({'At':address,'WatchOnRead':True,'WatchOnWrite':False})
    _collider_flag_watches.clear();_collider_flag_watches.update(wanted)


def _update_collider_tools():
    global _state_load_overlay_snapshot
    if not COLLIDER_EDITS.offsets:
        _remove_move_collider_watch()
    else:
        _ensure_collider_move_watch()
    _apply_collider_flag_locks()
    try:
        _sync_collider_flag_watches()
        _sync_registration_watch()
    except Exception as exc:status.set('Collider flag watch: '+str(exc))
    for control in (collider_move_info, collider_restore, collider_restore_all):
        control.visible = cb_move_collider.checked
    if not cb_move_collider.checked:
        return
    if collider_restore_all.clicked:
        _state_load_overlay_snapshot = None
        COLLIDER_EDITS.clear(restore=True)
        _refresh_runtime_overlays(force=True,recover_registry=True)
    if collider_restore.clicked and COLLIDER_EDITS.selected is not None:
        _state_load_overlay_snapshot = None
        COLLIDER_EDITS.restore(COLLIDER_EDITS.selected)
        _refresh_runtime_overlays(force=True,recover_registry=True)
    shape = _selected_collider()
    if shape is None:
        collider_move_info.set('Click a visible push/attack/target collider. Drag the selected center or X/Y/Z axes.')
    else:
        center,_radius = _collider_center(shape)
        record = COLLIDER_EDITS.offsets.get(COLLIDER_EDITS.selected)
        offset = record['offset'] if record else (0,0,0)
        owner_text='owner %08X' % shape[1] if shape[1] else 'No actor owner'
        collider_move_info.set('%s @ %08X | %s\nCenter: %.2f, %.2f, %.2f\nOffset from source shape: %.2f, %.2f, %.2f' %
                               (shape[0],COLLIDER_EDITS.selected,owner_text,*center,*offset))


def _toggle_collider_lock(kind):
    global _state_load_overlay_snapshot
    if _selected_collider() is None:return
    if kind=='link':
        try:
            if not _collider_watch_ready:raise ValueError('Position lock requires memory-watch support')
            enabled=COLLIDER_EDITS.toggle_follow_player(COLLIDER_EDITS.selected,PLAYER_PTR)
            try:
                _sync_registration_watch()
                if enabled and not _ensure_collider_move_watch():raise ValueError('Could not hook collider movement')
            except Exception:
                if enabled:COLLIDER_EDITS.offsets[COLLIDER_EDITS.selected]['follow']=None
                raise
            status.set('Link lock %s; preserves relative position and capsule rotation.' % ('enabled' if enabled else 'disabled'))
        except Exception as exc:status.set('Collider position lock: '+str(exc))
    else:
        try:
            if not _collider_watch_ready:raise ValueError('Flag locks require memory-watch support')
            enabled=COLLIDER_EDITS.toggle_flag_lock(COLLIDER_EDITS.selected,kind)
            try:_sync_registration_watch()
            except Exception:
                if enabled and kind=='registration':COLLIDER_EDITS.toggle_flag_lock(COLLIDER_EDITS.selected,kind)
                raise
            _sync_collider_flag_watches()
            status.set('Collider %s lock %s' % (kind,'enabled' if enabled else 'disabled'))
        except Exception as exc:status.set('Collider flag lock: '+str(exc))
    _state_load_overlay_snapshot=None


def _pick_runtime_collider(camera,click):
    ray=_screen_ray(camera,click);best=None
    for control,shapes in ((cb_push,_push_colliders),(cb_attack,_attack_colliders),(cb_target,_target_colliders)):
        if not control.checked:continue
        for shape in shapes:
            address=COLLIDER_EDITS.addresses.get(shape)
            if address is None:continue
            distance=_ray_collider_hit(camera.position,ray,shape)
            if distance is not None and (best is None or distance<best[0]):best=(distance,address)
    return best[1] if best else None


def _select_runtime_collider(address):
    if address is None:
        COLLIDER_EDITS.selected=COLLIDER_EDITS.selected_identity=COLLIDER_EDITS.drag=None
    else:
        _raw,_shape,identity=COLLIDER_EDITS.read(address)
        COLLIDER_EDITS.selected=address;COLLIDER_EDITS.selected_identity=identity;COLLIDER_EDITS.drag=None






def _move_collider_hud_layout():
    if COLLIDER_EDITS.selected is not None:
        top=min(150.0,max(10.0,H-322.0))
        return (10.0,top,430.0,top+312.0)
    return (10.0,150.0,420.0,216.0)


def _selected_collider_hud_info():
    shape=_selected_collider()
    if shape is None:
        return None
    record=COLLIDER_EDITS.offsets.get(COLLIDER_EDITS.selected,{})
    flags=COLLIDER_EDITS.flag_locks.get(COLLIDER_EDITS.selected,{})
    return (shape[0],COLLIDER_EDITS.selected,shape[1],_collider_center(shape)[0],
            record.get('offset',(0,0,0)),record.get('frozen') is not None,
            bool(flags.get('state')),bool(flags.get('hit')),bool(flags.get('registration')),bool(record.get('follow')))


def _collider_lock_button_rects():
    x0,y0,x1,_y1=_move_collider_hud_layout()
    mid=(x0+x1)*0.5
    return tuple((kind,(left,y0+240+row*34,right,y0+268+row*34))
                 for row,pair in enumerate((('state','hit'),('registration','link')))
                 for kind,left,right in ((pair[0],x0+10,mid-3),(pair[1],mid+3,x1-10)))


def _draw_move_collider_hud(hud,camera,info):
    if not cb_move_collider.checked or _clean_capture:
        return
    x0,y0,x1,y1=_move_collider_hud_layout()
    outline,panel_bg,text,button=0xFF3C9CFF,0xE610315D,0xFFF2F7FF,0xFF1B568D
    hud.rect_filled((x0,y0),(x1,y1),panel_bg,2).rect((x0,y0),(x1,y1),outline,2,2)
    if info is None:
        hud.text((x0+12,y0+12),text,'Collider Selector')
        hud.text((x0+12,y0+34),text,'Click a visible push / attack / target shape')
        return
    kind,address,owner,center,offset,frozen,state_lock,hit_lock,registration_lock,link_lock=info
    hud.text((x0+12,y0+10),text,'Collider Selector: '+kind)
    owner_text='Owner: %08X' % owner if owner else 'No actor owner'
    hud.text((x0+12,y0+34),text,'Address: %08X   %s' % (address,owner_text))
    hud.text((x0+12,y0+58),text,'Center: X=%.3f  Y=%.3f  Z=%.3f' % center)
    hud.text((x0+12,y0+80),text,'Offset: X=%.3f  Y=%.3f  Z=%.3f' % offset)
    titles=('Unfreeze Collider' if frozen else 'Freeze Collider',
            'Teleport Collider to Link','Teleport Link to Collider','Copy Shape XYZ Addresses')
    for rect,title in zip(_move_actor_button_rects(_move_collider_hud_layout()),titles):
        hud.rect_filled((rect[0],rect[1]),(rect[2],rect[3]),button,2)
        hud.text((rect[0]+80,rect[1]+5),text,title)
    for (_key,rect),label,enabled in zip(_collider_lock_button_rects(),
            ('State lock','Hit lock','Registration lock','Link pos/rot' if kind=='capsule' else 'Link position'),
            (state_lock,hit_lock,registration_lock,link_lock)):
        hud.rect_filled((rect[0],rect[1]),(rect[2],rect[3]),0xFF245C45 if enabled else button,2)
        hud.text((rect[0]+8,rect[1]+5),text,'%s: %s'%(label,'ON' if enabled else 'off'))
    projected=camera.project(center)
    if projected is not None:
        hud.circle(projected[0],MOVE_ACTOR_CENTER_RADIUS,C_MOVE_CENTER,2.5)


def _move_selected_collider_to(destination):
    global _state_load_overlay_snapshot
    shape=_selected_collider()
    if shape is None or not _ensure_collider_move_watch(COLLIDER_EDITS.selected):
        return False
    _state_load_overlay_snapshot=None
    COLLIDER_EDITS.translate(COLLIDER_EDITS.selected,_sub(destination,_collider_center(shape)[0]))
    _sync_moved_collider_shapes()
    return True


def _handle_move_collider(camera,click):
    global _state_load_overlay_snapshot
    if not cb_move_collider.checked or _clean_capture:
        return False
    shape = _selected_collider()
    center = _collider_center(shape)[0] if shape else None
    if click:
        x0,y0,x1,y1=_move_collider_hud_layout()
        if x0<=click[0]<=x1 and y0<=click[1]<=y1:
            COLLIDER_EDITS.drag=None
            if shape is not None:
                for kind,rect in _collider_lock_button_rects():
                    if rect[0]<=click[0]<=rect[2] and rect[1]<=click[1]<=rect[3]:
                        _toggle_collider_lock(kind)
                        return True
                for index,rect in enumerate(_move_actor_button_rects((x0,y0,x1,y1))):
                    if not (rect[0]<=click[0]<=rect[2] and rect[1]<=click[1]<=rect[3]):
                        continue
                    try:
                        if index==0 and _ensure_collider_move_watch(COLLIDER_EDITS.selected):
                            _state_load_overlay_snapshot=None
                            COLLIDER_EDITS.toggle_freeze(COLLIDER_EDITS.selected)
                        elif index==1 and _link is not None:
                            _move_selected_collider_to(tuple(_link))
                        elif index==2:
                            _teleport_point(center)
                        elif index==3:
                            addresses=COLLIDER_EDITS.watch_addresses(COLLIDER_EDITS.selected)
                            gui.set_clipboard('\n'.join('0x%08X' % address for address in addresses))
                            status.set('Copied shape XYZ: sphere center / cylinder base / capsule endpoint / triangle vertex / box minimum')
                    except Exception as exc:
                        status.set('Collider Selector: '+str(exc))
                    break
            return True
        if center is not None:
            projected = camera.project(center)
            if projected and math.dist(click,projected[0]) <= MOVE_ACTOR_CENTER_RADIUS:
                depth=max(camera.near,_dot(_sub(center,camera.position),camera.forward))
                hit=_camera_depth_hit(camera,click,depth)
                if hit:
                    COLLIDER_EDITS.drag=dict(kind='plane',depth=depth,offset=_sub(center,hit),origin=center)
                return True
            for vector in ((1,0,0),(0,1,0),(0,0,1)):
                geometry=_move_actor_axis_geometry(camera,center,vector)
                if geometry:
                    a,b,_world=geometry
                    t,distance=_edge_point(click,a,b)
                    if t>0.18 and distance <= MOVE_ACTOR_AXIS_PICK_RADIUS**2:
                        parameter=_ray_axis_parameter(camera,click,center,vector)
                        if parameter is not None:
                            COLLIDER_EDITS.drag=dict(kind='axis',origin=center,vector=vector,parameter=parameter)
                        return True
        _select_runtime_collider(_pick_runtime_collider(camera,click))
        return True
    drag=COLLIDER_EDITS.drag
    if not drag or center is None: return False
    x,y,inside=canvas.mouse_pos()
    if not canvas.left_down() or not inside:
        COLLIDER_EDITS.drag=None
        return True
    if drag['kind']=='plane':
        hit=_camera_depth_hit(camera,(x,y),drag['depth'])
        destination=tuple(hit[i]+drag['offset'][i] for i in range(3)) if hit else None
    else:
        parameter=_ray_axis_parameter(camera,(x,y),drag['origin'],drag['vector'])
        destination=_add_scaled(drag['origin'],drag['vector'],parameter-drag['parameter']) if parameter is not None else None
    if destination:
        try:
            _move_selected_collider_to(destination)
        except Exception as exc:
            status.set('Collider Selector: ' + str(exc))
            COLLIDER_EDITS.drag=None
    return True


def _draw_move_collider_gizmo(camera):
    if not cb_move_collider.checked or _clean_capture:
        return
    shape=_selected_collider()
    if shape is None: return
    center,_extent=_collider_center(shape)
    radius=_screen_marker_radius(camera,center,7,4,80)
    _queue_marker(center,radius,C_MOVE_CENTER)
    for vector,color in (((1,0,0),C_MOVE_X),((0,1,0),C_MOVE_Y),((0,0,1),C_MOVE_Z)):
        geometry=_move_actor_axis_geometry(camera,center,vector)
        if geometry:
            _queue_line(center,geometry[2],color)
            _queue_marker(geometry[2],radius,color)


def _handle_edit_tools(camera,click):
    global _triangle_selection, _triangle_ui_key
    if click and not _clean_capture and 8<=click[0]<=42 and 8<=click[1]<=42:
        return _pick_seam(camera,click)
    if cb_move_collider.checked:
        return _handle_move_collider(camera,click)
    if cb_floor_inspector.checked:
        if click and not _clean_capture:
            face=_pick_collision_face(camera,click)
            if face:
                _triangle_selection=face
                _triangle_ui_key=None
                return True
        return False
    move_changed,consumed=_handle_move_actor(camera,click)
    if not consumed and not cb_move_actor.checked:
        vertex_changed,consumed=_handle_vertex_select(camera,click)
        if not consumed:
            move_changed=_pick_seam(camera,click) or move_changed
        move_changed=vertex_changed or move_changed
    return move_changed


try:
    if hasattr(event, "on_memorywatch") and hasattr(debug, "set_memory_watch"):
        event.on_memorywatch(on_memorywatch)
        debug.set_memory_watch({
            "At": COLLIDER_SNAPSHOT_WATCH,
            "WatchOnRead": False,
            "WatchOnWrite": True,
        })
        _collider_watch_ready = True
        atexit.register(_remove_collider_watch)
except Exception as exc:
    _runtime_diag["watch_error"] = str(exc)


_draw_scene()
