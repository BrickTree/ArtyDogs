"""A small always-on-top panel that sits over the game showing the solution.

Clicks pass straight through it to the game and it never takes focus, so it can
sit over the battlefield without getting in the way. Needs the game in
Borderless/Windowed mode; exclusive fullscreen draws over every other window.
"""
from __future__ import annotations

import ctypes
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

from . import screen

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long


class Hud:
    def __init__(self, root: tk.Tk, palette: dict[str, str], number_family: str, ui_family: str,
                 position: tuple[float, float] = (0.5, 0.06)) -> None:
        self.palette = palette
        self.position = position  # centre-x and top-y as fractions of the main monitor
        self.visible = False
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.88)
        bg = palette["bg"]
        self.frame = tk.Frame(self.win, bg=bg, padx=14, pady=8, highlightthickness=2,
                              highlightbackground=palette["edge"])
        self.frame.pack()
        big = tkfont.Font(family=number_family, size=26, weight="bold")
        mid = tkfont.Font(family=number_family, size=15, weight="bold")
        small = tkfont.Font(family=ui_family, size=9, weight="bold")
        row = tk.Frame(self.frame, bg=bg)
        row.pack()
        self.cells = {}
        for key, label, font, colour in (("az", "AZ", big, palette["fg"]), ("el", "MIL", big, palette["accent"]),
                                         ("dist", "DIST", mid, palette["fg"])):
            cell = tk.Frame(row, bg=bg)
            cell.pack(side="left", padx=10)
            name = tk.Label(cell, text=label, font=small, fg=palette["muted"], bg=bg)
            name.pack(anchor="w")
            value = tk.Label(cell, text="—", font=font, fg=colour, bg=bg)
            value.pack(anchor="w")
            self.cells[key] = (name, value)
        self.note = tk.Label(self.frame, text="", font=small, fg=palette["muted"], bg=bg)
        self.note.pack(anchor="w", pady=(2, 0))
        # Dial assist: how far to turn and raise, read live off the gun sight.
        self.guide = tk.Label(self.frame, text="", font=mid, fg=palette["fg"], bg=bg)
        self._styled = False

    def _make_clickthrough(self) -> None:
        hwnd = screen.toplevel_hwnd(self.win.winfo_id())
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                               style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._styled = True

    def _place(self) -> None:
        mon = next((m for m in screen.monitors() if m.primary), None)
        if mon is None:
            return
        self.win.update_idletasks()
        l, t, r, b = mon.rect
        w = self.win.winfo_reqwidth()
        x = int(l + (r - l) * self.position[0] - w / 2)
        y = int(t + (b - t) * self.position[1])
        self.win.geometry(f"+{x}+{y}")

    def show(self) -> None:
        self._place()
        self.win.deiconify()
        self.win.update_idletasks()
        if not self._styled:
            self._make_clickthrough()
        self.win.attributes("-topmost", True)
        self.visible = True

    def hide(self) -> None:
        self.win.withdraw()
        self.visible = False

    def toggle(self) -> bool:
        (self.hide if self.visible else self.show)()
        return self.visible

    def set_guide(self, text: str, colour: str) -> None:
        if text:
            self.guide.configure(text=text, fg=colour)
            self.guide.pack(anchor="w", pady=(4, 0))
        else:
            self.guide.pack_forget()
        if self.visible:
            self._place()

    def update(self, az: str, el: str, el_label: str, dist: str, note: str, note_colour: str,
               border: str) -> None:
        self.cells["az"][1].configure(text=az)
        self.cells["el"][0].configure(text=el_label)
        self.cells["el"][1].configure(text=el)
        self.cells["dist"][1].configure(text=dist)
        self.note.configure(text=note, fg=note_colour)
        self.frame.configure(highlightbackground=border)
        if self.visible:
            self._place()  # width changes with the numbers; keep it centred


WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+: the window never appears in screenshots
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
KEY = "#010203"  # painted pixels of this colour are see-through


class SightMarks:
    """Markers drawn on the gun sight itself, over the game: where the needed elevation
    sits on the MIL ladder, the needed heading on the compass tape, and the level mark
    beside the tilt pips.

    A full-screen, see-through, click-through window. It asks Windows to keep it out
    of screenshots, so the app's own sight reads never see (or OCR) the markers;
    where Windows can't, the markers stay pure shapes with no digits.
    """

    def __init__(self, root: tk.Tk, font: tuple) -> None:
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        self.win.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(self.win, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = font
        self.visible = False
        self.hidden_from_capture = False
        self._styled = False
        self._rect: tuple[int, int, int, int] | None = None

    def _style(self) -> None:
        hwnd = screen.toplevel_hwnd(self.win.winfo_id())
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                               style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.hidden_from_capture = bool(_user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
        self._styled = True

    def show(self, rect: tuple[int, int, int, int]) -> None:
        if rect != self._rect:
            l, t, r, b = rect
            self.win.geometry(f"{r - l}x{b - t}+{l}+{t}")
            self._rect = rect
        if not self.visible:
            self.win.deiconify()
            self.win.update_idletasks()
            if not self._styled:
                self._style()
            self.win.attributes("-topmost", True)
            self.visible = True

    def hide(self) -> None:
        if self.visible:
            self.canvas.delete("all")
            self.win.withdraw()
            self.visible = False

    def draw(self, marks: list[tuple]) -> None:
        """Each mark: ("arrow", x, y, direction, colour, label) with direction right/up/down/left,
        or ("bar", x0, y0, x1, y1, colour). Labels are dropped if the window can be captured."""
        c = self.canvas
        c.delete("all")
        for m in marks:
            if m[0] == "bar":
                _k, x0, y0, x1, y1, colour = m
                c.create_rectangle(x0, y0, x1, y1, fill=colour, outline="#000000")
                continue
            _k, x, y, direction, colour, label = m
            s = 11
            pts = {"right": (x - 2 * s, y - s, x, y, x - 2 * s, y + s),
                   "left": (x + 2 * s, y - s, x, y, x + 2 * s, y + s),
                   "up": (x - s, y + 2 * s, x, y, x + s, y + 2 * s),
                   "down": (x - s, y - 2 * s, x, y, x + s, y - 2 * s)}[direction]
            c.create_polygon(*pts, fill=colour, outline="#000000", width=2)
            if label and self.hidden_from_capture:
                anchor, dx, dy = {"right": ("e", -2 * s - 6, 0), "left": ("w", 2 * s + 6, 0),
                                  "up": ("n", 0, 2 * s + 4), "down": ("s", 0, -2 * s - 4)}[direction]
                c.create_text(x + dx + 1, y + dy + 1, text=label, fill="#000000", font=self.font, anchor=anchor)
                c.create_text(x + dx, y + dy, text=label, fill=colour, font=self.font, anchor=anchor)
