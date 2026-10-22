"""Offline flag-sheet catalog and guarded live event/scene byte edits."""
import json
import os

from .actor_tree import GAME_INFO

TABLES=(('story','Story Flags',180),('registers','Event Registers',180),
        ('scene','Scene Flags',180),('zone','Zone Switches',180))
ROW_HEIGHT=82
LABEL_WIDTH=100
GREEN,RED,UNKNOWN=0xFF55D98A,0xFFFF6060,0xFF687582


class FlagView:
    def __init__(self,memory,catalog=None):
        self.memory=memory
        if catalog is None:
            with open(os.path.join(os.path.dirname(__file__),'data','flags.json'),encoding='utf-8') as source:
                catalog=json.load(source)
        if catalog.get('version')!=1:raise ValueError('Unsupported flag catalog')
        self.catalog=catalog
        self.reset()

    def reset(self):
        self.rows=[];self.message='Select a flag table.';self.table='story'

    def refresh(self,tree,table):
        self.rows=[];self.table=table
        if tree.version not in GAME_INFO:
            self.message='Waiting for a supported Wind Waker build.';return
        try:
            save=GAME_INFO[tree.version]
            if table=='scene':
                base=save+0x778
                data=bytes(self.memory.read_bytes(base,0x25))
                stage_id=data[0x24]
                if stage_id>=16:
                    self.message='Scene Flags | waiting for a loaded stage bank.';return
                context=(bytes(self.memory.read_bytes(save+0x5134,11)),stage_id)
                sheet=next((s for s in self.catalog['scenes'] if s['stage_id']==stage_id),None)
                if sheet is None:
                    sheet=dict(name='Stage %02X (undocumented)'%stage_id,rows=[
                        dict(offset=n,cells=[dict(column=i,span=1,label='') for i in range(8)]) for n in range(0x25)])
                self.message='Scene Flags | %s | stage ID %02X | live scene memory'%(sheet['name'],stage_id)
            else:
                base=save+0x624;data=bytes(self.memory.read_bytes(base,0x100))
                sheet=self.catalog[table];context=None
                self.message=('%s | Click a flag cell to enable or disable it. Merged registers open the value editor.' %
                              ('Story Flags' if table=='story' else 'Event Registers (incomplete source)'))
            for source in sheet['rows']:
                offset=source['offset'];cells=[]
                for spec in source['cells']:
                    column,span=spec['column'],spec['span']
                    shift=8-column-span;mask=((1<<span)-1)<<shift
                    token=dict(version=tree.version,table=table,offset=offset,mask=mask,context=context)
                    value=(data[offset]&mask)>>shift
                    writable=table!='scene' or offset<0x22
                    cells.append(dict(spec,token=token,value=value,maximum=mask>>shift,
                                      writable=writable,key=(table,offset,column),
                                      border=(GREEN if value else RED) if writable else UNKNOWN))
                self.rows.append(dict(offset=offset,address=base+offset,value=data[offset],cells=cells))
        except Exception as exc:
            self.rows=[];self.message='Flags read failed: '+str(exc)

    def _address(self,tree,token):
        version=token['version']
        if tree.mode!='flags' or tree.version!=version or version not in GAME_INFO:
            raise ValueError('Game/region changed; select the flag again')
        if bytes(self.memory.read_bytes(0x80000000,6))!=version.encode('ascii'):
            raise ValueError('Running game does not match the selected flag')
        table,offset,mask=token['table'],token['offset'],token['mask']
        if not isinstance(mask,int) or not 0<mask<=255:raise ValueError('Invalid flag mask')
        save=GAME_INFO[version]
        if table=='scene':
            if not 0<=offset<0x22:raise ValueError('Scene padding and stage ID are read-only')
            context=(bytes(self.memory.read_bytes(save+0x5134,11)),self.memory.read_bytes(save+0x79C,1)[0])
            if context!=token['context']:raise ValueError('Scene changed; select the flag again')
            return save+0x778+offset
        if table not in ('story','registers') or not (0<=offset<0x70 if table=='story' else 0x70<=offset<0x100):
            raise ValueError('Invalid event flag offset')
        return save+0x624+offset

    def read(self,tree,token):
        address=self._address(tree,token);mask=token['mask'];shift=(mask&-mask).bit_length()-1
        return (self.memory.read_bytes(address,1)[0]&mask)>>shift

    def write(self,tree,token,value=None):
        address=self._address(tree,token);mask=token['mask'];shift=(mask&-mask).bit_length()-1
        old=bytes(self.memory.read_bytes(address,1));current=(old[0]&mask)>>shift
        if value is None:value=0 if current else mask>>shift
        if isinstance(value,str):
            value=value.strip();value=int(value,16 if value.lower().startswith('0x') else 10)
        if not isinstance(value,int) or not 0<=value<=mask>>shift:
            raise ValueError('Value must be 0..%d'%(mask>>shift))
        data=bytes([(old[0]&~mask)|((value<<shift)&mask)])
        try:
            self.memory.write_bytes(address,bytearray(data))
            if bytes(self.memory.read_bytes(address,1))!=data:raise ValueError('Flag readback failed')
        except Exception:
            self.memory.write_bytes(address,bytearray(old));raise
        self.refresh(tree,token['table'])
        return value

    def cell(self,row,column):
        if not 0<=row<len(self.rows):return None
        return next((c for c in self.rows[row]['cells'] if c['column']<=column<c['column']+c['span']),None)
