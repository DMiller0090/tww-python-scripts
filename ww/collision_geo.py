"""ww.collision_geo — live reader for the STAGE COLLISION GEOMETRY (DZB triangles) in RAM.

This is the geometry counterpart to `ww.collision` (which only reads Link's contact FLAG bits).
It walks TWW's runtime background-collision manager `dBgS` and pulls out every registered
collision mesh's world-space vertices + triangle list, so a viewer can draw the actual walkable
surfaces the game tests against.

Self-contained on purpose (mirrors `ww/cull.py`): the caller passes a reader object exposing a
single `read_bytes(gc_addr, n) -> bytes` method over big-endian GC RAM; all typed reads are built
on top of it with `struct`. No dependency on `ww.memory` or the `dolphin` module, so it imports and
type-checks outside Dolphin.

RAM model (JP/GZLJ01, validated live 2026-07-06 on stage H_test; see knowledge/mechanics/collision.md):

  dBgS manager @ 0x803B93A8  ( = g_dComIfG_gameInfo 0x803B8108 + play 0x12A0 )
    cBgS::m_chk_element[256]  (stride 0x14)   each slot:
        +0x00 cBgW*  m_bgw_base_ptr
        +0x04 u32    m_flags       bit0 = slot in use
    cBgW (0xA8) — runtime wrapper per DZB instance:
        +0x6C u8     mFlags        GLOBAL_e=0x20 (static room), MOVE_BG_e=0x01 (movable object)
        +0x90 cBgD_Vtx_t* pm_vtx_tbl  WORLD-space vertices (use THIS, not m_v_tbl — they differ
                                       for movable BG whose base matrix is applied)
        +0x94 cBgD_t*     pm_bgd       the loaded DZB header
    cBgD_t (DZB header):
        +0x00 s32 m_v_num   +0x04 Vtx* m_v_tbl   (local verts; 12B: f32 x,y,z)
        +0x08 s32 m_t_num   +0x0C Tri* m_t_tbl   (10B: u16 vtx0,vtx1,vtx2,id,grp)
        +0x20 s32 m_g_num   +0x24 Grp* m_g_tbl   (0x34 each)
        +0x28 s32 m_ti_num  +0x2C Ti*  m_ti_tbl  (16B property records)

Link's current floor triangle: dBgS_LinkAcch ptr @ 0x803BD910 → +0x554 u16 polyIndex,
+0x556 u16 bgIndex (the manager slot). roof at +0x594. 0xFFFF / 0x100 = none.
"""
import struct

# --- JP/GZLJ01 addresses ------------------------------------------------------------------
DBGS               = 0x803B93A8   # dBgS collision manager (cBgS_ChkElm m_chk_element[256] @ +0)
LINK_ACCH_PTR      = 0x803BD910   # -> dBgS_LinkAcch; +0x554 gnd polyIndex, +0x556 gnd bgIndex
STAGE_NAME         = 0x803BD23C   # 11-byte ASCII current stage name

CHK_ELEM_STRIDE    = 0x14
CHK_ELEM_COUNT     = 256
OFF_BGW_PTR        = 0x00
OFF_BGW_FLAGS      = 0x04
OFF_CBGW_FLAGS     = 0x6C
OFF_CBGW_PM_VTX    = 0x90
OFF_CBGW_PM_BGD    = 0x94
FLAG_MOVE_BG       = 0x01
FLAG_GLOBAL        = 0x20
OFF_GND_POLYIDX    = 0x554
OFF_GND_BGIDX      = 0x556

_RAM_MIN = 0x80000000
_RAM_MAX = 0x81800000


def _valid(p):
    return _RAM_MIN <= p < _RAM_MAX


class _R:
    """Typed big-endian reads over a `rd.read_bytes(addr, n)` reader."""
    def __init__(self, rd):
        self.rd = rd

    def u16(self, a):
        return struct.unpack(">H", self.rd.read_bytes(a, 2))[0]

    def s32(self, a):
        return struct.unpack(">i", self.rd.read_bytes(a, 4))[0]

    def u32(self, a):
        return struct.unpack(">I", self.rd.read_bytes(a, 4))[0]

    def u8(self, a):
        return self.rd.read_bytes(a, 1)[0]

    def block(self, a, n):
        return self.rd.read_bytes(a, n)


def stage_name(rd):
    raw = rd.read_bytes(STAGE_NAME, 11)
    return raw.split(b"\x00")[0].decode("ascii", "replace")


def link_floor_tri(rd):
    """Return (bg_index, poly_index) of the triangle Link is standing on, or None."""
    r = _R(rd)
    acch = r.u32(LINK_ACCH_PTR)
    if not _valid(acch):
        return None
    poly = r.u16(acch + OFF_GND_POLYIDX)
    bg = r.u16(acch + OFF_GND_BGIDX)
    if poly == 0xFFFF or bg == 0x100:
        return None
    return (bg, poly)


