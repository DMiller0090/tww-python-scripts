"""tww_sim — bit-exact offline simulation of TWW (GZLJ01/JP) player physics.

A pure-stdlib, live-validated reproduction of the game's per-frame player procs
(``daPyProc_*``), organized as importable components so a script pulls in only what it needs::

    from tww_sim.swim import SwimState, plan_min_frames   # swim (superswimming)
    from tww_sim.land import LandState                    # land walking / rolling / turns
    from tww_sim.core import f32, cM_scos                 # shared console-math primitives

Sub-packages:
  ``core/``  shared player engine — ``fp`` (FMA-faithful f32), ``mathlib`` (console trig/tables),
             ``camera``, ``anim`` (J3D runtime), ``tables/`` (and, later, ``collision``).
  ``swim/``  the swim procs (SWIM_UP/WAIT/MOVE) + route planner.
  ``land/``  the land procs (MOVE/ATN_MOVE/turns/FRONT_ROLL) + input planner.

Importing ``tww_sim`` loads nothing heavy; the convenience names below resolve lazily (PEP 562)
so ``from tww_sim import LandState`` never drags in the swim package, and vice-versa.
"""
__version__ = "0.2.0"

# name -> (submodule, attr); resolved on first access so importing one component never
# imports the other (keeps swim/land isolated — the import-hygiene goal).
_LAZY = {
    "SwimState": ("tww_sim.swim", "SwimState"),
    "ArrowState": ("tww_sim.swim", "ArrowState"),
    "run_trace": ("tww_sim.swim", "run_trace"),
    "run_arrow": ("tww_sim.swim", "run_arrow"),
    "plan_min_frames": ("tww_sim.swim", "plan_min_frames"),
    "plan_hierarchical": ("tww_sim.swim", "plan_hierarchical"),
    "beam_search": ("tww_sim.swim", "beam_search"),
    "beam_search_to_dest": ("tww_sim.swim", "beam_search_to_dest"),
    "expand": ("tww_sim.swim", "expand"),
    "acts_to_seq": ("tww_sim.swim", "acts_to_seq"),
    "animdiff": ("tww_sim.swim", "animdiff"),
    "LandState": ("tww_sim.land", "LandState"),
    "run_walk": ("tww_sim.land", "run_walk"),
}


def __getattr__(name):
    import importlib
    try:
        mod, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(importlib.import_module(mod), attr)


def __dir__():
    return sorted(list(globals()) + list(_LAZY))
