"""Portable hardware-canvas scale cache; native queries are loaded on demand."""
import math
import sys
import time


def _display_scale(geometry):
    from .macos_canvas_scale import _display_scale as query
    return query(geometry)


class HardwareCanvasScale:
    def __init__(self, platform=None, clock=None, reader=None):
        self.platform = sys.platform if platform is None else platform
        self.clock = time.monotonic if clock is None else clock
        self.reader = _display_scale if reader is None else reader
        self.geometry = None
        self.next_check = 0.0
        self.value = 1.0
        self.last_error = None

    def get(self, panel, software=False):
        if software or self.platform != 'darwin':
            return 1.0
        geometry = getattr(panel, 'geometry', None)
        if not (isinstance(geometry, tuple) and len(geometry) == 4 and
                geometry[2] > 0 and geometry[3] > 0):
            geometry = None
        now = self.clock()
        if geometry == self.geometry and now < self.next_check:
            return self.value
        self.geometry, self.next_check = geometry, now + 0.5
        try:
            value = float(self.reader(geometry))
            if math.isfinite(value) and 1.0 <= value <= 4.0:
                self.value = value
                self.last_error = None
            else:
                raise ValueError('Invalid display scale')
        except Exception as exc:
            self.last_error = str(exc)
        return self.value
