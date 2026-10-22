"""Collision Viewer's CPU renderer over Dolphin's plain canvas.

Retained geometry and HUD commands are projected together and submitted through
CPU canvas primitives (depth_triangle_filled, depth_triangle_wire, line and text).
The hardware_mesh/hardware_state method names are retained as an internal adapter
interface; this module does not enable or submit to a GPU canvas.
"""
import heapq
from array import array

NEAR = 5.0                  # camera near clipping distance
MAX_FILL_TRIS = 8000        # nearest triangles drawn when the camera is still
MOVING_TRIS = 2500          # nearest triangles drawn while the camera is moving (then it refines)
MAX_OVERLAY_TRIS = 20000    # trigger volumes / markers
MAX_LINES = 30000           # trigger boxes, collider outlines, etc.
CELL = 1500.0               # spatial grid cell size (world units) used to skip far-away geometry
GRID_MIN_TRIS = 3000        # only build a grid for meshes at least this big
TEXT_ASCENT = 12            # translate top-left HUD anchors to CPU text baselines
_U32 = "I" if array("I").itemsize == 4 else "L"


def _alpha(color, scale):
    a = int(((color >> 24) & 0xFF) * scale)
    a = 0 if a < 0 else (255 if a > 255 else a)
    return (a << 24) | (color & 0x00FFFFFF)


class _RecordingHud:
    """Same surface as the fork's HardwareHud; records commands until commit()."""

    def __init__(self, owner):
        self._owner = owner
        self._cmds = []

    def line(self, a, b, color, thickness=1):
        self._cmds.append(("line", a, b, color, thickness)); return self

    def rect(self, a, b, color, rounding=0, thickness=1):
        self._cmds.append(("rect", a, b, color, rounding, thickness)); return self

    def rect_filled(self, a, b, color, rounding=0):
        self._cmds.append(("rect_filled", a, b, color, rounding)); return self

    def circle(self, center, radius, color, thickness=1):
        self._cmds.append(("circle", center, radius, color, thickness)); return self

    def circle_filled(self, center, radius, color):
        self._cmds.append(("circle_filled", center, radius, color)); return self

    def triangle(self, a, b, c, color, thickness=1):
        self._cmds.append(("triangle", a, b, c, color, thickness)); return self

    def triangle_filled(self, a, b, c, color):
        self._cmds.append(("triangle_filled", a, b, c, color)); return self

    def text(self, pos, color, text):
        self._cmds.append(("text", pos, color, str(text))); return self

    def commit(self):
        self._owner._set_hud(self._cmds)


