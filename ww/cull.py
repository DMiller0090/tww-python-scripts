"""ww.cull — self-contained TWW view-frustum culling scanner (JP/GZLJ01).

Everything cull_viewer.py needs to read the live culling frustum + actor cull verdicts, with NO
external dependency — so cloning tww-python-scripts alone is enough to run the viewer. Reads go
through a caller-supplied `rd` object exposing `read_bytes(gc_addr, n)` (big-endian GC memory).

This is a VENDORED PORT of the canonical, host-validated code in the tww_sim repo
(tww_sim/core/camera/frustum.py + harness/capture/capture_cull.py). Behavior is identical:
J3DUClipper `clip_box` on the live view*cullMtx and cull box, whose verdict matched the game's own
fopAcCnd_NODRAW_e for 60/60 box actors (0 mismatches). f32 math uses `struct` (the embedded Dolphin
Python has no _ctypes); struct 'f' round-trips IEEE single with round-half-to-even, bit-identical.
If you change the cull logic, mirror it in tww_sim (or vice-versa) — the two must stay in sync.

Addresses confirmed live 2026-07-05 (see tww_sim/knowledge/mechanics/culling.md):
  camera_class = [[0x803AD380]+0x34]; mDoLib_clipper static 0x80398bfc; actor list head 0x803654CC.
"""
import os, struct, math

# --- f32 (Gekko single-precision), struct-based -------------------------------------------
def _f(x):  return struct.unpack("f", struct.pack("f", x))[0]
def fmuls(a, b): return _f(a * b)
def fadds(a, b): return _f(a + b)
def fsubs(a, b): return _f(a - b)
DEG2RAD = _f(0.017453292)


# --- J3DUClipper port (clip_box is the cull truth; True == culled/outside) -----------------
def _cross(a, b):
    return (fsubs(fmuls(a[1], b[2]), fmuls(a[2], b[1])),
            fsubs(fmuls(a[2], b[0]), fmuls(a[0], b[2])),
            fsubs(fmuls(a[0], b[1]), fmuls(a[1], b[0])))

def _normalize(v):
    mag = fadds(fadds(fmuls(v[0], v[0]), fmuls(v[1], v[1])), fmuls(v[2], v[2]))
    if mag <= 0.0:
        return (0.0, 0.0, 0.0)
    inv = _f(1.0 / math.sqrt(mag))
    return (fmuls(v[0], inv), fmuls(v[1], inv), fmuls(v[2], inv))

def transform_point(m, v):
    x = fadds(fadds(fadds(fmuls(m[0][0], v[0]), fmuls(m[0][1], v[1])), fmuls(m[0][2], v[2])), m[0][3])
    y = fadds(fadds(fadds(fmuls(m[1][0], v[0]), fmuls(m[1][1], v[1])), fmuls(m[1][2], v[2])), m[1][3])
    z = fadds(fadds(fadds(fmuls(m[2][0], v[0]), fmuls(m[2][1], v[1])), fmuls(m[2][2], v[2])), m[2][3])
    return (x, y, z)

def _dot(p, pl):
    return fadds(fadds(fmuls(p[0], pl[0]), fmuls(p[1], pl[1])), fmuls(p[2], pl[2]))

def mtx_concat(a, b):
    out = [[0.0, 0.0, 0.0, 0.0] for _ in range(3)]
    for i in range(3):
        for j in range(4):
            s = fadds(fadds(fmuls(a[i][0], b[0][j]), fmuls(a[i][1], b[1][j])), fmuls(a[i][2], b[2][j]))
            if j == 3:
                s = fadds(s, a[i][3])
            out[i][j] = s
    return out

def calc_view_frustum(fovy, aspect, near):
    fovy, aspect, near = _f(fovy), _f(aspect), _f(near)
    tan_fovy = _f(math.tan(fmuls(fmuls(fovy, 0.5), DEG2RAD)))
    ny = fmuls(near, tan_fovy); nx = fmuls(aspect, ny)
    c0 = (-nx, -ny, -near); c1 = (-nx, ny, -near); c2 = (nx, ny, -near); c3 = (nx, -ny, -near)
    return [_normalize(_cross(c1, c0)), _normalize(_cross(c2, c1)),
            _normalize(_cross(c3, c2)), _normalize(_cross(c0, c3))]

