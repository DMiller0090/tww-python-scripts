"""Read/edit existing JP stage/room SCLS entries without source parsing."""
import struct

RAM_START,RAM_END=0x80000000,0x81800000


def _range(address,size):
    return RAM_START<=address and 0<=size and address+size<=RAM_END


def _number(text):
    if isinstance(text,int):return text
    text=text.strip()
    return int(text,16 if text.lower().startswith('0x') else 10)


class SclsEditor:
    def __init__(self,memory,game_info=0x803B8108,room_status=0x803B1188):
        self.memory,self.game_info,self.room_status=memory,game_info,room_status

    def tables(self):
        """Discover stage and all room lists with live, non-null SCLS pointers."""
        if bytes(self.memory.read_bytes(0x80000000,6))!=b'GZLJ01':
            raise ValueError('SCLS editing is verified for JP/GZLJ01 only')
        stage=bytes(self.memory.read_bytes(self.game_info+0x5134,11))
        tables,errors={},{}
        for scope in range(-1,64):
            field=(self.game_info+0x12A0+0x3EB0+0x4C if scope==-1 else
                   self.room_status+scope*0x114+0x38)
            try:
                if not struct.unpack('>I',self.memory.read_bytes(field,4))[0]:continue
                tables[scope]=self.table(scope)
            except Exception as exc:errors[scope]=str(exc)
        if stage!=bytes(self.memory.read_bytes(self.game_info+0x5134,11)):
            raise ValueError('Stage changed while reading SCLS lists; refresh again')
        return tables,errors

    def table(self,source='current',current_room=-1):
        if bytes(self.memory.read_bytes(0x80000000,6))!=b'GZLJ01':
            raise ValueError('SCLS editing is verified for JP/GZLJ01 only')
        source=str(source).strip().lower()
        room=current_room if source=='current' else -1 if source=='stage' else _number(source)
        if room==255:room=-1
        if not -1<=room<64:raise ValueError('Source must be current, stage or room 0..63')
        stage=bytes(self.memory.read_bytes(self.game_info+0x5134,11))
        if room==-1:
            field=self.game_info+0x12A0+0x3EB0+0x4C;room_proc=None
        else:
            base=self.room_status+room*0x114;field=base+0x38
            room_proc=struct.unpack('>I',self.memory.read_bytes(base+0x10C,4))[0]
        header=struct.unpack('>I',self.memory.read_bytes(field,4))[0]
        label='Stage' if room==-1 else 'Room %d'%room
        if header==0:raise ValueError('%s has no SCLS list; stage and room lists are separate'%label)
        if header&3 or not _range(header,8):raise ValueError('Invalid SCLS header pointer')
        count,entries=struct.unpack('>iI',self.memory.read_bytes(header,8))
        if not 0<=count<=4096 or (count and (entries&3 or not _range(entries,count*12))):
            raise ValueError('Invalid SCLS count or entry range')
        raw=bytes(self.memory.read_bytes(entries,count*12)) if count else b''
        identity=(stage,room,room_proc,field,header,count,entries)
        decoded=[]
        for index in range(count):
            record=raw[index*12:index*12+12]
            name=record[:8].split(b'\0',1)[0].decode('ascii',errors='replace')
            decoded.append(dict(index=index,address=entries+index*12,stage=name,
                                spawn=record[8],room=record[9],wipe=record[10],extra=record[11],raw=record))
        return dict(identity=identity,scope=room,label=label,count=count,entries=entries,records=decoded)

    def select(self,table,index):
        index=_number(index)
        if not 0<=index<table['count']:raise ValueError('SCLS index must be 0..%d'%(table['count']-1))
        return dict(identity=table['identity'],scope=table['scope'],index=index,raw=table['records'][index]['raw'])

    def write(self,token,stage,spawn,room,wipe,extra):
        table=self.table(token['scope'])
        if table['identity']!=token['identity']:raise ValueError('SCLS source changed; read the entry again')
        index=token['index']
        if not 0<=index<table['count']:raise ValueError('SCLS index is no longer valid')
        record=table['records'][index]
        if record['raw']!=token['raw']:raise ValueError('SCLS entry changed; read it again before editing')
        name=stage.strip().encode('ascii')
        if not 1<=len(name)<=8 or any(c<0x21 or c>0x7E for c in name):
            raise ValueError('Stage name must contain 1..8 printable ASCII characters without spaces')
        numbers=tuple(_number(v) for v in (spawn,room,wipe,extra))
        if any(not 0<=v<=255 for v in numbers):raise ValueError('Spawn, room, wipe and +0B must be 0..255')
        data=name.ljust(8,b'\0')+bytes(numbers);address=record['address'];old=record['raw']
        try:
            self.memory.write_bytes(address,bytearray(data))
            if bytes(self.memory.read_bytes(address,12))!=data:raise ValueError('SCLS entry readback failed')
        except Exception:
            self.memory.write_bytes(address,bytearray(old));raise
        return self.select(self.table(token['scope']),index)