def _build_grid(p):
    """Bucket triangles by centroid (x, z) so far-away geometry is never touched per frame."""
    cells, big = {}, []
    for i in range(len(p) // 9):
        x0, y0, z0, x1, y1, z1, x2, y2, z2 = p[i * 9:i * 9 + 9]
        ex = max(x0, x1, x2) - min(x0, x1, x2)
        ez = max(z0, z1, z2) - min(z0, z1, z2)
        if ex * ex + ez * ez > CELL * CELL:
            big.append(i)
            continue
        key = (int(((x0 + x1 + x2) / 3.0) // CELL), int(((z0 + z1 + z2) / 3.0) // CELL))
        cells.setdefault(key, []).append(i)
    return {"cells": cells, "big": big}


class SoftCanvas:
    def __init__(self, canvas, wire_color=0xFFFFFFFF, background=0xFF101418):
        self._canvas = canvas
        self._wire_color = wire_color
        self._background = background
        self._meshes = {}
        self._raw = {}
        self._grids = {}
        self._state = None
        self._pose = None
        self._drawn_pose = None
        self._refine = False
        self._hud = []
        self._dirty = True
        self.last_error = ""

    def __getattr__(self, name):
        return getattr(self._canvas, name)

    # ---- the three hardware-canvas calls the viewer makes -------------------------------------
    def hardware_mesh(self, group, positions, colors):
        positions, colors = bytes(positions), bytes(colors)
        if self._raw.get(group) == (positions, colors):
            return
        self._raw[group] = (positions, colors)
        p, c = array("f"), array(_U32)
        p.frombytes(positions)
        c.frombytes(colors)
        self._meshes[group] = (p, c)
        self._grids.pop(group, None)
        if group in (0, 1, 2, 3, 4) and len(p) // 9 >= GRID_MIN_TRIS:
            self._grids[group] = _build_grid(p)
        self._dirty = True

    def hardware_state(self, eye, right, up, forward, focal, radius, fill_opacity, wire_opacity,
                       filled=True, wireframe=True, enabled=True, xray=False, debug_on_top=False,
                       fullscreen=False, clean_capture=False, hud_visible=None, overlay_opacity=None):
        state = dict(
            eye=tuple(eye), right=tuple(right), up=tuple(up), forward=tuple(forward), focal=float(focal),
            radius=float(radius), fill=float(fill_opacity), wire=float(wire_opacity), filled=bool(filled),
            wireframe=bool(wireframe), enabled=bool(enabled),
            hud=(not clean_capture) if hud_visible is None else bool(hud_visible),
            overlay=float(fill_opacity if overlay_opacity is None else overlay_opacity))
        if state != self._state:   # the viewer calls this every frame; only redraw on a real change
            self._state = state
            self._pose = (state["eye"], state["right"], state["up"], state["forward"],
                          state["focal"], state["radius"])
            self._dirty = True

    def hud(self):
        return _RecordingHud(self)

    def _set_hud(self, cmds):
        cmds = list(cmds)
        if cmds != self._hud:
            self._hud = cmds
            self._dirty = True

    # ---- rendering ----------------------------------------------------------------------------
    def flush(self):
        if not (self._dirty or self._refine):
            return
        moving = self._pose != self._drawn_pose
        self._dirty = False
        self._refine = moving          # one full-detail pass once the camera stops
        self._drawn_pose = self._pose
        try:
            self._render(moving)
            self.last_error = ""
        except Exception as exc:  # never let a draw error take down the script's update loop
            self.last_error = "soft canvas: %r" % (exc,)

    def _render(self, moving=False):
        cv = self._canvas
        width, height = float(cv.width), float(cv.height)
        cv.clear()
        cv.rect_filled((0, 0), (width, height), self._background)
        st = self._state
        if st is not None and st["enabled"]:
            self._draw_scene(cv, st, width, height, moving)
        if st is None or st["hud"]:
            self._draw_hud(cv)
        cv.commit()

    def _draw_scene(self, cv, st, width, height, moving=False):
        cx, cy, focal = width * 0.5, height * 0.5, st["focal"]
        ex, ey, ez = st["eye"]
        rx, ry, rz = st["right"]
        ux, uy, uz = st["up"]
        fx, fy, fz = st["forward"]
        radius2 = st["radius"] * st["radius"] if st["radius"] > 0 else None

        def cam(p, o):
            dx, dy, dz = p[o] - ex, p[o + 1] - ey, p[o + 2] - ez
            return (dx * rx + dy * ry + dz * rz, dx * ux + dy * uy + dz * uz, dx * fx + dy * fy + dz * fz)

        def proj(v):
            return (cx + focal * v[0] / v[2], cy - focal * v[1] / v[2])

        def clip(poly):
            out = []
            for i in range(len(poly)):
                a, b = poly[i], poly[(i + 1) % len(poly)]
                a_in, b_in = a[2] >= NEAR, b[2] >= NEAR
                if a_in:
                    out.append(a)
                if a_in != b_in:
                    t = (NEAR - a[2]) / (b[2] - a[2])
                    out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR))
            return out

        def visible(vs):
            if all(v[2] < NEAR for v in vs):
                return False
            if all(v[2] >= NEAR for v in vs):
                xs = [cx + focal * v[0] / v[2] for v in vs]
                ys = [cy - focal * v[1] / v[2] for v in vs]
                if max(xs) < 0 or min(xs) > width or max(ys) < 0 or min(ys) > height:
                    return False
            return True

        def triangles(groups, limit):
            found = []
            for group in groups:
                mesh = self._meshes.get(group)
                if not mesh:
                    continue
                p, c = mesh
                ntri = min(len(p) // 9, len(c) // 3)
                for i in self._candidates(group, ntri, st["eye"], st["forward"], limit):
                    o = i * 9
                    vs = (cam(p, o), cam(p, o + 3), cam(p, o + 6))
                    if radius2 is not None and all(v[0] ** 2 + v[1] ** 2 + v[2] ** 2 > radius2 for v in vs):
                        continue
                    if not visible(vs):
                        continue
                    found.append((vs[0][2] + vs[1][2] + vs[2][2], vs, c[i * 3]))
            if len(found) > limit:
                found = heapq.nsmallest(limit, found, key=lambda t: t[0])
            return found

        def fan(vs):
            poly = list(vs) if all(v[2] >= NEAR for v in vs) else clip(vs)
            for i in range(1, len(poly) - 1):
                yield poly[0], poly[i], poly[i + 1]

        # 1) depth-tested collision mesh. Depth primitives go first so the canvas's software depth
        #    pass sits underneath everything drawn after it.
        fill_cap = MOVING_TRIS if moving else MAX_FILL_TRIS
        for _, vs, color in triangles((0, 1, 2, 3, 4), fill_cap):
            for a, b, c in fan(vs):
                depths = (a[2], b[2], c[2])
                pa, pb, pc = proj(a), proj(b), proj(c)
                if st["filled"]:
                    cv.depth_triangle_filled(pa, pb, pc, depths, _alpha(color, st["fill"]))
                if st["wireframe"] and not moving:
                    cv.depth_triangle_wire(pa, pb, pc, depths, _alpha(self._wire_color, st["wire"]), 1)

        # 2) translucent trigger volumes (group 7), drawn as plain 2D fills so the scene shows through
        for _, vs, color in triangles((7,), MAX_OVERLAY_TRIS):
            for a, b, c in fan(vs):
                cv.triangle_filled(proj(a), proj(b), proj(c), _alpha(color, st["overlay"]))

        # 3) retained line lists: 5 = scene lines, 8 = always on top
        for group, scale in ((5, st["wire"]), (8, 1.0)):
            mesh = self._meshes.get(group)
            if not mesh:
                continue
            p, c = mesh
            for i in range(min(len(p) // 6, len(c) // 2, MAX_LINES)):
                a, b = cam(p, i * 6), cam(p, i * 6 + 3)
                if a[2] < NEAR and b[2] < NEAR:
                    continue
                if radius2 is not None and a[0] ** 2 + a[1] ** 2 + a[2] ** 2 > radius2 \
                        and b[0] ** 2 + b[1] ** 2 + b[2] ** 2 > radius2:
                    continue
                if a[2] < NEAR or b[2] < NEAR:
                    near_pt, far_pt = (a, b) if a[2] < NEAR else (b, a)
                    t = (NEAR - near_pt[2]) / (far_pt[2] - near_pt[2])
                    clipped = (near_pt[0] + (far_pt[0] - near_pt[0]) * t,
                               near_pt[1] + (far_pt[1] - near_pt[1]) * t, NEAR)
                    a, b = (clipped, b) if a[2] < NEAR else (a, clipped)
                cv.line(proj(a), proj(b), _alpha(c[i * 2], scale), 1)

        # 4) above-scene marker triangles (group 6)
        for _, vs, color in triangles((6,), MAX_OVERLAY_TRIS):
            for a, b, c in fan(vs):
                cv.triangle_filled(proj(a), proj(b), proj(c), color)

    def _candidates(self, group, ntri, eye, forward, want):
        """Triangle indices worth testing: everything for small meshes, otherwise the nearest grid cells."""
        g = self._grids.get(group)
        if g is None:
            return range(ntri)
        ex, ez = eye[0], eye[2]
        fx, fz = forward[0], forward[2]
        horiz = (fx * fx + fz * fz) ** 0.5
        use_front = horiz > 0.25
        if use_front:
            fx, fz = fx / horiz, fz / horiz
        scored = []
        for key, idxs in g["cells"].items():
            dx, dz = (key[0] + 0.5) * CELL - ex, (key[1] + 0.5) * CELL - ez
            if use_front and dx * fx + dz * fz < -1.2 * CELL:   # whole cell is behind the camera
                continue
            scored.append((dx * dx + dz * dz, idxs))
        scored.sort(key=lambda t: t[0])
        pool = list(g["big"])         # triangles bigger than a cell are always considered
        for _, idxs in scored:
            pool.extend(idxs)
            if len(pool) >= want * 3:
                break
        return pool

    def _draw_hud(self, cv):
        for cmd in self._hud:
            kind = cmd[0]
            if kind == "line":
                cv.line(cmd[1], cmd[2], cmd[3], cmd[4])
            elif kind == "rect":
                cv.rect(cmd[1], cmd[2], cmd[3], cmd[4], cmd[5])
            elif kind == "rect_filled":
                cv.rect_filled(cmd[1], cmd[2], cmd[3], cmd[4])
            elif kind == "circle":
                cv.circle(cmd[1], cmd[2], cmd[3], cmd[4])
            elif kind == "circle_filled":
                cv.circle_filled(cmd[1], cmd[2], cmd[3])
            elif kind == "triangle":
                cv.triangle(cmd[1], cmd[2], cmd[3], cmd[4], cmd[5])
            elif kind == "triangle_filled":
                cv.triangle_filled(cmd[1], cmd[2], cmd[3], cmd[4])
            elif kind == "text":
                cv.text((cmd[1][0], cmd[1][1] + TEXT_ASCENT), cmd[2], cmd[3])
