"""ww.actor_tree - model behind tww_inspector.py: live processes as an expandable, editable row tree.

Process and runtime views share one tree engine (the heaps tab uses ww.heap_view):
  * processes - EVERY process in the game's layer tree (actors, but also MSG text boxes, scenes, cameras,
                kankyo, overlaps, ...), each expandable into its decomp class layout (see ww.structs);
  * runtime   - g_dComIfG_gameInfo, including save, play, draw lists and resource control.
Values are decoded from one memory snapshot per expanded root; edits encode the typed value and write it back.
"""
import struct

from .hit_watch import A_COND, A_PID, A_PROC, ACTOR_QUEUE, NODRAW, NOEXEC, _ok
from .heap import HeapMap
from .structs import Row

LAYER_LIST = {"GZLJ01": 0x80365B7C, "GZLE01": 0x803726DC, "GZLP01": 0x803793BC}     # l_fpcLy_LayerList
GAME_INFO = {"GZLJ01": 0x803B8108, "GZLE01": 0x803C4C08, "GZLP01": 0x803CC530}      # g_dComIfG_gameInfo
PLAY_OFFSET = 0x12A0                                                                  # dComIfG_inf_c::play
RUNTIME_CLASS = "dComIfG_inf_c"
RUNTIME_NAME = "g_dComIfG_gameInfo"
RUNTIME_KEY = (-1, "actor", 0, "")


class Item:
    """One visible table row: a process / runtime root (kind 'actor') or one field of it (kind 'field')."""
    __slots__ = ("key", "depth", "kind", "actor", "row")

    def __init__(self, key, depth, kind, actor, row=None):
        self.key, self.depth, self.kind, self.actor, self.row = key, depth, kind, actor, row

    @property
    def expandable(self):
        return self.kind == "actor" or (self.row is not None and self.row.expandable)

    @property
    def editable(self):
        return self.kind == "field" and self.row.kind in ("prim", "ptr", "bytes")


