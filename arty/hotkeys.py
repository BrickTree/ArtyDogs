"""System-wide hotkeys through RegisterHotKey.

No keyboard hook and nothing sent to the game: Windows simply tells us when the
key combination is pressed. The flip side is that a registered key never
reaches the game, so bind keys WARDOGS doesn't use.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_NOREPEAT = 0x4000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
MODIFIERS = {"ALT": 0x1, "CTRL": 0x2, "CONTROL": 0x2, "SHIFT": 0x4, "WIN": 0x8}
KEYS = {
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    **{f"NUMPAD{i}": 0x60 + i for i in range(10)},
    "NUMPAD_MULTIPLY": 0x6A, "NUMPAD_ADD": 0x6B, "NUMPAD_SUBTRACT": 0x6D,
    "NUMPAD_DECIMAL": 0x6E, "NUMPAD_DIVIDE": 0x6F,
    "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "PAUSE": 0x13, "SCROLLLOCK": 0x91, "SPACE": 0x20, "TAB": 0x09, "BACKQUOTE": 0xC0,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'F8', 'Ctrl+Shift+T', 'Alt+Numpad5' -> (modifier flags, virtual-key code)."""
    parts = [p.strip().upper() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods = 0
    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError(f"unknown modifier {p!r} in {spec!r}")
        mods |= MODIFIERS[p]
    key = parts[-1]
    if key in KEYS:
        return mods, KEYS[key]
    if len(key) == 1 and key.isalnum():
        return mods, ord(key)
    raise ValueError(f"unknown key {key!r} in {spec!r}")


class HotkeyThread(threading.Thread):
    """Registers the hotkeys and runs a message loop; calls on_hotkey(name) per press."""

    def __init__(self, bindings: dict[str, str], on_hotkey: Callable[[str], None]) -> None:
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = dict(bindings)
        self.on_hotkey = on_hotkey
        self.failed: dict[str, str] = {}  # binding name -> reason
        self.ready = threading.Event()
        self._thread_id = 0

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        for i, (name, spec) in enumerate(self.bindings.items(), start=1):
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as e:
                self.failed[name] = str(e)
                continue
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                ids[i] = name
            else:
                err = ctypes.get_last_error()
                self.failed[name] = (f"{spec} is already taken by another program"
                                     if err == ERROR_HOTKEY_ALREADY_REGISTERED
                                     else f"could not register {spec} (Windows error {err})")
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                try:
                    self.on_hotkey(ids[msg.wParam])
                except Exception:  # a failing handler must not kill the hotkeys
                    pass
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
