"""A slider that stays readable on the dark theme (Tk's own draws a black bar with no visible handle)."""
from __future__ import annotations

import tkinter as tk
from typing import Callable


class Slider(tk.Canvas):
    def __init__(self, parent: tk.Widget, from_: int, to: int, value: int, *, width: int = 150,
                 colours: dict[str, str], on_change: Callable[[int], None],
                 on_release: Callable[[], None] | None = None) -> None:
        super().__init__(parent, width=width, height=20, bg=colours["bg"], highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=True)
        self.lo, self.hi = from_, to
        self.value = max(from_, min(to, value))
        self.colours = colours
        self.on_change, self.on_release = on_change, on_release
        self.pad = 8
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: self.on_release and self.on_release())
        # Keyboard: arrows nudge by one step, so it works without the mouse too.
        self.bind("<Left>", lambda _e: self.set(self.value - 1, notify=True))
        self.bind("<Right>", lambda _e: self.set(self.value + 1, notify=True))
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self._draw()

    def _x_for(self, value: int) -> float:
        w = max(int(self.winfo_width()), int(self["width"]))
        return self.pad + (value - self.lo) / (self.hi - self.lo) * (w - 2 * self.pad)

    def _draw(self) -> None:
        self.delete("all")
        c = self.colours
        w = max(int(self.winfo_width()), int(self["width"]))
        y = 10
        self.create_line(self.pad, y, w - self.pad, y, fill=c["trough"], width=4, capstyle="round")
        x = self._x_for(self.value)
        self.create_line(self.pad, y, x, y, fill=c["fill"], width=4, capstyle="round")
        ring = c["focus"] if self.focus_get() is self else c["bg"]
        self.create_oval(x - 7, y - 7, x + 7, y + 7, fill=c["knob"], outline=ring, width=2)

    def _drag(self, event: tk.Event) -> None:
        self.focus_set()
        w = max(int(self.winfo_width()), int(self["width"]))
        frac = (event.x - self.pad) / max(w - 2 * self.pad, 1)
        self.set(round(self.lo + frac * (self.hi - self.lo)), notify=True)

    def set(self, value: int, notify: bool = False) -> None:
        value = max(self.lo, min(self.hi, int(value)))
        changed = value != self.value
        self.value = value
        self._draw()
        if notify and changed:
            self.on_change(value)
