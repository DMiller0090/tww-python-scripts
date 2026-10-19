"""Hit lifecycle sampling with explicit evidence limits.

Collision-end captures distinguish registered refreshes from unregistered pending
hits. Frame polling alone cannot distinguish repeated same-peer contacts. Type and
power are compared to first-observed data; CPU reads are provided by hit_trace.
No sampled state or cleared bit is treated as proof of actor damage consumption.
"""
import struct
import csv
import os
from .collider import ColliderDecoder
from collections import deque

from .heap import HeapMap

ACTOR_QUEUE = {"GZLJ01": 0x803654C8, "GZLE01": 0x80372028, "GZLP01": 0x80378D08}
EVENT_MODE_ADDR = {"GZLJ01": 0x803BD3A2}
BIG_ACTORS = {0x00A9: 0x4E00}      # PLAYER: daPy_lk_c colliders end near 0x4C30
SCAN_SIZE = 0x2000
NO_PID = 0xFFFFFFFF
A_PID, A_PROC, A_INIT, A_COND, A_TAG = 0x04, 0x08, 0x0C, 0x1C8, 0xC4
NOEXEC, NODRAW, DYING = 0x02, 0x04, 3
C_VT, C_STTS, S_ACTOR, S_APID = 0x3C, 0x44, 0x0C, 0x10
# side: name, cCcD rPrm, hitObj, type, GObj rPrm, GObj apid, GObj mAc, no-actor bit
SIDES = (("TG", 0x1C, 0x20, 0x10, 0x98, 0xA0, 0xA4, 1 << 0),
         ("AT", 0x04, 0x08, 0x28, 0x54, 0x5C, 0x60, 1 << 1),
         ("CO", 0x30, 0x34, None, 0xE0, 0xE8, 0xEC, 1 << 0))


def _ok(p):
    return p is not None and 0x80000000 <= p < 0x81800000 and (p & 3) == 0


def _state_key(context):
    # Frame timestamps belong in the record, but must not cause a disk write for
    # every idle collider on every frame.
    return {k:(_state_key(v) if isinstance(v,dict) else v) for k,v in context.items()
            if k not in ('game_frame','last_collision_frame')}


