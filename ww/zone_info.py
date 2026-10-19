"""Display-only room/zone flags (dStage_roomStatus_c and dSv_zone_c)."""
import struct


def read_zone_tables(reader, room_base, zone_base, room_count=64, zone_count=32):
    """Return room->zone, allocated zone slots and room status; no source parsing."""
    rooms, zones, statuses = {}, {}, {}
    room_data = reader.read_bytes(room_base, room_count * 0x114)
    for room in range(room_count):
        offset = room * 0x114
        zone = struct.unpack_from('>b', room_data, offset + 0x107)[0]
        statuses[room] = (room_data[offset+0x104], bool(room_data[offset+0x105]))
        if 0 <= zone < zone_count:
            rooms[room] = zone
    zone_data = reader.read_bytes(zone_base, zone_count * 0x4C)
    for zone in range(zone_count):
        offset = zone * 0x4C
        room = struct.unpack_from('>b', zone_data, offset)[0]
        if 0 <= room < room_count:
            zones[zone] = dict(room=room,
                switches=struct.unpack_from('>3H', zone_data, offset+2),
                items=struct.unpack_from('>H', zone_data, offset+8)[0],
                actors=struct.unpack_from('>16I', zone_data, offset+0xC))
    return rooms, zones, statuses


def set_flag_numbers(words, bits, start=0):
    return [start+index*bits+bit for index, word in enumerate(words)
            for bit in range(bits) if word & (1 << bit)]


def actor_flag_state(actor, zones):
    """fopAcM_onActor uses setID and home.roomNo, not current.roomNo."""
    flag = actor['set_id']
    if flag == 0xFFFF or actor['home_room'] is None:
        return 'none'
    if not 0 <= flag < 512:
        return 'invalid setID'
    zone = zones.get(actor['flag_zone'])
    if zone is None or zone['room'] != actor['home_room']:
        return 'unavailable (home zone unloaded)'
    return 'SET' if zone['actors'][flag >> 5] & (1 << (flag & 31)) else 'clear'


