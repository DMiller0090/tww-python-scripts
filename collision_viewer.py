"""GPU collision viewer for The Wind Waker JP (GZLJ01).
"""
from __future__ import annotations

import atexit
import csv
import json
import math
import os
import struct
from array import array

from dolphin import debug, event, gui, memory

from ww.actor import proc_name
from ww.addresses.address import Address
from ww.collider import (ColliderDecoder, attack_info as _collider_attack_info,
                         attack_type_names as _attack_type_names, finite_vec as _finite_vec,
                         target_type_name as _target_type_names, u16 as _u16, u32 as _u32,
                         valid_ptr as _valid_ptr, vec3 as _vec3)
from ww.collision_geo import read_collision
from ww.cull import read_camera
from ww.viewer_math import (ViewerCamera, add_scaled as _add_scaled,
                            angles_from_forward as _angles_from_forward, cross as _cross,
                            dot as _dot, forward_from_angles as _forward_from_angles,
                            normalize as _norm, screen_ray as _screen_ray, sub as _sub)


class DolphinReader:
    def read_bytes(self, address, size):
        return bytes(memory.read_bytes(address, size))


RD = DolphinReader()
COLLIDER_DECODER = ColliderDecoder(RD, memory.read_u32)


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

W, H = 900, 590
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

panel = gui.window(WINDOW_TITLE)
panel.enable_hardware_canvas()
canvas = panel.canvas(W, H)
cb_enabled = panel.checkbox("Collision mesh", True, group="Collision and View")
cb_movebg = panel.checkbox("Movable BG", True, group="Collision and View")
cb_follow_cam = panel.checkbox("Follow Cam", True, group="Collision and View")
cb_ground = panel.checkbox("Ground", True, group="Collision and View")
cb_slope = panel.checkbox("Slopes", True, group="Collision and View")
cb_wall = panel.checkbox("Walls", True, group="Collision and View")
cb_roof = panel.checkbox("Roofs", False, group="Collision and View")
cb_filled = panel.checkbox("Filled", True, group="Collision and View")
cb_wire = panel.checkbox("Wireframe", True, group="Collision and View")
cb_xray = panel.checkbox("X-ray", False, group="Collision and View")
cb_view_cull = panel.checkbox("Cull outside view", True, group="Collision and View")
move_speed_slider = panel.slider_float("Freecam speed", 10.0, 500.0, group="Collision and View")
move_speed_slider.value = 50.0
wire_opacity_slider = panel.slider_float("Wireframe opacity", 0.0, 1.0, group="Collision and View")
wire_opacity_slider.value = 1.0
opacity_slider = panel.slider_float("Collision opacity", 0.0, 1.0, group="Collision and View")
opacity_slider.value = 0.54
radius_slider = panel.slider_float("Draw radius", 0.0, 12000.0, group="Collision and View")
reset_button = panel.button("Reset view / refresh static cache", group="Tools")
cb_vertex_select = panel.checkbox("Vertex Select", False, group="Tools")
cb_filled_triggers = panel.checkbox("Filled triggers", True, group="Triggers")
trigger_opacity_slider = panel.slider_float("Trigger opacity", 0.0, 1.0, group="Triggers")
trigger_opacity_slider.value = 0.54
cb_on_top_triggers = panel.checkbox("On Top Triggers", False, group="Triggers")
cb_load = panel.checkbox("Load zones", True, group="Triggers")
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
cb_target_actor = panel.checkbox("Target Names", False, group="Colliders")
cb_contact_actor = panel.checkbox("Push Names", False, group="Colliders")
cb_coordinate_dot = panel.checkbox("Coordinate Dot", True, group="Colliders")
cb_actor_names = panel.checkbox("Actor Names", False, group="Colliders")
cb_move_actor = panel.checkbox("Move Actor", False, group="Actor Tools")
cb_zone_info = panel.checkbox("Zone / room info", False, group="Actor Tools")
cb_seams = panel.checkbox("Show Seam Clips", False, group="Seam Clips")
seam_initial_button = panel.button("Teleport seam initial", group="Seam Clips")
seam_clip_button = panel.button("Teleport seam clip", group="Seam Clips")
status = panel.text("Waiting for TWW collision...", group="Tools")
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
_diag = False

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
_runtime_diag_last = None
_status_summary_last = None

