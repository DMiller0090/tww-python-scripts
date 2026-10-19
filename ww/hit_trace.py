"""Optional, focused non-pausing CPU memory-access trace for a target collider.

The fork reports address/value but not PC or access width. Reads are CPU access
observations, not proof of actor damage consumption. Saved stack return addresses
provide caller clues when the registers API is available. Host memory reads don't
trigger these watches. All hooks are removed on disarm or identity change.
"""
import json
import struct


class HitTrace:
    def __init__(self,engine,edits,debug,registers=None):
        self.engine,self.edits,self.debug,self.registers=engine,edits,debug,registers
        self.address=None;self.identity=None;self.episode=None
        self.container=None
        self.watches={};self.sequence=0;self.frame=None;self.count=0;self.dropped=0;self.seen=set()
        self.total=0;self.last_error=''

    def _set_sites(self,peer=None):
        desired={self.address+0x1C:'target_hit_flag',self.address+0x20:'target_hit_object',
                 self.address+0xA0:'stored_attacker_pid',self.address+0xA4:'cached_attacker'}
        if peer is not None and 0x80000000<=peer<0x817FFFE8:
            desired.update({peer+0x10:'attack_type',peer+0x14:'attack_power'})
        for site in list(self.watches):
            if site not in desired:
                self.debug.remove_memory_breakpoint(site);del self.watches[site]
        for site,label in desired.items():
            if self.watches.get(site)!=label:
                self.debug.set_memory_watch({'At':site,'WatchOnRead':True,'WatchOnWrite':True})
                self.watches[site]=label

    def arm(self,address):
        self.close()
        if not callable(getattr(self.debug,'set_memory_watch',None)):
            raise ValueError('This Dolphin build has no non-pausing memory watch support')
        raw,_shape,identity=self.edits.read(address)
        self.engine.attach() if self.engine.queue is None else None
        self.address,self.identity=address,identity
        self.container=self.engine.containers.get(address)
        try:
            self._set_sites()
            if struct.unpack_from('>I',raw,0x1C)[0]&1:
                peer=struct.unpack_from('>I',raw,0x20)[0]
                self._assign(peer,'armed with a pending hit; earlier values unknown')
            self.engine.log('TRACE_ON','target=%08X identity=%s; CPU reads are unclassified callers' %
                            (address,identity),'ok')
        except Exception:
            self.close();raise

    def close(self):
        for site in list(self.watches):
            try:self.debug.remove_memory_breakpoint(site)
            except Exception:pass
        self.watches.clear();self.address=None;self.identity=None;self.episode=None
        self.container=None
        self.seen.clear()

    def validate(self):
        if self.address is None:return False
        try:
            # Actors sometimes disable geometry with a negative radius while
            # retaining hit information. Track lifetime, not drawable geometry.
            owner,pid,status,vtable,_kind=self.identity
            if owner==0 and self.container is not None and self.engine.containers.get(self.address)!=self.container:
                raise ValueError('containing actor left the actor queue')
            mem=self.edits.memory
            word=lambda a:struct.unpack('>I',bytes(mem.read_bytes(a,4)))[0]
            if (word(self.address+0x44)==status and word(self.address+0x114)==vtable and
                    ((owner==0 and pid==0xFFFFFFFF and getattr(self.edits,'allow_ownerless',False)) or word(owner+4)==pid)
                    and word(status+0x0C)==owner and word(status+0x10)==pid):
                return True
        except Exception:pass
        self.engine.log('TRACE_STOP','selected collider disappeared or changed identity','warn')
        self.close();return False

    def _assign(self,peer,source):
        if not 0x80000000<=peer<0x817FFFE8:
            self.episode=None;self._set_sites();return
        values=self.engine.peer_values('TG',peer)
        self.sequence+=1
        self.episode=dict(sequence=self.sequence,peer=peer,baseline=values,source=source,
                          frame=self.engine.frame,peer_owner=self.engine.peer_owner(peer))
        self._set_sites(peer)
        self.engine.log('HIT_ASSIGN','target=%08X episode=%d peer=%08X %s source=%s' %
                        (self.address,self.sequence,peer,self.engine.fmt(values),source),'info')

    def _stack(self):
        if self.registers is None:return []
        try:sp=self.registers.read_gpr(1)&0xFFFFFFFF
        except Exception:return []
        result=[];seen=set()
        for _ in range(8):
            if sp in seen or not 0x80000000<=sp<0x817FFFF8 or sp&3:break
            seen.add(sp)
            try:parent,lr=struct.unpack('>2I',bytes(self.engine.mem.read_bytes(sp,8)))
            except Exception:break
            if 0x80000000<=lr<0x81800000:result.append(lr)
            if parent<=sp or parent-sp>0x100000:break
            sp=parent
        return result

    def on_access(self,is_write,address,value,frame):
        label=self.watches.get(address)
        if label is None:return False
        if not self.validate():return True
        self.engine.frame=frame
        self.engine.host_paused=False
        if frame!=self.frame:
            if self.dropped:self.engine.session('TRACE_LIMIT dropped %d repetitive accesses in frame %s' % (self.dropped,self.frame))
            self.frame=frame;self.count=0;self.dropped=0;self.seen.clear()
        if is_write and label=='target_hit_object':
            self._assign(value,'CPU hit-pointer assignment (before store)')
        elif is_write and label=='target_hit_flag' and not value&1:
            self.episode=None;self._set_sites()
        context=self.engine.context(self.identity[0])
        key=(bool(is_write),address,value,str(context))
        if key in self.seen:return True
        self.seen.add(key)
        if self.count>=128:self.dropped+=1;return True
        self.count+=1;self.total+=1
        drift=False
        if not is_write and self.episode is not None and label in ('attack_type','attack_power'):
            index=0 if label=='attack_type' else 1
            maximum=0xFFFFFFFF if index==0 else 255
            baseline=self.episode['baseline']
            drift=bool(baseline is not None and 0<=value<=maximum and value!=baseline[index])
        record=dict(event='CPU_WRITE' if is_write else 'CPU_READ',field=label,address=address,
                    access_value=value,width='not supplied by fork',frame=frame,context=context,
                    target=self.address,episode=self.episode,stack_return_addresses=self._stack(),
                    interpretation='unclassified caller; actor consumption not established',drift=drift)
        self.engine.session('ACCESS '+json.dumps(record,sort_keys=True))
        self.engine.log('DRIFT_READ' if drift else record['event'],
                        '%s @%08X value=%X target=%08X event=%s process_pause=%s NOEXEC=%s' %
                        (label,address,value,self.address,context.get('event_mode'),context.get('process_pause'),
                         bool(context['condition']&2) if context.get('condition') is not None else None),'warn' if drift else 'dim')
        return True

    def status(self):
        return ('Trace armed @%08X; %d unique CPU accesses; episode %s' %
                (self.address,self.total,self.episode['sequence'] if self.episode else '-')
                if self.address is not None else 'Trace disarmed')
