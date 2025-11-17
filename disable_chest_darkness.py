"""
Run Once: Set chest environment lighting to 50% for visibility during dark world chest storage
"""

from ww import actor
from ww.actors import TBox
from ww.context.context import set_region
from ww.context.detect import detect_region

set_region(detect_region())

for a in actor.iter_actors(typed=True):
    if isinstance(a, TBox):
        a.write_lighting(0.5)