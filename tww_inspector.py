"""
tww_inspector.py - live process table in the style of TWW-Heap-Manager (TWWHeapVisualizer).

TWW Inspector tabs (click them on the canvas):

  Processes             EVERY process in the game's layer tree - actors, but also MSG text boxes, scenes,
                        cameras, kankyo, overlaps - with its kind, decomp class, pid, address, size, heap block,
                        and actor execute/draw skip flags. Click a row to expand it into the process's full class layout.
                        Skip flags reflect the last actor wrapper decisions, not the reason (culling, events,
                        stop status, etc.). Menu/pause guards can leave flags unchanged between frames.
  Runtime               the live g_dComIfG_gameInfo object from d_com_inf_game.h: save data,
                        play (stage, room, event control, cameras), draw lists and resource control.

  Heaps                 address-ordered GAME, DYN and ACT bars; drag free space to reserve it,
                        or double-click free space to split a range into a chosen number of blocks.
                        Select a purple reservation and press Release block to undo it.
  Colliders             live collider properties, expandable by owner, with guarded configuration edits.
  BG                    registered background collision meshes, grouped as MoveBG and BG.
  Flags                 Story Flags, Event Registers, current Scene Flags, and Zone Switches.
  SCLS                  existing stage/room scene-change entries; click destination cells to edit.

Expanded rows show inherited bases, nested classes (cXyz, collider blocks, ...), arrays, and unknown u32 words for
bytes the decomp does not name yet. Click an editable Value cell to populate the edit field below
the table, then press Apply value (decimal, 0x hex, float, true/false, hex bytes). Click its Lock checkbox
to freeze/unfreeze that value. The edit controls are created once, so selecting a value starts no external process.

Class layouts load directly from ww/data/inspector_layouts.json; no decomp source files are needed.
Mouse wheel scrolls; click Name, bsPCID or Start to sort. bsPCID defaults to newest first; click again to reverse.
The canvas toolbar provides Collapse all and Live update.

"""
import os
import atexit
import re
import sys
import time
import textwrap

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from dolphin import debug, event, gui, memory

from ww.actor import proc_name
from ww.actor_tree import ActorTree
from ww.heap_view import HeapView
from ww.collider_view import ColliderView
from ww.bg_records import BgRecords, COLUMNS as BG_RECORD_COLUMNS, SUBTABS as BG_SUBTABS
from ww.world_view import WorldView, BG_COLUMNS, ZONE_COLUMNS, ZONE_SWITCH_COLUMNS
from ww.flag_view import FlagView, TABLES as FLAG_TABLES, ROW_HEIGHT as FLAG_ROW_H, LABEL_WIDTH as FLAG_LABEL_W
from ww.collider_properties import FIELDS as COLLIDER_FIELDS
from ww.structs import Structs, load_layouts
from ww.scls import SclsEditor
from ww.scaled_canvas import ScaledCanvas

SIZES_FILE = os.path.join(_here, "ww", "data", "ActorInstanceSizes.txt")
LAYOUT_FILE = os.path.join(_here, "ww", "data", "inspector_layouts.json")

W, H = 1100, 528
ROW_H, TAB_H, HEAD_H, INDENT = 20, 28, 26, 14
FOOT_H = 24
BODY_BOTTOM = H - FOOT_H
TOP = TAB_H + HEAD_H                # first data row starts here
TEXT_BASE = 14                      # canvas text is placed by its BASELINE, so text is drawn at row top + this
TABS = (("actors", "Processes", 130), ("runtime", "Runtime", 80),
        ("heaps", "Heaps", 65), ("flags", "Flags", 70),
        ("colliders", "Colliders", 85), ("bg", "BG", 55), ("scls", "SCLS", 65))
SCLS_COLUMNS = (("Index", 0, None), ("Destination stage", 60, 'stage'),
                ("Spawn", 210, 'spawn'), ("Room", 270, 'room'),
                ("Wipe", 330, 'wipe'), ("Start (right-click to copy)", 390, None))
SCLS_START_X=SCLS_COLUMNS[-1][1]
SCLS_START_END_X=SCLS_START_X+180
COLUMNS = (("Name", 0), ("bsPCID", 250), ("Type / class", 330), ("Value", 500),
           ("Lock", 670), ("Offset", 712), ("Start", 792), ("End", 880), ("Info", 968))
NAME_X, PID_X, TYPE_X, VALUE_X, LOCK_X, OFFS_X, START_X, END_X, INFO_X = (c[1] for c in COLUMNS)
COLLAPSE_X, LIVE_X = 755, 890
HEAP_RELEASE_ALL_X, HEAP_LIVE_X = 865, 1015
UI_INTERVAL = 1.0 / 60.0
PROCESS_INTERVAL = 0.25
VALUE_INTERVAL = 0.10
HEAP_INTERVAL = 1.0
BG, HEAD, ZEBRA, SEL = 0xFF14181D, 0xFF222B36, 0xFF191F26, 0xFF2A4565
TXT, DIM, ACT, VAL, PTR, BAD = 0xFFE6EDF3, 0xFF8B98A8, 0xFF9CE3C0, 0xFFFFD24A, 0xFF7FB6FF, 0xFFFF7070
KIND_COLORS = {"actor": 0xFF9CE3C0, "msg": 0xFFFFB86B, "scene": 0xFFC792EA, "camera": 0xFF7FB6FF, "kankyo": 0xFF82E0AA,
               "overlap": 0xFFF78FB3, "draw": 0xFFB0B8C4, "runtime": 0xFFFFD24A, "process": 0xFFB0B8C4}
COLLIDER_INFO_X, COLLIDER_TYPE_X, COLLIDER_VALUE_X = 420, 610, 690
COLLIDER_OFFS_X, COLLIDER_PID_X, COLLIDER_LOCK_X = 860, 970, 1030
COLLIDER_LOCK_END_X = 1070
COLLIDER_ACTIONS = (('unlock','Unlock all colliders',8,188),)
COLLIDER_ROLE_COLORS = {'at':0xFFFF6060,'tg':0xFF3266B8,'co':0xFF39D9E8,'inactive':DIM}


def load_sizes(path):
    sizes = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                m = re.match(r"\s*(?:PROC_)?(\w+)\s*:\s*([0-9A-Fa-f]+)", line)
                if m:
                    sizes[m.group(1).upper()] = int(m.group(2), 16)
    except OSError:
        pass
    return sizes


# Let the canvas follow the available client area, with a bounded editor below.
CANVAS_MIN_W,CANVAS_MIN_H = 640,240
CONTROL_MIN_H,CONTROL_H = 80,120
WINDOW_STYLE = (
    'ScriptCanvasWidget { min-width: %dpx; min-height: %dpx; } '
    'QScrollArea { border: 0px; min-height: %dpx; max-height: %dpx; }'
) % (CANVAS_MIN_W,CANVAS_MIN_H,CONTROL_MIN_H,CONTROL_H)
panel = gui.window("TWW Inspector", style=WINDOW_STYLE)
# Dolphin sets the requested canvas size as a native minimum on some builds.
# Request the actual floor rather than relying on QSS to reduce a larger one.
canvas = ScaledCanvas(panel.canvas(CANVAS_MIN_W,CANVAS_MIN_H))
W,H=canvas.width,canvas.height
BODY_BOTTOM=H-FOOT_H
COLLAPSE_X,LIVE_X=W-345,W-210
HEAP_RELEASE_ALL_X,HEAP_LIVE_X=W-235,W-85
# Keep these ungrouped: the grouped layout reserves a large bottom panel. Creating
# them once avoids dialog/process startup and preserves typed text during refreshes.
edit_info = panel.text("Click an editable Value cell to select it.")
edit_box = panel.input_text("Value", "")
btn_apply = panel.button("Apply value")
btn_cancel = panel.button("Cancel edit")
btn_cancel.visible = False
btn_switch_set = panel.button('SET switch')
btn_switch_clear = panel.button('Clear switch')
btn_switch_set.visible = btn_switch_clear.visible = False
heap_start = panel.input_text('Start address (hex)', '')
heap_end = panel.input_text('End address (hex, exclusive)', '')
heap_count = panel.input_text('Number of blocks', '8')
heap_gaps = panel.input_text('Block gaps (blocks to skip between allocations)', '0')
heap_block_size = panel.input_text('Block size (payload bytes; blank = auto)', '')
btn_heap_add = panel.button('Create blocks')
btn_heap_cancel = panel.button('Cancel heap split')
heap_help = panel.text('Double-click green space to split it into a chosen number of purple blocks.')
world_details = panel.text('', style='QLabel { qproperty-wordWrap: true; min-width: 0px; max-width: 1050px; min-height: 92px; max-height: 92px; font-family: monospace; font-size: 12px; }')
world_details.visible = False
for control in (heap_start,heap_end,heap_count,heap_gaps,heap_block_size,btn_heap_add,btn_heap_cancel,heap_help):
    control.visible=False