_seams = {"stage": None, "room": None, "clips": [], "selected": None}
_selected_face = None
_selected_point = None


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
        collider = COLLIDER_DECODER.decode(data)
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
    if not owner:
        return
    try:
        procedure = memory.read_u16(owner + ACTOR_PROC)
    except Exception:
        return
    position = _collider_actor_label_anchor(collider)
    if not _finite_vec(position):
        return
    labels.append((position, proc_name(procedure, "Proc 0x%03X" % procedure)))


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
        sinks["push"].append((labels["contact_actor"], _append_actor_label))
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
                collider = COLLIDER_DECODER.decode(RD.read_bytes(collider_address, 0x150))
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
    rooms, zones = {}, {}
    try:
        room_data = RD.read_bytes(ROOM_STATUS_BASE, ROOM_STATUS_STRIDE * ROOM_STATUS_COUNT)
        for room_no in range(ROOM_STATUS_COUNT):
            zone_no = struct.unpack_from(">b", room_data,
                                         room_no * ROOM_STATUS_STRIDE + ROOM_STATUS_ZONE_NO_OFFSET)[0]
            if zone_no >= 0:
                rooms[room_no] = zone_no
        zone_data = RD.read_bytes(ZONE_ARRAY_BASE, ZONE_STRIDE * ZONE_COUNT)
        for zone_no in range(ZONE_COUNT):
            room_no = struct.unpack_from(">b", zone_data, zone_no * ZONE_STRIDE)[0]
            if room_no >= 0:
                base = zone_no * ZONE_STRIDE
                switches = struct.unpack_from(">3H", zone_data, base + 2)
                items = struct.unpack_from(">H", zone_data, base + 8)[0]
                zones[zone_no] = (room_no, switches, items)
    except Exception:
        return {}, {}
    return rooms, zones


def _actor_room_no(header, procedure, actor):
    try:
        if procedure == 0x0126:  # TBOX retains its authored room at +0x290.
            room = struct.unpack(">i", RD.read_bytes(actor + 0x290, 4))[0]
        else:
            room = struct.unpack_from(">b", header, ACTOR_CURRENT + 0x12)[0]
        return room if room >= 0 else None
    except Exception:
        return None


def _refresh_actor_records(camera):
    """Scan actor queue once for labels, manipulation, and room/zone data."""
    global _actor_positions, _actor_records, _actor_labels, _room_zones, _zones
    labels, positions, records = [], {}, {}
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
            header = RD.read_bytes(actor, ACTOR_CURRENT + 12)
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
        if (cb_actor_names.checked and len(labels) < MAX_ACTOR_LABELS and
                _overlay_visible(camera, position, 160.0)):
            label = proc_name(procedure, "Proc 0x%03X" % procedure)
            if cb_zone_info.checked:
                label += " [R%s Z%s]" % (
                    "-" if room_no is None else room_no,
                    "-" if zone_no is None else zone_no)
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
        collider = COLLIDER_DECODER.decode(collider_data)
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
            collider = COLLIDER_DECODER.decode(collider_data)
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
            collider = COLLIDER_DECODER.decode(collider_data)
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
            collider = COLLIDER_DECODER.decode(data)
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
            collider = COLLIDER_DECODER.decode(RD.read_bytes(actor + offset, 0x150))
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


