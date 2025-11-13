# tww-scripts

Wind Waker TAS scripting toolkit for **Dolphin’s Python runtime** (build with Python scripts).
Provides reusable modules (memory, math, camera, actors, analog lookup tables, etc.) and single-purpose scripts like *superswim to destination*.

---

## What this is

- Drop-in: lives inside `Dolphin Emulator/Load/Scripts/ww` (or equivalent).
- Library first: import as `from ww import ...` or `from ww.actors import ...`.
- Single-purpose scripts: enable/disable from Dolphin’s **Scripts** GUI.

> Dolphin’s embedded Python is typically 3.8. Avoid 3.10+ syntax (e.g., `int | None`).

---

## Quick start

1. Place/clone the repo so the `ww/` package is importable by Dolphin:
```
Dolphin Emulator/
  └─ Load/
    └─ Scripts/ (this repo)       
      └─ ww/
```

2. Launch Dolphin → Tools → Scripts → check a script (e.g. `ss_charge_destination.py`).

---

## Folder layout
```
ww/
  __init__.py
  config.py                 # paths to input tables, etc.
  memory.py                 # thin wrappers around dolphin.memory
  mathutils.py              # angle/halfword helpers, 2D geometry, wrapping
  camera.py                 # camera/c-stick angle readers
  collision.py              # simple collision flags
  analog.py                 # CSV-backed angle→stick lookup
  actor.py                  # actor list traversal + base Actor type
  actors/
    __init__.py             # registry + exports
    player.py               # actor child classes
  data/
    INPUT_DUMP_MAIN.csv     # main analog table
    INPUT_DUMP_ALT.csv      # optional alt table
    proc_name_structs.csv   # csv of actor proc ids/names
  addresses/
    ww_jp.py                # JP region addresses (scaffolding available for other versions eventually if needed)

scripts/
  ss_charge_destination.py  # superswim charge to destination example
```
---


## Using the library (example)
```python
from ww.actors import Player
from ww import analog

analog.load_table()                 # loads ww.config INPUT_TABLE_PATH
p = Player()
x, z = p.pos2d()                    # player ground position
xy = analog.stick_for_destination(dest_x, dest_z, actor=p)  # (0..255, 0..255)
```
---

## Requirements

- Dolphin build with Python scripting (embedded Python 3.8).

---

## Contributing

PRs welcome

---

