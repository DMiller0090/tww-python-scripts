from dataclasses import dataclass
from typing import Optional, Generic, TypeVar

from ww.context import current_region, GameRegion

T = TypeVar('T')

@dataclass
class RegionalValue(Generic[T]):
    """
    Named Tuple that adjusts the value it provides automatically based on the current context.
    """
    japan: Optional[T] = None
    north_america: Optional[T] = None
    pal: Optional[T] = None
    default: Optional[T] = None

    def __get__(self, instance, owner):
        context_region = current_region()
        if context_region is GameRegion.JAPAN:
            return self.japan
        elif context_region is GameRegion.NORTH_AMERICA:
            return self.north_america
        elif context_region is GameRegion.EUROPE:
            return self.pal
        return self.default
