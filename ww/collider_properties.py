"""Complete scalar layouts for dCcD colliders and their shared status blocks."""
import struct

# offset, struct format, writable, shift, width (None means the whole value)
FIELDS={
 'at_type':(0x10,'I',True,0,32),'at_power':(0x14,'B',True,0,8),
 'tg_type':(0x28,'I',True,0,32),'object_flags':(0x40,'I',True,0,32),
 'status_pointer':(0x44,'I',False,0,32),
}
for side,offset,generic in (('at',0,0x50),('tg',0x18,0x94),('co',0x2C,0xDC)):
 FIELDS.update({
  side+'_sprm':(offset,'I',True,0,32),side+'_enabled':(offset,'I',True,0,1),
  side+'_rprm':(offset+4,'I',True,0,32),side+'_hit':(offset+4,'I',True,0,1),
  side+'_hit_object':(offset+8,'I',True,0,32),
  side+'_g_sprm':(generic,'I',True,0,32),side+'_g_rprm':(generic+4,'I',True,0,32),
  side+'_callback':(generic+8,'I',True,0,32),side+'_hit_pid':(generic+12,'I',True,0,32),
  side+'_cached_actor':(generic+16,'I',True,0,32),side+'_effect_counter':(generic+20,'b',True,0,None),
 })
for side,offset in (('at',0),('tg',0x18)):
 FIELDS[side+'_group']=(offset,'I',True,1,3)
 FIELDS[side+'_no_peer_hit_info']=(offset,'I',True,4,1)
for side,base in (('at',0x50),('tg',0x94)):
 for name,offset in (('sound',0x1C),('material',0x1D),('hit_mark',0x1E),('special',0x1F)):
  FIELDS[side+'_'+name]=(base+offset,'B',True,0,8)

def vector_fields(fields,name,offset):
 for i,axis in enumerate('xyz'):fields[name+'.'+axis]=(offset+i*4,'f',True,0,None)

for name,offset in (('at_hit_position',0x70),('at_vector',0x7C),('at_reaction_vector',0x88),
                    ('tg_vector',0xB4),('tg_reaction_vector',0xC0),('tg_hit_position',0xCC),
                    ('aabb_min',0xF8),('aabb_max',0x104)):
 vector_fields(FIELDS,name,offset)
FIELDS.update(divide_range_bits=(0x48,'I',True,0,32),
              tg_shield_angle_pointer=(0xD8,'I',True,0,32))
for name,offset in (('at_vtable',0xC),('tg_vtable',0x24),('co_vtable',0x38),
                    ('object_vtable',0x3C),('divide_vtable',0x4C),('at_g_vtable',0x68),
                    ('tg_g_vtable',0xAC),('co_g_vtable',0xF4),('aabb_vtable',0x110),('shape_vtable',0x114)):
 FIELDS[name]=(offset,'I',False,0,32)

STATUS_FIELDS={
 'status.owner':(0xC,'I',False,0,32),'status.pid':(0x10,'I',False,0,32),
 'status.weight':(0x14,'B',True,0,8),'status.field_0x15':(0x15,'B',True,0,8),
 'status.damage':(0x16,'B',True,0,8),'status.vtable':(0x18,'I',False,0,32),
 'status.g_vtable':(0x1C,'I',False,0,32),'status.at_special':(0x20,'B',True,0,8),
 'status.tg_special':(0x21,'B',True,0,8),'status.room_id':(0x22,'B',True,0,8),
 'status.actor_perf_table_id':(0x24,'H',True,0,16),
 'status.at_pid':(0x28,'I',True,0,32),'status.at_old_pid':(0x2C,'I',True,0,32),
 'status.tg_pid':(0x30,'I',True,0,32),'status.tg_old_pid':(0x34,'I',True,0,32),
 'status.flags':(0x38,'I',True,0,32),
}
vector_fields(STATUS_FIELDS,'status.cc_move',0)

def fill_layout(fields,size,prefix=''):
 """Expose padding as bytes without reading beyond this concrete shape."""
 covered=set()
 for offset,fmt,*_ in fields.values():covered.update(range(offset,offset+struct.calcsize('>'+fmt)))
 for offset in range(size):
  if offset not in covered:fields[prefix+'unknown_%03X'%offset]=(offset,'B',True,0,8)
 return fields

