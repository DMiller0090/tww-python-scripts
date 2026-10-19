"""Expandable collider properties and position/value locks for TWW Inspector."""
import struct

from .actor_tree import GAME_INFO, PLAY_OFFSET
from .collider import ColliderDecoder, finite_vec, attack_type_names, target_type_name, physical_collision_name
from .collider_edit import ColliderEdits, shape_center, translated_shape
from .collider_properties import field_specs, raw_values, read_properties, write_property
from .hit_watch import HitWatch, NOEXEC, NODRAW

# cCcS::mpObjAt / mpObjTg / mpObjCo in c_cc_s.h (gameInfo.play.mCcS).
ROLE_CAPACITIES = {'at':256,'tg':768,'co':256}


class ColliderView:
    def __init__(self,memory,names=None):
        self.memory=memory
        self.discovery=HitWatch(memory,names,allow_ownerless=True)  # discovery helpers only
        self.edits=ColliderEdits(memory,ColliderDecoder(memory,memory.read_u32),allow_ownerless=True)
        self.records={};self.open=set();self.scanned=set();self.selected=None
        self.registry_addresses=set();self.registry_roles={};self.registry_counts=None
        self.registry_site=self.registry_base=self.registry_debug=None
        self.containers={};self.registry_captures=0;self.diagnostics={}
        self.raw_edits={}  # Preserve the selected layout after deliberate corruption.
        self.locks={};self.lock_sites=[];self.lock_debug=self.lock_base=None
        self.lock_key=()

    def name(self,record,role='at'):
        # Share Collision Viewer's refactored names and compact target-mask format.
        if role=='tg':return target_type_name(record['values']['tg_type'])
        if role=='co':return physical_collision_name(record['values']['co_sprm'])
        return '/'.join(attack_type_names(record['values']['at_type'])) or 'NONE'

    def reset(self):
        self.records.clear();self.open.clear();self.scanned.clear();self.selected=None
        self.discovery.reset()
        self.registry_addresses.clear();self.registry_roles.clear();self.registry_counts=None
        self.containers.clear();self.registry_captures=0;self.diagnostics={}
        self.raw_edits.clear()
        self.clear_locks()

    def clear_locks(self):
        self.locks.clear();self.close_lock_watch()

    def close_lock_watch(self):
        for site in self.lock_sites:self.lock_debug.remove_memory_breakpoint(site)
        self.lock_sites=[];self.lock_debug=self.lock_base=None
        self.lock_key=()

    def sync_lock_watch(self,debug,supported,version):
        base=GAME_INFO.get(version)
        base=base+PLAY_OFFSET+0x1404 if base is not None else None
        key=tuple(sorted(self.locks))
        if self.lock_sites and (not self.locks or base!=self.lock_base or key!=self.lock_key):self.close_lock_watch()
        if not self.locks or base is None or not supported or self.lock_sites:return
        if not callable(getattr(debug,'set_memory_watch',None)):return
        self.lock_debug,self.lock_base=debug,base
        self.lock_key=key
        try:
            # Separate cleanup keys from registry/Viewer hooks. Role
            # count writes occur inside Set, before CalcArea/contact processing.
            for offset,count,array,limit in ((0x2803,0x2800,0,256),(0x2806,0x2804,0x400,768),(0x280A,0x2808,0x1000,256)):
                index='read_u32(0x%X)' % (base+count)
                pointer='read_u32(0x%X+4*%s)' % (base+array,index)
                matches=' || '.join('%s == 0x%X' % (pointer,a) for a in key)
                condition='%s < %d && (%s)' % (index,limit,matches)
                debug.set_memory_watch(dict(At=base+offset,WatchOnRead=False,WatchOnWrite=True,Condition=condition))
                self.lock_sites.append(base+offset)
        except Exception:
            self.close_lock_watch();raise

    def on_lock_access(self,is_write,address,value,tree):
        if not is_write or self.lock_base is None or not 0<value<=768:return
        for count,offset,limit in ((0x2800,0,256),(0x2804,0x400,768),(0x2808,0x1000,256)):
            if address==self.lock_base+count and value<=limit:
                collider=self.memory.read_u32(self.lock_base+offset+4*(value-1))
                if collider in self.locks:self.apply_locks(tree,collider)
                return

    def is_locked(self,address,field=None):
        return ('position' if field is None else field) in self.locks.get(address,{}).get('values',{})

    def _capture_lock(self,record,field):
        if field=='position':
            _raw,shape,_identity=self.edits.read(record['address'])
            return dict(center=shape_center(shape))
        offset,fmt,_writable,shift,width=record['fields'][field]
        status=field.startswith('status.')
        base=record['values']['status_pointer'] if status else record['address']
        if status and record['values'][field] is None:raise ValueError('Status pointer is unreadable')
        data=bytes(self.memory.read_bytes(base+offset,struct.calcsize('>'+fmt)))
        return dict(target=base+offset,data=data,fmt=fmt,shift=shift,width=width,status=base if status else None)

    def toggle_lock(self,address,field,tree):
        field='position' if field is None else field
        group=self.locks.get(address)
        if group and field in group['values']:
            del group['values'][field]
            if not group['values']:self.locks.pop(address,None)
            if not self.locks:self.close_lock_watch()
            return 'Unlocked collider '+field
        previous=self.records[address];record=self._read(address,tree,previous['identity'])
        if field!='position' and field not in record['fields']:raise ValueError('Lock a stored field or position')
        value=self._capture_lock(record,field)
        if group is None:
            container=self.containers.get(address)
            if container is None and not record['ownerless']:container=(record['actor']['pid'],record['actor']['addr'])
            group=self.locks[address]=dict(identity=record['identity'],container=container,record=record,values={})
        group['values'][field]=value
        return 'Locked collider %s; reapplied every frame and on registration' % field

    def apply_locks(self,tree,only=None):
        for address,group in list(self.locks.items()):
            if only is not None and address!=only:continue
            try:
                host=group['container']
                if host is not None:
                    actor=tree.by_pid.get(host[0])
                    if actor is None or actor['addr']!=host[1] or tree._u32(host[1]+4)!=host[0]:
                        raise ValueError('Collider storage actor disappeared')
                try:record=self._read(address,tree,group['identity'])
                except Exception:
                    # Actor code can temporarily disable geometry or overwrite
                    # an identity field that is itself locked. Validate the
                    # remaining identity independently of drawable geometry.
                    owner,pid,status,vtable,kind=group['identity']
                    overridden={lock['target'] for name,lock in group['values'].items() if name!='position'}
                    for target,expected in ((address+0x44,status),(address+0x114,vtable),
                                            (status+0xC,owner),(status+0x10,pid)):
                        if target not in overridden and tree._u32(target)!=expected:
                            raise ValueError('Collider identity changed')
                    _raw,values=raw_values(self.edits,address,kind)
                    record=dict(group['record'],values=values)
                if (record['ownerless'] and host is None and not record.get('raw_edit') and
                        self.registry_counts is not None and address not in self.registry_addresses):
                    raise ValueError('Ownerless allocation no longer registered')
                for field,lock in group['values'].items():
                    if field=='position':continue
                    if lock['status'] is not None and tree._u32(address+0x44)!=lock['status']:
                        raise ValueError('Shared status pointer changed')
                    target,data=lock['target'],lock['data']
                    old=bytes(self.memory.read_bytes(target,len(data)))
                    if lock['width'] is not None:
                        mask=((1<<lock['width'])-1)<<lock['shift']
                        fmt='>'+lock['fmt']
                        data=struct.pack(fmt,(struct.unpack(fmt,old)[0]&~mask)|(struct.unpack(fmt,data)[0]&mask))
                    if old!=data:self.memory.write_bytes(target,bytearray(data))
                if 'position' in group['values']:
                    raw,shape,_identity=self.edits.read(address)
                    center=shape_center(shape);fixed=group['values']['position']['center']
                    updated=translated_shape(raw,shape[0],tuple(fixed[i]-center[i] for i in range(3)))
                    self.edits._write_shape(address,raw,updated,shape[0])
            except Exception:
                self.locks.pop(address,None)
        if not self.locks:self.close_lock_watch()

    def sync_registry_watch(self,debug,supported,enabled,version):
        base=GAME_INFO.get(version)
        base=base+PLAY_OFFSET+0x1404 if base is not None else None
        if self.registry_site is not None and (not enabled or base!=self.registry_base):self.close_registry_watch()
        if enabled and base is not None and supported and self.registry_site is None and callable(getattr(debug,'set_memory_watch',None)):
            # Third byte of the counter has its own cleanup key; a word store
            # overlaps it. Viewer uses byte zero.
            debug.set_memory_watch(dict(At=base+0x2802,WatchOnRead=False,WatchOnWrite=True))
            self.registry_site,self.registry_base,self.registry_debug=base+0x2802,base,debug

    def close_registry_watch(self):
        if self.registry_site is not None:self.registry_debug.remove_memory_breakpoint(self.registry_site)
        self.registry_site=self.registry_base=self.registry_debug=None

    def capture_registry(self):
        base=self.registry_base
        if base is None:return
        counts=struct.unpack('>4I',bytes(self.memory.read_bytes(base+0x2800,16)))
        if any(n>limit for n,limit in zip(counts,(256,768,256,1280))):return
        addresses=set();roles={}
        for role,offset,count in zip(('at','tg','co','all'),(0,0x400,0x1000,0x1400),counts):
            data=bytes(self.memory.read_bytes(base+offset,count*4))
            members={a for a in struct.unpack('>%dI' % count,data) if 0x80000000<=a<=0x817FFEB0 and not a&3}
            addresses.update(members)
            if role!='all':roles[role]=members
        self.registry_addresses=addresses;self.registry_roles=roles
        self.registry_counts=dict(zip(('at','tg','co'),counts[:3]))
        self.registry_captures+=1

    @staticmethod
    def state(record,role):
        """Sampled hit result and owner gates; clear flags do not prove execution."""
        values,context=record['values'],record['context']
        hit=bool(values[role+'_hit']) if role in ROLE_CAPACITIES else any(values[r+'_hit'] for r in ROLE_CAPACITIES)
        if record.get('raw_edit'):
            return dict(hit=hit,hit_text='Hit' if hit else 'No hit',owner='Raw memory edit',blocked=False,
                        detail='Raw memory editing with the original layout; collider validity is not assumed.')
        if record.get('ownerless'):
            return dict(hit=hit,hit_text='Hit' if hit else 'No hit',owner='No owner',blocked=False,
                        detail='%s: %s; owner execution/draw state is unavailable (no actor owner).' %
                        (role.upper(),'hit recorded' if hit else 'no hit recorded'))
        condition=context.get('condition');pause=context.get('process_pause');init=context.get('init_state')
        exec_paused=pause is not None and bool(pause&1)
        draw_paused=pause is not None and bool(pause&2)
        exec_skipped=condition is not None and bool(condition&NOEXEC)
        draw_skipped=condition is not None and bool(condition&NODRAW)
        not_executing=init is not None and init!=2  # fpcEx_Execute only accepts state 2.
        execution=not_executing or exec_paused or exec_skipped
        drawing=draw_paused or draw_skipped
        if execution and drawing:owner='No exec/draw'
        elif not_executing:owner='Not executing'
        elif exec_paused:owner='Execution paused'
        elif exec_skipped:owner='No exec'
        elif draw_paused:owner='Drawing paused'
        elif draw_skipped:owner='No draw'
        elif role=='inactive':owner='Roles disabled'
        elif any(v is None for v in (condition,pause,init)):owner='Owner unknown'
        else:owner='No skips'
        reasons=[]
        if not_executing:reasons.append('owner init state %d is not executable (requires 2)' % init)
        if exec_paused:reasons.append('owner execution pause enabled')
        if draw_paused:reasons.append('owner draw pause enabled')
        if exec_skipped:reasons.append('owner execute-skip condition set')
        if draw_skipped:reasons.append('owner draw-skip condition set')
        if not reasons:reasons.append('owner flags do not prove execution or drawing this frame')
        hit_text='Hit' if hit else 'No hit'
        return dict(hit=hit,hit_text=hit_text,owner=owner,blocked=bool(execution or drawing),
                    detail='%s: %s; %s.' % (role.upper(),hit_text,'; '.join(reasons)))

    def teleport(self,tree,to_collider):
        if not self.selected or self.selected[0] not in self.records:
            raise ValueError('Select a collider first')
        address=self.selected[0];previous=self.records[address]
        record=self._read(address,tree,previous['identity'])
        if record.get('ownerless') and not to_collider:
            raise ValueError('Ownerless geometry is read-only; Link can still teleport to it')
        players=[a for a in tree.actors if a['kind']=='actor' and
                 (a['name'].upper() in ('PLAYER','PROC_PLAYER') or a.get('cls')=='daPy_lk_c')]
        if len(players)!=1:raise ValueError('Link is not uniquely present in the loaded process list')
        player=players[0];base=player['addr']
        if tree._u32(base+4)!=player['pid']:raise ValueError('Link process changed; try again')
        position=struct.unpack('>3f',bytes(self.memory.read_bytes(base+0x1F8,12)))
        center=shape_center(record['shape'])
        if not finite_vec(position) or not finite_vec(center):raise ValueError('Invalid Link or collider position')
        if to_collider:
            # Match current and previous position to avoid a swept jump from the old location.
            payload=struct.pack('>3f',*center)
            backups={base+offset:bytes(self.memory.read_bytes(base+offset,12)) for offset in (0x1E4,0x1F8)}
            written=[]
            try:
                for target in backups:
                    written.append(target);self.memory.write_bytes(target,bytearray(payload))
                    if bytes(self.memory.read_bytes(target,12))!=payload:raise ValueError('Link position write failed')
            except Exception:
                for target in reversed(written):self.memory.write_bytes(target,bytearray(backups[target]))
                raise
            return 'Link moved to collider center (%.3f, %.3f, %.3f).' % center
        raw,shape,identity=self.edits.read(address)
        if identity!=record['identity']:raise ValueError('Collider changed; select it again')
        delta=tuple(position[i]-shape_center(shape)[i] for i in range(3))
        updated=translated_shape(raw,shape[0],delta)
        self.edits._write_shape(address,raw,updated,shape[0])
        self.records[address]=self._read(address,tree,identity)
        return 'Collider moved to Link; actor updates may move it again.'

    def _read(self,address,tree,identity=None):
        if address in self.raw_edits:
            original=self.raw_edits[address]
            if identity is not None and identity!=original['identity']:raise ValueError('Collider selection changed')
            raw,values=raw_values(self.edits,address,original['shape'][0])
            record=dict(original,values=values,raw_edit=True)
            return record
        raw,shape,current,values=read_properties(self.edits,address,identity)
        actor=tree.by_pid.get(current[1])
        ownerless=current[0]==0 and current[1]==0xFFFFFFFF
        if ownerless:
            actor=dict(pid=0xFFFFFFFF,addr=0,name='No owner',kind='ownerless',proc=None)
        elif actor is None or actor['addr']!=current[0] or values['status_pid']!=current[1]:
            raise ValueError('Collider owner is no longer a loaded process')
        self.discovery.version=tree.version
        return dict(address=address,identity=current,shape=shape,values=values,fields=field_specs(shape[0]),actor=actor,ownerless=ownerless,
                    context=self.discovery.context(actor['addr']))

    def select_address(self,address,tree):
        record=self._read(address,tree)
        self.records[address]=record;self.open.add(address);self.selected=(address,None)
        return record

    def refresh(self,tree):
        live={(a['pid'],a['addr']) for a in tree.actors if a['kind']=='actor'}
        self.scanned.intersection_update(live)
        self.containers={a:host for a,host in self.containers.items() if host in live}
        for address in list(self.raw_edits):
            host=self.raw_edits[address].get('raw_container')
            if host is not None and host not in live:
                self.raw_edits.pop(address,None);self.records.pop(address,None)
        if live and live.issubset(self.scanned):self.scanned.clear()
        candidates=set(self.records)
        candidates.update(self.registry_addresses)
        # Counts normally reset before the host callback. Stale registry slots
        # are only candidates: current owner/PID/shape checks below are required.
        if tree.version in GAME_INFO:
            base=GAME_INFO[tree.version]+PLAY_OFFSET+0x1404
            try:
                # Include retained AT/TG/CO slots too; mpObj alone can be full
                # or overwritten before a still-live subobject is inspected.
                data=bytes(self.memory.read_bytes(base,0x2800))
                candidates.update(a for a in struct.unpack('>2560I',data) if 0x80000000<=a<=0x817FFEB0 and not a&3)
            except Exception:pass
        budget=8
        for actor in sorted(tree.actors,key=lambda a:a['pid'],reverse=True):
            identity=(actor['pid'],actor['addr'])
            if actor['kind']!='actor' or identity in self.scanned or not budget:continue
            found,_=self.discovery.find_colliders(actor['addr'],actor['pid'],actor['proc'])
            for address in found:self.containers[address]=identity
            candidates.update(found);self.scanned.add(identity);budget-=1
        records={};rejected=[]
        for address in candidates:
            previous=self.records.get(address)
            try:
                record=self._read(address,tree,previous['identity'] if previous else None)
                if (record.get('ownerless') and not record.get('raw_edit') and self.registry_counts is not None and
                        address not in self.registry_addresses and address not in self.containers):continue
                records[address]=record
            except Exception as exc:
                if address in self.registry_addresses and len(rejected)<8:
                    rejected.append('%08X: %s' % (address,exc))
        self.records=records
        self.diagnostics=dict(captures=self.registry_captures,registered=self.registry_counts,
                              candidates=len(candidates),loaded=len(records),
                              ownerless=sum(r['ownerless'] for r in records.values()),rejected=rejected)
        self.open.intersection_update(records)
        if self.selected and self.selected[0] not in records:self.selected=None
        # The discovery helper's registry is not needed after each bounded scan.
        self.discovery.registered.clear()
        self.discovery.containers.clear()

    def refresh_values(self,tree):
        # Root summaries must update even when their property rows are collapsed.
        for address in list(self.records):
            record=self.records.get(address)
            if record is None:continue
            try:self.records[address]=self._read(address,tree,record['identity'])
            except Exception:
                self.records.pop(address,None);self.open.discard(address)
                if self.selected and self.selected[0]==address:self.selected=None

    def rows(self):
        rows=[]
        if not self.records:return rows
        ordered=sorted(self.records.items(),key=lambda p:(not p[1].get('ownerless',False),-p[1]['actor']['pid'],p[0]))
        for role,label in (('at','AT — Attack'),('tg','TG — Target / receives attacks'),
                           ('co','CO — Physical collision'),('inactive','Inactive — all roles disabled')):
            members=[(address,record) for address,record in ordered if
                     (not any(record['values'][r+'_enabled'] for r in ('at','tg','co')) if role=='inactive'
                      else record['values'][role+'_enabled'])]
            if role=='inactive' and not members:continue
            count=('%d/%d loaded' % (len(members),ROLE_CAPACITIES[role]) if role in ROLE_CAPACITIES
                   else '%d' % len(members))
            count+='; %d ownerless' % sum(r.get('ownerless',False) for _a,r in members)
            rows.append(dict(key=(None,role),record=None,field=None,editable=False,
                             section='%s (%s)' % (label,count),role=role))
            for address,record in members:
                rows.append(dict(key=(address,None),record=record,field=None,editable=False,role=role))
                if address in self.open:
                    for field in tuple(record['fields'])+('owner','owner_pid','status_pid','position','geometry','actor_state'):
                        rows.append(dict(key=(address,field),record=record,field=field,role=role,
                                         editable=field in record['fields'] and record['fields'][field][2]))
        return rows

    @staticmethod
    def value(row):
        field,record=row['field'],row['record']
        if field is None:return ''
        if record.get('ownerless') and not record.get('raw_edit'):
            if field=='owner':return 'No owner'
            if field=='owner_pid':return 'No owner'
            if field=='status_pid':return 'Invalid PID'
            if field=='actor_state':return 'N/A: no owner'
        if record.get('raw_edit') and field in ('position','geometry','actor_state'):
            return 'Raw layout; inspect stored fields'
        if field=='position':return '(%.3f, %.3f, %.3f)' % shape_center(record['shape'])
        if field=='geometry':return str(record['shape'][2:])
        if field=='actor_state':return str(record['context'])
        value=record['values'][field]
        if value is None:return 'Unreadable status pointer'
        if field == 'status.pid' or field.endswith('_pid'):
            return str(value)
        if field in record['fields']:
            _off,fmt,_writable,_shift,width=record['fields'][field]
            if fmt=='I' and width==32:return '0x%08X' % value
            if fmt=='f':return '%.9g' % value
        if field in ('owner',):return '0x%08X' % value
        return str(value)

    def write(self,address,identity,field,text,tree):
        # Recheck the process list as well as collider identity before writing.
        previous=self._read(address,tree,identity)
        container=self.containers.get(address)
        if container is not None:
            actor=tree.by_pid.get(container[0])
            if actor is None or actor['addr']!=container[1] or tree._u32(container[1]+4)!=container[0]:
                raise ValueError('Collider containing actor is no longer loaded')
        result=write_property(self.edits,address,identity,field,text,
                              raw_kind=previous['shape'][0] if previous.get('raw_edit') else None)
        try:self.records[address]=self._read(address,tree,identity)
        except Exception:
            # A successful destructive edit is not an edit failure. Keep the
            # original layout/address available so the user can repair it.
            self.raw_edits[address]=dict(previous,raw_container=container)
            self.records[address]=self._read(address,tree,identity)
        if self.is_locked(address,field):
            self.locks[address]['values'][field]=self._capture_lock(self.records[address],field)
        if self.is_locked(address,'position') and field.startswith(('shape.center.','shape.start.','shape.end.',
                                                                   'shape.a.','shape.b.','shape.c.')):
            try:self.locks[address]['values']['position']=self._capture_lock(self.records[address],'position')
            except Exception:pass
        if address in self.locks:self.locks[address]['record']=self.records[address]
        return result