def _point_in_triangle(point, a, b, c):
    px, py = point
    ax, ay = a; bx, by = b; cx, cy = c
    denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denominator) < 0.000001:
        return None
    u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
    v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
    w = 1.0 - u - v
    if u < -0.001 or v < -0.001 or w < -0.001:
        return None
    return u, v, w


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
    for mesh in _snapshot["meshes"].values():
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
            if distance <= camera.near or distance > nearest_distance + 0.01:
                continue
            # Prefer static collision on an effectively identical surface.
            if (abs(distance - nearest_distance) <= 0.01 and nearest is not None and
                    not mesh["is_movebg"] and nearest["move"]):
                continue
            projected = (camera.project(a), camera.project(b), camera.project(c))
            if any(point is None for point in projected):
                continue
            nearest_distance = distance
            nearest = {
                "screen": tuple(point[0] for point in projected),
                "world": (a, b, c),
                "class": surface_class,
                "move": mesh["is_movebg"],
            }
    return nearest


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
    """Draw each DZB exit as an extruded boundary, not extruding the tri edges."""
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
    if cb_wire.checked:
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
             cb_enemy_hp.checked, cb_actor_names.checked, cb_coordinate_dot.checked)
    if state == _hardware_state_key:
        return
    canvas.hardware_state(camera.position, camera.right, camera.up, camera.forward, _active_focal(),
                          radius_slider.value, opacity_slider.value, wire_opacity_slider.value,
                          cb_filled.checked, cb_wire.checked, True, cb_xray.checked,
                          cb_on_top_triggers.checked, _viewer_fullscreen, _clean_capture,
                          hud_visible=(not _clean_capture or cb_enemy_hp.checked or
                                       cb_actor_names.checked or cb_coordinate_dot.checked),
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


def _draw_zone_info_hud(hud):
    if not cb_zone_info.checked or _snapshot is None:
        return
    room = _read_room()
    zone = _room_zones.get(room)
    hud.rect_filled((10, 150), (420, 212), 0xDB10315D, 2).rect((10, 150), (420, 212), 0xFF3C9CFF, 2, 2)
    hud.text((22, 160), 0xFFF2F7FF, "Room / Zone")
    hud.text((22, 182), 0xFFF2F7FF, "Room: %s  Zone: %s" % (room, "-" if zone is None else zone))
    details = _zones.get(zone)
    if details is None:
        hud.text((22, 200), 0xFFF2F7FF, "Loaded zone slots: %d" % len(_zones))
    else:
        _zone_room, switches, items = details
        hud.text((22, 200), 0xFFF2F7FF, "Switch: %04X %04X %04X  Item: %04X" %
                 (switches[0], switches[1], switches[2], items))


def _draw_move_actor_hud(hud):
    if not cb_move_actor.checked or _clean_capture:
        return
    x0, y0, x1, y1 = _move_actor_hud_layout()
    outline, panel_bg, text, button = 0xFF3C9CFF, 0xE610315D, 0xFFF2F7FF, 0xFF1B568D
    hud.rect_filled((x0, y0), (x1, y1), panel_bg, 2).rect((x0, y0), (x1, y1), outline, 2, 2)
    info = _selected_actor_hud_info()
    if info is None:
        hud.text((x0 + 12, y0 + 12), text, "Move Actor")
        hud.text((x0 + 12, y0 + 34), text, "Click a dynamic actor or collider")
        return
    procedure, position, rotation = info
    label = proc_name(procedure, "Proc 0x%03X" % procedure)
    hud.text((x0 + 12, y0 + 10), text, "Move Actor: %s" % label)
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
    key = (W, H, _clean_capture, cb_seams.checked, cb_vertex_select.checked, len(_seams["clips"]),
           cb_enemy_hp.checked, tuple(_enemy_health), cb_actor_names.checked, tuple(_actor_labels),
           cb_attack_info.checked, tuple(_attack_labels),
           cb_attack_actor.checked, tuple(_attack_actor_labels),
           cb_target_info.checked, tuple(_target_info_labels),
           cb_target_actor.checked, tuple(_target_actor_labels),
           cb_contact_actor.checked, tuple(_contact_actor_labels),
           cb_zone_info.checked, tuple(sorted(_room_zones.items())), tuple(sorted(_zones.items())),
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
    _draw_enemy_hp_hud(hud, camera)
    _draw_actor_names_hud(hud, camera)
    _draw_attack_info_hud(hud, camera)
    _draw_attack_actor_hud(hud, camera)
    _draw_target_info_hud(hud, camera)
    _draw_target_actor_hud(hud, camera)
    _draw_contact_actor_hud(hud, camera)
    _draw_coordinate_dot_hud(hud, camera)
    if not cb_move_actor.checked:
        _draw_zone_info_hud(hud)
    _draw_move_actor_hud(hud)
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
                bary = _point_in_triangle(click, record["screen"][0], record["screen"][1], record["screen"][2])
                if bary is not None:
                    world = record["world"]
                    _set_selected_point(tuple(
                        sum(bary[index] * world[index][axis] for index in range(3))
                        for axis in range(3)), record)
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
            screen, world = record["screen"], record["world"]
            best = min(((_edge_point((mouse_x, mouse_y), screen[first], screen[second]), first, second)
                        for first, second in ((0, 1), (1, 2), (2, 0))),
                       key=lambda item: item[0][1])
            (t, _distance2), first, second = best
            a, b = world[first], world[second]
            _set_selected_point(tuple(a[axis] + (b[axis] - a[axis]) * t for axis in range(3)), record)
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
    return (10.0, 150.0, 420.0, 216.0) if _move_actor_selected is None else (10.0, 150.0, 430.0, 394.0)


def _move_actor_button_rects():
    x0, y0, x1, _y1 = _move_actor_hud_layout()
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
            


def _draw_scene():
    global _line_positions, _line_colors, _marker_positions, _marker_colors
    global _volume_positions, _volume_colors
    if save_settings_button.clicked:
        _save_settings()
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
    _queue_seams(camera)
    _draw_move_actor_gizmo(camera)
    _commit_overlays()
    _update_overlay_text(camera)


def _refresh_live(force=False, recover_registry=False):
    global _cache, _snapshot, _link, _link_actor, _selected_face
    _snapshot = read_collision(RD, cache=_cache)
    _cache = _snapshot
    _link = _read_link()
    try:
        _link_actor = memory.read_u32(PLAYER_PTR) & 0xFFFFFFFF
    except Exception:
        _link_actor = 0
    _refresh_game_camera()
    _refresh_runtime_overlays(force, recover_registry)
    stage, room = _snapshot["stage"], _read_room()
    if force or (stage, room) != (_seams["stage"], _seams["room"]):
        _seams.update(stage=stage, room=room, clips=_load_seams(stage, room), selected=None)
        _selected_face = None
        _set_selected_point(None)


def _update_status():
    global _runtime_diag_last, _status_summary_last
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
    diagnostic = ("tags %(tagged)d/%(actor_queue)d Co %(push_slots)d/%(push_enabled)d/%(push_decoded)d "
                  "At %(attack_slots)d/%(attack_enabled)d/%(attack_decoded)d" % _runtime_diag)
    if diagnostic != _runtime_diag_last:
        print("[TWW Collision Viewer] " + diagnostic)
        _runtime_diag_last = diagnostic


def _report_error(prefix, exc):
    global _last_error, _diag
    _last_error = prefix + str(exc)
    status.set(_last_error)
    if not _diag:
        import traceback
        print("[TWW Collision Viewer] " + traceback.format_exc())
        _diag = True


def _update(force=False, recover_registry=False):
    try:
        changed = _apply_input()
        _refresh_live(force=force, recover_registry=recover_registry)
        camera = _viewer_camera()
        click = canvas.take_click()
        move_changed, move_consumed = _handle_move_actor(camera, click)
        if not move_consumed and not cb_move_actor.checked:
            vertex_changed, click_consumed = _handle_vertex_select(camera, click)
            if not click_consumed:
                changed = _pick_seam(camera, click) or changed
            changed = vertex_changed or changed
        changed = move_changed or changed
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
    if (is_write and address == COLLIDER_SNAPSHOT_WATCH and value == 0 and
            (cb_attack.checked or cb_target.checked or cb_push.checked)):
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
            move_changed, move_consumed = _handle_move_actor(camera, click)
            if not move_consumed and not cb_move_actor.checked:
                vertex_changed, click_consumed = _handle_vertex_select(camera, click)
                if not click_consumed:
                    changed = _pick_seam(camera, click) or changed
                changed = vertex_changed or changed
            changed = move_changed or changed
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
    global _cache, _snapshot, _hardware_mesh_key, _hardware_dynamic_key, _hardware_state_key
    global _hardware_line_key, _hardware_on_top_line_key, _hardware_marker_key, _hardware_volume_key
    global _hardware_overlay_key, _stage_visible
    global _triggers, _push_colliders, _attack_colliders, _target_colliders, _player_attack_colliders
    global _enemy_health, _actor_labels, _attack_labels, _attack_actor_labels
    global _target_info_labels, _target_actor_labels, _contact_actor_labels
    global _actor_positions, _actor_records, _room_zones, _zones
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
            remove(COLLIDER_SNAPSHOT_WATCH)
            return
        except Exception:
            continue


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
