"""DZB property editing and Link ground-query geometry, from the bundled decomp.

This module has no Dolphin imports; callers supply memory readers/writers.
"""
import math
import struct

from .collision_geo import DBGS, CHK_ELEM_STRIDE, tri_normal

# (word index, shift, width); preserve every bit outside the edited field.
FIELDS = {
    'camera_id': (0, 0, 8), 'material_sound': (0, 8, 5), 'exit_id': (0, 13, 6),
    'poly_color': (0, 19, 8), 'shadow_through': (0, 27, 1),
    'link_no': (1, 0, 8), 'wall_code': (1, 8, 4), 'special_code': (1, 12, 4),
    'attribute_code': (1, 16, 5), 'ground_code': (1, 21, 5),
    'camera_move_bg': (2, 0, 8), 'room_camera_id': (2, 8, 8),
    'room_path_id': (2, 16, 8), 'room_path_point': (2, 24, 8),
    'camera_through': (3, 0, 1), 'object_through': (3, 1, 1), 'link_through': (3, 2, 1),
    'arrow_through': (3, 3, 1), 'hookshot_stick': (3, 4, 1),
    'bomb_through': (3, 5, 1), 'boomerang_through': (3, 6, 1), 'rope_through': (3, 7, 1),
    **{'info%d' % i: (i, 0, 32) for i in range(4)},
}


def decode_properties(words):
    return {name: (words[word] >> shift) & ((1 << width) - 1)
            for name, (word, shift, width) in FIELDS.items()}


def edit_words(words, field, text):
    if field not in FIELDS:
        raise ValueError('Unknown property. Fields: ' + ', '.join(FIELDS))
    word, shift, width = FIELDS[field]
    value = int(text, 0) if isinstance(text, str) else int(text)
    if not 0 <= value < (1 << width):
        raise ValueError('%s must be 0..%d' % (field, (1 << width) - 1))
    result = list(words)
    mask = ((1 << width) - 1) << shift
    result[word] = (result[word] & ~mask) | (value << shift)
    return tuple(result)


def inspect_triangle(reader, mesh, index):
    if not 0 <= index < mesh['t_num']:
        raise ValueError('triangle no longer exists')
    raw_tri = bytes(reader.read_bytes(mesh['t_tbl'] + index * 10, 10))
    triangle = struct.unpack('>5H', raw_tri)
    tid = triangle[3]
    if tid >= mesh['ti_num']:
        raise ValueError('triangle property index is out of range')
    raw = bytes(reader.read_bytes(mesh['ti_tbl'] + tid * 16, 16))
    users = sum(t[3] == tid for t in mesh['tris'])
    return triangle, struct.unpack('>4I', raw), users


def write_triangle_property(memory, mesh, slot, index, field, text, shared=False, meshes=()):
    """Copy a shared Ti to an unused slot before editing this triangle when possible."""
    # Validate the slot and current DZB pointers before touching scene memory.
    elm = bytes(memory.read_bytes(DBGS + slot * CHK_ELEM_STRIDE, CHK_ELEM_STRIDE))
    bgw, flags = struct.unpack_from('>2I', elm)
    if bgw != mesh['bgw'] or not flags & 1:
        raise ValueError('collision registration changed; select the triangle again')
    if struct.unpack('>I', bytes(memory.read_bytes(bgw + 0x94, 4)))[0] != mesh['pm_bgd']:
        raise ValueError('DZB was replaced')
    source = bytes(memory.read_bytes(mesh['pm_bgd'], 0x30))
    if (struct.unpack_from('>I', source, 0x0C)[0] != mesh['t_tbl'] or
            struct.unpack_from('>I', source, 0x2C)[0] != mesh['ti_tbl'] or
            struct.unpack_from('>i', source, 0x08)[0] != mesh['t_num'] or
            struct.unpack_from('>i', source, 0x28)[0] != mesh['ti_num']):
        raise ValueError('collision table pointers/counts changed')
    triangle, words, _users = inspect_triangle(memory, mesh, index)
    new = edit_words(words, field, text)
    # Read every live ID; cached triangles could predate another tool's edits.
    raw_tris = bytes(memory.read_bytes(mesh['t_tbl'], mesh['t_num'] * 10))
    live = [struct.unpack_from('>5H', raw_tris, i * 10) for i in range(mesh['t_num'])]
    tid = triangle[3]
    users = sum(t[3] == tid for t in live)
    occupied = {t[3] for t in live}
    seen_tables = {mesh['t_tbl']}
    for other in meshes:
        if other['ti_tbl'] != mesh['ti_tbl'] or other['t_tbl'] in seen_tables:
            continue
        seen_tables.add(other['t_tbl'])
        raw = bytes(memory.read_bytes(other['t_tbl'], other['t_num']*10))
        ids = [struct.unpack_from('>H',raw,i*10+6)[0] for i in range(other['t_num'])]
        occupied.update(ids)
        users += ids.count(tid)
    destination = tid
    if users > 1 and not shared:
        destination = next((i for i in range(min(65536, mesh['ti_num'])) if i not in occupied), None)
        if destination is None:
            raise ValueError('Property record is shared by %d triangles; no unused Ti slot. Enable Edit shared record to affect them all.' % users)
    addr = mesh['ti_tbl'] + destination * 16
    old = bytes(memory.read_bytes(addr, 16))
    id_addr = mesh['t_tbl'] + index * 10 + 6
    old_id = bytes(memory.read_bytes(id_addr, 2))
    try:
        memory.write_bytes(addr, bytearray(struct.pack('>4I', *new)))
        if destination != tid:
            memory.write_bytes(id_addr, bytearray(struct.pack('>H', destination)))
        if bytes(memory.read_bytes(addr,16)) != struct.pack('>4I',*new):
            raise ValueError('property write verification failed')
        if struct.unpack('>H',bytes(memory.read_bytes(id_addr,2)))[0] != destination:
            raise ValueError('triangle property index write verification failed')
    except Exception:
        memory.write_bytes(addr, bytearray(old))
        memory.write_bytes(id_addr, bytearray(old_id))
        raise
    return destination, users if shared else 1


def read_link_ground_query(reader, actor):
    """Read the engine's saved query origin and result, rather than recasting the render mesh."""
    data = bytes(reader.read_bytes(actor + 0x46C, 0x128))
    ground = struct.unpack_from('>f', data, 0x94)[0]
    origin = struct.unpack_from('>3f', data, 0xD4 + 0x24)
    poly, slot = struct.unpack_from('>2H', data, 0xD4 + 0x14)
    # Query pass-mask pointers are copied by SetExtChk from mAcch each frame.
    poly_pass, group_pass, actor_pid = struct.unpack_from('>3I', data, 0xD4)
    group_mask = 1
    link_pass = True
    if 0x80000000 <= group_pass < 0x81800000:
        group_mask = struct.unpack('>I', bytes(reader.read_bytes(group_pass + 4, 4)))[0]
    if 0x80000000 <= poly_pass < 0x81800000:
        link_pass = bool(bytes(reader.read_bytes(poly_pass + 6, 1))[0])
    proc = struct.unpack('>I', bytes(reader.read_bytes(actor + 0x31D8, 4)))[0]
    return dict(origin=origin, height=ground,
                floor=(slot, poly) if poly != 0xFFFF and slot < 256 and ground > -1e8 else None,
                flags=struct.unpack_from('>I', data, 0xD4 + 0x30)[0],
                origin_offset=struct.unpack_from('>f',data,0x98)[0]-struct.unpack_from('>f',data,0x8C)[0],
                enabled=not bool(struct.unpack_from('>I',data,0x28)[0]&3),
                actor_pid=actor_pid, group_mask=group_mask, link_pass=link_pass,
                gliding=proc == 0x93, procedure=proc,
                valid=(not bool(struct.unpack_from('>I',data,0x28)[0]&3) and
                       all(math.isfinite(v) and abs(v) < 1e8 for v in origin)))


def group_accepted(mesh, group, mask=1):
    """dBgW::ChkGrpThrough only filters the group at recursion depth two."""
    groups = mesh.get('groups', ())
    root = mesh.get('root_group', 0)
    seen = set()
    while 0 <= group < len(groups) and group != root and group not in seen:
        seen.add(group)
        parent, info = groups[group]
        if parent == root:
            return ((not info & 0x80700 and mask & 1) or
                    (info & 0x100 and mask & 2) or (info & 0x200 and mask & 4) or
                    (info & 0x400 and mask & 8) or (info & 0x80000 and mask & 0x10)) != 0
        group = parent
    return True


def ground_candidates(meshes, flags=3, group_mask=1, link_pass=True, actor_pid=None):
    """Accepted upward faces, including walls with ny>=0.014; no roof bucket."""
    result = []
    for slot, mesh in meshes.items():
        if actor_pid is not None and mesh.get('actor_id') == actor_pid:
            continue
        cache_key = (slot,flags,group_mask,link_pass,mesh.get('property_revision',0))
        cached = mesh.get('_ground_candidates')
        if cached and cached[0] == cache_key:
            result.extend(cached[1])
            continue
        begin = len(result)
        words = mesh.get('ti_words', ())
        normals = mesh.get('normals', ())
        for index, tri in enumerate(mesh['tris']):
            a,b,c = (mesh['verts'][v] for v in tri[:3])
            n = normals[index] if index < len(normals) else tri_normal(a,b,c)
            if n[1] < 0.014 or (n[1] >= 0.5 and not flags & 1) or (n[1] < 0.5 and not flags & 2):
                continue
            if link_pass and tri[3] < len(words) and words[tri[3]][3] & 4:
                continue
            if not group_accepted(mesh, tri[4], group_mask):
                continue
            # height(x,z) = ax + bz + c
            plane = (-n[0]/n[1], -n[2]/n[1], sum(n[i]*a[i] for i in range(3))/n[1])
            polygon = ground_footprint([(a[0],a[2]),(b[0],b[2]),(c[0],c[2])])
            if not polygon:
                continue
            result.append(dict(slot=slot, index=index, plane=plane, polygon=polygon,
                               bounds=(min(p[0] for p in polygon),min(p[1] for p in polygon),
                                       max(p[0] for p in polygon),max(p[1] for p in polygon)),
                               exit=mesh['exit_ids'][index]))
        mesh['_ground_candidates'] = (cache_key, result[begin:])
    return result


def height(plane, p):
    return plane[0]*p[0] + plane[1]*p[1] + plane[2]


def polygon_area(polygon):
    return sum(polygon[i][0]*polygon[(i+1)%len(polygon)][1] - polygon[(i+1)%len(polygon)][0]*polygon[i][1]
               for i in range(len(polygon))) * 0.5


def split_polygon(poly, plane, eps=1e-7):
    """Split an XZ convex polygon by ax+bz+c >= 0. Both outputs preserve winding."""
    values = [height(plane,p) for p in poly]
    if min(values) >= -eps:
        return list(poly), []
    if max(values) <= eps:
        return [], list(poly)
    inside, outside = [], []
    for i,a in enumerate(poly):
        b=poly[(i+1)%len(poly)]
        da,db=height(plane,a),height(plane,b)
        if da >= -eps: inside.append(a)
        if da <= eps: outside.append(a)
        if (da > eps and db < -eps) or (da < -eps and db > eps):
            t=da/(da-db)
            cross=(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t)
            inside.append(cross); outside.append(cross)
    def usable(p):
        return p if len(p)>=3 and abs(polygon_area(p))>1e-6 else []
    return usable(inside),usable(outside)


def clip_region(poly, planes):
    """Return intersection and disjoint outside pieces for a convex clipping region."""
    outside=[]
    for plane in planes:
        poly, part=split_polygon(poly,plane)
        if part: outside.append(part)
        if not poly: break
    return poly,outside


def footprint_planes(poly):
    sign=1 if polygon_area(poly)>0 else -1
    for i,a in enumerate(poly):
        b=poly[(i+1)%len(poly)]
        dx,dz=b[0]-a[0],b[1]-a[1]
        yield (-dz*sign,dx*sign,(dz*a[0]-dx*a[1])*sign)


def ground_footprint(triangle):
    """cM3d_CrossY_Tri_Front: exact XZ bounds, then edge products >= -20.

    The tolerance is an area, not a constant world-distance expansion. Clipping
    against the original bounding rectangle is essential near triangle corners.
    """
    x0,z0 = min(p[0] for p in triangle),min(p[1] for p in triangle)
    x1,z1 = max(p[0] for p in triangle),max(p[1] for p in triangle)
    if x0 == x1 or z0 == z1:
        return []
    bounds = [(x0,z0),(x1,z0),(x1,z1),(x0,z1)]
    planes = [(a,b,c+20.0) for a,b,c in footprint_planes(triangle)]
    return clip_region(bounds,planes)[0]


def first_ground_hit(candidates, point):
    best=None
    for face in candidates:
        y=height(face['plane'],(point[0],point[2]))
        if y >= point[1] or (best and y <= best[0]): continue
        if all(height(p,(point[0],point[2])) >= -1e-5 for p in footprint_planes(face['polygon'])):
            best=(y,face)
    return best


def exit_projection_cells(candidates, max_cells=12000, cache=None):
    """Exit regions capped by the next accepted floor; None ceiling means unbounded above.

    Exact planar footprint partitioning, so an intervening upward floor masks exits
    while a downward roof does not. Bounds are independent of viewer draw toggles.
    """
    output=[]
    grid, huge = {}, []
    cell_size = 1500.0
    def keys(bounds):
        x0,z0,x1,z1 = (int(v//cell_size) for v in bounds)
        if (x1-x0+1)*(z1-z0+1) > 64:
            return None
        return [(x,z) for x in range(x0,x1+1) for z in range(z0,z1+1)]
    for i,face in enumerate(candidates):
        cells=keys(face['bounds'])
        if cells is None:
            huge.append(i)
        else:
            for key in cells: grid.setdefault(key,[]).append(i)
    retained = set()
    for base in candidates:
        if base['exit'] == 0x3F: continue
        cells=[(base['polygon'],None)]
        x0,z0,x1,z1=base['bounds']
        cell_keys=keys(base['bounds'])
        indices=set(huge)
        if cell_keys is None:
            indices=range(len(candidates))
        else:
            for key in cell_keys: indices.update(grid.get(key,()))
            indices=sorted(indices)
        blockers=[]
        for blocker_index in indices:
            blocker=candidates[blocker_index]
            if blocker is base: continue
            bx0,bz0,bx1,bz1=blocker['bounds']
            if bx1<x0 or bx0>x1 or bz1<z0 or bz0>z1: continue
            higher=tuple(blocker['plane'][i]-base['plane'][i] for i in range(3))
            if max(height(higher,p) for p in base['polygon']) <= 1e-5: continue
            blockers.append(blocker)
        identity=(base['slot'],base['index'])
        retained.add(identity)
        dependencies=(base,*blockers)
        signature=tuple(id(face) for face in dependencies)
        previous=cache.get(identity) if cache is not None else None
        if previous is not None and previous[0] == signature:
            output.extend(previous[1])
            if len(output)>max_cells:
                raise ValueError('exit projection exceeds geometry budget')
            continue
        for blocker in blockers:
            higher=tuple(blocker['plane'][i]-base['plane'][i] for i in range(3))
            planes=list(footprint_planes(blocker['polygon']))+[higher]
            next_cells=[]
            for poly,ceiling in cells:
                constraints=planes
                if ceiling is not None:
                    constraints=planes+[tuple(ceiling[i]-blocker['plane'][i] for i in range(3))]
                inside,outside=clip_region(poly,constraints)
                next_cells.extend((part,ceiling) for part in outside)
                if inside: next_cells.append((inside,blocker['plane']))
            cells=next_cells
            if len(output)+len(cells)>max_cells:
                raise ValueError('exit projection exceeds geometry budget; decrease loaded collision or use raw zones')
        generated=[dict(base=base,polygon=poly,ceiling=ceiling) for poly,ceiling in cells]
        output.extend(generated)
        if cache is not None:
            # Keep dependency references alive so Python cannot recycle IDs into
            # a false cache hit after moving collision or a room change.
            cache[identity]=(signature,generated,dependencies)
    if cache is not None:
        for identity in list(cache):
            if identity not in retained:
                del cache[identity]
    return output


def exit_slice_polygons(cells, query_height):
    """Horizontal accepted exit regions at the engine query's Y coordinate."""
    result=[]
    for cell in cells:
        a,b,c=cell['base']['plane']
        above=(-a,-b,query_height-c)
        poly=cell['polygon']
        if max(height(above,p) for p in poly)<=0:
            continue  # the floor must be strictly below the query
        constraints=[above]
        if cell['ceiling'] is not None:
            a,b,c=cell['ceiling']
            constraints.append((a,b,c-query_height))
        poly,_outside=clip_region(poly,constraints)
        if poly:
            result.append(poly)
    return result
