"""Self-contained process/runtime layout reader for TWW Inspector.

The bundled inspector_layouts.json holds only display/edit metadata. Loading
never scans headers, source trees or a generated-cache directory.
"""
import json
import os
import re
import struct

PRIMS = {  # normalized c type -> (struct fmt, size)
    "u8": ("B", 1), "s8": ("b", 1), "bool": ("B", 1), "char": ("b", 1), "u16": ("H", 2), "s16": ("h", 2),
    "short": ("h", 2), "u32": ("I", 4), "s32": ("i", 4), "int": ("i", 4), "long": ("i", 4), "f32": ("f", 4),
    "float": ("f", 4), "u64": ("Q", 8), "s64": ("q", 8), "f64": ("d", 8), "double": ("d", 8),
    "uint8_t": ("B", 1), "int8_t": ("b", 1), "uint16_t": ("H", 2), "int16_t": ("h", 2), "uint32_t": ("I", 4),
    "int32_t": ("i", 4), "size_t": ("I", 4), "uintptr_t": ("I", 4), "intptr_t": ("i", 4),
    "fpc_ProcID": ("I", 4), "unsigned": ("I", 4), "unsigned int": ("I", 4), "unsigned char": ("B", 1),
    "unsigned short": ("H", 2), "unsigned long": ("I", 4), "signed char": ("b", 1),
}
BUILTIN = {  # tiny math/graphics types the headers describe without offset comments
    "Vec": {"s": 0xC, "f": [[0, "f32", "x", []], [4, "f32", "y", []], [8, "f32", "z", []]]},
    "SVec": {"s": 6, "f": [[0, "s16", "x", []], [2, "s16", "y", []], [4, "s16", "z", []]]},
    "csXyz": {"s": 6, "f": [[0, "s16", "x", []], [2, "s16", "y", []], [4, "s16", "z", []]]},
    "GXColor": {"s": 4, "f": [[0, "u8", "r", []], [1, "u8", "g", []], [2, "u8", "b", []], [3, "u8", "a", []]]},
    "Quaternion": {"s": 0x10, "f": [[0, "f32", "x", []], [4, "f32", "y", []], [8, "f32", "z", []], [12, "f32", "w", []]]},
    "Mtx": {"s": 0x30, "f": [[0, "f32", "m", [12]]]},
}
BUILTIN["cXyz"] = BUILTIN["Vec"]
BUILTIN["_GXColor"] = BUILTIN["GXColor"]

LAYOUT_VERSION = 1
LAYOUT_FILE = os.path.join(os.path.dirname(__file__), "data", "inspector_layouts.json")


def load_layouts(path=None):
    """Load the packaged layout definitions; there is no source/parser fallback."""
    path = path or LAYOUT_FILE
    with open(path, encoding="utf-8") as source:
        tables = json.load(source)
    if tables.get("version") != LAYOUT_VERSION or tables.get("canonical") is not True:
        raise ValueError("Unsupported inspector layout format: " + str(path))
    if not isinstance(tables.get("classes"), dict) or not isinstance(tables.get("procs"), dict):
        raise ValueError("Incomplete inspector layout definitions: " + str(path))
    return tables


