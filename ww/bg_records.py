"""Editable live DZB property records and groups for the BG inspector."""
import math
import struct

from .actor_tree import GAME_INFO, PLAY_OFFSET
from .collision_tools import FIELDS, decode_properties, edit_words

COLUMNS = (('Record', 0), ('Value', 640),
           ('Start (right-click to copy)', 830))
SUBTABS = (('meshes', 'Meshes', 100), ('ti', 'Ti Record', 120), ('group', 'DZB group', 130))
GROUP_FIELDS = {
    'name_pointer': (0, 'I'),
    **{'scale.'+axis: (4+i*4, 'f') for i, axis in enumerate('xyz')},
    **{'rotation.'+axis: (0x10+i*2, 'h') for i, axis in enumerate('xyz')},
    **{'translation.'+axis: (0x18+i*4, 'f') for i, axis in enumerate('xyz')},
    'parent': (0x24, 'H'), 'next_sibling': (0x26, 'H'), 'first_child': (0x28, 'H'),
    'room_id': (0x2A, 'H'), 'first_vertex_index': (0x2C, 'H'), 'tree_index': (0x2E, 'H'),
    'm_info': (0x30, 'I'),
    'water': (0x30, 0x100), 'lava': (0x30, 0x200),
    'poison': (0x30, 0x400), 'light': (0x30, 0x80000),
}


def bounded(address, size):
    return size >= 0 and 0x80000000 <= address < address+size <= 0x81800000


