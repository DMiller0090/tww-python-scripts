"""Inspector reservations using JKRExpHeap's actual free/used lists.

Mirrors JKRExpHeap allocFromHead/appendUsedList/recycleFreeBlock.
Call mutations only from
Dolphin's frame/paused-host callbacks, never from a separate worker thread.
Reserved payloads start with a small tag so reservations survive script reloads
and can be distinguished from game-owned allocations after a heap reset.
"""
import os
import struct

from .heap import HDR, MAGIC

TAG = b"TWWRESV1"
GROUP = 254
MIN_PAYLOAD = 16


def block_extent(block):
    node, size, _group, flags, state = block
    return node - ((flags & 0x7f) if state == "used" else 0), node + HDR + size


def split_plan(expected_block,begin,end,count,gaps=0,block_size=None):
    """Near-equal slots covering the range, with gaps free slots between allocations."""
    _obj,_start,_limit,node,size=expected_block
    if not 1<=count<=4096:raise ValueError('Number of blocks must be between 1 and 4096')
    if not 0<=gaps<=4096:raise ValueError('Block gaps must be between 0 and 4096')
    count=count+(count-1)*gaps
    if count>4096:raise ValueError('Too many blocks including gaps; reduce block count or gaps')
    if begin&3 or end&3:raise ValueError('Start and End must be aligned to 4 bytes')
    limit=node+HDR+size
    if not node+HDR<=begin<end<=limit:raise ValueError('Range must stay inside the clicked free block')
    if block_size is not None:
        if block_size<MIN_PAYLOAD or block_size&3:
            raise ValueError('Block size must be at least 16 bytes and aligned to 4 bytes')
        actual_end=begin+count*block_size+(count-1)*HDR
        if actual_end>end:raise ValueError('Requested block size, count and gaps do not fit Start–End')
        base,extra=block_size//4,0
    else:
        units=(end-begin-(count-1)*HDR)//4
        if units<count*(MIN_PAYLOAD//4):raise ValueError('Range is too small for this many blocks (16-byte minimum payload plus headers)')
        base,extra=divmod(units,count)
        actual_end=end
    if 0<limit-actual_end<HDR+4:raise ValueError('Block sizes leave too little space for a free header; adjust size or range')
    result=[];address=begin-HDR
    for i in range(count):
        payload=(base+(i<extra))*4
        result.append((address,payload));address+=HDR+payload
    return result


class HeapReservations:
    def __init__(self):
        self.records = {}

    def scan(self, heap_map):
        found = {}
        for name, heap in heap_map.heaps.items():
            if not heap.valid:
                continue
            for node, size, group, flags, state in heap.blocks:
                if state != "used" or group != GROUP or size < MIN_PAYLOAD:
                    continue
                token = bytes(heap_map.reader.read_bytes(node + HDR, MIN_PAYLOAD))
                if token.startswith(TAG) and len(token) == MIN_PAYLOAD:
                    found[(name, node)] = dict(name=name, node=node, size=size, flags=flags,
                                               obj=heap.object_addr, start=heap.start, end=heap.end, token=token)
        self.records = found

    def _snapshot(self, heap_map, name):
        heap = heap_map.heaps[name]
        heap_map._walk(heap)
        if not heap.valid:
            raise ValueError("heap is not consistent: " + heap.error)
        reader = heap_map.reader
        obj = heap.object_addr
        # JKRHeap::mMutex (+0x18), OSMutex::thread/count (+8/+0xC).
        # A paused emulated thread may still be inside an allocator operation.
        mutex = bytes(reader.read_bytes(obj + 0x18, 0x18))
        owner, count = struct.unpack_from(">2I", mutex, 8)
        if owner or count:
            raise ValueError("heap allocator is busy; resume briefly and drag again")
        controls = bytes(reader.read_bytes(obj + 0x78, 16))
        heads = struct.unpack(">4I", controls)
        headers = {b[0]: bytes(reader.read_bytes(b[0], HDR)) for b in heap.blocks}
        for node, size, group, flags, state in heap.blocks:
            magic, got_flags, got_group, got_size, _prev, _next = struct.unpack(">HBBIII", headers[node])
            if (got_flags, got_group, got_size) != (flags, group, size):
                raise ValueError("heap changed during read")
        states = {b[0]: b[4] for b in heap.blocks}
        chains, all_seen = [], set()
        for head, tail, state in ((heads[0], heads[1], "free"), (heads[2], heads[3], "used")):
            chain, prev, node = [], 0, head
            while node:
                if node not in headers or node in all_seen or states[node] != state:
                    raise ValueError("heap lists changed during read")
                _magic, _flags, _group, _size, previous, nxt = struct.unpack(">HBBIII", headers[node])
                if previous != prev:
                    raise ValueError("heap has inconsistent back links")
                chain.append(node)
                all_seen.add(node)
                prev, node = node, nxt
            if prev != tail:
                raise ValueError("heap has inconsistent tail pointer")
            chains.append(chain)
        if len(all_seen) != len(headers):
            raise ValueError("heap list membership changed during read")
        if (bytes(reader.read_bytes(heap.addr, 4)) != struct.pack(">I", obj) or
                bytes(reader.read_bytes(obj + 0x30, 12)) != struct.pack(">3I", heap.start, heap.end, heap.size)):
            raise ValueError("heap object changed during read")
        # Do not mutate a partial/in-progress heap snapshot.
        cursor = heap.start
        for block in heap.blocks:
            start, end = block_extent(block)
            if start != cursor:
                raise ValueError("heap has untracked gaps; reservation cancelled")
            cursor = end
        if cursor != heap.end:
            raise ValueError("heap coverage is incomplete")
        original = {obj + 0x18: mutex, obj + 0x78: controls,
                    obj + 0x30: bytes(reader.read_bytes(obj + 0x30, 12)),
                    heap.addr: bytes(reader.read_bytes(heap.addr, 4))}
        original.update(headers)
        return heap, chains[0], chains[1], original

    def _commit(self, heap_map, heap, free, used, blocks, expected, payload=None, payloads=()):
        # Re-link in free-address order and retain the game's used-list order.
        free.sort()
        changes = {}
        for chain, state in ((free, "free"), (used, "used")):
            for i, node in enumerate(chain):
                _node, size, group, flags, _old_state = blocks[node]
                changes[node] = struct.pack(">HBBIII", MAGIC if state == "used" else 0,
                                            flags if state == "used" else 0,
                                            group if state == "used" else 0, size,
                                            chain[i-1] if i else 0, chain[i+1] if i+1 < len(chain) else 0)
        changes[heap.object_addr + 0x78] = struct.pack(">4I", free[0] if free else 0,
                                                      free[-1] if free else 0,
                                                      used[0] if used else 0, used[-1] if used else 0)
        if payload:
            changes[payload[0]] = payload[1]
        changes.update(payloads)
        mem = heap_map.reader
        # Capture originals even where new headers replace formerly free payload.
        backups = {addr: bytes(mem.read_bytes(addr, len(data))) for addr, data in changes.items()}
        for addr, data in expected.items():
            if bytes(mem.read_bytes(addr, len(data))) != data:
                raise ValueError("heap changed; drag again")
        writes = [(addr, data) for addr, data in changes.items() if data != backups[addr]]
        written = []
        try:
            for addr, data in writes:
                written.append(addr)
                mem.write_bytes(addr, bytearray(data))
            for addr, data in writes:
                if bytes(mem.read_bytes(addr, len(data))) != data:
                    raise ValueError("memory write verification failed")
            heap_map._walk(heap)
            if not heap.valid:
                raise ValueError("heap validation failed: " + heap.error)
            self.scan(heap_map)
        except Exception:
            for addr in reversed(written):
                mem.write_bytes(addr, bytearray(backups[addr]))
            heap_map._walk(heap)
            self.scan(heap_map)
            raise
        heap_map.last_refresh_frame = None

    def reserve(self, heap_map, name, expected_block, begin, end):
        """Reserve a drag's byte range inside the exact free block it started in."""
        heap, free, used, original = self._snapshot(heap_map, name)
        expected_obj, expected_start, expected_end, expected_node, expected_size = expected_block
        if (heap.object_addr, heap.start, heap.end) != (expected_obj, expected_start, expected_end):
            raise ValueError("heap was replaced; drag again")
        blocks = {b[0]: b for b in heap.blocks}
        block = blocks.get(expected_node)
        if block is None or block[4] != "free" or block[1] != expected_size:
            raise ValueError("free block changed; drag again")
        node, size = block[:2]
        limit = node + HDR + size
        lo, hi = sorted((int(begin), int(end)))
        if lo < node + HDR or hi > limit or hi <= lo:
            raise ValueError("selection must stay inside one free block")
        # Four-byte allocator granularity. A tiny prefix is represented using the
        # used block's alignment-offset bits; tiny suffixes are absorbed as padding.
        used_node = (lo & ~3) - HDR
        used_end = min(limit, (max(hi, used_node + HDR + MIN_PAYLOAD) + 3) & ~3)
        if used_end - used_node - HDR < MIN_PAYLOAD:
            raise ValueError("selection is too close to the end; reserve at least 16 bytes")
        prefix = used_node - node
        flags = prefix if prefix < HDR + 4 else 0
        free.remove(node)
        del blocks[node]
        if prefix >= HDR + 4:
            free.append(node)
            blocks[node] = (node, prefix - HDR, 0, 0, "free")
        if limit - used_end >= HDR + 4:
            free.append(used_end)
            blocks[used_end] = (used_end, limit - used_end - HDR, 0, 0, "free")
        else:
            used_end = limit
        blocks[used_node] = (used_node, used_end - used_node - HDR, GROUP, flags, "used")
        used.append(used_node)
        token = TAG + os.urandom(MIN_PAYLOAD - len(TAG))
        self._commit(heap_map, heap, free, used, blocks, original, (used_node + HDR, token))
        return self.records[(name, used_node)]

    def release(self, heap_map, name, node):
        record = self.records.get((name, node))
        if record is None:
            raise ValueError("select an inspector reservation to release")
        heap, free, used, original = self._snapshot(heap_map, name)
        blocks = {b[0]: b for b in heap.blocks}
        block = blocks.get(node)
        token = bytes(heap_map.reader.read_bytes(node + HDR, MIN_PAYLOAD))
        if (heap.object_addr != record["obj"] or heap.start != record["start"] or heap.end != record["end"] or
                block != (node, record["size"], GROUP, record["flags"], "used") or token != record["token"]):
            self.records.pop((name, node), None)
            raise ValueError("reservation no longer exists; no memory changed")
        original[node + HDR] = token
        used.remove(node)
        del blocks[node]
        first, last = block_extent(block)
        # Coalesce both neighbors, matching recycleFreeBlock/joinTwoBlocks.
        for neighbor in sorted(free):
            n, size = blocks[neighbor][:2]
            if n + HDR + size == first:
                first = n
                free.remove(n)
                del blocks[n]
            elif n == last:
                last = n + HDR + size
                free.remove(n)
                del blocks[n]
        free.append(first)
        blocks[first] = (first, last - first - HDR, 0, 0, "free")
        # Remove the tag as well, so a later game allocation cannot inherit it.
        self._commit(heap_map, heap, free, used, blocks, original, (node + HDR, bytes(MIN_PAYLOAD)))

    def release_all(self, heap_map):
        """Release tracked reservations in one transaction per heap, preserving game blocks."""
        total=0
        for name in list(dict.fromkeys(name for name,_node in self.records)):
            records=[r for (heap_name,_),r in self.records.items() if heap_name==name]
            heap,free,used,original=self._snapshot(heap_map,name)
            blocks={b[0]:b for b in heap.blocks}
            payloads=[]
            for record in records:
                node=record['node'];block=blocks.get(node)
                token=bytes(heap_map.reader.read_bytes(node+HDR,MIN_PAYLOAD))
                if (heap.object_addr!=record['obj'] or heap.start!=record['start'] or heap.end!=record['end'] or
                        block!=(node,record['size'],GROUP,record['flags'],'used') or token!=record['token']):
                    raise ValueError('Reservation changed; released %d blocks before stopping' % total)
                original[node+HDR]=token
                payloads.append((node+HDR,bytes(MIN_PAYLOAD)))
                first,last=block_extent(block)
                del blocks[node]
                blocks[first]=(first,last-first-HDR,0,0,'free')
            released={r['node'] for r in records}
            used=[node for node in used if node not in released]
            # Merge every adjacent free extent, including alignment padding.
            merged=[]
            for block in sorted(blocks.values()):
                if merged and block[4]=='free' and merged[-1][4]=='free' and merged[-1][0]+HDR+merged[-1][1]==block[0]:
                    previous=merged[-1]
                    merged[-1]=(previous[0],previous[1]+HDR+block[1],0,0,'free')
                else:merged.append(block)
            blocks={b[0]:b for b in merged}
            free=[b[0] for b in merged if b[4]=='free']
            self._commit(heap_map,heap,free,used,blocks,original,payloads=payloads)
            total+=len(records)
        return total

    def reserve_range(self, heap_map, name, expected_block, begin, end, block_size):
        """Atomically fill one free interval with exact-size payloads and real headers.

        begin is the first payload address, end is exclusive. Remaining bytes stay
        free; if a tiny tail cannot hold a free header, omit the final allocation.
        """
        begin,end,block_size=int(begin),int(end),int(block_size)
        if begin&3 or end&3 or block_size&3 or block_size<MIN_PAYLOAD:
            raise ValueError('Use 4-byte aligned endpoints and a payload size of at least 16, divisible by 4')
        heap,free,used,original=self._snapshot(heap_map,name)
        obj,start,limit,node,size=expected_block
        if (heap.object_addr,heap.start,heap.end)!=(obj,start,limit):
            raise ValueError('heap was replaced; select the range again')
        blocks={b[0]:b for b in heap.blocks}
        block=blocks.get(node)
        if block is None or block[4]!='free' or block[1]!=size:
            raise ValueError('free block changed; select the range again')
        free_end=node+HDR+size
        if not node+HDR<=begin<end<=free_end:
            raise ValueError('Start–End must lie inside one free block (End is exclusive)')
        count=(end-begin+HDR)//(block_size+HDR)
        actual_end=begin+(count-1)*(block_size+HDR)+block_size
        if count and 0<free_end-actual_end<HDR+4:
            count-=1
            actual_end=begin+(count-1)*(block_size+HDR)+block_size
        if count<1:raise ValueError('No complete block fits this range without losing free-list metadata')
        if count>4096 or len(used)+count>heap_map.max_blocks:
            raise ValueError('Too many blocks; increase block size or shorten the range')
        free.remove(node);del blocks[node]
        first_node=begin-HDR;prefix=first_node-node
        if prefix>=HDR+4:
            free.append(node);blocks[node]=(node,prefix-HDR,0,0,'free')
        nodes=[];payloads=[]
        for i in range(count):
            address=first_node+i*(block_size+HDR)
            flags=prefix if i==0 and prefix<HDR+4 else 0
            blocks[address]=(address,block_size,GROUP,flags,'used')
            used.append(address);nodes.append(address)
            payloads.append((address+HDR,TAG+os.urandom(MIN_PAYLOAD-len(TAG))))
        if actual_end<free_end:
            free.append(actual_end);blocks[actual_end]=(actual_end,free_end-actual_end-HDR,0,0,'free')
        self._commit(heap_map,heap,free,used,blocks,original,payloads=payloads)
        return [self.records[(name,address)] for address in nodes]

    def reserve_split(self,heap_map,name,expected_block,begin,end,count,gaps=0,block_size=None):
        plan=split_plan(expected_block,begin,end,count,gaps,block_size)
        heap,free,used,original=self._snapshot(heap_map,name)
        obj,start,limit,node,size=expected_block
        if (heap.object_addr,heap.start,heap.end)!=(obj,start,limit):
            raise ValueError('Heap changed while the dialog was open; double-click again')
        blocks={b[0]:b for b in heap.blocks}
        block=blocks.get(node)
        if block is None or block[4]!='free' or block[1]!=size:
            raise ValueError('Free block changed while the dialog was open; double-click again')
        if len(used)+count>heap_map.max_blocks:raise ValueError('Heap block limit would be exceeded')
        free.remove(node);del blocks[node]
        prefix=begin-HDR-node
        if prefix>=HDR+4:
            free.append(node);blocks[node]=(node,prefix-HDR,0,0,'free')
        payloads=[]
        reserved=[]
        gap_node=None
        for i,(address,payload_size) in enumerate(plan):
            if i%(gaps+1):
                # Coalesce skipped slots into one real free-list block per gap.
                if gap_node is not None:
                    blocks[gap_node]=(gap_node,address+HDR+payload_size-gap_node-HDR,0,0,'free')
                else:
                    gap_node=address
                    free.append(address);blocks[address]=(address,payload_size,0,0,'free')
                continue
            gap_node=None
            flags=prefix if i==0 and prefix<HDR+4 else 0
            blocks[address]=(address,payload_size,GROUP,flags,'used');used.append(address)
            reserved.append(address)
            payloads.append((address+HDR,TAG+os.urandom(MIN_PAYLOAD-len(TAG))))
        actual_end=plan[-1][0]+HDR+plan[-1][1]
        if actual_end<node+HDR+size:
            free.append(actual_end);blocks[actual_end]=(actual_end,node+size-actual_end,0,0,'free')
        if len(free)+len(used)>heap_map.max_blocks:raise ValueError('Heap block limit would be exceeded')
        self._commit(heap_map,heap,free,used,blocks,original,payloads=payloads)
        return [self.records[(name,address)] for address in reserved]
