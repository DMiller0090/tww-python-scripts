"""Decode The Wind Waker's runtime dCcD collider records."""

from __future__ import annotations

import math
import struct


ATTACK_TYPE_FLAGS = (
    (0x00000002, "SWORD"),
    (0x00000008, "IMPACT"),
    (0x00000020, "BOMB"),
    (0x00000040, "BOOMERANG"),
    (0x00000080, "BOKO_STICK"),
    (0x00000100, "WATER"),
    (0x00000200, "FIRE"),
    (0x00000400, "MACHETE"),
    (0x00000800, "ENERGY"),
    (0x00001000, "SPIKE"),
    (0x00002000, "SPIN"),
    (0x00004000, "NORMAL_ARROW"),
    (0x00008000, "HOOKSHOT"),
    (0x00010000, "SKULL_HAMMER"),
    (0x00020000, "FLAME"),
    (0x00040000, "FIRE_ARROW"),
    (0x00080000, "ICE_ARROW"),
    (0x00100000, "LIGHT_ARROW"),
    (0x00200000, "WIND"),
    (0x00400000, "WIND_GUST"),
    (0x00800000, "LIGHT"),
    (0x01000000, "STALFOS_MACE"),
    (0x02000000, "FAN_SWING"),
    (0x04000000, "DARKNUT_SWORD"),
    (0x08000000, "GRAPPLING_HOOK"),
    (0x10000000, "MOBLIN_SPEAR"),
    (0x20000000, "PGANON_SWORD"),
)
ATTACK_TYPE_MASK = 0
for _flag, _name in ATTACK_TYPE_FLAGS:
    ATTACK_TYPE_MASK |= _flag

SHAPE_VTABLES = {
    "sphere": (0x8037D070, 0x8037D068, 0x80388788, 0x80388780),
    "capsule": (0x8037D104, 0x8037D0FC, 0x80388848, 0x80388840),
    "cylinder": (0x8037E5B0, 0x8037E5A8, 0x803887E8, 0x803887E0),
    "triangle": (0x803888A8, 0x803888A0),
}
SHAPE_VTABLE_TO_KIND = {
    vtable: kind for kind, vtables in SHAPE_VTABLES.items() for vtable in vtables
}
SHAPE_CROSS_AT_TG_CPS = {
    0x8023FEC4: "sphere",
    0x8023FB8C: "cylinder",
    0x8023F6F0: "capsule",
    0x8023F428: "triangle",
}


def valid_ptr(value):
    return 0x80000000 <= value < 0x81800000


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def vec3(data, offset):
    return struct.unpack_from(">3f", data, offset)


def finite_vec(value):
    return all(math.isfinite(component) and abs(component) < 1000000.0 for component in value)


def attack_info(data):
    try:
        return u32(data, 0x10), data[0x14]
    except (struct.error, IndexError):
        return None


def attack_type_names(damage_type):
    names = [name for flag, name in ATTACK_TYPE_FLAGS if damage_type & flag]
    leftover = damage_type & ~ATTACK_TYPE_MASK & 0xFFFFFFFF
    if leftover:
        names.append("0x%X" % leftover)
    return names


def target_type_name(target_type):
    set_names = [name for flag, name in ATTACK_TYPE_FLAGS if target_type & flag]
    clear_names = [name for flag, name in ATTACK_TYPE_FLAGS if not target_type & flag]
    if len(set_names) <= len(clear_names):
        leftover = target_type & ~ATTACK_TYPE_MASK & 0xFFFFFFFF
        if leftover:
            set_names.append("0x%X" % leftover)
        return "/".join(set_names) if set_names else "NONE"
    return " ".join(["ALL"] + ["~" + name for name in clear_names])


class ColliderDecoder:
    def __init__(self, reader, read_u32):
        self._reader = reader
        self._read_u32 = read_u32
        self._shape_cache = {}

    def owner(self, data):
        status_ptr = u32(data, 0x44)
        if not valid_ptr(status_ptr):
            return 0
        try:
            actor = self._read_u32(status_ptr + 0x0C) & 0xFFFFFFFF
            return actor if valid_ptr(actor) else 0
        except Exception:
            return 0

    def shape_kind(self, data):
        shape_vptr = u32(data, 0x114)
        kind = SHAPE_VTABLE_TO_KIND.get(shape_vptr)
        if kind is not None:
            return kind
        if shape_vptr in self._shape_cache:
            return self._shape_cache[shape_vptr]
        if not valid_ptr(shape_vptr):
            return None
        try:
            table = self._reader.read_bytes(shape_vptr, 0x18)
            for offset in (0x14, 0x0C):
                kind = SHAPE_CROSS_AT_TG_CPS.get(u32(table, offset))
                if kind is not None:
                    self._shape_cache[shape_vptr] = kind
                    return kind
        except Exception:
            pass
        return None

    def decode(self, data):
        kind = self.shape_kind(data) or SHAPE_VTABLE_TO_KIND.get(u32(data, 0))
        owner = self.owner(data)
        if kind is None:
            minimum, maximum = vec3(data, 0x0F8), vec3(data, 0x104)
            half = tuple((maximum[index] - minimum[index]) * 0.5 for index in range(3))
            center = tuple((maximum[index] + minimum[index]) * 0.5 for index in range(3))
            if (finite_vec(minimum) and finite_vec(maximum) and finite_vec(center) and
                    all(0.001 <= value <= 100000.0 for value in half)):
                return ("box", owner, center, half)
            return None
        if kind == "cylinder":
            center = vec3(data, 0x118)
            radius, height = struct.unpack_from(">2f", data, 0x124)
            if finite_vec(center) and 0.01 <= radius <= 100000.0 and 0.01 <= height <= 100000.0:
                return ("cylinder", owner, center, radius, height)
        elif kind == "sphere":
            center = vec3(data, 0x118)
            radius = struct.unpack_from(">f", data, 0x124)[0]
            if finite_vec(center) and 0.01 <= radius <= 100000.0:
                return ("sphere", owner, center, radius)
        elif kind == "capsule":
            start, end = vec3(data, 0x118), vec3(data, 0x124)
            radius = struct.unpack_from(">f", data, 0x134)[0]
            if finite_vec(start) and finite_vec(end) and 0.01 <= radius <= 100000.0:
                return ("capsule", owner, start, end, radius)
        elif kind == "triangle":
            points = (vec3(data, 0x12C), vec3(data, 0x138), vec3(data, 0x144))
            edge1 = tuple(points[1][i] - points[0][i] for i in range(3))
            edge2 = tuple(points[2][i] - points[0][i] for i in range(3))
            normal = (edge1[1] * edge2[2] - edge1[2] * edge2[1],
                      edge1[2] * edge2[0] - edge1[0] * edge2[2],
                      edge1[0] * edge2[1] - edge1[1] * edge2[0])
            if all(finite_vec(point) for point in points) and sum(v * v for v in normal) > 0.0001:
                return ("triangle", owner, points[0], points[1], points[2])
        return None