class Frustum:
    def __init__(self, fovy, aspect, near, far, planes):
        self.fovy, self.aspect, self.near, self.far, self.planes = fovy, aspect, near, far, planes
    def with_far(self, far):
        return Frustum(self.fovy, self.aspect, self.near, _f(far), self.planes)
    def clip_box(self, mtx, pmin, pmax):
        """True == AABB fully outside the frustum (culled). Corner set is symmetric in pmin/pmax."""
        corners = (
            (pmax[0], pmax[1], pmin[2]), (pmax[0], pmax[1], pmax[2]),
            (pmin[0], pmax[1], pmax[2]), (pmin[0], pmax[1], pmin[2]),
            (pmax[0], pmin[1], pmin[2]), (pmax[0], pmin[1], pmax[2]),
            (pmin[0], pmin[1], pmax[2]), (pmin[0], pmin[1], pmin[2]),
        )
        clip = [0, 0, 0, 0, 0, 0]
        for corner in corners:
            p = transform_point(mtx, corner)
            any_out = False
            neg_z = -p[2]
            if neg_z < self.near: clip[4] += 1; any_out = True
            if neg_z > self.far:  clip[5] += 1; any_out = True
            if _dot(p, self.planes[0]) > 0.0: clip[0] += 1; any_out = True
            if _dot(p, self.planes[1]) > 0.0: clip[1] += 1; any_out = True
            if _dot(p, self.planes[2]) > 0.0: clip[2] += 1; any_out = True
            if _dot(p, self.planes[3]) > 0.0: clip[3] += 1; any_out = True
            if not any_out:
                return False
        return any(c == 8 for c in clip)

def build_frustum(fovy, aspect, near, far):
    fovy, aspect, near, far = _f(fovy), _f(aspect), _f(near), _f(far)
    return Frustum(fovy, aspect, near, far, calc_view_frustum(fovy, aspect, near))


# --- JP/GZLJ01 addresses -------------------------------------------------------------------
CAM_BASE = 0x803AD380
VC = {"near": 0xC8, "far": 0xCC, "fovy": 0xD0, "aspect": 0xD4,
      "eye": 0xD8, "center": 0xE4, "up": 0xF0, "viewMtx": 0x140}
CLIPPER_ADDR = 0x80398bfc
CLIPPER_FP = struct.pack(">fff", 60.0, 1.28, 1.0)
ACTOR_LIST_HEAD = 0x803654CC
NODE_NEXT, NODE_GPTR = 0x00, 0x0C
AC = {"pid": 0x08, "cullType": 0x1BF, "status": 0x1C4, "condition": 0x1C8,
      "pos": 0x1F8, "cullMtx": 0x22C, "boxMin": 0x230, "boxMax": 0x23C, "cullFar": 0x248}
FOP_STTS_CULL = 0x100
FOP_CND_NODRAW = 0x04
L_CULLBOX = [((-40,0,-40),(40,125,40)),((-25,0,-25),(25,50,25)),((-50,0,-50),(50,100,50)),
             ((-75,0,-75),(75,150,75)),((-100,0,-100),(100,800,100)),((-125,0,-125),(125,250,125)),
             ((-150,0,-150),(150,300,150)),((-200,0,-200),(200,400,200)),((-600,0,-600),(600,900,600)),
             ((-250,0,-50),(250,450,50)),((-60,0,-20),(40,130,150)),((-75,0,-75),(75,210,75)),
             ((-70,-100,-80),(70,240,100)),((-60,-20,-60),(60,160,60))]
CULLBOX_CUSTOM = 0x0E
LINK_X = 0x803D78FC