def _read_mesh(r, bgw):
    """Read one cBgW into a mesh dict, or None if malformed. Uses WORLD-space verts (+0x90)."""
    pm_bgd = r.u32(bgw + OFF_CBGW_PM_BGD)
    if not _valid(pm_bgd):
        return None
    v_num = r.s32(pm_bgd + 0x00)
    t_num = r.s32(pm_bgd + 0x08)
    t_tbl = r.u32(pm_bgd + 0x0C)
    v_tbl = r.u32(bgw + OFF_CBGW_PM_VTX)   # world-space vertex table
    if not (0 < v_num < 300000 and 0 < t_num < 600000):
        return None
    if not (_valid(v_tbl) and _valid(t_tbl)):
        return None
    cbgw_flags = r.u8(bgw + OFF_CBGW_FLAGS)

    # Bulk-read the whole vertex + triangle tables (2 reads, not thousands).
    vbytes = r.block(v_tbl, v_num * 12)
    verts = list(struct.unpack(">%df" % (v_num * 3), vbytes[: v_num * 12]))
    verts = [(verts[i], verts[i + 1], verts[i + 2]) for i in range(0, len(verts), 3)]

    tbytes = r.block(t_tbl, t_num * 10)
    tris = []
    for i in range(t_num):
        a, b, c, tid, grp = struct.unpack_from(">5H", tbytes, i * 10)
        tris.append((a, b, c, tid, grp))

    # Precompute per-triangle centroid + surface class ONCE (they don't change for a given mesh
    # state). For the static room this is cached across frames with the mesh; for movable BG it is
    # recomputed each frame (few tris). This keeps the per-frame viewer off the cross-product+sqrt
    # (tri_normal) and classify path for thousands of static triangles.
    centroids = []
    classes = []
    for a, b, c, tid, grp in tris:
        v0 = verts[a]; v1 = verts[b]; v2 = verts[c]
        centroids.append(((v0[0]+v1[0]+v2[0]) / 3.0,
                          (v0[1]+v1[1]+v2[1]) / 3.0,
                          (v0[2]+v1[2]+v2[2]) / 3.0))
        classes.append(classify(tri_normal(v0, v1, v2)[1]))

    return {
        "bgw": bgw,
        "pm_bgd": pm_bgd,
        "is_global": bool(cbgw_flags & FLAG_GLOBAL),
        "is_movebg": bool(cbgw_flags & FLAG_MOVE_BG),
        "v_num": v_num,
        "t_num": t_num,
        "verts": verts,
        "tris": tris,
        "centroids": centroids,
        "classes": classes,
        "v_tbl": v_tbl,
    }


def read_collision(rd, cache=None):
    """Scan dBgS.m_chk_element[256] and return a snapshot of all registered collision meshes.

    Returns dict:
        {"stage": str,
         "floor": (bg_index, poly_index)|None,   # triangle Link stands on
         "meshes": {bg_index: mesh_dict, ...}}    # keyed by manager slot (== poly_info bgIndex)

    `cache` (the previous return value) lets STATIC (GLOBAL_e) room meshes be reused across frames
    without re-reading their (large, unchanging) vertex/triangle tables. Movable-BG meshes have
    world-space verts that change every frame, so they are always re-read. Cache validity keys on
    (bgw ptr, pm_bgd ptr, v_num, t_num, v_tbl ptr) so a stage change or slot reuse invalidates it.
    """
    r = _R(rd)
    old = (cache or {}).get("meshes", {}) if cache else {}
    meshes = {}
    for i in range(CHK_ELEM_COUNT):
        e = DBGS + i * CHK_ELEM_STRIDE
        flags = r.u32(e + OFF_BGW_FLAGS)
        if not (flags & 1):
            continue
        bgw = r.u32(e + OFF_BGW_PTR)
        if not _valid(bgw):
            continue
        pm_bgd = r.u32(bgw + OFF_CBGW_PM_BGD)
        if not _valid(pm_bgd):
            continue
        v_tbl = r.u32(bgw + OFF_CBGW_PM_VTX)
        v_num = r.s32(pm_bgd + 0x00)
        t_num = r.s32(pm_bgd + 0x08)
        prev = old.get(i)
        cbgw_flags = r.u8(bgw + OFF_CBGW_FLAGS)
        static = bool(cbgw_flags & FLAG_GLOBAL) and not (cbgw_flags & FLAG_MOVE_BG)
        if (static and prev is not None
                and prev["bgw"] == bgw and prev["pm_bgd"] == pm_bgd
                and prev["v_num"] == v_num and prev["t_num"] == t_num
                and prev["v_tbl"] == v_tbl):
            meshes[i] = prev            # unchanged static room mesh — reuse cached tables
            continue
        m = _read_mesh(r, bgw)
        if m is not None:
            meshes[i] = m

    return {
        "stage": stage_name(rd),
        "floor": link_floor_tri(rd),
        "meshes": meshes,
    }


# --- geometry helpers (pure, no reader) ---------------------------------------------------

def tri_normal(v0, v1, v2):
    """Unit face normal via cross((v1-v0),(v2-v0)). Winding matches the DZB (vtx0,vtx1,vtx2)."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    m = (nx * nx + ny * ny + nz * nz) ** 0.5
    if m < 1e-9:
        return (0.0, 0.0, 0.0)
    return (nx / m, ny / m, nz / m)


def classify(ny):
    """Surface class from normal Y, matching cBgW_CheckB* (ny>=0.5 ground, ny<-0.8 roof, else wall)."""
    if ny >= 0.5:
        return "ground"
    if ny < -0.8:
        return "roof"
    return "wall"
