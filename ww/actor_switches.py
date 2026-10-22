"""Self-contained switch-write targets decoded from actor fields/parameters.

The catalog records verified onSwitch arguments, not whether a conditional
call executed. No decomp source, runtime source parsing or CPU hooks are used.
"""
import json
import os
import struct

_CATALOG = None


def load_switch_catalog():
    global _CATALOG
    if _CATALOG is None:
        with open(os.path.join(os.path.dirname(__file__),'data','actor_switches.json'),encoding='utf-8') as source:
            document=json.load(source)
        if document.get('version')!=1:raise ValueError('Unsupported switch catalog')
        _CATALOG=document['actors']
    return _CATALOG


def _read(reader,base,header,spec):
    if 'const' in spec:return spec['const']
    offset,fmt=spec['offset'],'>'+spec['format']
    size=struct.calcsize(fmt)
    raw=header[offset:offset+size] if offset+size<=len(header) else reader.read_bytes(base+offset,size)
    value=struct.unpack(fmt,raw)[0]
    value >>= spec.get('shift',0)
    if 'mask' in spec:value &= spec['mask']
    return value+spec.get('add',0)


def switch_state(reader,save_base,number,room,rooms,zones):
    """Match dSv_info_c::onSwitch routing across all three switch banks."""
    if number in (-1,255):return 'None',None,None,None
    if not 0<=number<240:return 'Invalid',None,None,None
    if number<128:
        address=save_base+0x778+4+(number>>5)*4
        word=struct.unpack('>I',reader.read_bytes(address,4))[0]
        return 'Stage',bool(word & (1<<(number&31))),None,address
    if number<192:
        bit=number-128;address=save_base+0x79C+4+(bit>>5)*4
        word=struct.unpack('>I',reader.read_bytes(address,4))[0]
        return 'Dungeon',bool(word & (1<<(bit&31))),None,address
    zone_no=rooms.get(room);zone=zones.get(zone_no)
    if zone is None or zone['room']!=room:return 'Zone',None,zone_no,None
    bit=number-192
    address=save_base+0x7A8+zone_no*0x4C+2+(bit>>4)*2
    return 'Zone',bool(zone['switches'][bit>>4] & (1<<(bit&15))),zone_no,address


def actor_switches(reader,base,proc,header,rooms,zones,save_base=0x803B8108,stay_room=None,catalog=None):
    """Return each configured switchNo and the actual room argument its call uses."""
    entries=(load_switch_catalog() if catalog is None else catalog).get(str(proc),())
    out=[]
    for index,spec in enumerate(entries):
        try:
            condition=spec.get('when')
            if condition:
                value=_read(reader,base,header,condition)
                if 'equals' in condition and value!=condition['equals']:continue
                if 'not_equals' in condition and value==condition['not_equals']:continue
            number=_read(reader,base,header,spec)
            if 0x80000000<=number<=0xFFFFFFFF:number-=0x100000000  # onSwitch takes int.
            if number in (-1,255) or number in spec.get('disabled',()):continue
            mode=spec['room']
            if mode=='home':room=struct.unpack_from('>b',header,0x1E2)[0]
            elif mode=='current':room=struct.unpack_from('>b',header,0x20A)[0]
            elif mode=='stay':room=stay_room
            else:room=_read(reader,base,header,spec['room_field'])
            room=room if room is not None and 0<=room<64 else None
            scope,state,zone,address=switch_state(reader,save_base,number,room,rooms,zones)
            out.append(dict(index=index,number=number,room=room,zone=zone,scope=scope,
                            state='SET' if state is True else 'clear' if state is False else
                                  'invalid' if scope=='Invalid' else 'unavailable',
                            room_source=mode,label=spec['label'],source=spec.get('source',''),
                            overlay=spec.get('overlay',True),address=address))
        except (ValueError,KeyError,IndexError,struct.error,OSError):
            continue
    return out
