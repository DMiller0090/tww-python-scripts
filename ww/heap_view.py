"""Heap visualization and reversible inspector reservations for tww_inspector.

Actor-owned JKRSolidHeap pointers are at +0xF0.
Process allocation uses zeldaHeap (DYN).
GAME = gameHeap; DYN = zeldaHeap; ACT = archiveHeap. Archive/resource
allocations can be shared; no owner is inferred from names or group ids.
"""
import bisect
import time

from .heap import HDR, _ok
from .heap_reserve import HeapReservations, split_plan

HEAPS = (("game", "GAME"), ("zelda", "DYN"), ("archive", "ACT"))
FREE, USED, META, CUSTOM = 0xFF42BE70, 0xFFD95454, 0xFF4A5564, 0xFFAD70DB
BG, HEAD, TXT, DIM, SELECT = 0xFF14181D, 0xFF222B36, 0xFFE6EDF3, 0xFF8B98A8, 0xFF2A4565
BAR_X, BAR_RIGHT = 16, 1084
BAR_TOP, BAR_STEP, BAR_H = 80, 69, 26
LIST_TOP, LIST_ROW = 331, 20


def size_text(value):
    if value >= 1024 * 1024:
        return "%.2f MiB" % (value / (1024 * 1024))
    if value >= 1024:
        return "%.1f KiB" % (value / 1024)
    return "%d B" % value


def fit(text, width):
    count = max(0, int(width / 7) - 1)
    return text if len(text) <= count else text[:max(0, count - 1)] + "~"


