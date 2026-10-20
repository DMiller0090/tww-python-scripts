"""Registered BG meshes and editable actor switches for TWW Inspector."""
import struct

from .actor_tree import GAME_INFO, PLAY_OFFSET
from .zone_info import read_zone_tables, set_flag_numbers
from .actor_switches import actor_switches

BG_STATIC, BG_MOVABLE, BAD = 0xFF9CE3C0, 0xFFC792EA, 0xFFFF7070
BG_COLUMNS = (('BG / field',0),('Owner',300),('bsPCID',590),('Value',680),
              ('mRoomNo',800),('Address (right-click to copy)',900))
BG_ROOM_OFFSET = 0xB8
ZONE_COLUMNS = (('Actor',0),('bsPCID',250),('RoomNo',330),('SwitchNo',405),
                ('Scope',500),('Zone',580),('State',640),('Room source',760),('Use',900))
ZONE_SWITCH_COLUMNS = (('Zone',0),('Room',80),('mSwitch[0]',160),('mSwitch[1]',270),
                       ('mSwitch[2]',380),('Set switches',490))
NORMAL = 0xFFE6EDF3


def _ptr(value): return 0x80000000 <= value < 0x81800000

def _num(value): return '-' if value is None else str(value)


class WorldView:
    def __init__(self, memory, names):
        self.memory, self.names = memory, names
        self.reset()

    def reset(self):
        self.tables = {'bg': [], 'zone': []}
        self.open_bg = set()
        self.messages = {'bg': 'Waiting for BG registrations.', 'zone': 'Waiting for zone flags.'}

    def rows(self, mode):
        if mode=='flags':mode='zone'
        if mode != 'bg': return self.tables[mode]
        rows = []
        for row in self.tables['bg']:
            rows.append(row)
            if row.get('identity') in self.open_bg:
                for label,value in row['fields']:
                    token=row.get('field_edits',{}).get(label)
                    address=token['address'] if token else None
                    rows.append(dict(key=row['key']+(label,),child=True,color=row['color'],
                                     values=[label,'','',value,'','%08X'%address if address is not None else ''],
                                     detail=row['detail'],bg_edit=token,address=address))
        return rows

    def toggle_bg(self, row):
        identity = row.get('identity')
        if identity is None: return
        if identity in self.open_bg: self.open_bg.remove(identity)
        else: self.open_bg.add(identity)

    def refresh(self, tree):
        mode = 'zone' if tree.mode=='flags' else tree.mode
        self.tables[mode] = []
        if tree.version not in GAME_INFO:
            self.messages[mode] = 'Waiting for a supported Wind Waker build.'
            return
        try:
            if mode == 'bg': self._bg(tree)
            else: self._zone(tree)
        except Exception as exc:
            self.tables[mode] = []
            self.messages[mode] = '%s read failed: %s' % (mode.upper(), exc)

    def _bg(self, tree):
        base = GAME_INFO[tree.version] + PLAY_OFFSET
        registry = self.memory.read_bytes(base, 256*0x14)
        owners = {a['addr']: a for a in tree.actors}
        stage=bytes(self.memory.read_bytes(base+0x5134,11))
        rows = []
        for slot in range(256):
            bgw, flags, pid, owner = struct.unpack_from('>4I', registry, slot*0x14)
            if not flags & 1: continue
            kind, color, dzb = 'Unreadable', BAD, None
            process = owners.get(owner)
            name = process['name'] if process and process['pid']==pid else ('Unknown/stale' if owner else 'No owner')
            context=dict(version=tree.version,stage=stage,slot=slot,
                         registration=bytes(registry[slot*0x14:(slot+1)*0x14]),bgw=bgw,dzb=None,header=None)
            fields=[('Registry slot address','%08X'%(base+slot*0x14))]
            field_edits={}
            def add(label,value,address,fmt='I',hex_value=False,scope='bgw'):
                shown=('0x%X'%value if hex_value else str(value))
                fields.append((label,shown))
                field_edits[label]=dict(context=context,label=label,address=address,format=fmt,
                                        raw=struct.pack('>'+fmt,value),scope=scope,hex=hex_value)
            add('Registry flags',flags,base+slot*0x14+4,hex_value=True,scope='registry')
            add('Owner address',owner,base+slot*0x14+0xC,hex_value=True,scope='registry')
            add('cBgW',bgw,base+slot*0x14,hex_value=True,scope='registry')
            room_edit=None;room=None;room_address=None
            details = 'Registered slot %d | cBgW %08X | registry flags 0x%X' % (slot,bgw,flags)
            try:
                if not _ptr(bgw) or not _ptr(bgw+0xBB): raise ValueError('invalid cBgW pointer')
                raw = self.memory.read_bytes(bgw,0xBC)
                mesh_flags = raw[0x6C]
                movable = bool(mesh_flags & 1) or (_ptr(owner) and not mesh_flags & 0x20)
                kind = 'MoveBG' if movable else 'BG'
                color = BG_MOVABLE if movable else BG_STATIC
                vtx, dzb = struct.unpack_from('>2I',raw,0x90)
                root = struct.unpack_from('>i',raw,0xA4)[0]
                context['dzb']=dzb
                add('cBgW flags',mesh_flags,bgw+0x6C,'B',True)
                add('DZB',dzb,bgw+0x94,hex_value=True)
                add('World vertex table',vtx,bgw+0x90,hex_value=True)
                add('Root group',root,bgw+0xA4,'i')
                room_address=bgw+BG_ROOM_OFFSET
                room=struct.unpack_from('>H',raw,BG_ROOM_OFFSET)[0]
                add('mRoomNo',room,room_address,'H')
                room_edit=field_edits['mRoomNo']
                add('mFlag',raw[0xBA],bgw+0xBA,'B',True)
                add('mRoomNo2',raw[0xBB],bgw+0xBB,'B')
                if not _ptr(dzb): raise ValueError('invalid DZB pointer')
                data = self.memory.read_bytes(dzb,0x30)
                vn,vt,tn,tt = struct.unpack_from('>iIiI',data)
                gn,gt,pn,pt = struct.unpack_from('>iIiI',data,0x20)
                context['header']=bytes(data)
                for label,value,offset,fmt,hex_value in (
                        ('Vertex count',vn,0,'i',False),('Triangle count',tn,8,'i',False),
                        ('Group count',gn,0x20,'i',False),('Property count',pn,0x28,'i',False),
                        ('Local vertex table',vt,4,'I',True),('Triangle table',tt,0xC,'I',True),
                        ('Group table',gt,0x24,'I',True),('Property table',pt,0x2C,'I',True)):
                    add(label,value,dzb+offset,fmt,hex_value,'dzb')
                details = ('Slot %d | %s | owner %08X bsPCID %d | registry flags 0x%X / cBgW flags 0x%02X\n'
                           'cBgW %08X | DZB %08X | world vertices %08X | local vertices %08X\n'
                           'Triangles %08X | groups %08X | properties %08X | root group %d') % (
                               slot,kind,owner,pid,flags,mesh_flags,bgw,dzb,vtx,vt,tt,gt,pt,root)
                if min(vn,tn,gn,pn)<0: raise ValueError('negative mesh count')
            except Exception as exc:
                details += '\nMesh data unavailable: '+str(exc)
                fields.append(('Mesh data unavailable',str(exc)))
            if room is not None:
                details+='\nmRoomNo %d @%08X | 0xFFFF uses DZB group lookup. Root Address is mRoomNo; expanded Address is field storage.'%(room,room_address)
            rows.append(dict(key=('bg',slot),identity=(slot,bgw,dzb,owner,pid),kind=kind,
                             values=['%s #%d'%(kind,slot),name,str(pid) if owner else '-','',
                                     str(room) if room is not None else '-',
                                     '%08X'%room_address if room_address is not None else ''],
                             color=color,detail=details,fields=fields,field_edits=field_edits,
                             room_edit=room_edit,address=room_address))
        self.open_bg.intersection_update(row['identity'] for row in rows)
        grouped = []
        for kind,color in (('MoveBG',BG_MOVABLE),('BG',BG_STATIC),('Unreadable',BAD)):
            members = [row for row in rows if row['kind']==kind]
            if kind=='Unreadable' and not members: continue
            grouped.append(dict(key=('bg-section',kind),section='%s (%d)' % (kind,len(members)),
                                color=color,detail=''))
            grouped.extend(sorted(members,key=lambda row:row['key'][1]))
        self.tables['bg'] = grouped
        self.messages['bg'] = '%d / 256 registered total | BG: green | MoveBG: purple | click mRoomNo or an expanded Value to edit; right-click Address to copy' % len(rows)

    def validate_bg(self, tree, token):
        if not token:raise ValueError('This field is derived or unavailable')
        context=token['context']
        if tree.mode!='bg' or tree.version!=context['version'] or bytes(self.memory.read_bytes(0x80000000,6))!=context['version'].encode('ascii'):
            raise ValueError('Game/region changed; select the BG field again')
        base=GAME_INFO[tree.version]+PLAY_OFFSET
        if bytes(self.memory.read_bytes(base+0x5134,11))!=context['stage']:
            raise ValueError('Stage changed; select the BG field again')
        if bytes(self.memory.read_bytes(base+context['slot']*0x14,0x14))!=context['registration']:
            raise ValueError('BG registration changed; select the field again')
        if token['scope']!='registry':
            if struct.unpack('>I',bytes(self.memory.read_bytes(context['bgw']+0x94,4)))[0]!=context['dzb']:
                raise ValueError('DZB changed; select the field again')
            if token['scope']=='dzb' and bytes(self.memory.read_bytes(context['dzb'],0x30))!=context['header']:
                raise ValueError('DZB header changed; select the field again')
        if bytes(self.memory.read_bytes(token['address'],len(token['raw'])))!=token['raw']:
            raise ValueError('Field changed; select it again')
        return token['address']

    def write_bg(self, tree, token, text):
        address=self.validate_bg(tree,token)
        text=text.strip()
        value=int(text,16) if text.lower().startswith('0x') else int(text,10)
        if token['label']=='mRoomNo' and value==-1:value=0xFFFF
        data=struct.pack('>'+token['format'],value)
        try:
            self.memory.write_bytes(address,bytearray(data))
            if bytes(self.memory.read_bytes(address,len(data)))!=data:
                raise ValueError('BG field readback failed')
        except Exception:
            self.memory.write_bytes(address,bytearray(token['raw']))
            raise
        self.refresh(tree)
        return value

    def _zone_switch_address(self, tree, token, word):
        if tree.mode not in ('zone','flags') or tree.version!='GZLJ01' or token['version']!=tree.version:
            raise ValueError('Zone edit is no longer in the same game/region')
        if bytes(self.memory.read_bytes(0x80000000,6))!=b'GZLJ01':
            raise ValueError('Zone edit requires the running JP/GZLJ01 game')
        zone,room=token['zone'],token['room']
        if not 0<=zone<32 or not 0<=room<64 or not 0<=word<3:
            raise ValueError('Invalid zone, room or switch word')
        if bytes(self.memory.read_bytes(0x803BD23C,11))!=token['stage']:
            raise ValueError('Stage changed; select the switch word again')
        base=GAME_INFO[tree.version]+0x7A8+zone*0x4C
        owner=struct.unpack('>b',self.memory.read_bytes(base,1))[0]
        room_status=0x803B1188+room*0x114
        mapped=struct.unpack('>b',self.memory.read_bytes(room_status+0x107,1))[0]
        room_proc=struct.unpack('>I',self.memory.read_bytes(room_status+0x10C,4))[0]
        if owner!=room or mapped!=zone or room_proc!=token['room_proc']:
            raise ValueError('Room/zone allocation changed; select the switch word again')
        return base+2+word*2

    def read_zone_switch(self, tree, token, word):
        address=self._zone_switch_address(tree,token,word)
        return struct.unpack('>H',self.memory.read_bytes(address,2))[0]

    def write_zone_switch(self, tree, token, word, text):
        text=text.strip()
        value=int(text,16) if text.lower().startswith('0x') else int(text,10)
        if not 0<=value<=0xFFFF:raise ValueError('Switch word must be 0..65535 (0x0000..0xFFFF)')
        address=self._zone_switch_address(tree,token,word)
        old=bytes(self.memory.read_bytes(address,2))
        data=struct.pack('>H',value)
        try:
            self.memory.write_bytes(address,bytearray(data))
            if bytes(self.memory.read_bytes(address,2))!=data:
                raise ValueError('Switch word readback failed')
        except Exception:
            self.memory.write_bytes(address,bytearray(old))
            raise
        self.refresh(tree)
        return value

    def write_target_state(self, tree, token, enabled):
        if tree.mode not in ('zone','flags') or tree.version!='GZLJ01' or bytes(self.memory.read_bytes(0x80000000,6))!=b'GZLJ01':
            raise ValueError('Switch edit requires the running JP/GZLJ01 game')
        if bytes(self.memory.read_bytes(0x803BD23C,11))!=token['stage']:
            raise ValueError('Stage changed; select the State cell again')
        self.refresh(tree)
        current=next((r.get('state_edit') for r in self.rows('zone') if r['key']==token['key']),None)
        if current!=token:
            raise ValueError('Actor or switch target changed; select the State cell again')
        number=token['number'];address=token['address']
        if address is None:raise ValueError('Switch storage is unavailable')
        if token['scope']=='Zone':
            self._zone_switch_address(tree,token['zone_token'],(number-192)//16)
            size,bit=2,(number-192)%16
        elif token['scope'] in ('Stage','Dungeon'):
            size,bit=4,(number if number<128 else number-128)%32
        else:raise ValueError('Invalid switch storage')
        old=bytes(self.memory.read_bytes(address,size))
        value=int.from_bytes(old,'big');mask=1<<bit
        data=((value|mask) if enabled else (value&~mask)).to_bytes(size,'big')
        try:
            self.memory.write_bytes(address,bytearray(data))
            if bytes(self.memory.read_bytes(address,size))!=data:
                raise ValueError('Switch state readback failed')
        except Exception:
            self.memory.write_bytes(address,bytearray(old))
            raise
        self.refresh(tree)

    def _zone(self, tree):
        if tree.version != 'GZLJ01':
            self.messages['zone'] = 'Zone room-control address is currently verified for JP/GZLJ01 only.'
            return
        save_base=GAME_INFO[tree.version]
        rooms,zones,_statuses=read_zone_tables(self.memory,0x803B1188,save_base+0x7A8)
        rows=[dict(key=('zone-section','switches'),section='Zone switches — mZone[].mZoneBit.mSwitch[3]',color=NORMAL,detail='')]
        for number,zone in sorted(zones.items()):
            switches=zone['switches']
            bits=set_flag_numbers(switches,16,192)
            text=', '.join(str(n) for n in bits) or 'none'
            address=save_base+0x7A8+number*0x4C+2
            token=dict(version=tree.version,zone=number,room=zone['room'],
                       stage=bytes(self.memory.read_bytes(0x803BD23C,11)),
                       room_proc=struct.unpack('>I',self.memory.read_bytes(0x803B1188+zone['room']*0x114+0x10C,4))[0])
            rows.append(dict(key=('zone-switch',number),color=NORMAL,zone_edit=token,
                values=[str(number),str(zone['room']),*['0x%04X'%n for n in switches],text],
                detail='Zone %d | RoomNo %d | mSwitch[3] @%08X\n[0] 0x%04X = switches 192..207 | [1] 0x%04X = 208..223 | [2] 0x%04X = 224..239\nSet switches: %s | mSwitch[2] is cleared by clearRoomSwitch' %
                       (number,zone['room'],address,*switches,text)))
        rows.append(dict(key=('zone-section','actors'),section='Actor switch targets — roomNo + switchNo (conditional calls)',color=NORMAL,detail=''))
        rows.append(dict(key=('zone-header','actors'),table_header=True,color=NORMAL,columns=ZONE_COLUMNS,
                         values=[name for name,_x in ZONE_COLUMNS],detail=''))
        stay=struct.unpack('>b',self.memory.read_bytes(0x803E9F48,1))[0]
        count=0;actors=set()
        for process in sorted(tree.actors,key=lambda a:a['pid'],reverse=True):
            if process['kind']!='actor':continue
            try:
                raw=self.memory.read_bytes(process['addr'],0x20C)
                if struct.unpack_from('>I',raw,4)[0]!=process['pid']:continue
                proc=struct.unpack_from('>H',raw,8)[0]
                targets=actor_switches(self.memory,process['addr'],proc,raw,rooms,zones,save_base,stay)
                for target in targets:
                    number=target['number'];room=target['room'];scope=target['scope']
                    key=('zone-target',process['pid'],target['index'])
                    state_edit=dict(key=key,actor=process['addr'],proc=proc,number=number,room=room,
                                    scope=scope,address=target['address'],
                                    stage=bytes(self.memory.read_bytes(0x803BD23C,11)))
                    if scope=='Zone' and target['address'] is not None:
                        state_edit['zone_token']=next(r['zone_edit'] for r in rows if r['key']==('zone-switch',target['zone']))
                    detail=('%s | bsPCID %d | RoomNo %s (%s) | SwitchNo %d (0x%02X)\n'
                            'Scope %s | Zone %s | state %s | switch word %s\n%s | %s | potential write target; execution/conditions not traced') % (
                                process['name'],process['pid'],_num(room),target['room_source'],number,number,
                                scope,_num(target['zone']),target['state'],
                                '-' if target['address'] is None else '%08X'%target['address'],
                                target['label'],target['source'])
                    rows.append(dict(key=key,state_edit=state_edit,color=NORMAL,columns=ZONE_COLUMNS,
                        values=[process['name'],str(process['pid']),_num(room),str(number),scope,_num(target['zone']),
                                target['state'],target['room_source'],target['label']],detail=detail))
                    count+=1;actors.add(process['pid'])
            except Exception:continue
        self.tables['zone']=rows
        self.messages['zone']='%d loaded zones | %d potential switch targets from %d actors | 61 verified actor types; other switch sources not yet decoded' % (len(zones),count,len(actors))