class BgRecords:
    def __init__(self, memory):
        self.memory = memory
        self.reset()

    def reset(self):
        self.records = {'ti': [], 'group': []}
        self.open = set()
        self.message = 'Select Ti Record or DZB group to inspect loaded BG data.'
        self._row_cache = {}

    def refresh(self, tree, frame=0):
        self._row_cache = {}
        self.records = {'ti': [], 'group': []}
        if tree.version not in GAME_INFO:
            self.message = 'Waiting for a supported Wind Waker build.'
            return
        stage = bytes(self.memory.read_bytes(GAME_INFO[tree.version]+PLAY_OFFSET+0x5134, 11))
        base = GAME_INFO[tree.version]+PLAY_OFFSET
        registry = bytes(self.memory.read_bytes(base, 256*0x14))
        errors = 0
        # Shared tables are displayed once, with every registration in the identity token.
        tables = {}
        for slot in range(256):
            registration = registry[slot*0x14:slot*0x14+16]
            bgw, flags, pid, owner = struct.unpack('>4I', registration)
            if not flags & 1:
                continue
            try:
                if not bounded(bgw, 0xA8): raise ValueError('invalid cBgW')
                raw = bytes(self.memory.read_bytes(bgw,0xA8))
                dzb = struct.unpack_from('>I',raw,0x94)[0]
                if not bounded(dzb,0x30): raise ValueError('invalid DZB')
                header = bytes(self.memory.read_bytes(dzb,0x30))
                gn, gt, tn, tt = struct.unpack_from('>iIiI',header,0x20)
                tri_count, tri_table = struct.unpack_from('>iI',header,8)
                if not 0 <= tri_count <= 600000 or (tri_count and not bounded(tri_table,tri_count*10)):
                    raise ValueError('invalid triangle table')
                movable = bool(raw[0x6C]&1) or (owner != 0 and not raw[0x6C]&0x20)
                label = '%s #%d' % ('MoveBG' if movable else 'BG',slot)
                token = dict(version=tree.version,stage=stage,slot=slot,registration=registration,
                             bgw=bgw,dzb=dzb,header=header)
                for kind,count,address,stride in (('ti',tn,tt,16),('group',gn,gt,0x34)):
                    if not 0<=count<=65536 or (count and not bounded(address,count*stride)):
                        raise ValueError('invalid record table')
                    key = kind,address,count
                    if key not in tables:
                        tables[key] = dict(kind=kind,address=address,count=count,stride=stride,
                                           tokens=[],labels=[],tri_table=tri_table,tri_count=tri_count)
                    tables[key]['tokens'].append(token)
                    tables[key]['labels'].append(label)
            except Exception:
                errors += 1
        for table in tables.values():
            try:
                raw = bytes(self.memory.read_bytes(table['address'],table['count']*table['stride']))
                for index in range(table['count']):
                    address = table['address']+index*table['stride']
                    record = dict(table=table,index=index,address=address,
                                  raw=raw[index*table['stride']:(index+1)*table['stride']],
                                  key=(table['kind'],table['address'],index))
                    self.records[table['kind']].append(record)
            except Exception:
                errors += 1
        keys = {r['key'] for records in self.records.values() for r in records}
        self.open.intersection_update(keys)
        self.message = '%d Ti records | %d DZB groups%s. Expand a record; click Value to edit; right-click Start to copy.' % (
            len(self.records['ti']),len(self.records['group']), ' | %d unreadable meshes/tables'%errors if errors else '')

    def rows(self, kind):
        cache_key = kind, frozenset(self.open)
        if cache_key in self._row_cache:
            return self._row_cache[cache_key]
        rows = []
        for record in self.records[kind]:
            prefix = ', '.join(record['table']['labels'])
            if kind == 'ti':
                values = decode_properties(struct.unpack('>4I',record['raw']))
                fields = list(FIELDS)
            else:
                values = {}
                for name,(offset,fmt) in GROUP_FIELDS.items():
                    values[name] = (int(bool(struct.unpack_from('>I',record['raw'],offset)[0]&fmt))
                                    if isinstance(fmt,int) else struct.unpack_from('>'+fmt,record['raw'],offset)[0])
                fields = list(GROUP_FIELDS)
            rows.append(dict(key=record['key'],record=record,field=None,address=record['address'],
                             values=[prefix+' / '+('Ti ' if kind=='ti' else 'Group ')+str(record['index']),'',
                                     '%08X'%record['address']]))
            if record['key'] not in self.open: continue
            for name in fields:
                value = values[name]
                if kind=='ti': offset=FIELDS[name][0]*4
                else: offset=GROUP_FIELDS[name][0]
                shown = ('0x%08X'%value if name.startswith('info') or name in ('m_info','name_pointer') else str(value))
                rows.append(dict(key=record['key']+(name,),record=record,field=name,address=record['address']+offset,
                                 values=[name,shown,'%08X'%(record['address']+offset)]))
        self._row_cache = {cache_key: rows}
        return rows

    def validate(self, tree, record):
        table = record['table']
        for token in table['tokens']:
            if tree.version != token['version'] or bytes(self.memory.read_bytes(0x80000000,6)).decode('ascii')!=tree.version:
                raise ValueError('Game/region changed; select the field again')
            if bytes(self.memory.read_bytes(GAME_INFO[tree.version]+PLAY_OFFSET+0x5134,11))!=token['stage']:
                raise ValueError('Stage changed; select the field again')
            base=GAME_INFO[tree.version]+PLAY_OFFSET+token['slot']*0x14
            if bytes(self.memory.read_bytes(base,16))!=token['registration']:
                raise ValueError('BG registration changed; select the field again')
            if struct.unpack('>I',bytes(self.memory.read_bytes(token['bgw']+0x94,4)))[0]!=token['dzb']:
                raise ValueError('DZB replaced; select the field again')
            if bytes(self.memory.read_bytes(token['dzb'],0x30))!=token['header']:
                raise ValueError('DZB tables changed; select the field again')
        if bytes(self.memory.read_bytes(record['address'],table['stride']))!=record['raw']:
            raise ValueError('Record changed; select the field again')

    def write(self, tree, row, text):
        record, field = row['record'], row['field']
        if field is None: raise ValueError('Expand the record and select a Value')
        self.validate(tree,record)
        data = bytearray(record['raw'])
        if record['table']['kind']=='ti':
            data[:] = struct.pack('>4I',*edit_words(struct.unpack('>4I',data),field,text.strip()))
        else:
            offset, fmt = GROUP_FIELDS[field]
            if isinstance(fmt,int):
                value=int(text.strip(),0)
                if value not in (0,1): raise ValueError('Category bit must be 0 or 1')
                old=struct.unpack_from('>I',data,offset)[0]
                struct.pack_into('>I',data,offset,(old|fmt) if value else (old&~fmt))
            else:
                value=float(text) if fmt=='f' else int(text.strip(),0)
                if fmt=='f' and not math.isfinite(value): raise ValueError('Value must be finite')
                struct.pack_into('>'+fmt,data,offset,value)
        try:
            self.memory.write_bytes(record['address'],data)
            if bytes(self.memory.read_bytes(record['address'],len(data)))!=bytes(data):
                raise ValueError('BG record readback failed')
        except Exception:
            self.memory.write_bytes(record['address'],bytearray(record['raw']))
            raise