class ActorTree:
    def __init__(self, memory, structs, names, sizes=None, heap_refresh=180):
        self.mem, self.structs, self.names = memory, structs, names
        self.sizes = {k.upper(): v for k, v in (sizes or {}).items()}
        self.version = self.queue = self.heap = None
        self.heap_refresh = heap_refresh
        self.actors, self.by_pid = [], {}          # every process (name kept for compatibility)
        self.runtime = None
        self.mode = "actors"                       # "actors" (all processes) | "runtime" | "heaps"
        self.open, self.flat = set(), []
        self.filter_text, self.sort = "", "addr"
        self.sort_descending = False
        self.snap, self.locks = {}, {}
        self.lock_owners = {}
        self._dirty, self._frame, self.last_error = True, 0, ""

    # ---- memory helpers ----------------------------------------------------------------------
    def _u32(self, a):
        try:
            return self.mem.read_u32(a) & 0xFFFFFFFF
        except Exception:
            return None

    def _u16(self, a):
        try:
            return self.mem.read_u16(a)
        except Exception:
            return None

    def attach(self):
        try:
            gid = bytes(self.mem.read_bytes(0x80000000, 6)).decode("ascii", "replace")
        except Exception:
            return False
        if gid not in ACTOR_QUEUE:
            return False
        self.version, self.queue = gid, ACTOR_QUEUE[gid]
        self.heap = HeapMap(gid, self.mem, heaps=("game", "zelda", "archive"))
        return True

    # ---- process list ------------------------------------------------------------------------
    def _actor_addrs(self):
        out, node, n = set(), self._u32(self.queue), 0
        while _ok(node) and n < 2048:
            addr = self._u32(node + 0x0C)
            if _ok(addr):
                out.add(addr)
            node, n = self._u32(node + 8), n + 1
        return out

    def _walk_layers(self):
        """(process addr, layer id, list index) for every process in every layer's list tree."""
        out, seen = [], set()
        layer, n = self._u32(LAYER_LIST[self.version]), 0
        while _ok(layer) and n < 512:
            lid = self._u32(layer + 0x0C)
            lists, count = self._u32(layer + 0x10), self._u32(layer + 0x14)
            if _ok(lists) and count is not None and count <= 64:
                for li in range(count):
                    node, c = self._u32(lists + li * 0x0C), 0
                    while _ok(node) and c < 4096:
                        proc = node - 0x18                      # layer tag = base_process_class + 0x18
                        if _ok(proc) and proc not in seen:
                            seen.add(proc)
                            out.append((proc, lid, li))
                        node, c = self._u32(node + 8), c + 1
            layer, n = self._u32(layer + 8), n + 1
        return out

    def refresh(self, frame):
        self._frame = frame
        if self.queue is None and not self.attach():
            return False
        actor_set = self._actor_addrs()
        procs = self._walk_layers()
        known = {addr for addr, _l, _i in procs}
        procs += [(a, None, None) for a in actor_set if a not in known]     # safety net: actors the layer walk missed
        out = []
        for addr, lid, li in procs:
            pid = self._u32(addr + A_PID)
            if pid is None:
                continue
            old = self.by_pid.get(pid)
            if old is not None and old["addr"] == addr:
                if old["kind"] == "actor":
                    old["cond"] = self._u32(addr + A_COND)
                out.append(old)
                continue
            proc = self._u16(addr + A_PROC)
            name = self.names(proc, "%04X" % (proc or 0))
            cls = self.structs.class_for_proc(name,self.version,proc)
            kind = "actor" if addr in actor_set else self.structs.kind_for_class(cls)
            if kind == "actor" and not cls:
                cls = "fopAc_ac_c"
            cond = self._u32(addr + A_COND) if kind == "actor" else None
            out.append(dict(pid=pid, addr=addr, proc=proc, name=name, cond=cond, size=0, cls=cls, rows=None,
                            kind=kind, layer=lid, list=li))
        if [a["pid"] for a in out] != [a["pid"] for a in self.actors]:
            self._dirty = True
        self.actors, self.by_pid = out, {a["pid"]: a for a in out}
        for key in [k for k in self.open if k[0] != -1 and k[0] not in self.by_pid]:
            self.open.discard(key)
            self.snap.pop(key[0], None)
        for addr, (pid, base) in list(self.lock_owners.items()):
            actor = self.by_pid.get(pid)
            if pid != -1 and (actor is None or actor["addr"] != base):
                self.locks.pop(addr, None)
                self.lock_owners.pop(addr, None)
        return True

    def object_size(self, a):
        """Resolve object bounds without expanding every field in the layout."""
        key=str(a['name']).upper().removeprefix('PROC_')
        canonical=self.structs.proc_ids.get(str(a.get('proc')),self.structs.proc_aliases.get(key,key))
        explicit=self.structs.classes.get(a['cls'],{}).get('s',0)
        size = a["size"] or explicit or self.sizes.get(canonical,0) or self.sizes.get(key,0)
        if not size and a["cls"]:
            size = self.structs.class_size(a["cls"])
        if not size and self.heap:
            loc = self.locate(a["addr"])
            if loc and loc.block == a["addr"]:
                size = loc.size
        return size

    def _prepare(self, a):
        """Resolve size and rows for a process the first time it is expanded."""
        if a["rows"] is not None:
            return
        a["size"] = self.object_size(a) or 0x100
        if a["cls"] and a["cls"] in self.structs.classes:
            a["rows"] = self.structs.rows(a["cls"], 0, a["size"], "")
        else:
            a["rows"] = [Row(0, a["size"], "unknown", "unk_0", "gap")]

    # ---- runtime view ------------------------------------------------------------------------
    def _ensure_runtime(self):
        if self.runtime is not None or self.version not in GAME_INFO:
            return
        self.structs.select_runtime_region(self.version)
        size = self.structs.class_size(RUNTIME_CLASS) or 0x1D1C8
        rt = dict(pid=-1, addr=GAME_INFO[self.version], proc=None, name=RUNTIME_NAME, cond=0, size=size,
                  cls=RUNTIME_CLASS, kind="runtime", layer=None, list=None, rows=None)
        rt["rows"] = self.structs.rows(RUNTIME_CLASS, 0, size, "")
        # Guard old caches built from upstream's incorrect resource offset/size
        # comments. The corrected header gives two 0x900-byte arrays (0x1200).
        for i,row in enumerate(rt['rows']):
            if row.name=='mResControl' and row.size>0x1200:
                rt['rows'][i]=Row(row.off,0x1200,row.type,row.name,'conflict',path=row.path)
        self.runtime = rt
        self.open.add(RUNTIME_KEY)

    def set_mode(self, mode):
        if mode != self.mode:
            self.mode, self._dirty = mode, True
            if mode == "runtime":
                if self.queue is None:
                    self.attach()
                self._ensure_runtime()

    # ---- heap --------------------------------------------------------------------------------
    def locate(self, addr, max_age=None):
        if not self.heap:
            return None
        self.heap.refresh(self._frame, max_age=self.heap_refresh if max_age is None else max_age)
        return self.heap.locate(addr)

    def heap_text(self, addr):
        loc = self.locate(addr)
        return loc.text() if loc else "?"

    # ---- tree --------------------------------------------------------------------------------
    def toggle(self, item):
        if not item.expandable:
            return
        if item.key in self.open:
            self.open.discard(item.key)
        else:
            self.open.add(item.key)
            if item.kind == "actor":
                self._prepare(item.actor)
        self._dirty = True

    def collapse_all(self):
        self.open.clear()
        self.snap.clear()
        if self.mode == "runtime":
            self.open.add(RUNTIME_KEY)
        self._dirty = True

    def set_filter(self, text):
        if text != self.filter_text:
            self.filter_text, self._dirty = text, True

    def set_sort(self, mode, descending=None):
        if descending is None:
            descending = mode == "pid"
        if mode != self.sort or descending != self.sort_descending:
            self.sort, self.sort_descending, self._dirty = mode, descending, True

    def _matches(self, a):
        f = self.filter_text.strip().lower()
        if not f:
            return True
        hay = "%s %d %x %08x %s %s" % (a["name"], a["pid"], a["pid"], a["addr"], a["cls"] or "", a["kind"])
        return all(tok in hay.lower() for tok in f.split())

    def build(self):
        flat = []
        if self.mode in ("heaps", "bg", "zone", "flags", "scls"):
            self.flat, self._dirty = [], False
            return
        if self.mode == "runtime":
            self._ensure_runtime()
            rt = self.runtime
            if rt is not None:
                flat.append(Item(RUNTIME_KEY, 0, "actor", rt))
                if RUNTIME_KEY in self.open:
                    self._add(flat, rt, rt["rows"], 1, top_filter=self.filter_text.strip().lower())
            self.flat, self._dirty = flat, False
            return
        order = {"addr": lambda a: a["addr"], "pid": lambda a: a["pid"],
                 "name": lambda a: (str(a["name"]).lower(), a["pid"])}[self.sort]
        for a in sorted(self.actors, key=order, reverse=self.sort_descending):
            if not self._matches(a):
                continue
            key = (a["pid"], "actor", 0, "")
            flat.append(Item(key, 0, "actor", a))
            if key in self.open:
                self._prepare(a)
                self._add(flat, a, a["rows"], 1)
        self.flat, self._dirty = flat, False

    def _add(self, flat, a, rows, depth, top_filter=""):
        for r in rows:
            if top_filter and depth == 1 and top_filter not in ("%s %s" % (r.name, r.type)).lower():
                continue
            key = (a["pid"], r.path, r.off, r.name)
            flat.append(Item(key, depth, "field", a, r))
            if r.expandable and key in self.open:
                self._add(flat, a, self.structs.children(r), depth + 1)

    def items(self):
        if self._dirty:
            self.build()
        return self.flat

    # ---- values ------------------------------------------------------------------------------
    def _roots(self):
        if self.mode == "runtime":
            return [self.runtime] if self.runtime else []
        return self.actors

    def read_snapshots(self):
        for a in self._roots():
            if (a["pid"], "actor", 0, "") in self.open and a["rows"] is not None:
                try:
                    self.snap[a["pid"]] = bytes(self.mem.read_bytes(a["addr"], a["size"]))
                except Exception as exc:
                    self.last_error = "read %08X: %r" % (a["addr"], exc)

    def raw(self, item):
        data = self.snap.get(item.actor["pid"])
        r = item.row
        if data is None or r is None or r.off + r.size > len(data):
            return None
        return data[r.off:r.off + r.size]

    def value(self, item):
        """(display text, editable text) for a field row."""
        r = item.row
        if r.kind=='conflict':return '<layout conflict>', ''
        if r.kind in ("prim", "ptr"):
            raw = self.raw(item)
            if raw is None or len(raw) != struct.calcsize(">" + r.fmt):
                return "?", ""
            v = struct.unpack(">" + r.fmt, raw)[0]
            if r.fmt in "fd":
                return "%.6g" % v, "%.9g" % v
            if r.kind == "ptr":
                return ("0x%08X" % v if v else "NULL"), "0x%08X" % v
            if r.type == "bool":
                return ("true" if v else "false"), str(int(v))
            if r.type == "fpc_ProcID" or r.name == "mBsPcId":
                return str(v), str(v)
            hexv = " (0x%X)" % (v & ((1 << (8 * r.size)) - 1)) if (v > 9 or v < 0) else ""
            return "%d%s" % (v, hexv), str(v)
        if r.kind == "bytes":
            raw = self.raw(item)
            text = raw.hex() if raw else "?"
            return (text[:24] + ("..." if len(text) > 24 else "")), text
        if r.kind == "array":
            return "[%d]" % r.count, ""
        if r.kind == "gap":
            return "unknown 0x%X bytes" % r.size, ""
        return "{%s}" % (r.cls or r.type), ""

    def info(self, item):
        """Annotation column: pointer target, process details."""
        a = item.actor
        if item.kind == "actor":
            size = self.object_size(a)
            return "sz 0x%X" % size if size else "Size unknown"
        r = item.row
        if r.kind=='conflict':return '0x1B00 type > 0x1200 field'
        if r.kind == "ptr":
            raw = self.raw(item)
            if raw and len(raw) == 4:
                p = struct.unpack(">I", raw)[0]
                if not p:
                    return ""
                for other in self.actors:
                    if other["addr"] <= p < other["addr"] + (other["size"] or 0x100):
                        return "-> %s bsPCID=%d +0x%X" % (other["name"], other["pid"], p - other["addr"])
                return "-> " + self.heap_text(p) if _ok(p) else ""
        return ""

    def status(self, a):
        """Last wrapper skip flags, not a diagnosis of culling, freezing or pause.

        fopAc_Execute/Draw set these when skipping the actor's method. Outer
        menu/pause guards can leave them unchanged; clear bits do not prove
        execution or rendering this frame. See decomp/src/f_op/f_op_actor.cpp.
        """
        if a['kind']!='actor':return 'Actor state: N/A'
        condition=a.get('cond')
        if condition is None:return 'Actor state unreadable'
        skipped=condition & (NOEXEC|NODRAW)
        return {0:'No exec/draw skips',
                NOEXEC:'No exec',
                NODRAW:'No draw',
                NOEXEC|NODRAW:'No exec/draw'}[skipped]

    # ---- editing -----------------------------------------------------------------------------
    def encode(self, item, text):
        r = item.row
        text = text.strip()
        if r.kind == "bytes":
            data = bytes.fromhex(text.replace(" ", ""))
            if len(data) != r.size:
                raise ValueError("need exactly %d bytes of hex" % r.size)
            return data
        if r.kind not in ("prim", "ptr"):
            raise ValueError("not an editable value")
        if r.fmt in "fd":
            return struct.pack(">" + r.fmt, float(text))
        if r.type == "bool" and text.lower() in ("true", "false"):
            value = 1 if text.lower() == "true" else 0
        else:
            value = int(text, 0)
        bits = 8 * r.size
        signed = r.fmt.islower()
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        if r.kind == "ptr" or not signed:
            if -(1 << (bits - 1)) <= value < 0:
                value &= (1 << bits) - 1      # allow -1 style input for unsigned/pointer fields
        if not lo <= value <= hi:
            raise ValueError("value out of range for %s (%d..%d)" % (r.type, lo, hi))
        return struct.pack(">" + r.fmt, value)

    def is_current(self, item):
        a = item.actor
        if a["pid"] == -1:
            return self.runtime is a
        current = self.by_pid.get(a["pid"])
        return (current is not None and current["addr"] == a["addr"]
                and self._u32(a["addr"] + A_PID) == a["pid"])

    def is_locked(self, item):
        addr = item.actor["addr"] + item.row.off
        return addr in self.locks and self.lock_owners.get(addr) == (item.actor["pid"], item.actor["addr"])

    def toggle_lock(self, item):
        if not item.editable or not self.is_current(item):
            return False, "Cannot lock: value is not editable or process no longer exists."
        addr = item.actor["addr"] + item.row.off
        if self.is_locked(item):
            self.locks.pop(addr, None)
            self.lock_owners.pop(addr, None)
            return True, "Unlocked %s @ %08X" % (item.row.name, addr)
        try:
            # Freeze the current memory value, rather than a possibly older display snapshot.
            data = bytes(self.mem.read_bytes(addr, item.row.size))
            if len(data) != item.row.size:
                raise ValueError("incomplete memory read")
            self.locks[addr] = data
            self.lock_owners[addr] = (item.actor["pid"], item.actor["addr"])
            return True, "Locked %s @ %08X (rewritten every frame)" % (item.row.name, addr)
        except Exception as exc:
            return False, "Lock failed: %s" % exc

    def set_value(self, item, text, lock=False):
        try:
            if not self.is_current(item):
                raise ValueError("process no longer exists")
            data = self.encode(item, text)
            addr = item.actor["addr"] + item.row.off
            self.mem.write_bytes(addr, bytearray(data))
            if lock:
                self.locks[addr] = data
                self.lock_owners[addr] = (item.actor["pid"], item.actor["addr"])
            else:
                self.locks.pop(addr, None)
                self.lock_owners.pop(addr, None)
            snap = self.snap.get(item.actor["pid"])
            if snap is not None:                       # show the new value immediately
                self.snap[item.actor["pid"]] = snap[:item.row.off] + data + snap[item.row.off + len(data):]
            return True, "wrote %s -> %08X (%s)" % (data.hex(), addr, "locked" if lock else "once")
        except Exception as exc:
            return False, "edit failed: %s" % (exc,)

    def apply_locks(self):
        for addr, data in list(self.locks.items()):
            pid, base = self.lock_owners[addr]
            if pid != -1 and self._u32(base + A_PID) != pid:
                self.locks.pop(addr, None)
                self.lock_owners.pop(addr, None)
                continue
            try:
                self.mem.write_bytes(addr, bytearray(data))
            except Exception:
                pass
