from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import threading
from collections.abc import Callable

from .parser import HotkeyCombo

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def hotkey_to_windows_codes(combo: HotkeyCombo) -> tuple[int, int]:
    modifiers = 0
    if "alt" in combo.modifiers:
        modifiers |= MOD_ALT
    if "ctrl" in combo.modifiers:
        modifiers |= MOD_CONTROL
    if "shift" in combo.modifiers:
        modifiers |= MOD_SHIFT
    if "win" in combo.modifiers:
        modifiers |= MOD_WIN
    key = combo.key.upper()
    if len(key) == 1:
        return modifiers, ord(key)
    named = {"ENTER": 0x0D, "ESCAPE": 0x1B, "SPACE": 0x20}
    return modifiers, named.get(key, 0)


class WindowsHotkeyBackend:
    def __init__(self, callback: Callable[[], None], hotkey_id: int = 0xBEEF) -> None:
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._running = False
        self._registered = False
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: Exception | None = None

    def register(self, combo: HotkeyCombo) -> None:
        if os.name != "nt":
            raise RuntimeError("전역 단축키 등록은 Windows에서만 지원됨")
        modifiers, virtual_key = hotkey_to_windows_codes(combo)
        if modifiers == 0 or virtual_key == 0:
            raise ValueError("단축키를 Windows 가상 키 코드로 변환할 수 없음")
        self.unregister()
        self._ready.clear()
        self._error = None
        self._running = True
        self._thread = threading.Thread(target=self._message_loop, args=(modifiers, virtual_key), daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            self.unregister()
            raise TimeoutError("RegisterHotKey 메시지 루프 준비 시간 초과")
        if self._error is not None:
            self.unregister()
            raise self._error
        if not self._registered:
            self.unregister()
            raise OSError("RegisterHotKey 등록 상태 확인 실패")

    def unregister(self) -> None:
        self._registered = False
        self._running = False
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = 0

    def _message_loop(self, modifiers: int, virtual_key: int) -> None:
        msg = ctypes.wintypes.MSG()
        # RegisterHotKey(hWnd=None)는 등록한 스레드의 메시지 큐로 WM_HOTKEY를 보낸다.
        # 등록과 GetMessage 루프를 같은 백그라운드 스레드에서 처리해야 실제 전역 핫키가 동작한다.
        ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            if not ctypes.windll.user32.RegisterHotKey(None, self.hotkey_id, modifiers, virtual_key):
                self._error = OSError("RegisterHotKey 실패: 조합이 이미 사용 중이거나 차단됐을 수 있음")
                return
            self._registered = True
            self._ready.set()
            while self._running and ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    self.callback()
        finally:
            if self._registered:
                ctypes.windll.user32.UnregisterHotKey(None, self.hotkey_id)
            self._registered = False
            self._running = False
            self._ready.set()