def _u32(rd, a): return struct.unpack(">I", rd.read_bytes(a, 4))[0]
def _u16(rd, a): return struct.unpack(">H", rd.read_bytes(a, 2))[0]
def _u8(rd, a):  return rd.read_bytes(a, 1)[0]
def _f32(rd, a): return struct.unpack(">f", rd.read_bytes(a, 4))[0]
def _vec3(rd, a): return list(struct.unpack(">fff", rd.read_bytes(a, 12)))
def _mtx34(rd, a):
    f = struct.unpack(">12f", rd.read_bytes(a, 48))
    return [list(f[0:4]), list(f[4:8]), list(f[8:12])]
def _valid(p): return 0x80000000 <= p < 0x81800000


def read_camera(rd):
    cam = _u32(rd, _u32(rd, CAM_BASE) + 0x34)
    return {"camera_class": cam,
            "fovy": _f32(rd, cam + VC["fovy"]), "aspect": _f32(rd, cam + VC["aspect"]),
            "near": _f32(rd, cam + VC["near"]), "render_far": _f32(rd, cam + VC["far"]),
            "eye": _vec3(rd, cam + VC["eye"]), "center": _vec3(rd, cam + VC["center"]),
            "up": _vec3(rd, cam + VC["up"]), "viewMtx": _mtx34(rd, cam + VC["viewMtx"])}


def _locate_clipper(rd):
    try:
        fovy, aspect, near = struct.unpack(">fff", rd.read_bytes(CLIPPER_ADDR + 0x4C, 12))
        if 10.0 < fovy < 170.0 and 0.5 < aspect < 3.0 and near > 0.0:
            return CLIPPER_ADDR
    except Exception:
        pass
    data = rd.read_bytes(0x80000000, 0x2000000)
    i = data.find(CLIPPER_FP)
    if i < 0:
        raise RuntimeError("mDoLib_clipper not found (fovy=60/aspect=1.28/near=1.0 fingerprint)")
    return 0x80000000 + i - 0x4C


def read_clipper(rd):
    c = _locate_clipper(rd)
    return {"clipper_addr": c, "fovy": _f32(rd, c + 0x4C), "aspect": _f32(rd, c + 0x50),
            "near": _f32(rd, c + 0x54), "far": _f32(rd, c + 0x58),
            "planes": [_vec3(rd, c + 0x04 + p * 12) for p in range(4)]}


_PROC_NAMES = None
def _proc_name(pid):
    global _PROC_NAMES
    if _PROC_NAMES is None:
        _PROC_NAMES = {}
        csvp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "proc_name_structs.csv")
        try:
            import csv
            with open(csvp, newline="") as f:
                for row in csv.DictReader(f):
                    nm, val = (row.get("ProcName") or "").strip(), (row.get("ProcValue") or "").strip()
                    if nm and val:
                        try: _PROC_NAMES.setdefault(int(val, 0), nm)
                        except ValueError: pass
        except Exception:
            pass
    return _PROC_NAMES.get(pid, "#%d" % pid)


def _box_world_corners(bmin, bmax, cullMtx):
    corners = [(bmax[0], bmax[1], bmin[2]), (bmax[0], bmax[1], bmax[2]),
               (bmin[0], bmax[1], bmax[2]), (bmin[0], bmax[1], bmin[2]),
               (bmax[0], bmin[1], bmin[2]), (bmax[0], bmin[1], bmax[2]),
               (bmin[0], bmin[1], bmax[2]), (bmin[0], bmin[1], bmin[2])]
    if cullMtx is None:
        return [list(c) for c in corners]
    return [list(transform_point(cullMtx, c)) for c in corners]


