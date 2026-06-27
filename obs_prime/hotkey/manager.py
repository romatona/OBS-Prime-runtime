from __future__ import annotations

import time
from collections.abc import Callable

from .parser import parse_hotkey
from .windows_backend import WindowsHotkeyBackend


class HotkeyManager:
    """Small hotkey boundary.

    Global OS registration is optional for the MVP because Windows global hooks
    can require extra packages/permissions. Validation, debounce, and busy
    protection are implemented here so GUI and future real registration share it.
    """

    def __init__(self, callback: Callable[[str], None], debounce_ms: int = 1500) -> None:
        self.callback = callback
        self.debounce_ms = debounce_ms
        self.combo = parse_hotkey("ctrl+alt+r")
        self.enabled = False
        self.busy = False
        self.last_press_ms = 0
        self.status = "disabled"
        self.last_error = ""
        self.backend: WindowsHotkeyBackend | None = None

    def configure(self, combo: str, enabled: bool, debounce_ms: int, register_global: bool = False) -> None:
        old_combo = self.combo
        old_backend = self.backend
        try:
            parsed = parse_hotkey(combo)
        except ValueError as exc:
            self.last_error = str(exc)
            self.status = "failed"
            self.combo = old_combo
            raise
        new_backend: WindowsHotkeyBackend | None = None
        if enabled and register_global:
            try:
                new_backend = WindowsHotkeyBackend(lambda: self.trigger_for_test())
                new_backend.register(parsed)
            except Exception as exc:
                self.combo = old_combo
                self.backend = old_backend
                self.last_error = str(exc)
                self.status = "failed"
                raise
        if old_backend and old_backend is not new_backend:
            old_backend.unregister()
        self.combo = parsed
        self.enabled = enabled
        self.debounce_ms = debounce_ms
        self.backend = new_backend
        self.status = "registered" if new_backend else ("validated" if enabled else "disabled")
        self.last_error = ""

    def trigger_for_test(self) -> bool:
        if not self.enabled:
            self.status = "disabled"
            return False
        now = int(time.monotonic() * 1000)
        if self.busy:
            self.status = "busy"
            return False
        if now - self.last_press_ms < self.debounce_ms:
            self.status = "debounced"
            return False
        self.last_press_ms = now
        self.busy = True
        try:
            self.callback("hotkey")
        finally:
            self.busy = False
            self.status = "registered" if self.backend else "validated"
        return True