fill_layout(STATUS_FIELDS,0x3C,'status.')
LAYOUTS={}
for kind,size in (('sphere',0x12C),('cylinder',0x130),('capsule',0x138),('triangle',0x150),('box',0x118)):
 fields=dict(FIELDS)
 if kind in ('sphere','cylinder'):
  vector_fields(fields,'shape.center',0x118)
  fields['shape.radius']=(0x124,'f',True,0,None)
  if kind=='cylinder':fields['shape.height']=(0x128,'f',True,0,None)
  fields['geometry_vtable']=(size-4,'I',False,0,32)
 elif kind=='capsule':
  vector_fields(fields,'shape.start',0x118);vector_fields(fields,'shape.end',0x124)
  fields['geometry_vtable']=(0x130,'I',False,0,32)
  fields['shape.radius']=(0x134,'f',True,0,None)
 elif kind=='triangle':
  vector_fields(fields,'shape.normal',0x118)
  fields['shape.plane_d']=(0x124,'f',True,0,None)
  fields['geometry_vtable']=(0x128,'I',False,0,32)
  for name,offset in (('a',0x12C),('b',0x138),('c',0x144)):vector_fields(fields,'shape.'+name,offset)
 fill_layout(fields,size)
 fields=dict(sorted(fields.items(),key=lambda item:item[1][0]))
 fields.update(sorted(STATUS_FIELDS.items(),key=lambda item:item[1][0]))
 LAYOUTS[kind]=fields

# The inspector is a memory editor: even identity and dispatch fields are writable.
for fields in (FIELDS,STATUS_FIELDS,*LAYOUTS.values()):
 for name,(offset,fmt,_writable,shift,width) in tuple(fields.items()):
  fields[name]=(offset,fmt,True,shift,width)

def field_specs(kind):return LAYOUTS[kind]

def raw_values(edits,address,kind):
 raw=bytes(edits.memory.read_bytes(address,0x150))
 status=struct.unpack_from('>I',raw,0x44)[0]
 info=None
 if 0x80000000<=status<=0x817FFFC4:
  try:info=bytes(edits.memory.read_bytes(status,0x3C))
  except Exception:pass
 values={}
 for name,(offset,fmt,_writable,shift,width) in field_specs(kind).items():
  data=info if name.startswith('status.') else raw
  if data is None:values[name]=None;continue
  value=struct.unpack_from('>'+fmt,data,offset)[0]
  values[name]=(value>>shift)&((1<<width)-1) if width is not None else value
 values['status_pointer']=status
 values.update(owner=values['status.owner'],owner_pid=values['status.pid'],status_pid=values['status.pid'],
               status_weight=values['status.weight'],status_damage=values['status.damage'])
 return raw,values


def read_properties(edits,address,identity=None):
 raw,shape,current=edits.read(address)
 if identity is not None and current!=identity:raise ValueError('collider identity changed; select it again')
 values={}
 status=struct.unpack_from('>I',raw,0x44)[0]
 info=bytes(edits.memory.read_bytes(status,0x3C))
 for name,(offset,fmt,_writable,shift,width) in field_specs(shape[0]).items():
  value=struct.unpack_from('>'+fmt,info if name.startswith('status.') else raw,offset)[0]
  values[name]=(value>>shift)&((1<<width)-1) if width is not None else value
 values.update(owner=shape[1],owner_pid=current[1],status_pid=struct.unpack_from('>I',info,0x10)[0],
               status_weight=info[0x14],status_damage=info[0x16])
 return raw,shape,current,values


def write_property(edits,address,identity,field,text,raw_kind=None):
 if raw_kind is None:
  raw,_shape,_current,_values=read_properties(edits,address,identity)
  kind=_shape[0]
 else:
  kind=raw_kind;raw,_values=raw_values(edits,address,kind)
 fields=field_specs(kind)
 if field not in fields:raise ValueError('Unknown collider property: '+field)
 offset,fmt,writable,shift,width=fields[field]
 if not writable:raise ValueError(field+' is read-only (identity or vtable)')
 if field.startswith('status.') and _values[field] is None:
  raise ValueError('Status pointer is unreadable; edit status_pointer to restore it first')
 target=(_values['status_pointer'] if field.startswith('status.') else address)+offset
 previous=bytes(edits.memory.read_bytes(target,struct.calcsize('>'+fmt)))
 if fmt=='f':
  value=float(text)
  data=struct.pack('>f',value)
 elif width is None:
  value=int(text,0);data=struct.pack('>'+fmt,value)
 else:
  value=int(text,0)
  if not 0<=value<(1<<width):raise ValueError('%s must be 0..%d' % (field,(1<<width)-1))
  old=struct.unpack('>'+fmt,previous)[0];mask=((1<<width)-1)<<shift
  data=struct.pack('>'+fmt,(old&~mask)|(value<<shift))
 try:
  edits.memory.write_bytes(target,bytearray(data))
  if bytes(edits.memory.read_bytes(target,len(data)))!=data:raise ValueError('collider write verification failed')
 except Exception:
  edits.memory.write_bytes(target,bytearray(previous))
  raise
 return value