def read_actors(rd, cam, clip):
    view = cam["viewMtx"]
    base_fr = build_frustum(clip["fovy"], clip["aspect"], clip["near"], clip["far"])
    cullpoint = clip["far"]
    out, seen = [], set()
    node = _u32(rd, ACTOR_LIST_HEAD)
    while _valid(node) and node not in seen and len(seen) < 20000:
        seen.add(node)
        gptr = _u32(rd, node + NODE_GPTR)
        node = _u32(rd, node + NODE_NEXT)
        if not _valid(gptr):
            continue
        if not (_u32(rd, gptr + AC["status"]) & FOP_STTS_CULL):
            continue
        pid = _u16(rd, gptr + AC["pid"])
        cullType = _u8(rd, gptr + AC["cullType"])
        cullMtxP = _u32(rd, gptr + AC["cullMtx"])
        cullFar = _f32(rd, gptr + AC["cullFar"])
        game_culled = bool(_u32(rd, gptr + AC["condition"]) & FOP_CND_NODRAW)
        pos = _vec3(rd, gptr + AC["pos"])
        cullMtx = _mtx34(rd, cullMtxP) if _valid(cullMtxP) else None
        is_box = cullType <= CULLBOX_CUSTOM
        our_culled, corners = None, None
        if is_box:
            if cullType == CULLBOX_CUSTOM:
                bmin, bmax = _vec3(rd, gptr + AC["boxMin"]), _vec3(rd, gptr + AC["boxMax"])
            else:
                bmin = [float(v) for v in L_CULLBOX[cullType][0]]
                bmax = [float(v) for v in L_CULLBOX[cullType][1]]
            pMtx = mtx_concat(view, cullMtx) if cullMtx is not None else view
            fr = base_fr.with_far(cullFar * cullpoint) if cullFar > 0.0 else base_fr
            our_culled = fr.clip_box(pMtx, bmin, bmax)
            corners = _box_world_corners(bmin, bmax, cullMtx)
        out.append({"name": _proc_name(pid), "pid": pid, "addr": gptr, "pos": pos,
                    "cullType": cullType, "is_box": is_box, "cullFar": cullFar,
                    "our_culled": our_culled, "game_culled": game_culled,
                    "agree": (our_culled == game_culled) if our_culled is not None else None,
                    "corners": corners})
    return out


def frustum_world_corners(eye, center, up, fovy, aspect, near, far):
    def sub(a, b): return [a[0]-b[0], a[1]-b[1], a[2]-b[2]]
    def add(*vs): return [sum(c) for c in zip(*vs)]
    def scale(v, s): return [v[0]*s, v[1]*s, v[2]*s]
    def cross(a, b): return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
    def norm(v):
        m = math.sqrt(sum(c*c for c in v)) or 1.0
        return [c/m for c in v]
    fwd = norm(sub(center, eye)); right = norm(cross(fwd, up)); up2 = cross(right, fwd)
    tan = math.tan(math.radians(fovy) * 0.5)
    def face(dist):
        c = add(eye, scale(fwd, dist)); hh = dist * tan; hw = hh * aspect
        return [add(c, scale(right, -hw), scale(up2, -hh)), add(c, scale(right,  hw), scale(up2, -hh)),
                add(c, scale(right,  hw), scale(up2,  hh)), add(c, scale(right, -hw), scale(up2,  hh))]
    return face(near) + face(far)


def full_snapshot(rd):
    """Everything the live viewer needs in one pass: camera + culling frustum + Link + actors."""
    cam = read_camera(rd)
    clip = read_clipper(rd)
    actors = read_actors(rd, cam, clip)
    link = [_f32(rd, LINK_X), _f32(rd, LINK_X + 4), _f32(rd, LINK_X + 8)]
    boxed = [a for a in actors if a["our_culled"] is not None]
    fc = frustum_world_corners(cam["eye"], cam["center"], cam["up"],
                               cam["fovy"], cam["aspect"], cam["near"], clip["far"])
    return {"camera": {k: cam[k] for k in ("eye", "center", "up", "fovy", "aspect", "near", "render_far")},
            "cull_far": clip["far"], "frustum_corners": fc, "link": link, "actors": actors,
            "counts": {"total": len(actors), "boxed": len(boxed),
                       "agree": sum(1 for a in boxed if a["agree"]),
                       "mismatch": sum(1 for a in boxed if not a["agree"])}}