def _norm_type(t):
    t = re.sub(r"\b(const|volatile|struct|class|enum|signed)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class Row:
    """One displayed field. kind: prim | ptr | class | array | gap | bytes."""
    __slots__ = ("off", "size", "type", "name", "kind", "fmt", "cls", "count", "path")

    def __init__(self, off, size, type_, name, kind, fmt=None, cls=None, count=0, path=""):
        self.off, self.size, self.type, self.name, self.kind = off, size, type_, name, kind
        self.fmt, self.cls, self.count, self.path = fmt, cls, count, path

    @property
    def expandable(self):
        return self.kind in ("class", "array") or (self.kind == "gap" and self.size > 4)


class Structs:
    def __init__(self, tables, sizes=None):
        self.classes = {name:dict(cdef,ns=cdef.get('ns','')) for name,cdef in tables['classes'].items()}
        self.canonical=tables.get('canonical',False)
        for name, cdef in BUILTIN.items():
            have = self.classes.get(name)
            if have is None or not have["f"]:       # the headers describe these without offset comments
                self.classes[name] = {"b": [], "f": cdef["f"], "s": cdef["s"], "ns": ""}
        self.procs = tables["procs"]
        self.regional_procs=tables.get('regional_procs',{})
        self.proc_ids=tables.get('proc_ids',{})
        self.proc_aliases=tables.get('proc_aliases',{})
        self.sizes = sizes or {}
        self._short = {}
        for q in self.classes:
            self._short.setdefault(q.split("::")[-1], []).append(q)
        self._flat = {}

    KIND_ROOTS = (("fopAc_ac_c", "actor"), ("msg_class", "msg"), ("scene_class", "scene"), ("process_node_class", "scene"),
                  ("camera_class", "camera"), ("kankyo_class", "kankyo"), ("overlap_task_class", "overlap"),
                  ("leafdraw_class", "draw"))

    def kind_for_class(self, cname, _seen=()):
        """actor / msg / scene / camera / kankyo / overlap / draw / process, from the class's base chain."""
        if not cname:
            return "process"
        for root, kind in self.KIND_ROOTS:
            if cname == root:
                return kind
        cdef = self.classes.get(cname)
        if cdef and cname not in _seen:
            for base in cdef["b"]:
                resolved = self.resolve(base, cdef["ns"])
                if resolved:
                    kind = self.kind_for_class(resolved, _seen + (cname,))
                    if kind != "process":
                        return kind
            # Some C-style process layouts embed the framework base at +0
            # instead of declaring C++ inheritance (for example MSG2).
            for off, type_text, _name, dims in cdef['f']:
                if off or dims or '*' in type_text or '&' in type_text:continue
                resolved=self.resolve(type_text,cdef['ns'])
                if resolved:
                    kind=self.kind_for_class(resolved,_seen+(cname,))
                    if kind!='process':return kind
        return "process"

    def class_size(self, cname):
        cdef = self.classes.get(cname)
        if not cdef:
            return 0
        if cdef.get("s"):
            return cdef["s"]
        return self.type_size(cname, cdef["ns"], []) or 0

    def class_for_proc(self, proc_name, version=None, proc_id=None):
        key = (proc_name or "").upper()
        if key.startswith("PROC_"):
            key = key[5:]
        key=self.proc_ids.get(str(proc_id),self.proc_aliases.get(key,key))
        if key in self.regional_procs:return self.resolve(self.regional_procs[key].get(version),'')
        found = self.resolve(self.procs.get(key), "")
        if found:
            return found
        if self.canonical:return None
        camel = "".join(w.capitalize() for w in key.split("_"))
        for guess in ("da%s_c" % camel, "da%s::Act_c" % camel, "%s_class" % key.lower(), "d%s_c" % camel):
            if guess in self.classes and self.classes[guess]["f"]:
                return guess
        return None

    def resolve(self, type_text, ns):
        if not type_text:
            return None
        t = _norm_type(type_text).rstrip("*& ").strip()
        if t in self.classes:
            return t
        if self.canonical:return None
        while ns:
            if ns + "::" + t in self.classes:
                return ns + "::" + t
            ns = ns.rpartition("::")[0]
        cands = self._short.get(t.split("::")[-1], [])
        return cands[0] if len(cands) == 1 else (None if not cands else min(cands, key=len))

    def flatten(self, cname, _seen=()):
        if cname in self._flat:
            return self._flat[cname]
        cdef = self.classes.get(cname)
        out = []
        if cdef is not None and cname not in _seen:
            for base in cdef["b"]:
                b = self.resolve(base, cdef["ns"])
                if b:
                    out.extend(self.flatten(b, _seen + (cname,)))
            out.extend(cdef["f"])
        self._flat[cname] = out
        return out

    def type_size(self, type_text, ns, dims):
        t = _norm_type(type_text)
        count = 1
        for d in dims:
            if d is None:
                return None
            count *= d
        if t.endswith("*"):
            return 4 * count
        if t in PRIMS:
            return PRIMS[t][1] * count
        cname = self.resolve(t, ns)
        if cname:
            cdef = self.classes[cname]
            if cdef.get("s"):
                return cdef["s"] * count
            fields = self.flatten(cname)
            if fields:
                ends = []
                for off, ft, _n, fd in fields:
                    s = self.type_size(ft, cdef["ns"], fd)
                    ends.append(off + (s if s else 4))
                return max(ends) * count
        return None

    def rows(self, cname, base=0, total=None, path=""):
        """Rows covering a class instance: named fields, with unknown bytes between them as gap rows."""
        cdef = self.classes.get(cname)
        ns = cdef["ns"] if cdef else ""
        fields = sorted(self.flatten(cname), key=lambda f: f[0])
        seen, uniq = set(), []
        for f in fields:
            if (f[0], f[2]) not in seen:
                seen.add((f[0], f[2]))
                uniq.append(f)
        rows, cursor = [], 0
        for i, (off, ftype, name, dims) in enumerate(uniq):
            if off > cursor:
                rows.append(Row(base + cursor, off - cursor, "unknown", "unk_%X" % (base + cursor), "gap", path=path))
            nxt = next((uniq[j][0] for j in range(i + 1, len(uniq)) if uniq[j][0] > off), None)
            limit = (nxt - off) if nxt is not None else None
            declared = self.type_size(ftype, ns, dims)
            size = declared if declared else (limit or 4)
            rows.append(self._make_row(base + off, size, ftype, name, dims, ns, path))
            cursor = max(cursor, off + size)
        if total and cursor < total:
            rows.append(Row(base + cursor, total - cursor, "unknown", "unk_%X" % (base + cursor), "gap", path=path))
        return rows

    def _make_row(self, off, size, ftype, name, dims, ns, path):
        t = _norm_type(ftype)
        fpath = path + "/" + name
        total_dims = 1
        for d in dims:
            total_dims *= d or 1
        if dims and dims != [None]:
            el = size // total_dims if total_dims else size
            return Row(off, size, t + "".join("[%s]" % ("?" if d is None else d) for d in dims), name, "array",
                       fmt=(t, el), count=min(total_dims, 512), path=fpath, cls=self.resolve(t, ns))
        if t.endswith("*"):
            return Row(off, 4, t, name, "ptr", "I", path=fpath)
        if t in PRIMS:
            return Row(off, PRIMS[t][1], t, name, "prim", PRIMS[t][0], path=fpath)
        cname = self.resolve(t, ns)
        if cname and self.flatten(cname):
            return Row(off, size, t, name, "class", cls=cname, path=fpath)
        if size == 4:
            return Row(off, 4, t, name, "prim", "I", path=fpath)          # enum / unknown 4-byte type
        if size in (1, 2):
            return Row(off, size, t, name, "prim", "B" if size == 1 else "H", path=fpath)
        return Row(off, size, t, name, "bytes", path=fpath)

    def children(self, row):
        """Child rows of an expandable row."""
        if row.kind == "class":
            return self.rows(row.cls, row.off, row.size, row.path)
        if row.kind == "gap":
            out, o = [], row.off
            while o + 4 <= row.off + row.size:
                out.append(Row(o, 4, "u32", "unk_%X" % o, "prim", "I", path=row.path))
                o += 4
            while o < row.off + row.size:
                out.append(Row(o, 1, "u8", "unk_%X" % o, "prim", "B", path=row.path))
                o += 1
            return out
        if row.kind == "array":
            t, el = row.fmt
            out = []
            cname = self.resolve(t, "")
            for i in range(row.count):
                o = row.off + i * el
                nm = "[%d]" % i
                if t in PRIMS:
                    out.append(Row(o, PRIMS[t][1], t, nm, "prim", PRIMS[t][0], path=row.path + nm))
                elif t.endswith("*"):
                    out.append(Row(o, 4, t, nm, "ptr", "I", path=row.path + nm))
                elif cname and self.flatten(cname):
                    out.append(Row(o, el, t, nm, "class", cls=cname, path=row.path + nm))
                else:
                    out.append(Row(o, el, t, nm, "bytes", path=row.path + nm))
            return out
        return []

    def actor_rows(self, proc_name, size=None):
        cname = self.class_for_proc(proc_name) or "fopAc_ac_c"
        if cname not in self.classes:
            return cname, []
        total = size or None
        return cname, self.rows(cname, 0, total, "")
