"""ww.heap - read JKRExpHeap block lists from emulated memory and say where an address lives.

Gives hit-watch / the actor inspector "real heap" context: which game heap an address is in, whether it
sits in a used or a free block, how big that block is, and a signature that changes when the block is
freed or reallocated (the signal for a stale pointer).

Layouts come from the zeldaret/tww decomp:
  JKRHeap     mStart +0x30, mEnd +0x34, mSize +0x38
  JKRExpHeap  mHeadFreeList +0x78, mHeadUsedList +0x80
  CMemBlock   u16 magic 'HM', u8 flags, u8 group, u32 size, prev, next; payload starts 0x10 after the header
"""
import bisect
import struct

HEAP_POINTERS = {   # JKRExpHeap* globals (gameHeap, zeldaHeap, commandHeap, archiveHeap) per build
    "GZLJ01": {"game": 0x803E9DF8, "zelda": 0x803E9E00, "command": 0x803E9E08, "archive": 0x803E9E10},
    "GZLE01": {"game": 0x803F6920, "zelda": 0x803F6928, "command": 0x803F6930, "archive": 0x803F6938},
    "GZLP01": {"game": 0x803FE120, "zelda": 0x803FE128, "command": 0x803FE130, "archive": 0x803FE138},
}
MAGIC = 0x484D
HDR = 0x10
RAM_LO, RAM_HI = 0x80000000, 0x81800000


def _ok(p):
    return p is not None and RAM_LO <= p < RAM_HI and (p & 3) == 0


class Heap:
    def __init__(self, name, addr):
        self.name, self.addr = name, addr
        self.object_addr = 0
        self.start = self.end = self.size = 0
        self.error = ""
        self.blocks = []        # (header addr, payload size, group, flags, 'used' | 'free'), sorted by address
        self.starts = []
        self.valid = False
        self.truncated = False


class HeapLoc:
    __slots__ = ("heap", "state", "block", "size", "group", "offset")

    def __init__(self, heap, state, block=0, size=0, group=0, offset=0):
        self.heap, self.state, self.block, self.size, self.group, self.offset = heap, state, block, size, group, offset

    def signature(self):
        return (self.heap, self.state, self.block, self.size, self.group)

    def text(self):
        if self.heap is None:
            return "outside game heaps"
        if self.state == "gap":
            return "%s heap, between blocks" % self.heap
        return "%s %s blk@%08X sz%X g%d %s0x%X" % (
            self.heap, self.state, self.block, self.size, self.group, "-" if self.offset < 0 else "+", abs(self.offset))


class HeapMap:
    def __init__(self, version, reader, heaps=("zelda", "game"), max_blocks=30000):
        self.reader = reader
        self.pointers = HEAP_POINTERS.get(version, {})
        self.heaps = {n: Heap(n, self.pointers[n]) for n in heaps if n in self.pointers}
        self.max_blocks = max_blocks
        self.generation = 0
        self.last_refresh_frame = None
        self.last_error = ""

    def _u32(self, a):
        try:
            return self.reader.read_u32(a) & 0xFFFFFFFF
        except Exception:
            return None

    def refresh(self, frame=None, max_age=120):
        """Re-walk the block lists unless they are younger than max_age frames."""
        if frame is not None and self.last_refresh_frame is not None and frame - self.last_refresh_frame < max_age:
            return False
        self.last_refresh_frame = frame
        self.generation += 1
        for heap in self.heaps.values():
            try:
                self._walk(heap)
            except Exception as exc:
                heap.valid = False
                heap.error = str(exc)
                self.last_error = "%s: %r" % (heap.name, exc)
        return True

    def _walk(self, heap):
        obj = self._u32(heap.addr)
        heap.valid = False
        heap.truncated = False
        heap.error = ""
        heap.blocks, heap.starts = [], []
        heap.object_addr = obj or 0
        if not _ok(obj):
            heap.error = "heap not initialized"
            return
        data = bytes(self.reader.read_bytes(obj, 0x88))
        start, end, size = struct.unpack_from(">3I", data, 0x30)
        head_free, _tail_free, head_used = struct.unpack_from(">3I", data, 0x78)
        if not (_ok(start) and RAM_LO < end <= RAM_HI and start < end and end - start == size):
            heap.error = "invalid heap range"
            return
        heap.start, heap.end, heap.size = start, end, size
        blocks, seen = [], set()
        for state, node in (("used", head_used), ("free", head_free)):
            n = 0
            while node:
                if n >= self.max_blocks:
                    heap.truncated = True
                    heap.error = "block limit reached"
                    return
                if not _ok(node) or not start <= node <= end - HDR or node in seen:
                    heap.error = "invalid or cyclic block list"
                    return
                seen.add(node)
                hdr = bytes(self.reader.read_bytes(node, HDR))
                magic, flags, group, bsize, _prev, nxt = struct.unpack(">HBBIII", hdr)
                # setFreeBlock/recycleFreeBlock clear mMagic; initiate can leave HM
                # on a newly created free block. Only used blocks require HM.
                if ((state == "used" and magic != MAGIC) or
                        (state == "free" and magic not in (0, MAGIC)) or
                        node + HDR + bsize > end or
                        (state == "used" and node - (flags & 0x7f) < start)):
                    heap.error = "invalid block header"
                    return
                blocks.append((node, bsize, group, flags, state))
                node, n = nxt, n + 1
        blocks.sort()
        previous_end = start
        for node, bsize, _group, flags, state in blocks:
            block_start = node - (flags & 0x7f) if state == "used" else node
            if block_start < previous_end:
                heap.error = "overlapping blocks"
                return
            previous_end = node + HDR + bsize
        heap.blocks = blocks
        heap.starts = [b[0] for b in blocks]
        heap.valid = True

    def locate(self, addr):
        for heap in self.heaps.values():
            if heap.valid and heap.start <= addr < heap.end:
                i = bisect.bisect_right(heap.starts, addr) - 1
                if i >= 0:
                    node, size, group, _flags, state = heap.blocks[i]
                    if addr < node + HDR + size:
                        return HeapLoc(heap.name, state, node + HDR, size, group, addr - (node + HDR))
                return HeapLoc(heap.name, "gap")
        return HeapLoc(None, "outside")

    def stats(self):
        return {h.name: (len(h.blocks), h.valid) for h in self.heaps.values()}