class HitWatch:
    def __init__(self, memory, names=None, session_file="", source="hit_watch", max_session_lines=20000,allow_ownerless=False):
        self.mem = memory
        self.allow_ownerless=allow_ownerless
        self.names = names or (lambda pid, default="?": default)
        self.session_file = session_file
        self.source = source
        self.max_session_lines = max_session_lines
        self.version = None
        self.queue = None
        self.heap = None
        self.frame = 0
        self.live, self.actors, self.tracked = {}, {}, {}
        self.stats = dict(actors=0, colliders=0, frozen=0, culled=0, held=0)
        self.recent = deque(maxlen=14)           # (text, level) for an on-screen log
        self._sess_lines = 0
        self._prev = dict(frozen=0, evt=None)
        self._labeled = set()
        self.decoder=ColliderDecoder(memory,self._u32)
        self.focus_address=None
        self.scenario=''
        self.state_addresses={}
        self.registered={}
        self.containers={}  # address -> (PID, actor storage); never the collision owner
        self._scan_done=set()
        self._sizes=self._load_sizes()
        self._context_previous=None
        self.collision_serial=0;self.last_collision_frame=None
        self.active_sides={side[0]:set() for side in SIDES}

    # ---- raw reads ---------------------------------------------------------------------------
    def _u32(self, a):
        try:
            return self.mem.read_u32(a) & 0xFFFFFFFF
        except Exception:
            return None

    def _u16(self, a):
        try:
            return self.mem.read_u16(a)
        except Exception:
            return None

    def _u8(self, a):
        try:
            return self.mem.read_u8(a)
        except Exception:
            return None

    def pname(self, proc):
        return self.names(proc, "%04X" % (proc or 0)) if proc is not None else "?"

    # ---- setup -------------------------------------------------------------------------------
    def attach(self):
        try:
            gid = bytes(self.mem.read_bytes(0x80000000, 6)).decode("ascii", "replace")
        except Exception:
            return False
        if gid not in ACTOR_QUEUE:
            return False
        self.version, self.queue = gid, ACTOR_QUEUE[gid]
        self.heap = HeapMap(gid, self.mem)
        self.log("START", "build %s, actor queue %08X, heaps %s" % (gid, self.queue, sorted(self.heap.heaps)), "ok")
        return True

    # ---- logging -----------------------------------------------------------------------------
    def session(self, text):
        """Compatibility hook for research callers; disk logging is disabled."""
        return

    def log(self, tag, msg, level="info"):
        line = "%-8s %s" % (tag, msg)
        self.recent.append(("[%d] %s" % (self.frame, line), level))
        self.session(line)

    # ---- heap helpers ------------------------------------------------------------------------
    def locate(self, addr, max_age=120):
        if self.heap is None or addr is None:
            return None
        self.heap.refresh(self.frame, max_age=max_age)
        return self.heap.locate(addr)

    def heap_text(self, addr, max_age=120):
        loc = self.locate(addr, max_age)
        return "?" if loc is None else loc.text()

    # ---- discovery ---------------------------------------------------------------------------
    def _load_sizes(self):
        try:
            root=os.path.join(os.path.dirname(__file__),'data')
            with open(os.path.join(root,'ActorInstanceSizes.txt'),encoding='utf-8-sig') as f:
                by_name={line.split(':')[0].strip():int(line.split(':')[1],16)
                         for line in f if ':' in line}
            with open(os.path.join(root,'proc_name_structs.csv'),newline='') as f:
                return {int(row['ProcValue'],0):by_name[row['ProcName'].removeprefix('PROC_')]
                        for row in csv.DictReader(f) if row.get('ProcName','').removeprefix('PROC_') in by_name}
        except (OSError,ValueError,KeyError):return BIG_ACTORS

    def reset(self,reason='reset'):
        self.live.clear();self.actors.clear();self.tracked.clear();self.registered.clear()
        self.containers.clear()
        self._scan_done.clear();self._context_previous=None
        self.collision_serial=0;self.last_collision_frame=None
        self.active_sides={side[0]:set() for side in SIDES}
        if self.heap:self.heap.last_refresh_frame=None
        self.log('RESET',reason,'dim')

    def context(self,owner=None):
        values=dict(menu_pause=None,hitstop_timer=None,stop_mask=None,emulator_paused=getattr(self,'host_paused',False),scenario=self.scenario,event_mode=self._u8(EVENT_MODE_ADDR[self.version])
                    if self.version in EVENT_MODE_ADDR else None)
        for name,address in self.state_addresses.items():
            values[name]=(self._u32(address) if name=='stop_mask' else self._u8(address))
        if self.version=='GZLJ01':values['game_frame']=self._u32(0x803E9D34)
        values['last_collision_frame']=self.last_collision_frame
        if _ok(owner):
            values.update(process_pause=self._u8(owner+0x0B),init_state=self._u8(owner+A_INIT),condition=self._u32(owner+A_COND),
                          status=self._u32(owner+0x1C4),event_command=self._u16(owner+0xF8))
        return values

    def observe_collider(self,address,raw=None,container=None):
        try:
            raw=bytes(self.mem.read_bytes(address,0x150)) if raw is None else bytes(raw)
            shape=self.decoder.decode(raw)
            if shape is None:return False
            status=struct.unpack_from('>I',raw,0x44)[0]
            if not _ok(status):return False
            if shape[1]==0 and self.allow_ownerless:
                if self._u32(status+0x0C)!=0 or self._u32(status+0x10)!=NO_PID:return False
                pid=NO_PID
            elif _ok(shape[1]):pid=self._u32(shape[1]+4)
            else:return False
            if pid is None or self._u32(status+0x10)!=pid:return False
            self.registered[address]=(shape[1],pid,status,struct.unpack_from('>I',raw,0x114)[0])
            if container is not None:self.containers[address]=container
            known=self.actors.get(pid)
            if known is not None and address not in known['colls']:known['colls'].append(address)
            return True
        except Exception:return False

    def sample_registry(self,base,frame):
        """Pre-store counter-reset sample; Move and DrawClear both reset these counts."""
        self.frame=frame
        if self.queue is None and not self.attach():return
        count=self._u32(base+0x280C)
        if count is None or not 0<=count<=0x500:return
        self.collision_serial+=1;self.last_collision_frame=frame
        for side,offset,count_offset,limit in (('AT',0,0x2800,0x100),('TG',0x400,0x2804,0x300),
                                               ('CO',0x1000,0x2808,0x100)):
            n=self._u32(base+count_offset)
            self.active_sides[side]=(set(struct.unpack('>%dI' % n,bytes(self.mem.read_bytes(base+offset,n*4))))
                                     if n is not None and 0<n<=limit else set())
        raw=bytes(self.mem.read_bytes(base+0x1400,count*4))
        current=set(struct.unpack('>%dI' % count,raw)) if count else set()
        # A side list can accept an entry even when the combined list is full.
        current.update(*self.active_sides.values())
        # Unregistered embedded shapes can retain hit data during event skips.
        # Their containing actor anchors allocation lifetime, not hit-PID lookup.
        self.registered={a:i for a,i in self.registered.items()
                         if i[0]!=0 or a in current or a in self.containers or a==self.focus_address}
        for address in current:
            if _ok(address) and self.focus_address in (None,address):self.observe_collider(address)
        self.walk_actors(discover=False)
        self._sample_hits('registry-reset')

    def find_colliders(self,actor,pid,proc):
        size=min(0x40000,self._sizes.get(proc,BIG_ACTORS.get(proc,SCAN_SIZE)))
        try:data=bytes(self.mem.read_bytes(actor,size))
        except Exception:return [],0
        found=[]
        for off in range(0x200,max(0x200,size-0x150+1),4):
            status=struct.unpack_from('>I',data,off+C_STTS)[0]
            if not actor<=status<=actor+size-0x1C:continue
            so=status-actor
            status_owner=struct.unpack_from('>I',data,so+S_ACTOR)[0]
            status_pid=struct.unpack_from('>I',data,so+S_APID)[0]
            if not ((status_owner==actor and status_pid==pid) or
                    (self.allow_ownerless and status_owner==0 and status_pid==NO_PID)):continue
            if self.observe_collider(actor+off,data[off:off+0x150],container=(pid,actor)):found.append(actor+off)
        return found,0

    def walk_actors(self,discover=True):
        self.live.clear();node=self._u32(self.queue);seen=set();budget=0 if self.focus_address is not None else 8
        registered_by_pid={}
        for address,identity in self.registered.items():
            registered_by_pid.setdefault(identity[1],[]).append((address,identity))
        while _ok(node) and node not in seen and len(seen)<2048:
            seen.add(node);actor=self._u32(node+0x0C)
            if _ok(actor):
                pid=self._u32(actor+A_PID)
                if pid is not None:
                    proc=self._u16(actor+A_PROC)
                    self.live[pid]=dict(actor=actor,proc=proc,cond=self._u32(actor+A_COND) or 0,
                                        dying=self._u8(actor+A_INIT)==DYING)
                    known=self.actors.get(pid)
                    if known is None or known['actor']!=actor:
                        known=self.actors[pid]=dict(actor=actor,proc=proc,colls=[])
                    for address,identity in registered_by_pid.get(pid,()):
                        if identity[:2]==(actor,pid) and address not in known['colls']:known['colls'].append(address)
                    identity=(pid,actor)
                    if discover and budget and identity not in self._scan_done:
                        found,_near=self.find_colliders(actor,pid,proc)
                        owned=[a for a in found if self.registered[a][0]==actor]
                        known['colls']=list(set(known['colls']+owned));budget-=1
                        self._scan_done.add(identity)
            node=self._u32(node+8)
        for pid in list(self.actors):
            if pid not in self.live:del self.actors[pid]
        self._scan_done.intersection_update((pid,info['actor']) for pid,info in self.live.items())
        expired={a for a,(pid,actor) in self.containers.items()
                 if pid not in self.live or self.live[pid]['actor']!=actor}
        for address in expired:self.containers.pop(address,None)
        self.registered={addr:identity for addr,identity in self.registered.items()
                         if addr not in expired and ((identity[1] in self.live and self.live[identity[1]]['actor']==identity[0]) or
                         (self.allow_ownerless and identity[0]==0 and identity[1]==NO_PID and
                          self._u32(addr+C_STTS)==identity[2] and self._u32(addr+0x114)==identity[3] and
                          self._u32(identity[2]+S_ACTOR)==0 and self._u32(identity[2]+S_APID)==NO_PID))}
        self.stats['actors']=len(self.live)
        self.stats['colliders']=sum(len(a['colls']) for a in self.actors.values())+sum(i[0]==0 for i in self.registered.values())
        self.stats['frozen']=sum(bool(v['cond']&NOEXEC and not v['cond']&NODRAW) for v in self.live.values())
        self.stats['culled']=sum(bool(v['cond']&NOEXEC and v['cond']&NODRAW) for v in self.live.values())

    # ---- hit analysis ------------------------------------------------------------------------
    def peer_values(self, side, peer):
        if side=='CO' or not _ok(peer):
            return None
        if side == "TG":
            return (self._u32(peer + 0x10), self._u8(peer + 0x14))
        return (self._u32(peer + 0x28), None)

    @staticmethod
    def fmt(v):
        if v is None:
            return "?"
        return "type=%s atp=%s" % ("%08X" % v[0] if v[0] is not None else "?", v[1] if v[1] is not None else "-")

    def peer_owner(self, peer):
        if not _ok(peer):
            return None, None
        stts = self._u32(peer + C_STTS)
        if not _ok(stts):
            return None, None
        owner = self._u32(stts + S_ACTOR)
        return (owner, self._u32(owner + A_PID)) if _ok(owner) else (None, None)

    def classify(self, apid, peer, ac, noactor):
        """('OK'|'GONE'|'REUSED'|'NONE', detail) for the other end of a pending hit."""
        if noactor:
            return "NOACTOR", "getter bypasses actor lookup"
        if apid in (None, NO_PID):
            return "NONE", "no id"
        attacker = self.live.get(apid)
        # GetAc searches the actor queue, not the Execute eligibility/init state.
        # A queued-for-deletion actor remains searchable until ActorQTo removes it.
        alive = attacker is not None
        _owner, opid = self.peer_owner(peer)
        if _ok(peer) and opid is not None and opid != apid:
            return "REUSED", "now bsPCID %d" % opid
        if not alive:
            return "GONE", ("cached mAc=%08X skips pid check" % ac) if ac else "pid not live"
        return "OK", self.pname(attacker["proc"])+(' (deletion pending; still in actor queue)' if attacker['dying'] else '')

    def predict(self,apid,ac,noactor,alive):
        """Getter eligibility only. Actor damage handling cannot be inferred from this."""
        permitted=noactor or (apid not in (None,NO_PID) and (bool(ac) or alive))
        detail=('no-actor bypass' if noactor else 'cached actor bypasses fresh PID lookup' if ac and permitted else
                'actor identity available' if permitted else 'actor lookup rejects')
        return ('GETTER MAY RETURN HIT POINTER: ' if permitted else 'GETTER RETURNS NULL: ')+detail+'; consumption unverified'

    def _sample_hits(self,phase):
        for pid,actor in self.actors.items():
            info=self.live.get(pid)
            if not info:continue
            for address in actor['colls']:
                if self.focus_address is not None and address!=self.focus_address:continue
                for side in SIDES:self._check(pid,info,address,side,phase)
        for key in list(self.tracked):
            if (self.tracked[key]['owner_pid']==NO_PID and key[0] not in self.registered or
                    self.tracked[key]['owner_pid']!=NO_PID and self.tracked[key]['owner_pid'] not in self.live):
                self.log('OWNER_GONE','%08X %s receiver no longer live' % key,'dim')
                del self.tracked[key]
        for address,identity in self.registered.items():
            if identity[0]==0 and self.focus_address in (None,address):
                info=dict(actor=0,proc=None,cond=0)
                for side in SIDES:self._check(NO_PID,info,address,side,phase)

    def _check(self,pid,info,coll,side,phase='frame'):
        sname,c_r,c_hit,_c_type,g_r,g_apid,g_ac,noact=side;key=(coll,sname)
        try:raw=bytes(self.mem.read_bytes(coll,0xF8));status=struct.unpack_from('>I',raw,C_STTS)[0]
        except Exception:return
        if self._u32(status+S_ACTOR)!=info['actor'] or self._u32(status+S_APID)!=pid:
            self.tracked.pop(key,None);return
        get=lambda off:struct.unpack_from('>I',raw,off)[0]
        if not get(c_r)&1:
            previous=self.tracked.pop(key,None)
            if previous:self.log('CLEARED','%s @%08X after %df; flag cleared, damage consumption unverified' %
                                 (sname,coll,self.frame-previous['since']),'dim')
            return
        peer,apid,ac=get(c_hit),get(g_apid),get(g_ac);noactor=bool(get(g_r)&noact)
        identity=(pid,info['actor'],status,peer,apid)
        state=self.tracked.get(key)
        refreshed=(phase in ('collision-end','registry-reset') and coll in self.active_sides[sname] and
                   state is not None and state.get('collision_serial')!=self.collision_serial)
        if state is not None and (state['identity']!=identity or refreshed):
            self.log('REPLACED','%s @%08X identity changed or registered at counter reset; conservative new baseline, assignment unverified' % (sname,coll),'info')
            state=None
        values=self.peer_values(sname,peer);_owner,peer_pid=self.peer_owner(peer)
        context=self.context(info['actor'])
        context['side_registered_last_collision']=coll in self.active_sides[sname]
        context.update(receiver_ownerless=pid==NO_PID,peer_noactor=noactor,
                       peer_saved_pid=apid,peer_cached_actor=ac,
                       hit_callback=get(g_r+4),peer_actor_present=apid in self.live,
                       peer_actor_context=self.context(self.live[apid]['actor']) if apid in self.live else None,
                       getter=self.predict(apid,ac,noactor,apid in self.live))
        container=self.containers.get(coll)
        if container:
            context['storage_actor_pid']=container[0]
            context['storage_actor_context']=self.context(container[1])
        if state is None:
            location=self.locate(peer)
            state=self.tracked[key]=dict(identity=identity,owner_pid=pid,since=self.frame,snap=values,
                baseline_phase=phase,collision_serial=self.collision_serial,peer=peer,apid=apid,flags=set(),last_cur=values,last_owner=peer_pid,
                sig=location.signature() if location else None,context=context,
                was_frozen=bool(info['cond']&NOEXEC) if pid!=NO_PID else None)
            self.log('HIT+', '%s %s @%08X peer=%08X bsPCID=%d first-observed=%s %s context=%s' %
                     (sname,'No actor owner' if pid==NO_PID else self.pname(info['proc']),coll,peer,apid,phase,self.fmt(values),context),'info')
        age=self.frame-state['since'];frozen=bool(info['cond']&NOEXEC) if pid!=NO_PID else None
        if age>=2 and 'HELD' not in state['flags']:
            state['flags'].add('HELD')
            self.log('HELD' if self.last_collision_frame is not None else 'HELD?',
                     '%s @%08X unchanged %df; NOEXEC=%s process_pause=%s event=%s (execution/refresh unverified without access trace)' %
                     (sname,coll,age,frozen,context.get('process_pause'),context.get('event_mode')),'warn')
        if values is not None and values!=state['last_cur']:
            state['flags'].add('CHANGED')
            self.log('DATA_CHANGED','%s @%08X same hit peer=%08X: %s -> %s baseline=%s' %
                     (sname,coll,peer,self.fmt(state['last_cur']),self.fmt(values),self.fmt(state['snap'])),'warn')
        state['last_cur']=values
        classification,detail=self.classify(apid,peer,ac,noactor)
        if classification in ('GONE','REUSED') and classification not in state['flags']:
            state['flags'].add(classification)
            self.log(classification,'%s @%08X peer=%08X %s; %s' %
                     (sname,coll,peer,detail,self.predict(apid,ac,noactor,apid in self.live)),'bad')
        if ac and _ok(ac) and apid not in (None,NO_PID) and self._u32(ac+4)!=apid and 'CACHE_MISMATCH' not in state['flags']:
            state['flags'].add('CACHE_MISMATCH')
            self.log('CACHE_MISMATCH','%s @%08X cached actor %08X has another PID' % (sname,coll,ac),'bad')
        if age and state['sig'] is not None and 'HEAPCHG' not in state['flags']:
            location=self.locate(peer,max_age=30)
            if location is not None and location.signature()!=state['sig']:
                state['flags'].add('HEAPCHG');self.log('HEAPCHG','%s @%08X peer allocation signature changed' % (sname,coll),'bad')
        if state['was_frozen'] and not frozen:
            self.log('RESUME?','%s @%08X NOEXEC cleared; %s' %
                     (sname,coll,self.predict(apid,ac,noactor,apid in self.live)),'info')
        state['was_frozen']=frozen
        if _state_key(context)!=_state_key(state['context']):
            self.session('STATE %s @%08X %s' % (sname,coll,context));state['context']=context

    def tick(self,frame):
        if frame<self.frame:self.reset('frame counter moved backwards')
        self.frame=frame
        if self.queue is None and not self.attach():return
        if self.focus_address is not None:self.observe_collider(self.focus_address)
        self.walk_actors();self._sample_hits('frame')
        self.stats['held']=sum(self.frame-s['since']>=2 for s in self.tracked.values())
        context=self.context()
        if _state_key(context)!=self._context_previous:
            self.session('STATE '+str(context));self._context_previous=_state_key(context)
        if frame%300==0:self.session('STATS '+str(self.stats))

    # ---- collider labels for the viewer ------------------------------------------------------
    def begin_sample(self):
        self._labeled = set()

    def collider_label(self, address, data, owner=None):
        """Multi-line label for one live collider (raw 0x150-byte object), or None if already labeled."""
        if address in self._labeled or (self.queue is None and not self.attach()):
            return None
        self._labeled.add(address)
        u32 = lambda o: struct.unpack_from(">I", data, o)[0]
        opid = self._u32(owner + A_PID) if _ok(owner) else None
        oname = self.pname(self._u16(owner + A_PROC)) if _ok(owner) else "?"
        lines = ["HW %s bsPCID=%s | %s" % (oname, "%d" % opid if opid is not None else "?", self.heap_text(address))]
        for sname, c_r, c_hit, c_type, g_r, g_apid, g_ac, noact in SIDES:
            if not u32(c_r) & 1:
                continue
            peer, apid, ac = u32(c_hit), u32(g_apid), u32(g_ac)
            status, detail = self.classify(apid, peer, ac, bool(u32(g_r) & noact))
            other = self.live.get(apid)
            arrow = "<-" if sname == "TG" else "->"
            lines.append("%s%s %s bsPCID=%d %s%s" % (sname, arrow, self.pname(other["proc"]) if other else "?", apid & 0xFFFFFFFF,
                                                   status, "" if status == "OK" else " (%s)" % detail))
            lines.append("  hitObj %08X %s" % (peer, self.heap_text(peer)))
        return "\n".join(lines)

    def status_line(self):
        return "hit-watch %s | actors %d | colliders %d | frozen %d culled %d | held %d | heaps %s" % (
            self.version or "?", self.stats["actors"], self.stats["colliders"], self.stats["frozen"],
            self.stats["culled"], self.stats["held"], self.heap.stats() if self.heap else "-")
