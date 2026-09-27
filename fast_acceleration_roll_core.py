"""Pure mechanics used by the live Fast Acceleration Roll Dolphin script."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from tww_sim.core.mathlib import cM_scos_s16, f32, main_stick_decode


ATN_FORWARD_COS = f32(0.99)
ATN_SIDE_ACCEL = f32(5.0)
MOVE_ACCEL = f32(3.5)
ROLL_MULTIPLIER = f32(1.5)
ROLL_ADD = f32(0.5)
ROLL_CAP = f32(26.0)
CUT_END_FRAMES = {65: f32(16.0), 66: f32(17.0)}


def s16(value: int) -> int:
    value = int(value) & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def cut_exits_next_update(proc: int, animation: float, rate: float) -> bool:
    end = CUT_END_FRAMES.get(int(proc))
    return bool(end is not None and rate > 0.0
                and f32(f32(animation) + f32(rate)) > end)


@dataclass(frozen=True)
class GateChoice:
    stick_x: int
    stick_y: int
    target: int
    offset: int
    cosine: float
    potential_roll_speed: float


@lru_cache(maxsize=1)
def _full_sticks() -> tuple[tuple[int, int, int], ...]:
    """All clean, full-magnitude GC inputs and their decoded stick angles."""
    rows = []
    for x in range(1, 255):
        for y in range(1, 255):
            angle, magnitude = main_stick_decode(x, y)
            if angle is not None and magnitude == 1.0:
                rows.append((x, y, angle))
    return tuple(rows)


@lru_cache(maxsize=4096)
def choose_gate(facing: int, camera: int) -> GateChoice:
    """Best deliverable input that selects SIDE while remaining closest to forward.

    Link's world target is ``stick_angle + 0x8000 + camera``.  The first target
    frame must be just outside the ATN forward cosine bucket so the following
    frame receives the 5-unit SIDE acceleration.
    """
    facing &= 0xFFFF
    camera &= 0xFFFF
    best = None
    for x, y, angle in _full_sticks():
        target = (angle + 0x8000 + camera) & 0xFFFF
        offset = s16(target - facing)
        if abs(offset) >= 0x2000:
            continue
        cosine = cM_scos_s16(offset)
        if cosine >= ATN_FORWARD_COS:
            continue
        normal = f32(ATN_SIDE_ACCEL * cosine)
        normal = f32(normal + ATN_SIDE_ACCEL)
        normal = f32(normal + MOVE_ACCEL)
        normal = f32(normal + MOVE_ACCEL)
        roll = min(ROLL_CAP, f32(f32(normal * ROLL_MULTIPLIER) + ROLL_ADD))
        key = (cosine, -abs(offset), -x, -y)
        if best is None or key > best[0]:
            best = (key, GateChoice(x, y, target, offset, cosine, roll))
    if best is None:
        raise RuntimeError("no full-magnitude SIDE-gate input exists for this camera/facing")
    return best[1]


@lru_cache(maxsize=4096)
def ranked_gate_candidates(facing: int, camera: int, limit: int = 32) -> tuple[GateChoice, ...]:
    """Simulator-ranked, DTM-deliverable SIDE gates for live verification."""
    facing &= 0xFFFF
    camera &= 0xFFFF
    by_target = {}
    for x, y, angle in _full_sticks():
        target = (angle + 0x8000 + camera) & 0xFFFF
        offset = s16(target - facing)
        if not 0 < abs(offset) < 0x2000:
            continue
        cosine = cM_scos_s16(offset)
        if cosine >= ATN_FORWARD_COS:
            continue
        normal = f32(ATN_SIDE_ACCEL * cosine)
        normal = f32(normal + ATN_SIDE_ACCEL)
        normal = f32(normal + MOVE_ACCEL)
        normal = f32(normal + MOVE_ACCEL)
        roll = min(ROLL_CAP, f32(f32(normal * ROLL_MULTIPLIER) + ROLL_ADD))
        choice = GateChoice(x, y, target, offset, cosine, roll)
        old = by_target.get(target)
        if old is None or (x, y) < (old.stick_x, old.stick_y):
            by_target[target] = choice

    sides = []
    for positive in (True, False):
        rows = [g for g in by_target.values() if (g.offset > 0) == positive]
        rows.sort(key=lambda g: (-g.potential_roll_speed, abs(g.offset), g.stick_x, g.stick_y))
        sides.append(rows)
    result = []
    for i in range(max(len(sides[0]), len(sides[1]))):
        for rows in sides:
            if i < len(rows):
                result.append(rows[i])
                if len(result) >= max(1, int(limit)):
                    return tuple(result)
    return tuple(result)


@lru_cache(maxsize=4096)
def stick_for_world_target(target: int, camera: int) -> tuple[int, int, int]:
    """Full input closest to a fixed world target under the current camera.

    Returns ``(x, y, signed_target_error)``. This lets a live launch retain the
    gate's world direction even if targeting nudges the camera after row 1.
    """
    target &= 0xFFFF
    camera &= 0xFFFF
    best = None
    for x, y, angle in _full_sticks():
        actual = (angle + 0x8000 + camera) & 0xFFFF
        error = s16(actual - target)
        key = (abs(error), x, y)
        if best is None or key < best[0]:
            best = (key, x, y, error)
    if best is None:
        raise RuntimeError("no full-magnitude input exists for this world target")
    return best[1], best[2], best[3]


def early_timeline(gate: GateChoice, full_26: bool = False) -> tuple[dict, ...]:
    """Inputs authored on consecutive game frames for the fast launch family."""
    base = {"StickX": gate.stick_x, "StickY": gate.stick_y,
            "CStickX": 128, "CStickY": 100, "L": False, "A": False}
    rows = []
    first = dict(base)
    first["L"] = True
    rows.append(first)
    rows.extend(dict(base) for _ in range(3 + int(bool(full_26))))
    roll = dict(base)
    roll["A"] = True
    rows.append(roll)
    rows.extend(dict(base) for _ in range(3))
    return tuple(rows)


def roll_speed(speed_f: float) -> float:
    return min(ROLL_CAP, max(f32(5.0), f32(f32(f32(speed_f) * ROLL_MULTIPLIER) + ROLL_ADD)))
