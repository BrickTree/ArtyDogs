"""Windows screen plumbing: DPI awareness, monitors, cursor position, capture.

Everything works in physical pixels, so the cursor position, window rectangles
and screenshots all line up even on a scaled (125%/150%) display.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image, ImageGrab

user32 = ctypes.WinDLL("user32", use_last_error=True)

Rect = tuple[int, int, int, int]


def make_dpi_aware() -> None:
    """Call before creating any window."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


@dataclass(frozen=True)
class Monitor:
    rect: Rect
    work: Rect  # minus the taskbar
    primary: bool

    def contains(self, x: int, y: int) -> bool:
        return self.rect[0] <= x < self.rect[2] and self.rect[1] <= y < self.rect[3]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def monitors() -> list[Monitor]:
    found: list[Monitor] = []

    def collect(hmon, _hdc, _rect, _param):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            m, w = info.rcMonitor, info.rcWork
            found.append(Monitor((m.left, m.top, m.right, m.bottom),
                                 (w.left, w.top, w.right, w.bottom), bool(info.dwFlags & 1)))
        return True

    callback = _MonitorEnumProc(collect)  # keep a reference for the duration of the call
    user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def monitor_at(x: int, y: int) -> Monitor:
    mons = monitors()
    for m in mons:
        if m.contains(x, y):
            return m
    return next((m for m in mons if m.primary), mons[0])


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def grab(rect: Rect) -> Image.Image:
    """Screenshot of a screen rectangle (virtual-desktop coordinates)."""
    return ImageGrab.grab(bbox=rect, all_screens=True)


def toplevel_hwnd(child_hwnd: int) -> int:
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    return user32.GetAncestor(child_hwnd, 2) or child_hwnd  # GA_ROOT


def window_rect(hwnd: int) -> Rect | None:
    r = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom
