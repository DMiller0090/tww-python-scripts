"""Independent dCcD shape offsets. Actor transforms are never written.

Offsets are re-applied when cCcS::Set registers the shape, before CalcArea and
collision tests. Repeated At/Tg/Co registrations never compound translation.
"""
import math
import struct

from .collider import finite_vec, valid_ptr, u32, vec3

POINTS = {'sphere': (0x118,), 'cylinder': (0x118,),
          'capsule': (0x118, 0x124), 'triangle': (0x12C, 0x138, 0x144),
          'box': (0x0F8, 0x104)}


def player_rotate(point, angles, inverse=False):
    """Link's shape_angle uses the game's Y*X*Z rotation matrix."""
    operations=((2,angles[2]),(0,angles[0]),(1,angles[1]))
    if inverse:operations=tuple((axis,-angle) for axis,angle in reversed(operations))
    x,y,z=point
    for axis,angle in operations:
        radians=angle*math.tau/65536.0;c,s=math.cos(radians),math.sin(radians)
        if axis==0:y,z=c*y-s*z,s*y+c*z
        elif axis==1:x,z=c*x+s*z,-s*x+c*z
        else:x,y=c*x-s*y,s*x+c*y
    return x,y,z


def translated_shape(data, kind, delta):
    if kind not in POINTS or not finite_vec(delta):
        raise ValueError('unsupported shape or invalid translation')
    result = bytearray(data)
    for offset in POINTS[kind]:
        point = tuple(v+d for v,d in zip(vec3(data,offset),delta))
        if not finite_vec(point):
            raise ValueError('translated shape is outside supported coordinates')
        struct.pack_into('>3f', result, offset, *point)
    # Mirror CalcAabBox rather than translating a stale previous-frame AABB.
    if kind != 'box':
        points = [vec3(result, offset) for offset in POINTS[kind]]
        minimum = [min(p[i] for p in points) for i in range(3)]
        maximum = [max(p[i] for p in points) for i in range(3)]
        if kind in ('sphere','capsule'):
            radius = struct.unpack_from('>f',result,0x124 if kind=='sphere' else 0x134)[0]
            minimum = [v-radius for v in minimum]
            maximum = [v+radius for v in maximum]
        elif kind == 'cylinder':
            radius, cylinder_height = struct.unpack_from('>2f',result,0x124)
            minimum[0] -= radius; minimum[2] -= radius
            maximum[0] += radius; maximum[2] += radius; maximum[1] += cylinder_height
        struct.pack_into('>6f',result,0x0F8,*minimum,*maximum)
    if kind == 'triangle':
        # cM3dGPla mNormal @+0x118, mD @+0x124; translation preserves normal.
        normal=vec3(data,0x118)
        distance=struct.unpack_from('>f',data,0x124)[0]
        if not finite_vec(normal) or not math.isfinite(distance):
            raise ValueError('triangle plane is invalid')
        struct.pack_into('>f',result,0x124,distance-sum(normal[i]*delta[i] for i in range(3)))
    return bytes(result)


def shape_bytes(data,kind):
    offsets = list(POINTS[kind])
    if kind=='triangle': offsets += [0x124]  # include plane distance in idempotence check
    return b''.join(data[o:o+(4 if o==0x124 and kind=='triangle' else 12)] for o in offsets)


def shape_center(collider):
    kind,_owner,*geometry=collider
    if kind=='cylinder':
        bottom,_radius,height=geometry
        return (bottom[0],bottom[1]+height*0.5,bottom[2])
    if kind=='capsule':
        return tuple((geometry[0][i]+geometry[1][i])*0.5 for i in range(3))
    if kind=='triangle':
        return tuple(sum(p[i] for p in geometry)/3 for i in range(3))
    return geometry[0]


class ColliderEdits:
    def __init__(self, memory, decoder, allow_ownerless=False):
        self.memory, self.decoder = memory, decoder
        self.allow_ownerless=allow_ownerless
        self.offsets = {}
        self.flag_locks = {}
        self.addresses = {}
        self._address_keys = {}
        self.selected_identity = None
        self.selected = None
        self.drag = None
        self.last_error = ''

    def track(self,address,data,collider):
        if collider is not None:
            old = self._address_keys.get(address)
            if old != collider and self.addresses.get(old) == address:
                self.addresses.pop(old, None)
            self._address_keys[address] = collider
            self.addresses[collider]=address
        return collider

    def identity(self,data,collider):
        owner=collider[1]
        if not valid_ptr(owner):
            status=u32(data,0x44)
            if self.allow_ownerless and owner==0 and valid_ptr(status) and not status&3 and status<=0x817FFFE4:
                info=bytes(self.memory.read_bytes(status,0x1C))
                if u32(info,0x0C)==0 and u32(info,0x10)==0xFFFFFFFF:
                    return (0,0xFFFFFFFF,status,u32(data,0x114),collider[0])
            raise ValueError('collider has no validated actor owner')
        pid=u32(bytes(self.memory.read_bytes(owner+4,4)),0)
        return (owner,pid,u32(data,0x44),u32(data,0x114),collider[0])

    def read(self,address):
        raw=bytes(self.memory.read_bytes(address,0x150))
        collider=self.decoder.decode(raw)
        if collider is None: raise ValueError('collider is no longer valid')
        return raw,collider,self.identity(raw,collider)

    def translate(self,address,delta):
        raw,collider,identity=self.read(address)
        record=self.offsets.get(address)
        if record and record['identity'] != identity:
            self.offsets.pop(address,None)
            raise ValueError('collider owner changed')
        offset=tuple(delta[i]+(record['offset'][i] if record else 0.0) for i in range(3))
        updated=translated_shape(raw,collider[0],delta)
        self._write_shape(address,raw,updated,collider[0])
        frozen=record.get('frozen') if record else None
        if frozen is not None:
            frozen=tuple(frozen[i]+delta[i] for i in range(3))
        following=record.get('follow') if record else None
        if following is not None:
            following=dict(following,relative=tuple(following['relative'][i]+delta[i] for i in range(3)))
            if 'local_points' in following:
                header=bytes(self.memory.read_bytes(following['actor']+0x20C,6))
                local_delta=player_rotate(delta,struct.unpack('>3h',header),inverse=True)
                following['local_points']=tuple(tuple(p[i]+local_delta[i] for i in range(3)) for p in following['local_points'])
        updated_record=dict(record or {})
        updated_record.update(identity=identity,offset=offset,last=shape_bytes(updated,collider[0]),
                              frozen=frozen,follow=following)
        self.offsets[address]=updated_record
        return self.decoder.decode(updated)

    def toggle_freeze(self,address):
        self.apply_registration(address)
        raw,collider,identity=self.read(address)
        record=self.offsets.get(address)
        if record is None:
            self.translate(address,(0,0,0))
            record=self.offsets[address]
        if record['identity'] != identity:
            raise ValueError('collider owner changed')
        record['frozen']=None if record.get('frozen') is not None else shape_center(collider)
        if record['frozen'] is not None:record['follow']=None
        return record['frozen'] is not None

    def toggle_follow_player(self,address,player_pointer):
        self.apply_registration(address)
        raw,shape,identity=self.read(address)
        record=self.offsets.get(address)
        if record and record.get('follow') is not None:
            record['follow']=None;return False
        player=u32(bytes(self.memory.read_bytes(player_pointer,4)),0)
        if not valid_ptr(player):raise ValueError('Link is unavailable')
        header=bytes(self.memory.read_bytes(player,0x212))
        if struct.unpack_from('>H',header,8)[0]!=0xA9:raise ValueError('Player pointer is not PROC_PLAYER')
        position=vec3(header,0x1F8)
        if not finite_vec(position):raise ValueError('Link position is invalid')
        if record is None:
            self.translate(address,(0,0,0));record=self.offsets[address]
        record['follow']=dict(pointer=player_pointer,actor=player,pid=u32(header,4),
                              relative=tuple(shape_center(shape)[i]-position[i] for i in range(3)))
        if shape[0]=='capsule':
            angles=struct.unpack_from('>3h',header,0x20C)
            record['follow']['local_points']=tuple(player_rotate(
                tuple(vec3(raw,offset)[i]-position[i] for i in range(3)),angles,inverse=True)
                for offset in POINTS['capsule'])
            record['rotation_source']=raw
        record['frozen']=None
        return True

    def watch_addresses(self,address):
        _raw,collider,_identity=self.read(address)
        first=address+POINTS[collider[0]][0]
        return (first,first+4,first+8)

    def toggle_flag_lock(self,address,kind):
        if kind not in ('state','hit','registration'):raise ValueError('Unknown collider lock')
        raw,shape,identity=self.read(address)
        if kind=='registration' and not identity[0]:
            raise ValueError('Registration lock requires an actor owner to validate its lifetime')
        record=self.flag_locks.get(address)
        if record is None or record['identity']!=identity:
            roles=tuple(o for o in (0,0x18,0x2C) if u32(raw,o)&1)
            if not roles:raise ValueError('Collider has no enabled AT/TG/CO roles to lock')
            record=dict(identity=identity,roles=roles,state=False,hit=False,registration=False)
            self.flag_locks[address]=record
        record[kind]=not record[kind]
        enabled=record[kind]
        if not any(record.get(k) for k in ('state','hit','registration')):self.flag_locks.pop(address,None)
        self.apply_flag_locks(address)
        return enabled

    def flag_watch_addresses(self):
        return {address+offset+(4 if kind=='hit' else 0)
                for address,record in self.flag_locks.items()
                for kind in ('state','hit') if record[kind] or (kind=='state' and record.get('registration'))
                for offset in record['roles']}

    def apply_flag_locks(self,address):
        record=self.flag_locks.get(address)
        if record is None:return
        try:
            raw,_shape,identity=self.read(address)
            if identity!=record['identity']:
                self.flag_locks.pop(address,None);return
            for kind in ('state','hit'):
                if not (record[kind] or (kind=='state' and record.get('registration'))):continue
                for offset in record['roles']:
                    field=offset+(4 if kind=='hit' else 0)
                    current=u32(raw,field)
                    if not current&1:
                        self.memory.write_bytes(address+field,bytearray(struct.pack('>I',current|1)))
                        if u32(bytes(self.memory.read_bytes(address+field,4)),0)!=(current|1):
                            raise ValueError('Collider flag lock readback failed')
        except Exception as exc:
            self.last_error=str(exc);self.flag_locks.pop(address,None)

    def apply_registrations(self,base,owners):
        """Append once before CalcArea, matching cCcS::Set's bounded arrays."""
        for address,record in list(self.flag_locks.items()):
            if not record.get('registration'):continue
            try:
                _raw,_shape,identity=self.read(address)
                if identity!=record['identity'] or owners.get(identity[0])!=identity[1]:
                    self.flag_locks.pop(address,None);continue
                # All arrays must have space before publishing any pointer/count.
                arrays=[(0x1400,0x280C,1280)]
                arrays += [(start,count,limit) for role,start,count,limit in
                           ((0,0,0x2800,256),(0x18,0x400,0x2804,768),(0x2C,0x1000,0x2808,256))
                           if role in record['roles']]
                planned=[]
                for start,count,limit in arrays:
                    n=u32(bytes(self.memory.read_bytes(base+count,4)),0)
                    if n>limit:raise ValueError('Invalid collider registry count')
                    pointers=bytes(self.memory.read_bytes(base+start,n*4))
                    if any(u32(pointers,i*4)==address for i in range(n)):continue
                    if n==limit:raise ValueError('Collider registry is full')
                    planned.append((base+start+n*4,base+count,n))
                self.apply_flag_locks(address)
                if address not in self.flag_locks:continue
                self.apply_registration(address)
                before=[]
                try:
                    for slot,count,n in planned:
                        for site,value in ((slot,address),(count,n+1)):
                            old=bytes(self.memory.read_bytes(site,4));before.append((site,old))
                            data=struct.pack('>I',value)
                            self.memory.write_bytes(site,bytearray(data))
                            if bytes(self.memory.read_bytes(site,4))!=data:
                                raise ValueError('Registration write readback failed')
                except Exception:
                    for site,old in reversed(before):self.memory.write_bytes(site,bytearray(old))
                    raise
            except Exception as exc:
                self.last_error=str(exc);self.flag_locks.pop(address,None)

    def _write_shape(self,address,raw,updated,kind):
        # Write only geometric fields, never hit flags, status, owner or actor memory.
        spans=[(0x0F8,24)] + [(o,12) for o in POINTS[kind] if o not in (0x0F8,0x104)]
        if kind=='triangle': spans.append((0x124,4))
        done=[]
        try:
            for offset,size in spans:
                if raw[offset:offset+size] != updated[offset:offset+size]:
                    done.append((offset,size))
                    self.memory.write_bytes(address+offset,bytearray(updated[offset:offset+size]))
            for offset,size in done:
                if bytes(self.memory.read_bytes(address+offset,size)) != updated[offset:offset+size]:
                    raise ValueError('collider geometry write verification failed')
        except Exception:
            for offset,size in reversed(done):
                self.memory.write_bytes(address+offset,bytearray(raw[offset:offset+size]))
            raise

    def apply_registration(self,address):
        record=self.offsets.get(address)
        if record is None: return
        try:
            raw,collider,identity=self.read(address)
            if identity != record['identity']:
                self.offsets.pop(address,None)
                return
            unchanged=shape_bytes(raw,collider[0]) == record['last']
            following=record.get('follow')
            if unchanged and following is None:
                return
            offset=record['offset']
            if following is not None:
                player=following['actor']
                if u32(bytes(self.memory.read_bytes(following['pointer'],4)),0)!=player:
                    raise ValueError('Link was replaced; position lock released')
                header=bytes(self.memory.read_bytes(player,0x212))
                if u32(header,4)!=following['pid'] or struct.unpack_from('>H',header,8)[0]!=0xA9:
                    raise ValueError('Link identity changed; position lock released')
                position=vec3(header,0x1F8)
                if not finite_vec(position):raise ValueError('Link position is invalid')
                center=shape_center(collider)
                offset=tuple(position[i]+following['relative'][i]-center[i] for i in range(3))
            elif record.get('frozen') is not None:
                center=shape_center(collider)
                offset=tuple(record['frozen'][i]-center[i] for i in range(3))
            updated=translated_shape(raw,collider[0],offset)
            if following is not None and collider[0]=='capsule':
                if not unchanged:record['rotation_source']=raw
                angles=struct.unpack_from('>3h',header,0x20C)
                updated=bytearray(raw)
                for field,point in zip(POINTS['capsule'],following['local_points']):
                    rotated=player_rotate(point,angles)
                    struct.pack_into('>3f',updated,field,*(position[i]+rotated[i] for i in range(3)))
                updated=translated_shape(updated,'capsule',(0,0,0))
                offset=tuple((vec3(updated,0x118)[i]+vec3(updated,0x124)[i])*0.5-center[i] for i in range(3))
            self._write_shape(address,raw,updated,collider[0])
            record['offset']=(tuple(record['offset'][i]+offset[i] for i in range(3))
                              if following is not None and unchanged else offset)
            record['last']=shape_bytes(updated,collider[0])
        except Exception as exc:
            self.last_error=str(exc)
            self.offsets.pop(address,None)

    def restore(self,address):
        record=self.offsets.get(address)
        if record is None: return
        raw,collider,identity=self.read(address)
        if identity == record['identity'] and shape_bytes(raw,collider[0]) == record['last']:
            if collider[0]=='capsule' and 'rotation_source' in record:
                updated=bytearray(raw)
                for field in POINTS['capsule']:updated[field:field+12]=record['rotation_source'][field:field+12]
                updated=translated_shape(updated,'capsule',(0,0,0))
            else:updated=translated_shape(raw,collider[0],tuple(-v for v in record['offset']))
            self._write_shape(address,raw,updated,collider[0])
        self.offsets.pop(address,None)

    def clear(self,restore=False):
        self.flag_locks.clear()
        if restore:
            for address in list(self.offsets):
                try: self.restore(address)
                except Exception: pass
        else:
            self.offsets.clear()
        self.addresses.clear()
        self._address_keys.clear()
        self.selected_identity = None
        self.selected=self.drag=None


def ray_hit(origin, direction, shape):
    """Nearest forward ray/shape intersection for picking the visible collider itself."""
    def dot(a,b): return sum(x*y for x,y in zip(a,b))
    def sub(a,b): return tuple(x-y for x,y in zip(a,b))
    def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    def sphere(center,radius):
        offset=sub(origin,center)
        b=dot(offset,direction); c=dot(offset,offset)-radius*radius
        disc=b*b-c
        if disc<0: return None
        near,far=-b-math.sqrt(disc),-b+math.sqrt(disc)
        return max(0.0,near) if far>=0 else None
    kind,_owner,*geometry=shape
    if kind=='sphere': return sphere(*geometry)
    if kind=='box':
        center,half=geometry; lo,hi=0.0,float('inf')
        for i in range(3):
            if abs(direction[i])<1e-9:
                if abs(origin[i]-center[i])>half[i]: return None
            else:
                a,b=sorted(((center[i]-half[i]-origin[i])/direction[i],(center[i]+half[i]-origin[i])/direction[i]))
                lo,hi=max(lo,a),min(hi,b)
                if lo>hi: return None
        return lo
    if kind=='triangle':
        a,b,c=geometry; e1,e2=sub(b,a),sub(c,a)
        p=cross(direction,e2); det=dot(e1,p)
        if abs(det)<1e-9: return None
        q=sub(origin,a); u=dot(q,p)/det
        r=cross(q,e1); v=dot(direction,r)/det
        distance=dot(e2,r)/det
        return distance if u>=0 and v>=0 and u+v<=1 and distance>=0 else None
    if kind=='cylinder':
        bottom,radius,height=geometry
        start=sub(origin,bottom); hits=[]
        a=direction[0]**2+direction[2]**2
        b=start[0]*direction[0]+start[2]*direction[2]
        c=start[0]**2+start[2]**2-radius**2
        if c<=0 and 0<=start[1]<=height: return 0.0
        disc=b*b-a*c
        if a>1e-9 and disc>=0:
            for t in ((-b-math.sqrt(disc))/a,(-b+math.sqrt(disc))/a):
                if t>=0 and 0<=start[1]+t*direction[1]<=height: hits.append(t)
        if abs(direction[1])>1e-9:
            for y in (0,height):
                t=(y-start[1])/direction[1]
                if t>=0 and (start[0]+t*direction[0])**2+(start[2]+t*direction[2])**2<=radius**2: hits.append(t)
        return min(hits) if hits else None
    if kind=='capsule':
        start,end,radius=geometry; axis=sub(end,start); relative=sub(origin,start)
        aa=dot(axis,axis)
        if aa<1e-9: return sphere(start,radius)
        ad=dot(axis,direction); ar=dot(axis,relative); dr=dot(direction,relative)
        a=aa-ad*ad; b=aa*dr-ar*ad
        c=aa*dot(relative,relative)-ar*ar-radius*radius*aa
        hits=[]
        if c<=0 and 0<=ar<=aa: return 0.0
        disc=b*b-a*c
        if a>1e-9 and disc>=0:
            t=(-b-math.sqrt(disc))/a
            if t>=0 and 0<=ar+t*ad<=aa: hits.append(t)
        for endpoint in (start,end):
            t=sphere(endpoint,radius)
            if t is not None: hits.append(t)
        return min(hits) if hits else None
    return None