def splash(lines, color=0xFF9CE3C0):
    """Plain message screen, so the canvas is never blank."""
    try:
        canvas.clear()
        canvas.rect_filled((0, 0), (W, H), BG)
        for index, line in enumerate(lines):
            canvas.text((14, 26 + index * 20), color, line)
        canvas.commit()
    except Exception:
        pass


splash(["TWW Inspector", "loading class layouts..."])

class CanvasStatus:
    def __init__(self):
        self.message = "loading class layouts..."

    def set(self, message):
        self.message = message
        if "state" in globals():
            state["dirty"] = True


status = CanvasStatus()
heap_view = HeapView()
heap_view.resize(W)
collider_view = ColliderView(memory,proc_name)
world_view = WorldView(memory,proc_name)
bg_records = BgRecords(memory)
flag_view = FlagView(memory)
scls_editor = SclsEditor(memory)
atexit.register(collider_view.close_registry_watch)
atexit.register(collider_view.clear_locks)

tree = None
load_error = ""
try:
    tables = load_layouts(LAYOUT_FILE)
    tree = ActorTree(memory, Structs(tables), proc_name, load_sizes(SIZES_FILE))
    status.set("%d classes, %d actor profiles loaded" % (len(tables["classes"]), len(tables["procs"])))
    splash(["TWW Inspector", "class layouts loaded - waiting for the first update..."])
except Exception as exc:
    load_error = "could not load inspector layouts from %s: %r" % (LAYOUT_FILE, exc)
    status.set(load_error)
    splash([load_error], BAD)

state = dict(scroll=0, selected=None, tick=0, dirty=True, frame=0, live=True,
             editor=None, next_ui=0.0, next_process=0.0, next_value=0.0, next_heap=0.0,
             heap_split=None,flags_table='story',flag_detail=None,
             scls_tables={},scls_rows=[],bg_table='meshes')
if tree is not None:
    tree.set_sort("pid")
    status.set("Click a Value to edit; click Lock to freeze. bsPCID: newest first. End addresses are exclusive.")


def fit(text, width_px):
    chars = max(0, int(width_px / 7.0) - 1)
    return text if len(text) <= chars else text[:max(0, chars - 1)] + "~"


def switch_mode(mode):
    if tree is None or mode == tree.mode:
        return
    if tree.mode=='colliders':
        collider_view.close_registry_watch()
    cancel_edit()
    close_heap_split()
    heap_view.drag = None
    heap_view.pending = None
    tree.set_mode(mode)
    state["scroll"], state["selected"], state["dirty"] = 0, None, True
    sync_controls()
    if mode == "heaps":
        heap_view.refresh(tree, state["tick"])
        state["next_heap"] = time.monotonic() + HEAP_INTERVAL
    elif mode=='colliders':
        collider_view.refresh(tree)
    elif mode in ('bg','zone'):
        world_view.refresh(tree)
        if mode=='bg' and state['bg_table']!='meshes':bg_records.refresh(tree,state['tick'])
    elif mode=='flags':refresh_flags()
    elif mode=='scls':refresh_scls()
    else:
        tree.read_snapshots()
    status.set("tab: " + {"runtime": "g_dComIfG_gameInfo: save, play, drawlist, resource control and brightness", "actors": "all processes", "heaps": "GAME / DYN / ACT heaps",
                         "colliders":"collider properties; click a row to expand, then a Value to edit",
                         "bg":"click mRoomNo or an expanded Value to edit; right-click Address to copy",
                         "zone":"zone mSwitch arrays and actor roomNo/switchNo targets",
                         "flags":"Click a flag cell to enable or disable it. Green: enabled; red: disabled. Merged cells open the value editor.",
                         "scls":"Stage and loaded room lists; click a destination cell to edit. Right-click Start to copy its address."}[mode])
    if mode == "runtime" and tree.runtime is None:
        status.set("runtime tab needs a detected Wind Waker build")


def cancel_edit():
    state["editor"] = None
    edit_box.value = ""
    edit_info.set("Click an editable Value cell to select it.")
    if tree is not None:sync_controls()


def sync_controls():
    mode=tree.mode
    zone_edit=(mode in ('zone','flags') and isinstance(state['editor'],dict) and 'zone_switch' in state['editor'])
    state_edit=(mode in ('zone','flags') and isinstance(state['editor'],dict) and 'switch_state' in state['editor'])
    flag_edit=(mode=='flags' and isinstance(state['editor'],dict) and 'flag_cell' in state['editor'])
    bg_edit=(mode=='bg' and isinstance(state['editor'],dict) and ('bg_record' in state['editor'] or 'bg_mesh' in state['editor']))
    scls_edit=(mode=='scls' and isinstance(state['editor'],dict) and 'scls' in state['editor'])
    world_details.visible=mode in ('bg','zone','flags') and not (zone_edit or state_edit or flag_edit or bg_edit)
    for control in (edit_info,edit_box,btn_apply):control.visible=mode not in ('heaps','bg','zone','flags','scls') or zone_edit or flag_edit or scls_edit or bg_edit
    btn_cancel.visible=flag_edit
    if state_edit:edit_info.visible=True
    btn_switch_set.visible=btn_switch_clear.visible=state_edit or flag_edit
    form=(mode=='heaps' and state['heap_split'] is not None)
    for control in (heap_start,heap_end,heap_count,heap_gaps,heap_block_size,btn_heap_add,btn_heap_cancel,heap_help):control.visible=form


if tree is not None:sync_controls()


def close_heap_split():
    state['heap_split']=None
    heap_view.last_click=heap_view.split_request=heap_view.drag=None
    sync_controls()


def open_heap_split(request):
    state['heap_split']=request
    heap_start.value='%08X' % request['begin'];heap_end.value='%08X' % request['end']
    try:
        if not 1<=int(heap_count.value.strip(),10)<=4096:heap_count.value='8'
    except ValueError:heap_count.value='8'
    try:
        if not 0<=int(heap_gaps.value.strip(),10)<=4096:heap_gaps.value='0'
    except ValueError:heap_gaps.value='0'
    heap_help.set('Clicked %08X. Block size is payload bytes (decimal or 0x hex); blank splits the range automatically. Gaps skips block-sized slots.' % request['clicked'])
    _=btn_heap_add.clicked;_=btn_heap_cancel.clicked
    sync_controls()
    heap_view.drag=None
    state['dirty']=True


def open_heap_reservation(request):
    close_heap_split()
    heap_view.refresh(tree,state['tick'])
    record=heap_view.reservations.records.get((request['name'],request['node']))
    if record is None or record['token']!=request['token'] or record['size']!=request['size']:
        status.set('Reservation changed; double-click it again.');return
    heap_view.selected_block=(request['name'],request['node']);heap_view.selected_pid=None
    status.set(heap_view.detail())
    state['dirty']=True


def submit_heap_split():
    request=state['heap_split']
    if request is None:return
    begin=int(heap_start.value.strip(),16);end=int(heap_end.value.strip(),16)
    count=int(heap_count.value.strip(),10)
    size=heap_block_size.value.strip()
    block_size=(int(size,16) if size.lower().startswith('0x') else int(size,10)) if size else None
    status.set(heap_view.queue_split(request,begin,end,count,int(heap_gaps.value.strip(),10),block_size))
    close_heap_split()


def poll_heap_split():
    if state['heap_split'] is None:
        _=btn_heap_add.clicked;_=btn_heap_cancel.clicked
        return
    if btn_heap_cancel.clicked:
        _=btn_heap_add.clicked;close_heap_split();status.set('Heap split cancelled.');return
    if btn_heap_add.clicked:
        try:submit_heap_split()
        except Exception as exc:
            status.set('Cannot split free block: '+str(exc))



