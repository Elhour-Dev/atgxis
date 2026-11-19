"""Keep the PC from going to sleep while clips are being made (an idle laptop sleeping at school stops every recording)."""
from __future__ import annotations

import sys
from typing import Callable

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter() -> Callable[[int], int] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes, fn.restype = [ctypes.c_uint], ctypes.c_uint
        return fn
    except (OSError, AttributeError):
        return None


class KeepAwake:
    """Windows: asks the system not to sleep from idleness until stop() (the screen may still turn off). Elsewhere: does nothing.
    It cannot stop sleep that you cause yourself (closing the lid, choosing Sleep). Must be started and stopped on the same thread."""

    def __init__(self, enabled: bool = True, set_state: Callable[[int], int] | None = None):
        self.enabled, self._set, self.active = enabled, set_state, False

    def start(self) -> bool:
        if not self.enabled:
            return False
        fn = self._set or _windows_setter()
        if fn is None:
            return False
        self._set = fn
        self.active = bool(fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        return self.active

    def stop(self) -> None:
        if self.active and self._set:
            self._set(ES_CONTINUOUS)                  # back to normal power management
        self.active = False