class HeapView:
    def __init__(self):
        self.bar_right = BAR_RIGHT
        self.map = None
        self.owners = {}           # (heap name, header) -> {pid: label}
        self.processes = []        # every loaded process, even if unattributed
        self.totals = {}           # pid -> {heap name: directly attributable bytes}
        self.selected_pid = None
        self.selected_block = None
        self.scroll = 0
        self.reservations = HeapReservations()
        self.drag = None
        self.pending = None
        self.message = None
        self.last_click = None
        self.split_request = None
        self.hover = None

    def resize(self,width):
        right=max(BAR_X+1,int(width)-16)
        if right!=self.bar_right:
            self.bar_right=right
            # Pointer gestures were captured in the old geometry.
            self.drag=self.last_click=self.split_request=None
            self.hover=None

    def _block(self, heap, address):
        if not heap.valid or not heap.start <= address < heap.end:
            return None
        i = bisect.bisect_right(heap.starts, address) - 1
        if i >= 0:
            block = heap.blocks[i]
            node, size, _group, _flags, state = block
            if state == "used" and node + HDR <= address < node + HDR + size:
                return block
        return None

    def refresh(self, tree, frame):
        self.map = tree.heap
        if self.map is None:
            return
        self.map.refresh(frame, max_age=0)
        self.reservations.scan(self.map)
        self.owners, self.totals = {}, {}
        self.processes = sorted(tree.actors, key=lambda a: a["pid"], reverse=True)
        for process in self.processes:
            pid = process["pid"]
            self.totals[pid] = {name: 0 for name, _label in HEAPS}
            addresses = [(process["addr"], "instance")]
            if process["kind"] == "actor":
                solid = tree._u32(process["addr"] + 0xF0)
                if _ok(solid):
                    addresses.append((solid, "solid heap"))
            seen = set()
            for address, role in addresses:
                for name, _label in HEAPS:
                    heap = self.map.heaps.get(name)
                    block = self._block(heap, address) if heap else None
                    if block is None:
                        continue
                    node, size, _group, flags, _state = block
                    key = (name, node)
                    label = "%s bsPCID=%d (%s)" % (process["name"], pid, role)
                    owners = self.owners.setdefault(key, {})
                    owners[pid] = (owners[pid] + ", " + role) if pid in owners else label
                    if key not in seen:
                        self.totals[pid][name] += HDR + size + (flags & 0x7f)
                        seen.add(key)
        if self.selected_pid not in self.totals:
            self.selected_pid = None
        self.scroll = min(self.scroll, max(0, len(self.processes) - 1))

    def stats(self, heap):
        free = sum(b[1] for b in heap.blocks if b[4] == "free")
        used = sum(HDR + b[1] + (b[3] & 0x7f) for b in heap.blocks if b[4] == "used")
        largest = max((b[1] for b in heap.blocks if b[4] == "free"), default=0)
        return free, used, max(0, heap.size - free - used), largest

    def detail(self):
        if self.drag:
            lo, hi = sorted((self.drag["begin"], self.drag["end"]))
            return "Reserve %08X-%08X (%s): release mouse to commit; drag outside this free block to cancel." % (lo, hi, size_text(hi-lo))
        if self.map is None:
            return "Waiting for heap data..."
        if self.selected_block:
            name, node = self.selected_block
            heap = self.map.heaps.get(name)
            if heap and heap.valid:
                block = next((b for b in heap.blocks if b[0] == node), None)
                if block:
                    _node, size, group, _flags, state = block
                    owner = "; ".join(self.owners.get((name, node), {}).values())
                    reserved = (name, node) in self.reservations.records
                    label = "Inspector reservation — select Release block to return it to the heap" if reserved else owner or ("free" if state == "free" else "shared / unattributed allocation")
                    return "Start %08X | End %08X (exclusive) | Size %d B (0x%X) | Header %08X | %s" % (
                        node+HDR,node+HDR+size,size,size,node,label)
            return "Selected block is no longer present."
        if self.selected_pid is not None:
            return "Highlighted: directly linked allocations for bsPCID=%d" % self.selected_pid
        return "Double-click green space to split it into custom blocks. Drag to reserve; select purple then Release block to free."

    def free_at(self,click):
        if not click or self.map is None:return None
        x,y=click
        if not BAR_X<=x<self.bar_right:return None
        for i,(name,_label) in enumerate(HEAPS):
            heap=self.map.heaps.get(name);top=BAR_TOP+i*BAR_STEP
            if not heap or not heap.valid or not top<=y<top+BAR_H:continue
            address=int(heap.start+(x-BAR_X)/(self.bar_right-BAR_X)*heap.size)
            for node,size,_group,_flags,kind in heap.blocks:
                if kind=='free' and node+HDR<=address<node+HDR+size:
                    return dict(name=name,expected=(heap.object_addr,heap.start,heap.end,node,size),
                                clicked=address,begin=(address+3)&~3,end=node+HDR+size)
        return None

    def double_click(self,click,now=None):
        if not click:return False
        now=time.monotonic() if now is None else now
        request=self.free_at(click) or self.reservation_at(click);previous=self.last_click
        self.last_click=(now,click,request) if request else None
        if (request and previous and 0<=now-previous[0]<=0.45 and previous[2]['expected']==request['expected']
                and previous[2]['name']==request['name'] and
                previous[2].get('kind')==request.get('kind') and previous[2].get('token')==request.get('token') and
                sum((a-b)**2 for a,b in zip(click,previous[1]))<=25):
            self.split_request=request;self.last_click=None;self.drag=None;self.pending=None
            return True
        return False

    def reservation_at(self,point):
        if not point or self.map is None:return None
        x,y=point
        if not BAR_X<=x<self.bar_right:return None
        for i,(name,_label) in enumerate(HEAPS):
            heap=self.map.heaps.get(name);top=BAR_TOP+i*BAR_STEP
            if not heap or not heap.valid or not top<=y<top+BAR_H:continue
            address=int(heap.start+(x-BAR_X)/(self.bar_right-BAR_X)*heap.size)
            for (heap_name,node),record in self.reservations.records.items():
                if heap_name==name and node-(record['flags']&0x7F)<=address<node+HDR+record['size']:
                    return dict(kind='reservation',name=name,node=node,token=record['token'],
                                expected=(heap.object_addr,heap.start,heap.end,node,record['size']),
                                begin=node+HDR,end=node+HDR+record['size'],size=record['size'])
        return None

    def poll_cursor(self,canvas,pointer=None):
        x,y,inside=canvas.mouse_pos() if pointer is None else pointer
        hover=None
        if inside and self.map and BAR_X<=x<self.bar_right:
            for i,(name,_label) in enumerate(HEAPS):
                heap=self.map.heaps.get(name);top=BAR_TOP+i*BAR_STEP
                if heap and heap.valid and top<=y<top+BAR_H:
                    address=int(heap.start+(x-BAR_X)/(self.bar_right-BAR_X)*heap.size)
                    hover=(name,address,(address+3)&~3,x)
                    break
        changed=hover!=self.hover;self.hover=hover
        return changed

    def handle_input(self, click, wheel, bottom):
        visible = max(1, (bottom - LIST_TOP) // LIST_ROW)
        if wheel:
            self.scroll = max(0, min(self.scroll - int(round(wheel * 3)), max(0, len(self.processes) - visible)))
        if click:
            x, y = click
            if self.map and BAR_X <= x < self.bar_right:
                for i, (name, _label) in enumerate(HEAPS):
                    top = BAR_TOP + i * BAR_STEP
                    heap = self.map.heaps.get(name)
                    if top <= y < top + BAR_H and heap and heap.valid:
                        address = heap.start + (x - BAR_X) / (self.bar_right - BAR_X) * heap.size
                        self.selected_block, self.selected_pid = None, None
                        for node, size, _group, flags, state in heap.blocks:
                            start = node - (flags & 0x7f) if state == "used" else node
                            if start <= address < node + HDR + size:
                                self.selected_block = (name, node)
                                owners = self.owners.get((name, node), {})
                                if len(owners) == 1:
                                    self.selected_pid = next(iter(owners))
                                break
            if 0 <= x < self.bar_right and LIST_TOP <= y < LIST_TOP + visible * LIST_ROW:
                index = self.scroll + int((y - LIST_TOP) // LIST_ROW)
                if index < len(self.processes):
                    self.selected_pid = self.processes[index]["pid"]
                    self.selected_block = None
        self.scroll = max(0, min(self.scroll, max(0, len(self.processes) - visible)))
        return bool(click or wheel)

    def poll_drag(self, canvas, click=None):
        """Track the canvas pointer; commit only after a meaningful drag ends."""
        down = canvas.left_down()
        x, y, inside = canvas.mouse_pos()
        changed = self.poll_cursor(canvas,(x,y,inside))
        if click and down and self.map and self.drag is None:
            cx, cy = click
            for index, (name, _label) in enumerate(HEAPS):
                heap = self.map.heaps.get(name)
                top = BAR_TOP + index * BAR_STEP
                if not heap or not heap.valid or not (BAR_X <= cx < self.bar_right and top <= cy < top + BAR_H):
                    continue
                address = int(heap.start + (cx - BAR_X) / (self.bar_right - BAR_X) * heap.size)
                for node, size, _group, _flags, state in heap.blocks:
                    if state == "free" and node + HDR <= address < node + HDR + size:
                        self.drag = dict(name=name, expected=(heap.object_addr, heap.start, heap.end, node, size),
                                         begin=address, end=address, x=cx, top=top, valid=True, moved=False)
                        changed = True
                        break
        if self.drag:
            drag = self.drag
            _obj, start, end, node, size = drag["expected"]
            address = int(start + (x - BAR_X) / (self.bar_right - BAR_X) * (end - start))
            drag["valid"] = (inside and drag["top"] <= y < drag["top"] + BAR_H and
                             node + HDR <= address <= node + HDR + size)
            drag["end"] = max(node + HDR, min(address, node + HDR + size))
            drag["moved"] |= abs(x - drag["x"]) >= 3
            if drag['moved']:self.last_click=None
            changed = True
            if not down:
                self.drag = None
                if drag["moved"]:
                    if drag["valid"]:
                        self.pending = ("reserve", drag)
                        self.message = "Reserving selected free space..."
                    else:
                        self.message = "Reservation cancelled: drag must stay within the same free block."
        return changed

    def queue_release(self):
        if self.selected_block in self.reservations.records:
            self.pending = ("release", self.selected_block)
            return "Releasing inspector reservation..."
        return "Select a purple inspector reservation first; game-owned blocks cannot be released here."

    def queue_release_all(self):
        self.pending=('release_all',None)
        return 'Releasing all inspector reservations...'

    def queue_manual(self, tree, frame, begin, end, block_size):
        self.refresh(tree,frame)
        if self.map is None:raise ValueError('Waiting for heap data')
        for name,_label in HEAPS:
            heap=self.map.heaps.get(name)
            if not heap or not heap.valid:continue
            for node,size,_group,_flags,kind in heap.blocks:
                if kind=='free' and node+HDR<=begin<end<=node+HDR+size:
                    expected=(heap.object_addr,heap.start,heap.end,node,size)
                    self.pending=('manual',dict(name=name,expected=expected,begin=begin,end=end,block_size=block_size))
                    return 'Adding custom blocks to %s...' % dict(HEAPS)[name]
        raise ValueError('Start–End must lie inside one free block in GAME, DYN or ACT')

    def queue_split(self,request,begin,end,count,gaps=0,block_size=None):
        split_plan(request['expected'],begin,end,count,gaps,block_size)
        self.pending=('split',dict(request,begin=begin,end=end,count=count,gaps=gaps,block_size=block_size))
        return 'Creating %d custom blocks...' % count

    def execute_pending(self, tree, frame):
        """Called by the inspector's Dolphin callback, with emulation stopped between frames."""
        if self.pending is None:
            return None
        action, request = self.pending
        self.pending = None
        try:
            if self.map is None or tree.heap is not self.map:
                raise ValueError("heap context changed")
            if action == "reserve":
                record = self.reservations.reserve(self.map, request["name"], request["expected"],
                                                   request["begin"], request["end"])
                self.selected_block = (record["name"], record["node"])
                message = "Reserved %s at %08X; the heap allocator now treats this range as used." % (
                    size_text(record["size"]), record["node"] + HDR)
            elif action == 'manual':
                records=self.reservations.reserve_range(self.map,request['name'],request['expected'],
                    request['begin'],request['end'],request['block_size'])
                self.selected_block=(request['name'],records[0]['node'])
                message='Added %d purple blocks of %s payload; remainder stays free.' % (len(records),size_text(request['block_size']))
            elif action == 'split':
                records=self.reservations.reserve_split(self.map,request['name'],request['expected'],
                                                        request['begin'],request['end'],request['count'],request.get('gaps',0),request.get('block_size'))
                self.selected_block=(request['name'],records[0]['node'])
                message='Created %d purple blocks in %08X–%08X (including interior headers).' % (
                    len(records),request['begin'],request['end'])
            elif action == 'release_all':
                count=self.reservations.release_all(self.map)
                self.selected_block=None
                message='Released %d inspector blocks across all heaps.' % count
            else:
                name, node = request
                self.reservations.release(self.map, name, node)
                self.selected_block = None
                message = "Reservation released; space is available to the heap allocator."
            self.refresh(tree, frame)
            return message
        except Exception as exc:
            self.refresh(tree, frame)
            return "Heap edit failed: %s" % exc

    def draw(self, canvas, bottom):
        canvas.text((16, 48), DIM, "Double-click green to create / purple for details | green = free | red = game used | purple = custom | drag to reserve")
        for index, (name, label) in enumerate(HEAPS):
            y = BAR_TOP + index * BAR_STEP
            heap = self.map.heaps.get(name) if self.map else None
            canvas.text((16, y - 9), TXT, label)
            canvas.rect_filled((BAR_X, y), (self.bar_right, y + BAR_H), META)
            if not heap or not heap.valid:
                canvas.text((100, y - 9), DIM, "Unavailable: " + (heap.error if heap else "waiting for heap"))
                continue
            free, used, meta, largest = self.stats(heap)
            summary = "%s free | %s used | %s metadata | largest free %s | %08X-%08X" % (
                size_text(free), size_text(used), size_text(meta), size_text(largest), heap.start, heap.end)
            canvas.text((74, y - 9), TXT, fit(summary, self.bar_right - 74))
            scale = (self.bar_right - BAR_X) / heap.size
            for node, size, _group, flags, state in heap.blocks:
                start = node - (flags & 0x7f) if state == "used" else node + HDR
                x0, x1 = BAR_X + (start - heap.start) * scale, BAR_X + (node + HDR + size - heap.start) * scale
                reserved = (name, node) in self.reservations.records
                canvas.rect_filled((x0, y), (x1, y + BAR_H), CUSTOM if reserved else USED if state == "used" else FREE)
                if reserved and x1-x0>=2:canvas.line((x0,y),(x0,y+BAR_H),META)
                owners = self.owners.get((name, node), {})
                selected = ((self.selected_pid is not None and self.selected_pid in owners) or
                            self.selected_block == (name, node))
                if selected:
                    canvas.rect((x0, y), (max(x0 + 1, x1), y + BAR_H), TXT)
                if x1 - x0 > 80 and (owners or reserved):
                    label_text = "Inspector reservation" if reserved else next(iter(owners.values()))
                    canvas.text((x0 + 3, y + 17), TXT, fit(label_text, x1 - x0 - 4))
            if self.drag and self.drag["name"] == name:
                lo, hi = sorted((self.drag["begin"], self.drag["end"]))
                x0, x1 = BAR_X + (lo - heap.start) * scale, BAR_X + (hi - heap.start) * scale
                canvas.rect_filled((x0, y), (x1, y + BAR_H), 0x66AD70DB)
                canvas.rect((x0, y), (x1, y + BAR_H), 0xFFFFD24A if self.drag["valid"] else DIM)
            if self.hover and self.hover[0]==name:
                _name,address,aligned,x=self.hover
                canvas.line((x,y-2),(x,y+BAR_H+2),TXT)
                canvas.text((BAR_X,y+BAR_H+15),TXT,
                            'Cursor %08X | Allocation start %08X (aligned to 4 bytes)' % (address,aligned))
            else:canvas.text((BAR_X, y + BAR_H + 15), DIM,
                        "%d used blocks / %d free blocks; %.1f%% free" % (
                            sum(b[4] == "used" for b in heap.blocks), sum(b[4] == "free" for b in heap.blocks),
                            free * 100 / heap.size))
        canvas.text((16, 292), TXT, fit(self.detail(), self.bar_right - 16))
        canvas.text((16, 311), DIM, "All loaded processes: directly linked blocks only. Shared archives/other allocations remain red above.")
        canvas.rect_filled((0, LIST_TOP - 17), (1100, LIST_TOP), HEAD)
        for title, x in (("Process", 16), ("bsPCID", 390), ("GAME", 470), ("DYN", 620), ("ACT", 770), ("Total", 920)):
            canvas.text((x, LIST_TOP - 4), DIM, title)
        visible = max(1, (bottom - LIST_TOP) // LIST_ROW)
        self.scroll = max(0, min(self.scroll, max(0, len(self.processes) - visible)))
        for slot, process in enumerate(self.processes[self.scroll:self.scroll + visible]):
            y = LIST_TOP + slot * LIST_ROW
            pid = process["pid"]
            if pid == self.selected_pid:
                canvas.rect_filled((0, y), (1100, y + LIST_ROW), SELECT)
            canvas.text((16, y + 14), TXT, fit(process["name"], 365))
            canvas.text((390, y + 14), TXT, "%d" % pid)
            totals = self.totals.get(pid, {})
            for (name, _label), x in zip(HEAPS, (470, 620, 770)):
                canvas.text((x, y + 14), DIM, size_text(totals.get(name, 0)))
            canvas.text((920, y + 14), TXT, size_text(sum(totals.values())))
        if len(self.processes) > visible:
            height = bottom - LIST_TOP
            bar = max(10, visible / len(self.processes) * height)
            y = LIST_TOP + self.scroll / len(self.processes) * height
            canvas.rect_filled((1094, y), (1098, y + bar), DIM)
