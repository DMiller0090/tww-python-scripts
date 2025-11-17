from enum import Enum, unique, auto
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator
from dataclasses import dataclass


@unique
class GameRegion(Enum):
    JAPAN = auto()
    NORTH_AMERICA = auto()
    EUROPE = auto()

ww_context: ContextVar[GameRegion] = ContextVar("region", default = GameRegion.JAPAN)

def current_region() -> GameRegion:
    return ww_context.get()

# Might delete this?
# Trying to think of a way to merge the current_region function into this, so doing "WindWakerContext.region" would
# automatically pull in the correct contextual region.
@dataclass
class WindWakerContext:
    region: GameRegion

@contextmanager
def region(region: GameRegion) -> Generator[WindWakerContext, Any, None]:
    """
    Use in with statements for regional control.
    For example,
    with region(GameRegion.NORTH_AMERICA) as _:
       do_stuff()
    """
    token = ww_context.set(region)
    try:
        yield WindWakerContext(region)
    finally:
        ww_context.reset(token)


def japan(func):
    """
    Helper Decorator
    """
    def with_japan():
        with region(GameRegion.JAPAN) as _:
            func()
    return with_japan

def north_america(func):
    """
    Helper Decorator
    """
    def with_north_america():
        with region(GameRegion.NORTH_AMERICA) as _:
            func()
    return with_north_america

def pal(func):
    """
    Helper Decorator
    """
    def with_pal():
        with region(GameRegion.PAL) as _:
            func()
    return with_pal