def begin_zone_switch_edit(row, x):
    if 'state_edit' in row and ZONE_COLUMNS[6][1]<=x<ZONE_COLUMNS[7][1]:
        token=row['state_edit']
        if token['address'] is None:
            cancel_edit();status.set('Switch storage is unavailable.');return True
        state['editor']=dict(switch_state=token)
        edit_info.set('Room %s / Switch %d — %s; current state: %s' %
                      (token['room'],token['number'],token['scope'],row['values'][6]))
        status.set('Choose SET switch or Clear switch.')
        sync_controls()
        return True
    if 'zone_edit' not in row:return False
    for word in range(3):
        left=ZONE_SWITCH_COLUMNS[word+2][1]
        right=ZONE_SWITCH_COLUMNS[word+3][1]
        if left<=x<right:
            token=row['zone_edit']
            try:
                value=world_view.read_zone_switch(tree,token,word)
                state['editor']=dict(zone_switch=dict(token),word=word)
                edit_box.value='0x%04X'%value
                edit_info.set('Zone %d / Room %d — mSwitch[%d] (u16; decimal or 0x hex)' %
                              (token['zone'],token['room'],word))
                status.set('Edit this 16-bit switch word, then Apply value.')
                sync_controls()
            except Exception as exc:status.set('Cannot select zone switch: '+str(exc))
            return True
    return False


def apply_switch_state(enabled):
    item=state['editor']
    if isinstance(item,dict) and 'flag_cell' in item:
        apply_flag_value(item['maximum'] if enabled else 0);return
    if not isinstance(item,dict) or 'switch_state' not in item:return
    try:
        tree.refresh(state['tick'])
        token=item['switch_state']
        world_view.write_target_state(tree,token,enabled)
        label='SET' if enabled else 'clear'
        edit_info.set('Room %s / Switch %d — %s; current state: %s' %
                      (token['room'],token['number'],token['scope'],label))
        status.set('Switch %d: %s' % (token['number'],label))
        state['dirty']=True
    except Exception as exc:status.set('Switch state edit failed: '+str(exc))


def begin_edit(item):
    if not item.editable or not tree.is_current(item):
        status.set("Cannot edit: value is not editable or process no longer exists.")
        return
    tree.read_snapshots()
    if tree.raw(item) is None:
        status.set("Cannot edit: value could not be read.")
        return
    _shown, value = tree.value(item)
    state["editor"] = item
    edit_box.value = value
    r = item.row
    edit_info.set("%s.%s — %s @ %08X (%d bytes)" % (
        item.actor["name"], r.name, r.type, item.actor["addr"] + r.off, r.size))
    status.set("Type a new value below, then Apply value. Lock stays in the table.")


def apply_edit():
    item = state["editor"]
    if item is None:
        status.set("Click an editable Value cell first.")
        return
    if isinstance(item,dict) and 'bg_mesh' in item:
        try:
            token=item['bg_mesh']
            world_view.write_bg(tree,token,edit_box.value)
            cancel_edit()
            status.set('Updated BG field '+token['label'])
        except Exception as exc:status.set('BG edit failed: '+str(exc))
        state['dirty']=True
        return
    if isinstance(item,dict) and 'bg_record' in item:
        try:
            bg_records.write(tree,item['bg_record'],edit_box.value)
            bg_records.refresh(tree,state['tick'])
            cancel_edit()
            status.set('Updated shared BG record; all users of this record are affected.')
        except Exception as exc:status.set('BG edit failed: '+str(exc))
        state['dirty']=True
        return
    if isinstance(item,dict) and 'scls' in item:
        apply_scls_edit(item);return
    # Recheck the layer tree even if live update was paused while typing.
    tree.refresh(state["tick"])
    if isinstance(item,dict) and 'flag_cell' in item:
        apply_flag_value(edit_box.value);return
    if isinstance(item,dict) and 'zone_switch' in item:
        try:
            value=world_view.write_zone_switch(tree,item['zone_switch'],item['word'],edit_box.value)
            edit_box.value='0x%04X'%value
            status.set('Updated Zone %d mSwitch[%d] = 0x%04X' %
                       (item['zone_switch']['zone'],item['word'],value))
            state['dirty']=True
        except Exception as exc:status.set('Zone switch edit failed: '+str(exc))
        return
    if isinstance(item,dict) and item.get('collider'):
        try:
            collider_view.write(item['collider'],item['identity'],item['field'],edit_box.value.strip(),tree)
            status.set('Updated collider %s; actor code may overwrite this setting.' % item['field'])
        except Exception as exc:
            status.set('Collider edit failed: '+str(exc))
        return
    if not tree.is_current(item):
        cancel_edit()
        status.set("Edit discarded: this process no longer exists.")
        return
    ok, message = tree.set_value(item, edit_box.value, lock=tree.is_locked(item))
    status.set(message)
    if ok:
        # Keep the target and field available for repeated edits. Refreshes never
        # overwrite a value the user is in the middle of typing.
        _shown, edit_box.value = tree.value(item)


def refresh_scls():
    try:
        if tree.version!='GZLJ01':raise ValueError('SCLS is verified for JP/GZLJ01 only')
        tables,errors=scls_editor.tables()
        changed={scope:t['identity'] for scope,t in tables.items()}!={scope:t['identity'] for scope,t in state['scls_tables'].items()}
        current=memory.read_bytes(0x803E9F48,1)[0]
        rows=[]
        for scope in sorted(set(tables)|set(errors)):
            label='Stage' if scope==-1 else 'Room %d'%scope
            if scope==current:label+=' (current)'
            table=tables.get(scope)
            rows.append(dict(section=label+(' (%d)'%table['count'] if table else ' — unreadable'),
                             detail=errors.get(scope,'')))
            if table:
                rows.extend(dict(key=(scope,record['index']),scope=scope,record=record)
                            for record in table['records'])
        state.update(scls_tables=tables,scls_rows=rows)
        item=state['editor'];cancelled=False
        if isinstance(item,dict) and 'scls' in item:
            token=item['scls'];table=tables.get(token['scope'])
            if (table is None or table['identity']!=token['identity'] or
                    table['records'][token['index']]['raw']!=token['raw']):
                cancel_edit();cancelled=True
                status.set('SCLS entry or source changed; select the cell again.')
        if not any(row.get('key')==state['selected'] for row in rows):state['selected']=None
        if changed and not cancelled:
            status.set('%d SCLS entries in %d lists%s. Click a destination cell to edit; right-click Start to copy.'%
                       (sum(t['count'] for t in tables.values()),len(tables),
                        '; %d unreadable'%len(errors) if errors else ''))
    except Exception as exc:
        state.update(scls_tables={},scls_rows=[],selected=None,scroll=0)
        cancel_edit();status.set('SCLS read failed: '+str(exc))
    state['dirty']=True


def select_scls_cell(index,x):
    rows=state['scls_rows']
    if not 0<=index<len(rows) or 'section' in rows[index]:return
    row=rows[index];record=row['record'];table=state['scls_tables'][row['scope']]
    cancel_edit();state['selected']=row['key']
    field=next((field for i,(_title,start,field) in enumerate(SCLS_COLUMNS)
                if start<=x<(SCLS_COLUMNS[i+1][1] if i+1<len(SCLS_COLUMNS) else W)),None)
    if field:
        state['editor']=dict(scls=scls_editor.select(table,record['index']),field=field,record=dict(record))
        edit_box.value=str(record[field])
        edit_info.set('%s SCLS[%d].%s @ %08X — %s'%(table['label'],record['index'],field,record['address'],
            '1..8 ASCII characters' if field=='stage' else '0..255, decimal or 0x hex'))
        sync_controls()


def copy_scls_address(click):
    x,y=click
    if not (SCLS_START_X<=x<SCLS_START_END_X and TOP<=y<BODY_BOTTOM):return
    index=state['scroll']+int((y-TOP)//ROW_H);rows=state['scls_rows']
    if not 0<=index<len(rows) or 'section' in rows[index]:return
    row=rows[index]
    try:
        table=scls_editor.table(row['scope'])
        if table['identity']!=state['scls_tables'][row['scope']]['identity']:
            raise ValueError('SCLS source changed; refresh again')
        address=table['records'][row['record']['index']]['address']
        gui.set_clipboard('0x%08X'%address)
        status.set('Copied %s SCLS[%d] address: 0x%08X'%(table['label'],row['record']['index'],address))
    except Exception as exc:status.set('SCLS address copy failed: '+str(exc))


def apply_scls_edit(item):
    try:
        if tree.version!='GZLJ01':raise ValueError('SCLS is verified for JP/GZLJ01 only')
        fields={key:item['record'][key] for key in ('stage','spawn','room','wipe','extra')}
        fields[item['field']]=edit_box.value
        token=scls_editor.write(item['scls'],**fields)
        table=scls_editor.table(token['scope']);record=table['records'][token['index']]
        item.update(scls=token,record=dict(record))
        edit_box.value=str(record[item['field']])
        refresh_scls()
        status.set('Updated %s SCLS[%d].%s'%(table['label'],token['index'],item['field']))
    except Exception as exc:status.set('SCLS edit failed: '+str(exc))


def draw_scls():
    canvas.rect_filled((0,TAB_H),(W,TOP),HEAD)
    for title,x,_field in SCLS_COLUMNS:
        canvas.text((x+8,TOP-9),DIM,title,size=10 if x==SCLS_START_X else 13)
    rows=state['scls_rows'];visible=max(1,(BODY_BOTTOM-TOP)//ROW_H)
    state['scroll']=max(0,min(state['scroll'],max(0,len(rows)-visible)))
    for slot,row in enumerate(rows[state['scroll']:state['scroll']+visible]):
        y=TOP+slot*ROW_H
        if 'section' in row:
            canvas.rect_filled((0,y),(W,y+ROW_H),HEAD)
            canvas.text((8,y+TEXT_BASE),TXT,fit(row['section']+'  '+row['detail'],W-16))
            continue
        record=row['record'];selected=row['key']==state['selected']
        if selected or slot%2:
            canvas.rect_filled((0,y),(W,y+ROW_H),SEL if selected else ZEBRA)
        values=(str(record['index']),record['stage'],str(record['spawn']),str(record['room']),
                '0x%02X'%record['wipe'],'%08X'%record['address'])
        for (_title,x,field),value in zip(SCLS_COLUMNS,values):
            canvas.text((x+8,y+TEXT_BASE),VAL if field else TXT,value)
    if not rows:canvas.text((8,TOP+TEXT_BASE),DIM,fit('No loaded SCLS lists. '+status.message,W-16))
    if len(rows)>visible:
        height=BODY_BOTTOM-TOP;bar=max(20,int(visible/len(rows)*height))
        top=TOP+int(state['scroll']/len(rows)*height)
        canvas.rect_filled((W-6,top),(W-2,top+bar),0xFF4A5A6E)


def refresh_flags():
    if state['flags_table']=='zone':world_view.refresh(tree)
    else:
        flag_view.refresh(tree,state['flags_table'])
        detail=state.get('flag_detail')
        if detail and detail['token']['table']=='scene':
            current=next((c for r in flag_view.rows for c in r['cells'] if c['key']==detail['key']),None)
            if current is None or current['token']!=detail['token']:
                cancel_edit();state.update(flag_detail=None,selected=None)


def apply_flag_value(value):
    item=state['editor']
    if not isinstance(item,dict) or 'flag_cell' not in item:return
    try:
        tree.refresh(state['tick'])
        result=flag_view.write(tree,item['flag_cell'],value)
        edit_box.value=str(result)
        status.set('Flag/register updated: %d (0x%X)'%(result,result))
        state['dirty']=True
    except Exception as exc:status.set('Flag edit failed: '+str(exc))


def handle_flags_input(click,wheel):
    if click and click[1]<TAB_H:return False
    if click and TAB_H<=click[1]<TOP:
        left=0
        for table,_title,width in FLAG_TABLES:
            if left<=click[0]<left+width:
                cancel_edit();state.update(flags_table=table,scroll=0,selected=None,flag_detail=None,dirty=True)
                refresh_flags()
                status.set('Click a flag cell to enable or disable it. Green: enabled; red: disabled. Merged cells open the value editor.'
                           if table!='zone' else world_view.messages['zone'])
                break
            left+=width
        return True
    if state['flags_table']=='zone':return False
    visible=max(1,(BODY_BOTTOM-TOP-HEAD_H)//FLAG_ROW_H)
    if wheel:
        state['scroll']=max(0,min(state['scroll']-int(round(wheel)),max(0,len(flag_view.rows)-visible)))
        state['dirty']=True
    if click:
        x,y=click;body_top=TOP+HEAD_H
        if not (FLAG_LABEL_W<=x<W-8 and body_top<=y<BODY_BOTTOM):return True
        index=state['scroll']+int((y-body_top)//FLAG_ROW_H)
        column=int((x-FLAG_LABEL_W)/((W-FLAG_LABEL_W-8)/8))
        cell=flag_view.cell(index,column)
        if cell is None:return True
        cancel_edit();state['selected']=cell['key'];state['flag_detail']=cell
        if not cell['writable']:
            status.set('Scene padding / stage ID is read-only.');state['dirty']=True;return True
        try:
            if cell['span']==1:
                value=flag_view.write(tree,cell['token'])
                status.set('Flag %s: %s'%(cell['label'] or 'Unknown','SET' if value else 'clear'))
            else:
                value=flag_view.read(tree,cell['token'])
                state['editor']=dict(flag_cell=cell['token'],maximum=cell['maximum'])
                edit_box.value=str(value)
                edit_info.set(fit(cell['label'] or 'Register',1000)+'\nValue 0..%d; decimal or 0x hex. SET fills this field; Clear writes zero.'%cell['maximum'])
                status.set('Edit this register value, or choose SET switch / Clear switch.')
                sync_controls()
        except Exception as exc:status.set('Flag edit failed: '+str(exc))
        state['dirty']=True
    return True


def draw_flags():
    left=0
    for table,title,width in FLAG_TABLES:
        canvas.rect_filled((left,TAB_H),(left+width,TOP),SEL if table==state['flags_table'] else HEAD)
        canvas.text((left+8,TOP-8),TXT,title);left+=width
    if state['flags_table']=='zone':
        draw_world_table();return
    body_top=TOP+HEAD_H
    canvas.rect_filled((0,TOP),(W,body_top),HEAD)
    canvas.text((8,body_top-9),DIM,'Byte / value')
    width=(W-FLAG_LABEL_W-8)/8
    for column in range(8):canvas.text((FLAG_LABEL_W+column*width+8,body_top-9),TXT,'0x%02X'%(128>>column))
    visible=max(1,(BODY_BOTTOM-body_top)//FLAG_ROW_H)
    state['scroll']=max(0,min(state['scroll'],max(0,len(flag_view.rows)-visible)))
    for slot,row in enumerate(flag_view.rows[state['scroll']:state['scroll']+visible]):
        y=body_top+slot*FLAG_ROW_H
        canvas.text((8,y+18),TXT,'+%02X'%row['offset'])
        canvas.text((8,y+36),DIM,'0x%02X'%row['value'])
        for cell in row['cells']:
            x=FLAG_LABEL_W+cell['column']*width;right=x+cell['span']*width
            selected=cell['key']==state['selected']
            canvas.rect_filled((x+2,y+2),(right-2,y+FLAG_ROW_H-2),SEL if selected else ZEBRA)
            canvas.rect((x+2,y+2),(right-2,y+FLAG_ROW_H-2),cell['border'],0,2)
            label=cell['label'] or ('Padding' if state['flags_table']=='scene' and row['offset'] in (0x22,0x23) else 'Unknown')
            chars=max(1,int((right-x-14)/7))
            lines=textwrap.wrap(label,chars) or ['']
            for n,line in enumerate(lines[:4]):
                if n==3 and len(lines)>4:line=fit(line+'...',right-x-14)
                canvas.text((x+7,y+16+n*14),TXT if cell['writable'] else DIM,line)
            shown=('SET' if cell['value'] else 'clear') if cell['span']==1 else 'Value %d / %d'%(cell['value'],cell['maximum'])
            canvas.text((x+7,y+FLAG_ROW_H-8),DIM,shown)
    if not flag_view.rows:canvas.text((12,body_top+20),DIM,fit(flag_view.message,W-24))
    if len(flag_view.rows)>visible:
        height=BODY_BOTTOM-body_top;bar=max(20,int(visible/len(flag_view.rows)*height))
        top=body_top+int(state['scroll']/len(flag_view.rows)*height)
        canvas.rect_filled((W-6,top),(W-2,top+bar),DIM)
    detail=flag_view.message+'\nSingle-click a flag cell to enable or disable it. Green: enabled / nonzero | Red: disabled / zero | Gray: read-only.\n'
    cell=state['flag_detail']
    if cell:
        detail+=(cell['label'] or 'Unknown bit')+'\nByte +%02X, mask 0x%02X | %s'%(cell['token']['offset'],cell['token']['mask'],
                 'multi-bit register' if cell['span']>1 else 'single bit')
    lines=[]
    for line in detail.splitlines():lines.extend(textwrap.wrap(line,140) or [''])
    shown='\n'.join(lines[:7])
    if shown!=state.get('world_detail'):
        world_details.set(shown);state['world_detail']=shown


def handle_input():
    items = (world_view.rows(tree.mode) if tree.mode in ('bg','zone','flags') else
             collider_view.rows() if tree.mode=='colliders' else [] if tree.mode=='heaps' else tree.items())
    wheel = canvas.take_wheel()
    click = canvas.take_click()
    right_click = canvas.take_right_click()
    if tree.mode=='bg' and (not click or click[1]>=TAB_H):
        handle_bg_input(click,right_click,wheel);return
    if tree.mode=='colliders' and click and TAB_H<=click[1]<TOP:
        handle_collider_action(click[0]);return
    if tree.mode=='flags' and handle_flags_input(click,wheel):return
    if tree.mode=='scls' and (not click or click[1]>=TAB_H):
        if right_click:copy_scls_address(right_click)
        if wheel:state['scroll']-=int(round(wheel*3));state['dirty']=True
        if click and TOP<=click[1]<BODY_BOTTOM:
            select_scls_cell(state['scroll']+int((click[1]-TOP)//ROW_H),click[0])
            state['dirty']=True
        visible=max(1,(BODY_BOTTOM-TOP)//ROW_H)
        state['scroll']=max(0,min(state['scroll'],max(0,len(state['scls_rows'])-visible)))
        return
    if tree.mode == "heaps" and (not click or click[1] >= TAB_H):
        if state['heap_split'] is not None:return
        if heap_view.double_click(click):
            request=heap_view.split_request;heap_view.split_request=None
            if request.get('kind')=='reservation':open_heap_reservation(request)
            else:open_heap_split(request)
            return
        changed = heap_view.handle_input(click, wheel, BODY_BOTTOM)
        changed = heap_view.poll_drag(canvas, click) or changed
        if heap_view.message:
            status.set(heap_view.message)
            heap_view.message = None
        if changed:
            state["dirty"] = True
        return
    if wheel:
        state["scroll"] -= int(round(wheel * 3))
        state["dirty"] = True
    if click:
        x, y = click
        state["dirty"] = True
        if not (0 <= x < W and 0 <= y < BODY_BOTTOM):
            return
        if y < TAB_H:
            live_x=HEAP_LIVE_X if tree.mode=='heaps' else LIVE_X
            if tree.mode=='colliders' and COLLAPSE_X-190<=x<COLLAPSE_X:
                try:
                    tree.refresh(state['tick'])
                    status.set(collider_view.teleport(tree,x<COLLAPSE_X-95))
                except Exception as exc:status.set('Teleport failed: '+str(exc))
                return
            elif tree.mode=='bg' and COLLAPSE_X-110<=x<COLLAPSE_X:
                world_view.open_bg.clear();bg_records.open.clear();state['scroll']=0
                return
            elif tree.mode=='heaps' and HEAP_RELEASE_ALL_X<=x<live_x:
                close_heap_split()
                heap_view.drag=None
                status.set(heap_view.queue_release_all())
                return
            elif COLLAPSE_X <= x < live_x:
                if tree.mode in ('bg','zone','flags','scls'):return
                if tree.mode == "heaps":
                    status.set(heap_view.queue_release())
                elif tree.mode=='colliders':
                    collider_view.open.clear()
                    state['scroll']=0
                else:
                    tree.collapse_all()
                    state["scroll"] = 0
            elif x >= live_x:
                state["live"] = not state["live"]
                status.set("Live update " + ("on" if state["live"] else "paused"))
            else:
                left = 0
                for mode, _label, width in TABS:
                    if left <= x < left + width:
                        switch_mode(mode)
                    left += width
        elif y < (TOP+HEAD_H if tree.mode in ('flags','colliders') else TOP):
            for start, end, mode in ((NAME_X, PID_X, "name"), (PID_X, TYPE_X, "pid"),
                                     (START_X, END_X, "addr")):
                if start <= x < end and tree.mode == "actors":
                    tree.set_sort(mode, descending=(not tree.sort_descending if tree.sort == mode else mode == "pid"))
                    state["scroll"] = 0
                    status.set("Sorted by %s (%s)" % (mode, "descending" if tree.sort_descending else "ascending"))
        else:
            body_top=TOP+HEAD_H if tree.mode in ('flags','colliders') else TOP
            index = state["scroll"] + int((y - body_top) // ROW_H)
            if 0 <= index < len(items):
                item = items[index]
                if tree.mode in ('bg','zone','flags'):
                    if item.get('section') or item.get('table_header'):return
                    state['selected']=item['key']
                    if tree.mode in ('zone','flags'):
                        if begin_zone_switch_edit(item,x):return
                        cancel_edit()
                    if tree.mode=='bg' and x<300:world_view.toggle_bg(item)
                    status.set(world_view.messages['zone' if tree.mode=='flags' else tree.mode])
                    return
                if tree.mode=='colliders':
                    handle_collider_row(item,x)
                    return
                state["selected"] = item.key
                arrow_zone = NAME_X + 6 + item.depth * INDENT
                if item.expandable and x < PID_X and (x < arrow_zone + 18 or item.kind == "actor"):
                    tree.toggle(item)
                    tree.read_snapshots()
                elif item.editable and LOCK_X <= x < OFFS_X:
                    _ok, message = tree.toggle_lock(item)
                    status.set(message)
                elif item.editable and VALUE_X <= x < LOCK_X:
                    begin_edit(item)
    body_top=TOP+HEAD_H if tree.mode in ('flags','colliders') else TOP
    visible = (BODY_BOTTOM - body_top) // ROW_H
    count=(len(world_view.rows(tree.mode)) if tree.mode in ('bg','zone','flags') else
           len(collider_view.rows()) if tree.mode=='colliders' else len(tree.items()))
    if tree.mode=='flags' and state['flags_table']!='zone':
        visible=max(1,(BODY_BOTTOM-body_top)//FLAG_ROW_H);count=len(flag_view.rows)
    state["scroll"] = max(0, min(state["scroll"], max(0, count - visible)))


def handle_collider_action(x):
    for _action,_label,left,right in COLLIDER_ACTIONS:
        if not left<=x<right:continue
        collider_view.clear_locks();status.set('All collider locks released.')
        state['dirty']=True
        return


def handle_collider_row(row,x):
    if row.get('section'):return
    address,field=row['key'];collider_view.selected=row['key']
    state['selected']=row['key']
    if COLLIDER_LOCK_X<=x<COLLIDER_LOCK_END_X:
        if field is not None and (field=='position' or row['editable']):
            try:status.set(collider_view.toggle_lock(address,field,tree))
            except Exception as exc:status.set('Collider lock failed: '+str(exc))
            state['dirty']=True
        return
    if field is None and x<COLLIDER_INFO_X:
        if address in collider_view.open:collider_view.open.remove(address)
        else:collider_view.open.add(address)
        summary=collider_view.state(row['record'],row['role'])['detail']
        if row.get('role')=='co':summary=collider_view.name(row['record'],'co')+'; '+summary
        status.set(summary)
    elif field is None and COLLIDER_VALUE_X<=x<COLLIDER_OFFS_X:
        status.set(collider_view.state(row['record'],row['role'])['detail'])
    elif field is not None and COLLIDER_VALUE_X<=x<COLLIDER_OFFS_X:
        edit_box.value=collider_view.value(row)
        edit_info.set('Collider %08X.%s — %s' % (address,field,edit_box.value))
        state['editor']=(dict(collider=address,identity=row['record']['identity'],field=field)
                         if row['editable'] else None)
        status.set('Apply value to edit this collider field.' if row['editable'] else
                   'Derived summary; edit the underlying stored fields.')


def draw_colliders():
    canvas.rect_filled((0,TAB_H),(W,TOP),HEAD)
    for action,label,left,right in COLLIDER_ACTIONS:
        canvas.rect_filled((left,TAB_H+3),(right,TOP-3),SEL)
        canvas.text((left+6,TOP-9),TXT,label)
    body_top=TOP+HEAD_H
    canvas.rect_filled((0,TOP),(W,body_top),HEAD)
    for title,x in (('Types / field',0),('Owner',COLLIDER_INFO_X),('Shape',COLLIDER_TYPE_X),
                    ('Value',COLLIDER_VALUE_X),('Offset / addr',COLLIDER_OFFS_X),('bsPCID',COLLIDER_PID_X),('Lock',COLLIDER_LOCK_X)):
        canvas.text((x+8,body_top-9),DIM,title)
    table_bottom=BODY_BOTTOM
    rows=collider_view.rows();visible=(table_bottom-body_top)//ROW_H
    state['scroll']=max(0,min(state['scroll'],max(0,len(rows)-visible)))
    for slot,row in enumerate(rows[state['scroll']:state['scroll']+visible]):
        y=body_top+slot*ROW_H;ty=y+TEXT_BASE
        role_color=COLLIDER_ROLE_COLORS[row['role']]
        if row.get('section'):
            canvas.rect_filled((0,y),(W,y+ROW_H),HEAD)
            canvas.rect_filled((0,y),(4,y+ROW_H),role_color)
            canvas.text((8,ty),role_color,row['section'])
            continue
        record=row['record'];field=row['field'];address=record['address']
        if row['key']==collider_view.selected:canvas.rect_filled((0,y),(W,y+ROW_H),SEL)
        elif (state['scroll']+slot)%2:canvas.rect_filled((0,y),(W,y+ROW_H),ZEBRA)
        if field is None:
            canvas.rect_filled((0,y),(4,y+ROW_H),role_color)
            canvas.text((8,ty),role_color,fit(('v ' if address in collider_view.open else '> ')+collider_view.name(record,row['role']),COLLIDER_INFO_X-12))
            canvas.text((COLLIDER_PID_X+8,ty),TXT,'—' if record.get('ownerless') else '%d' % record['actor']['pid'])
            canvas.text((COLLIDER_TYPE_X+8,ty),TXT,record['shape'][0])
            summary=collider_view.state(record,row['role'])
            canvas.text((COLLIDER_VALUE_X+8,y+8),VAL if summary['hit'] else DIM,summary['hit_text'],size=10)
            canvas.text((COLLIDER_VALUE_X+8,y+18),BAD if summary['blocked'] else DIM,summary['owner'],size=10)
            canvas.text((COLLIDER_OFFS_X+8,ty),TXT,'%08X' % address)
            canvas.text((COLLIDER_INFO_X+8,ty),DIM,fit(record['actor']['name'],COLLIDER_TYPE_X-COLLIDER_INFO_X-12))
        else:
            canvas.text((24,ty),TXT,fit(field,COLLIDER_INFO_X-30))
            spec=record['fields'].get(field)
            canvas.text((COLLIDER_TYPE_X+8,ty),DIM,{'I':'u32','H':'u16','B':'u8','b':'s8','f':'f32'}.get(spec[1],spec[1]) if spec else 'derived')
            canvas.text((COLLIDER_VALUE_X+8,ty),VAL if row['editable'] else DIM,fit(collider_view.value(row),COLLIDER_OFFS_X-COLLIDER_VALUE_X-12))
            canvas.text((COLLIDER_OFFS_X+8,ty),DIM,('stts+%X' if field.startswith('status.') else '+0x%X') % spec[0] if spec else '')
            canvas.text((COLLIDER_INFO_X+8,ty),DIM,'editable' if row['editable'] else 'read-only')
        if field is not None and (field=='position' or row['editable']):
            locked=collider_view.is_locked(address,field)
            canvas.rect((COLLIDER_LOCK_X+14,y+4),(COLLIDER_LOCK_X+26,y+16),ACT if locked else DIM)
            if locked:
                canvas.line((COLLIDER_LOCK_X+16,y+10),(COLLIDER_LOCK_X+19,y+13),ACT)
                canvas.line((COLLIDER_LOCK_X+19,y+13),(COLLIDER_LOCK_X+24,y+6),ACT)
    if not rows:canvas.text((16,body_top+TEXT_BASE),DIM,'Discovering colliders...')


def bg_rows():
    return world_view.rows('bg') if state['bg_table']=='meshes' else bg_records.rows(state['bg_table'])


def begin_bg_mesh_edit(token):
    if token is None:
        cancel_edit();status.set('This row describes a location rather than a stored field.')
        return
    try:
        world_view.validate_bg(tree,token)
        cancel_edit()
        state['editor']={'bg_mesh':token}
        value=int.from_bytes(token['raw'],'big',signed=token['format'] in ('i','h','b'))
        edit_box.value='0x%X'%value if token['hex'] else str(value)
        help_text='Decimal or 0x hex.'
        if token['label']=='mRoomNo':help_text+=' 65535 / 0xFFFF / -1 uses the DZB group room lookup.'
        edit_info.set('%s @ %08X\n%s'%(token['label'],token['address'],help_text))
        sync_controls()
    except Exception as exc:status.set('BG edit failed: '+str(exc))


def handle_bg_input(click,right_click,wheel):
    if click and TAB_H<=click[1]<TOP:
        left=0
        for mode,title,width in BG_SUBTABS:
            if left<=click[0]<left+width:
                cancel_edit();state.update(bg_table=mode,scroll=0,selected=None,dirty=True,world_detail=None)
                if mode!='meshes':bg_records.refresh(tree,state['tick'])
                break
            left+=width
        return
    rows=bg_rows();body_top=TOP+HEAD_H
    visible=max(1,(BODY_BOTTOM-body_top)//ROW_H)
    if wheel:
        state['scroll']=max(0,min(state['scroll']-int(round(wheel*3)),max(0,len(rows)-visible)))
        state['dirty']=True
    if right_click:
        x,y=right_click
        index=state['scroll']+int((y-body_top)//ROW_H)
        address_x=BG_COLUMNS[-1][1] if state['bg_table']=='meshes' else BG_RECORD_COLUMNS[2][1]
        if address_x<=x<W and body_top<=y<BODY_BOTTOM and 0<=index<len(rows):
            row=rows[index]
            try:
                if state['bg_table']=='meshes':
                    token=row.get('bg_edit') if row.get('child') else row.get('room_edit')
                    if token is None:return
                    address=world_view.validate_bg(tree,token)
                else:
                    bg_records.validate(tree,row['record'])
                    address=row['address']
                gui.set_clipboard('0x%08X'%address)
                status.set('Copied address 0x%08X'%address)
            except Exception as exc:status.set('Copy failed: '+str(exc))
    if not click:return
    x,y=click;index=state['scroll']+int((y-body_top)//ROW_H)
    if not (body_top<=y<BODY_BOTTOM and 0<=index<len(rows)):return
    row=rows[index];state.update(selected=row['key'],dirty=True)
    if state['bg_table']=='meshes':
        if row.get('section'):return
        if row.get('child') and BG_COLUMNS[3][1]<=x<BG_COLUMNS[4][1]:
            begin_bg_mesh_edit(row.get('bg_edit'))
        elif not row.get('child') and BG_COLUMNS[4][1]<=x<BG_COLUMNS[5][1]:
            begin_bg_mesh_edit(row.get('room_edit'))
        elif x<BG_COLUMNS[1][1]:world_view.toggle_bg(row)
        return
    if row['field'] is None:
        if x<BG_RECORD_COLUMNS[1][1]:
            key=row['record']['key']
            if key in bg_records.open:bg_records.open.remove(key)
            else:bg_records.open.add(key)
        return
    if BG_RECORD_COLUMNS[1][1]<=x<BG_RECORD_COLUMNS[2][1]:
        cancel_edit()
        state['editor']={'bg_record':row}
        edit_box.value=row['values'][1]
        edit_info.set('%s @ %08X\nShared record: edits affect every user. Decimal or 0x hex.' %
                      (row['field'],row['address']))
        sync_controls()


def draw_bg():
    left=0
    for mode,title,width in BG_SUBTABS:
        canvas.rect_filled((left,TAB_H),(left+width,TOP),SEL if state['bg_table']==mode else HEAD)
        canvas.text((left+8,TOP-8),TXT,title);left+=width
    if state['bg_table']=='meshes':
        draw_world_table();return
    rows=bg_rows();body_top=TOP+HEAD_H
    canvas.rect_filled((0,TOP),(W,body_top),HEAD)
    for title,x in BG_RECORD_COLUMNS:
        if x==0:title='Ti Record' if state['bg_table']=='ti' else 'DZB group'
        canvas.text((x+8,body_top-9),DIM,title,size=10 if x==BG_RECORD_COLUMNS[2][1] else 13)
    visible=max(1,(BODY_BOTTOM-body_top)//ROW_H)
    state['scroll']=max(0,min(state['scroll'],max(0,len(rows)-visible)))
    for slot,row in enumerate(rows[state['scroll']:state['scroll']+visible]):
        y=body_top+slot*ROW_H
        if row['key']==state['selected']:canvas.rect_filled((0,y),(W,y+ROW_H),SEL)
        elif slot%2:canvas.rect_filled((0,y),(W,y+ROW_H),ZEBRA)
        for col,((title,x),value) in enumerate(zip(BG_RECORD_COLUMNS,row['values'])):
            right=BG_RECORD_COLUMNS[col+1][1] if col+1<len(BG_RECORD_COLUMNS) else W
            if col==0:
                value=(('v ' if row['record']['key'] in bg_records.open else '> ') if row['field'] is None else '  ')+value
            canvas.text((x+8,y+TEXT_BASE),VAL if col==1 and row['field'] else TXT,fit(value,right-x-12))
    if not rows:canvas.text((12,body_top+TEXT_BASE),DIM,bg_records.message)
    if len(rows)>visible:
        height=BODY_BOTTOM-body_top;bar=max(20,int(visible/len(rows)*height))
        top=body_top+int(state['scroll']/len(rows)*height)
        canvas.rect_filled((W-6,top),(W-2,top+bar),DIM)
    help_text=('Ti records hold four packed words (info0..info3); triangles reference a shared record by index. Edits affect all referencing triangles.'
               if state['bg_table']=='ti' else
               'DZB groups form a mesh hierarchy, separate from Ti records. m_info: water 0x100, lava 0x200, poison 0x400, light 0x80000; none means normal. Category filtering uses root children (depth 2). Hierarchy edits do not rebuild spatial trees.')
    selected=next((row for row in rows if row['key']==state['selected']),None)
    detail=('\n'+', '.join(selected['record']['table']['labels'])) if selected else ''
    shown=bg_records.message+'\n'+help_text+'\nStart: right-click to copy the exact record/field address.'+detail
    if shown!=state.get('world_detail'):
        world_details.set(shown);state['world_detail']=shown


def draw_world_table():
    columns = BG_COLUMNS if tree.mode=='bg' else ZONE_SWITCH_COLUMNS
    rows=world_view.rows(tree.mode)
    body_top=TOP+HEAD_H if tree.mode in ('bg','flags') else TOP
    visible=max(1,(BODY_BOTTOM-body_top)//ROW_H)
    state['scroll']=max(0,min(state['scroll'],max(0,len(rows)-visible)))
    header_columns=columns
    if tree.mode in ('zone','flags') and rows:
        first=rows[state['scroll']]
        header_columns=first.get('columns',ZONE_COLUMNS if first['key']==('zone-section','actors') else columns)
    canvas.rect_filled((0,body_top-HEAD_H),(W,body_top),HEAD)
    for title,x in header_columns:
        canvas.text((x+8,body_top-9),DIM,title,size=10 if tree.mode=='bg' and x==BG_COLUMNS[-1][1] else 13)
    for slot,row in enumerate(rows[state['scroll']:state['scroll']+visible]):
        y=body_top+slot*ROW_H
        if row.get('section'):
            canvas.rect_filled((0,y),(W,y+ROW_H),HEAD)
            canvas.rect_filled((0,y),(4,y+ROW_H),row['color'])
            canvas.text((8,y+TEXT_BASE),row['color'],row['section'])
            continue
        if row.get('table_header'):canvas.rect_filled((0,y),(W,y+ROW_H),HEAD)
        elif row['key']==state['selected']:canvas.rect_filled((0,y),(W,y+ROW_H),SEL)
        elif (state['scroll']+slot)%2:canvas.rect_filled((0,y),(W,y+ROW_H),ZEBRA)
        canvas.rect_filled((0,y),(4,y+ROW_H),row['color'])
        row_columns=row.get('columns',columns)
        for index,((_title,x),value) in enumerate(zip(row_columns,row['values'])):
            right=row_columns[index+1][1] if index+1<len(row_columns) else W
            inset=24 if index==0 and row.get('child') else 8
            if tree.mode=='bg' and index==0 and row.get('identity') is not None:
                value=('v ' if row['identity'] in world_view.open_bg else '> ')+str(value)
            canvas.text((x+inset,y+TEXT_BASE),row['color'],fit(str(value),right-x-inset-4))
    world_mode='zone' if tree.mode=='flags' else tree.mode
    if not rows:canvas.text((12,body_top+TEXT_BASE),DIM,fit(world_view.messages[world_mode],W-24))
    selected=next((r for r in rows if r['key']==state['selected']),None)
    detail=selected['detail'] if selected else world_view.messages[world_mode]
    lines=[]
    for line in detail.splitlines():lines.extend(textwrap.wrap(line,140) or [''])
    shown='\n'.join(lines[:4])
    if shown!=state.get('world_detail'):
        world_details.set(shown);state['world_detail']=shown
    if len(rows)>visible:
        bar=max(20,int(visible/len(rows)*(BODY_BOTTOM-body_top)))
        top=body_top+int(state['scroll']/len(rows)*(BODY_BOTTOM-body_top))
        canvas.rect_filled((W-6,top),(W-2,top+bar),0xFF4A5A6E)


def draw():
    canvas.clear()
    canvas.rect_filled((0, 0), (W, H), BG)
    left = 0
    for mode, label, width in TABS:                                   # tab strip
        active = tree.mode == mode
        canvas.rect_filled((left, 0), (left + width - 3, TAB_H), SEL if active else HEAD)
        text = "%s (%d)" % (label, len(tree.actors)) if mode == "actors" else label
        canvas.text((left + 12, TAB_H - 9), TXT if active else DIM, text)
        left += width
    if tree.mode=='colliders':
        ready=collider_view.selected and collider_view.selected[0] in collider_view.records
        for x,label in ((COLLAPSE_X-190,'Link → col'),(COLLAPSE_X-95,'Col → Link')):
            allowed=ready and (label=='Link → col' or not collider_view.records[collider_view.selected[0]].get('ownerless'))
            canvas.text((x+5,TAB_H-9),ACT if allowed else DIM,label)
    if tree.mode=='bg':
        canvas.text((COLLAPSE_X-102,TAB_H-9),TXT,'Collapse all')
    if tree.mode not in ('heaps','bg','zone','flags','scls'):
        canvas.text((COLLAPSE_X + 8, TAB_H - 9), TXT,"Collapse all")
    elif tree.mode=='heaps':
        can_release = heap_view.selected_block in heap_view.reservations.records
        canvas.text((COLLAPSE_X + 8, TAB_H - 9), TXT if can_release else DIM, "Release block")
        canvas.text((HEAP_RELEASE_ALL_X + 8, TAB_H - 9),
                    TXT if heap_view.reservations.records else DIM, "Release all blocks")
    live_x=HEAP_LIVE_X if tree.mode=='heaps' else LIVE_X
    canvas.text((live_x + 8, TAB_H - 9), ACT if state["live"] else DIM,
                "[%s] %s" % ("x" if state["live"] else " ", "Live" if tree.mode=='heaps' else "Live update"))
    if tree.mode == "heaps":
        heap_view.draw(canvas, BODY_BOTTOM)
        draw_footer()
        canvas.commit()
        return
    if tree.mode=='colliders':
        draw_colliders()
        draw_footer();canvas.commit()
        return
    if tree.mode in ('bg','zone'):
        if tree.mode=='bg':draw_bg()
        else:draw_world_table()
        draw_footer();canvas.commit()
        return
    if tree.mode=='flags':
        draw_flags()
        draw_footer();canvas.commit()
        return
    if tree.mode=='scls':
        draw_scls()
        draw_footer();canvas.commit()
        return
    canvas.rect_filled((0, TAB_H), (W, TOP), HEAD)
    for title, x in COLUMNS:
        mode = {"Name": "name", "bsPCID": "pid", "Start": "addr"}.get(title)
        marker = (" v" if tree.sort_descending else " ^") if tree.mode == "actors" and mode == tree.sort else ""
        canvas.text((x + 8, TOP - 9), DIM, title + marker)
    items = tree.items()
    visible = (BODY_BOTTOM - TOP) // ROW_H
    first = state["scroll"]
    for slot in range(visible):
        index = first + slot
        if index >= len(items):
            break
        item = items[index]
        y = TOP + slot * ROW_H
        if item.key == state["selected"]:
            canvas.rect_filled((0, y), (W, y + ROW_H), SEL)
        elif index % 2:
            canvas.rect_filled((0, y), (W, y + ROW_H), ZEBRA)
        ty = y + TEXT_BASE
        x0 = NAME_X + 6 + item.depth * INDENT
        a = item.actor
        start = a["addr"] + (item.row.off if item.row is not None else 0)
        size = item.row.size if item.row is not None else tree.object_size(a)
        canvas.text((START_X + 8, ty), TXT, "%08X" % start)
        canvas.text((END_X + 8, ty), DIM, "%08X" % (start + size) if size else "—")
        if item.expandable:
            if item.key in tree.open:
                canvas.triangle_filled((x0, y + 7), (x0 + 9, y + 7), (x0 + 4.5, y + 14), DIM)
            else:
                canvas.triangle_filled((x0 + 1, y + 5), (x0 + 1, y + 15), (x0 + 8, y + 10), DIM)
        if item.kind == "actor":
            runtime = a["kind"] == "runtime"
            label = a["name"]
            canvas.text((x0 + 14, ty), KIND_COLORS.get(a["kind"], ACT), fit(label, PID_X - x0 - 16))
            if not runtime:
                canvas.text((PID_X + 8, ty), TXT, "%d" % a["pid"])
            kind_text = a['cls'] if runtime else "[%s] %s" % (a["kind"], a["cls"] or "(unknown layout)")
            canvas.text((TYPE_X + 8, ty), TXT, fit(kind_text, VALUE_X - TYPE_X - 12))
            status_text = "Runtime object" if runtime else tree.status(a)
            status_color=BAD if a['kind']=='actor' and (a.get('cond') is None or a['cond'] & 0x06) else DIM
            canvas.text((VALUE_X + 8, ty), status_color, fit(status_text,OFFS_X-VALUE_X-12))
            canvas.text((INFO_X + 8, ty), DIM, fit(tree.info(item) if (a["rows"] is not None or not runtime) else "", W - INFO_X - 12))
        else:
            r = item.row
            canvas.text((OFFS_X + 8, ty), DIM, "+0x%X" % r.off)
            canvas.text((x0 + 14, ty), TXT, fit(r.name, PID_X - x0 - 16))
            canvas.text((TYPE_X + 8, ty), DIM, fit(r.type, VALUE_X - TYPE_X - 12))
            shown, _edit = tree.value(item)
            canvas.text((VALUE_X + 8, ty), PTR if r.kind == "ptr" else (VAL if item.editable else DIM), fit(shown, LOCK_X - VALUE_X - 12))
            if item.editable:
                locked = tree.is_locked(item)
                canvas.rect((LOCK_X + 14, y + 4), (LOCK_X + 26, y + 16), ACT if locked else DIM)
                if locked:
                    canvas.line((LOCK_X + 16, y + 10), (LOCK_X + 19, y + 13), ACT)
                    canvas.line((LOCK_X + 19, y + 13), (LOCK_X + 24, y + 6), ACT)
            canvas.text((INFO_X + 8, ty), DIM, fit(tree.info(item), W - INFO_X - 12))
    total = max(1, len(items))
    if total > visible:
        bar = max(20, int(visible / total * (BODY_BOTTOM - TOP)))
        top = TOP + int(first / total * (BODY_BOTTOM - TOP))
        canvas.rect_filled((W - 6, top), (W - 2, top + bar), 0xFF4A5A6E)
    draw_footer()
    canvas.commit()


def draw_footer():
    canvas.rect_filled((0, BODY_BOTTOM), (W, H), HEAD)
    canvas.text((8, H - 8), DIM, fit(status.message, W - 16))


def update():
    try:collider_view.sync_registry_watch(debug,hasattr(event,'on_memorywatch'),tree.mode=='colliders',tree.version)
    except Exception as exc:status.set('Collider registry capture: '+str(exc))
    try:collider_view.sync_lock_watch(debug,hasattr(event,'on_memorywatch'),tree.version)
    except Exception as exc:status.set('Collider locks: frame polling only; '+str(exc))
    message = heap_view.execute_pending(tree, state["tick"])
    if message:
        status.set(message)
    poll_heap_split()
    if btn_cancel.clicked:
        cancel_edit()
        # Drain an Apply click too: cancelling always wins within one UI poll.
        _ = btn_apply.clicked
        _ = btn_switch_set.clicked
        _ = btn_switch_clear.clicked
        status.set("Edit cancelled.")
    elif btn_switch_set.clicked:
        apply_switch_state(True)
    elif btn_switch_clear.clicked:
        apply_switch_state(False)
    elif btn_apply.clicked:
        apply_edit()
    now = time.monotonic()
    # UI polling, process discovery, and value reads have independent clocks. Fast-forward
    # must not multiply expensive memory walks or redraws; locks still run every frame.
    if state["tick"] == 1 or (state["live"] and now >= state["next_process"]):
        tree.refresh(state["tick"])
        state["next_process"] = now + PROCESS_INTERVAL
        if tree.mode != "heaps":
            state["dirty"] = True
        if tree.mode=='colliders':collider_view.refresh(tree)
        elif tree.mode in ('bg','zone'):
            world_view.refresh(tree)
            if tree.mode=='bg' and state['bg_table']!='meshes':bg_records.refresh(tree,state['tick'])
        elif tree.mode=='flags':refresh_flags()
        elif tree.mode=='scls':refresh_scls()
    if tree.mode == "heaps":
        if state["live"] and now >= state["next_heap"]:
            heap_view.refresh(tree, state["tick"])
            state["next_heap"] = now + HEAP_INTERVAL
            state["dirty"] = True
    elif state["tick"] == 1 or (state["live"] and now >= state["next_value"]):
        if tree.mode=='colliders':collider_view.refresh_values(tree)
        elif tree.mode not in ('bg','zone','flags','scls'):tree.read_snapshots()
        state["next_value"] = now + VALUE_INTERVAL
        state["dirty"] = True


def guarded(step, name):
    """Report a failed UI stage on screen without generating log files."""
    try:
        step()
        return True
    except Exception as exc:
        status.set("%s error: %r" % (name, exc))
        return False


def paint():
    if tree.queue is None:
        splash(["TWW Inspector",
                "No Wind Waker build detected yet (reading the game id at 0x80000000).",
                "Start the game; supported builds: GZLJ01, GZLE01, GZLP01."])
    else:
        draw()


def sync_canvas_size():
    global W, H, BODY_BOTTOM, COLLAPSE_X, LIVE_X, HEAP_RELEASE_ALL_X, HEAP_LIVE_X
    resized=canvas.resize()
    width,height=int(canvas.width),int(canvas.height)
    if width<=0 or height<=0 or (not resized and (width,height)==(W,H)):return
    W,H=width,height
    BODY_BOTTOM=H-FOOT_H
    COLLAPSE_X,LIVE_X=W-345,W-210
    HEAP_RELEASE_ALL_X,HEAP_LIVE_X=W-235,W-85
    heap_view.resize(W)
    state['dirty']=True


def tick_ui():
    """Poll the UI at most 60 Hz, regardless of emulation/fast-forward speed."""
    now = time.monotonic()
    if now < state["next_ui"]:
        return
    state["next_ui"] = now + UI_INTERVAL
    state["tick"] += 1
    if tree is None:
        if state["tick"] % 30 == 1:
            splash([load_error or "class layouts not loaded"], BAD)
        return
    guarded(sync_canvas_size, "canvas size")
    guarded(update, "update")
    guarded(handle_input, "input")
    if state["dirty"]:
        if guarded(paint, "draw"):
            state["dirty"] = False
        else:
            splash(["TWW Inspector: drawing failed", status.message], BAD)


@event.on_frameadvance
def on_frame():
    # Dolphin only emits host updates while emulation is paused; while the game runs, scripts are
    # driven by frame advance, so the whole UI has to tick from here as well.
    state["frame"] += 1
    if tree is not None:
        tree.apply_locks()
        collider_view.apply_locks(tree)
    tick_ui()


@event.on_hostupdate
def on_host():
    tick_ui()


def on_memorywatch(is_write,address,value):
    try:
        if tree is not None:collider_view.on_lock_access(is_write,address,value,tree)
        if collider_view.registry_base is not None and is_write and address==collider_view.registry_base+0x2800 and value==0:
            collider_view.capture_registry()
    except Exception as exc:
        status.set('Collider memory hook failed: '+str(exc))


if hasattr(event,'on_memorywatch'):
    event.on_memorywatch(on_memorywatch)


@event.on_savestateload
def on_state_load(*_):
    collider_view.close_registry_watch()
    cancel_edit()
    close_heap_split()
    collider_view.reset()
    world_view.reset()
    bg_records.reset()
    flag_view.reset();state['flag_detail']=None
    state.update(scls_tables={},scls_rows=[],selected=None)
    state['world_detail']=None
    heap_view.pending=heap_view.drag=None
    heap_view.selected_block=None
    state.update(next_process=0.0,next_value=0.0,next_heap=0.0,dirty=True)
