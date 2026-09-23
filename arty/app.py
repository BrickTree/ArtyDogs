"""The companion window: reads map coordinates on hotkeys and shows the firing
solution in big type, meant to live on a second monitor."""
from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from collections import deque
from typing import NamedTuple
from datetime import datetime

from PIL import Image, ImageTk

from . import config as cfgmod
from . import screen
from .ballistics import (ArcSolution, Point, Solution, azimuth_deg, azimuth_mil, bracket, compass_point,
                         corrected_aim, distance_m, load_data, miss_components, range_at_mil, shift_aim, solve,
                         spread_m)
from .coords import best_coord, find_coords, parse_number
from .gametable import GameTable
from .elevcheck import SOURCE_LABEL, ElevationLog, best_map, detect_map, fit, range_vs_ground
from .hotkeys import HotkeyThread
from .ocr import Ocr, ReadoutReader, ReadResult
from .shotlog import Shot, ShotLog
from .sight import SightReading, read_sight
from .terrain import TerrainMap, available_maps
from .speech import Speaker, digits
from .trim import Obs, Trim, height_mil_for, learn
from .widgets import Slider

BG, CARD, EDGE, INPUT = "#0f1216", "#171c22", "#262e38", "#0b0e12"
FG, MUTED, DIM = "#e8eaed", "#8b95a1", "#4c5561"
AMBER, GREEN, RED, BLUE = "#ffb547", "#38d27a", "#ff5d5d", "#5aa9ff"
DIM_AMBER = "#8c6a33"  # the arc you're not firing: readable, but clearly not the one

ROLES = ("gun", "target", "impact")
ROLE_LABEL = {"gun": "GUN", "target": "TARGET", "impact": "IMPACT"}
ROLE_COLOR = {"gun": BLUE, "target": AMBER, "impact": RED}
MAX_FAILED_SAVES = 10
# The hotkeys the app registers. Screen reads happen only when one of these is pressed.
HOTKEY_NAMES = ("gun", "target", "impact", "snapshot", "dial")
DZ_SANE = 150.0  # a height difference bigger than this is a misread (or a stale height), not a hill
SIGHT_ROW_SANE = 150.0  # a sight (mil, range) row further than this from the table is a misread
SIGHT_FRESH_S = 180.0  # a sight reading older than this at F9 isn't the shot you just fired
LEVEL_OK = 0.25  # tilt pip within this many dashes of the middle mark counts as level


class Plan(NamedTuple):
    """What to dial: the solution for the fire point, which is the aim point plus the gun's trim."""
    sol: Solution
    arc: ArcSolution | None
    trim: Trim | None
    offsets: tuple[float, float]  # trim applied, metres (further, right)
    fire: Point


class ArtyApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = cfgmod.load()
        self.data = load_data()
        self.weapon_id = self.cfg["weapon"] if self.cfg["weapon"] in self.data.weapons else next(iter(self.data.weapons))
        self.points: dict[str, Point | None] = {r: None for r in ROLES}
        self.asl: dict[str, float | None] = {"gun": None, "target": None}  # heights above sea level
        # Fire correction, in metres along the gun->target line: (further, right). The aim
        # point is the target shifted by this; see the `aim` property.
        self.adj: tuple[float, float] = (0.0, 0.0)
        self.adj_steps = 0
        self.arc_choice: dict[str, str] = {}  # weapon id -> the arc you're firing
        self.history: deque[tuple[str, Point]] = deque(maxlen=int(self.cfg["history_size"]))
        self.shotlog = ShotLog(cfgmod.SHOT_LOG)
        self._obs_cache: list[Obs] | None = None  # the shot log reduced for trim learning
        self.elevlog = ElevationLog(cfgmod.ELEV_LOG)  # every height the game printed, and where
        # The game's own firing table, from the rows its sight prints (see arty/gametable.py).
        self.games = {w.id: GameTable.load(w, cfgmod.GAME_TABLE_SEED, cfgmod.SIGHT_LOG)
                      for w in self.data.weapons.values() if w.id == "sph2"}
        self._row_counts: dict[tuple[float, float], int] = {}
        self._elev_key: tuple | None = None
        self._elev_report = ""
        self._started = datetime.now().isoformat(timespec="seconds")  # this session's height reads start here
        self._plan_cache: Plan | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.engine_ready = False
        self.engine_failed = False
        self._own_rect: tuple[int, int, int, int] | None = None
        self._thumbs: dict[str, ImageTk.PhotoImage] = {}
        self._last_capture: dict[str, dict] = {}  # role -> the capture job its last read came from
        self._clip_seen: str | None = None
        self._clip_primed = False
        self._own_clip = ""
        self._ticks = 0
        voice = self.cfg["voice"]
        self.speaker = Speaker(voice["volume"], voice["rate"])
        self.speaker.start()
        self._last_spoken: str | None = None
        self._callout_job: str | None = None
        self._callout_prefix = ""
        self._dial_busy = False  # a sight check (F11) is being read
        self._sight_rows: set[tuple[float, float]] = set()
        self._last_sight: tuple[float, SightReading] | None = None  # (monotonic time, reading)
        # Terrain heights for the current gun/target, from the map's heightfield.
        self._terrain_maps: dict[str, TerrainMap] = {}
        self._terrain_key: tuple | None = None
        self._terrain_info: dict | None = None

        self._make_fonts()
        self._build()
        self._render_terrain()
        self._place_window()
        self._fit_height()
        self.root.update_idletasks()
        self._hwnd = screen.toplevel_hwnd(self.root.winfo_id())
        self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.report_callback_exception = self._on_tk_error

        threading.Thread(target=self._worker, daemon=True, name="ocr").start()
        self.hotkeys = HotkeyThread({k: v for k, v in self.cfg["hotkeys"].items() if k in HOTKEY_NAMES},
                                    self._on_hotkey)
        self.hotkeys.start()
        self.root.after(400, self._report_hotkeys)
        self._select_weapon(self.weapon_id)
        self.root.after(40, self._poll)

    # ------------------------------------------------------------------ layout
    def _make_fonts(self) -> None:
        fams = set(tkfont.families(self.root))
        num = "Bahnschrift" if "Bahnschrift" in fams else "Segoe UI"
        ui = "Segoe UI" if "Segoe UI" in fams else "TkDefaultFont"
        self._num_family, self._ui_family = num, ui
        self.f_huge = tkfont.Font(family=num, size=44, weight="bold")
        self.f_big = tkfont.Font(family=num, size=24, weight="bold")
        self.f_weapon = tkfont.Font(family=num, size=12, weight="bold")
        self.f_label = tkfont.Font(family=ui, size=9, weight="bold")
        self.f_ui = tkfont.Font(family=ui, size=10)
        self.f_small = tkfont.Font(family=ui, size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=11)
        self.f_mono_small = tkfont.Font(family="Consolas", size=9)

    def _card(self, parent: tk.Widget, **pack) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD, highlightbackground=EDGE, highlightthickness=1, padx=12, pady=10)
        f.pack(fill="x", pady=(0, 10), **pack)
        return f

    def _label(self, parent: tk.Widget, text: str = "", font=None, fg: str = MUTED, **kw) -> tk.Label:
        return tk.Label(parent, text=text, font=font or self.f_label, fg=fg, bg=parent["bg"], **kw)

    def _button(self, parent: tk.Widget, text: str, command, fg: str = FG) -> tk.Label:
        b = tk.Label(parent, text=text, font=self.f_small, fg=fg, bg=EDGE, padx=9, pady=4, cursor="hand2")
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.configure(bg="#33404e"))
        b.bind("<Leave>", lambda _e: b.configure(bg=EDGE))
        return b

    def _build(self) -> None:
        r = self.root
        r.title("WARDOGS Arty")
        r.configure(bg=BG)
        r.minsize(440, 600)
        outer = tk.Frame(r, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        self._label(head, "WARDOGS ARTY").pack(side="left")
        self.engine_lbl = self._label(head, "● OCR starting…", self.f_small, AMBER)
        self.engine_lbl.pack(side="left", padx=10)
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg["always_on_top"]))
        tk.Checkbutton(head, text="Pin on top", variable=self.topmost_var, command=self._toggle_topmost,
                       font=self.f_small, fg=MUTED, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG, bd=0, highlightthickness=0).pack(side="right")
        self.dial_btn = self._button(head, f"Check sight ({self.cfg['hotkeys']['dial']})", self._check_sight)
        self.dial_btn.pack(side="right", padx=(0, 10))

        wrow = tk.Frame(outer, bg=BG)
        wrow.pack(fill="x", pady=(10, 10))
        self.weapon_btns: dict[str, tk.Label] = {}
        for i, (wid, w) in enumerate(self.data.weapons.items()):
            b = tk.Label(wrow, text=f"{w.label.upper()}\n{w.min_m:.0f}-{w.max_m:.0f} m", font=self.f_weapon,
                         justify="center", pady=6, cursor="hand2")
            b.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 6, 0))
            b.bind("<Button-1>", lambda _e, wid=wid: self._select_weapon(wid))
            self.weapon_btns[wid] = b

        # --- firing solution
        sol = self._card(outer)
        top = tk.Frame(sol, bg=CARD)
        top.pack(fill="x")
        azc = tk.Frame(top, bg=CARD)
        azc.pack(side="left", fill="x", expand=True)
        self._label(azc, "AZIMUTH").pack(anchor="w")
        self.az_val = self._label(azc, "—", self.f_huge, FG)
        self.az_val.pack(anchor="w")
        self.az_sub = self._label(azc, " ", self.f_small)
        self.az_sub.pack(anchor="w")
        dc = tk.Frame(top, bg=CARD)
        dc.pack(side="right", anchor="n")
        self._label(dc, "DISTANCE").pack(anchor="e")
        self.dist_val = self._label(dc, "—", self.f_big, FG)
        self.dist_val.pack(anchor="e", pady=(8, 0))
        self.dist_sub = self._label(dc, " ", self.f_small)
        self.dist_sub.pack(anchor="e")

        tk.Frame(sol, bg=EDGE, height=1).pack(fill="x", pady=(10, 8))
        self._label(sol, "ELEVATION  (MIL)").pack(anchor="w")
        arcs = tk.Frame(sol, bg=CARD)
        arcs.pack(fill="x")
        self.arc_cols = []
        for i in range(2):
            col = tk.Frame(arcs, bg=CARD, cursor="hand2")
            col.pack(side="left", fill="x", expand=True)
            name = self._label(col, "", self.f_label, MUTED, cursor="hand2")
            name.pack(anchor="w")
            val = self._label(col, "—", self.f_huge, AMBER, cursor="hand2")
            val.pack(anchor="w")
            note = self._label(col, "", self.f_small, DIM, cursor="hand2")
            note.pack(anchor="w")
            for w in (col, name, val, note):  # click an arc to say that's the one you're firing
                w.bind("<Button-1>", lambda _e, i=i: self._choose_arc(i))
            self.arc_cols.append((col, name, val, note))

        prow = tk.Frame(sol, bg=CARD)
        prow.pack(fill="x", pady=(8, 0))
        self.pill = tk.Label(prow, text="", font=self.f_label, padx=10, pady=3)
        self.pill.pack(side="left")
        self.adjust_lbl = self._label(prow, "", self.f_small, AMBER)
        self.adjust_lbl.pack(side="left", padx=10)
        trow = tk.Frame(sol, bg=CARD)
        trow.pack(fill="x", pady=(6, 0))
        self.trim_on = bool(self.cfg["auto_trim"])
        self.trim_btn = self._button(trow, "", self._toggle_trim)
        self.trim_btn.pack(side="right", anchor="n")
        self.trim_lbl = self._label(trow, "", self.f_small, MUTED, justify="left", anchor="w")
        self.trim_lbl.pack(side="left", fill="x", expand=True)
        trow.bind("<Configure>", lambda e: self.trim_lbl.configure(
            wraplength=max(e.width - self.trim_btn.winfo_reqwidth() - 16, 200)))
        self.tip_lbl = self._label(sol, "", self.f_small, MUTED, justify="left", anchor="w")
        self.tip_lbl.pack(fill="x", pady=(6, 0))
        self.tip_lbl.bind("<Configure>", lambda e: self.tip_lbl.configure(wraplength=max(e.width - 4, 200)))
        self.dial_lbl = self._label(sol, "", self.f_weapon, FG, justify="left", anchor="w")
        self.dial_lbl.pack(fill="x", pady=(4, 0))

        # --- voice: the solution spoken, so your eyes can stay on the sight
        vc = self._card(outer)
        vrow = tk.Frame(vc, bg=CARD)
        vrow.pack(fill="x")
        self._label(vrow, "VOICE").pack(side="left", padx=(0, 10))
        voice = self.cfg["voice"]
        self.voice_var = tk.BooleanVar(value=bool(voice["enabled"]))
        tk.Checkbutton(vrow, text="Speak each firing solution", variable=self.voice_var, command=self._toggle_voice,
                       font=self.f_small, fg=MUTED, bg=CARD, selectcolor=INPUT, activebackground=CARD,
                       activeforeground=FG, bd=0, highlightthickness=0).pack(side="left")
        self._button(vrow, "Test", self._test_voice).pack(side="right")
        srow = tk.Frame(vc, bg=CARD)
        srow.pack(fill="x", pady=(6, 0))
        colours = {"bg": CARD, "trough": EDGE, "fill": AMBER, "knob": FG, "focus": AMBER}
        # Let go of a slider and you hear the new setting.
        self._label(srow, "Volume", self.f_small, MUTED).pack(side="left")
        self.vol_scale = Slider(srow, 0, 100, voice["volume"], width=150, colours=colours,
                                on_change=self._set_volume, on_release=lambda: self._test_voice(short=True))
        self.vol_scale.pack(side="left", padx=(6, 4))
        self.vol_lbl = self._label(srow, f"{voice['volume']}%", self.f_small, FG, width=4, anchor="w")
        self.vol_lbl.pack(side="left", padx=(0, 16))
        self._label(srow, "Speed", self.f_small, MUTED).pack(side="left")
        self.rate_scale = Slider(srow, -3, 6, voice["rate"], width=100, colours=colours,
                                 on_change=self._set_rate, on_release=lambda: self._test_voice(short=True))
        self.rate_scale.pack(side="left", padx=(6, 4))
        self.rate_lbl = self._label(srow, f"{voice['rate']:+d}", self.f_small, FG, width=3, anchor="w")
        self.rate_lbl.pack(side="left")

        # --- points
        pts = self._card(outer)
        self.vars: dict[tuple[str, str], tk.StringVar] = {}
        self.entries: dict[tuple[str, str], tk.Entry] = {}
        self.thumb_lbls: dict[str, tk.Label] = {}
        self.src_lbls: dict[str, tk.Label] = {}
        self.flag_lbls: dict[str, tk.Label] = {}
        for row, role in enumerate(ROLES):
            self._label(pts, ROLE_LABEL[role], self.f_label, ROLE_COLOR[role]).grid(
                row=2 * row, column=0, sticky="w", padx=(0, 10))
            for col, axis in ((1, "x"), (3, "y")):
                self._label(pts, axis.upper(), self.f_small, DIM).grid(row=2 * row, column=col, sticky="e")
                var = tk.StringVar()
                e = tk.Entry(pts, textvariable=var, width=7, font=self.f_mono, bg=INPUT, fg=FG,
                             insertbackground=FG, relief="flat", highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=ROLE_COLOR[role])
                e.grid(row=2 * row, column=col + 1, sticky="w", padx=(3, 8), ipady=2)
                e.bind("<Return>", lambda _e, role=role: self._commit_entries(role, explicit=True))
                if role != "impact":  # an impact correction only happens on purpose
                    e.bind("<FocusOut>", lambda _e, role=role: self._commit_entries(role))
                self.vars[(role, axis)] = var
                self.entries[(role, axis)] = e
            thumb = tk.Label(pts, bg=CARD)
            thumb.grid(row=2 * row, column=5, sticky="w")
            self.thumb_lbls[role] = thumb
            # Shown once a read exists: saves that screenshot so misreads can be studied.
            flag = tk.Label(pts, text="✗ wrong", font=self.f_small, fg=DIM, bg=CARD, cursor="hand2")
            flag.bind("<Button-1>", lambda _e, role=role: self._flag_read(role))
            flag.bind("<Enter>", lambda _e, f=flag: f.configure(fg=RED))
            flag.bind("<Leave>", lambda _e, f=flag: f.configure(fg=DIM))
            self.flag_lbls[role] = flag
            src = self._label(pts, " ", self.f_small, DIM)
            src.grid(row=2 * row + 1, column=0, columnspan=6, sticky="w", pady=(0, 6))
            self.src_lbls[role] = src
        pts.grid_columnconfigure(5, weight=1)

        hbox = tk.Frame(pts, bg=CARD)
        hbox.grid(row=6, column=0, columnspan=7, sticky="we", pady=(2, 2))
        hrow = tk.Frame(hbox, bg=CARD)
        hrow.pack(fill="x")
        self._label(hrow, "HEIGHT", self.f_label, MUTED).pack(side="left", padx=(0, 10))
        self.asl_vars: dict[str, tk.StringVar] = {}
        self.asl_entries: dict[str, tk.Entry] = {}
        for role in ("gun", "target"):
            self._label(hrow, f"{role} ASL", self.f_small, DIM).pack(side="left")
            var = tk.StringVar()
            e = tk.Entry(hrow, textvariable=var, width=5, font=self.f_mono, bg=INPUT, fg=FG,
                         insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground=EDGE, highlightcolor=ROLE_COLOR[role])
            e.pack(side="left", padx=(3, 10), ipady=2)
            e.bind("<Return>", lambda _e: self._commit_asl())
            e.bind("<FocusOut>", lambda _e: self._commit_asl())
            self.asl_vars[role] = var
            self.asl_entries[role] = e
        self.height_lbl = self._label(hrow, "", self.f_small, MUTED)
        self.height_lbl.pack(side="left")
        terr = tk.Frame(hbox, bg=CARD)
        terr.pack(fill="x", pady=(4, 0))
        self.terrain_btn = self._button(terr, "", self._cycle_terrain)
        self.terrain_btn.pack(side="left", anchor="n")
        self.terrain_lbl = self._label(terr, "", self.f_small, MUTED, justify="left", anchor="w")
        self.terrain_lbl.pack(side="left", fill="x", expand=True, padx=(8, 0))
        terr.bind("<Configure>", lambda e: self.terrain_lbl.configure(
            wraplength=max(e.width - self.terrain_btn.winfo_reqwidth() - 20, 200)))

        self.aim_lbl = self._label(pts, "", self.f_small, AMBER)
        self.aim_lbl.grid(row=7, column=0, columnspan=7, sticky="w")
        btns = tk.Frame(pts, bg=CARD)
        btns.grid(row=8, column=0, columnspan=7, sticky="w", pady=(6, 0))
        for text, cmd in (("Swap", self._swap), ("Paste → gun", lambda: self._paste("gun")),
                          ("Paste → target", lambda: self._paste("target")),
                          ("Copy target", self._copy_target), ("Clear", self._clear)):
            self._button(btns, text, cmd).pack(side="left", padx=(0, 6))
        self.clip_var = tk.BooleanVar(value=bool(self.cfg["watch_clipboard"]))
        tk.Checkbutton(pts, text="Copied coordinates become the target (game, chat, Discord…)",
                       variable=self.clip_var, command=self._toggle_clipboard, font=self.f_small, fg=MUTED,
                       bg=CARD, selectcolor=INPUT, activebackground=CARD, activeforeground=FG, bd=0,
                       highlightthickness=0).grid(row=9, column=0, columnspan=7, sticky="w", pady=(6, 0))

        # --- fire adjustment: spotter calls, applied along the actual line of fire
        adj = self._card(outer)
        ahead = tk.Frame(adj, bg=CARD)
        ahead.pack(fill="x")
        self._label(ahead, "ADJUST FIRE").pack(side="left")
        self._label(ahead, "  metres, along your line of fire", self.f_small, DIM).pack(side="left")
        grid = tk.Frame(adj, bg=CARD)
        grid.pack(fill="x", pady=(6, 4))
        for r, (minus, plus, axis) in enumerate((("DROP", "ADD", 0), ("LEFT", "RIGHT", 1))):
            for c, (word, sign, step) in enumerate([(minus, -1, s) for s in (50, 25, 10)] +
                                                   [(plus, +1, s) for s in (10, 25, 50)]):
                b = self._button(grid, f"{word} {step}", lambda sign=sign, step=step, axis=axis:
                                 self._nudge(sign * step if axis == 0 else 0.0, sign * step if axis == 1 else 0.0))
                b.grid(row=r, column=c, padx=(0, 4) if c != 2 else (0, 14), pady=2, sticky="ew")
        arow = tk.Frame(adj, bg=CARD)
        arow.pack(fill="x")
        self.adj_lbl = self._label(arow, "no correction", self.f_small, DIM)
        self.adj_lbl.pack(side="left")
        self.keep_var = tk.BooleanVar(value=bool(self.cfg["keep_correction"]))
        tk.Checkbutton(arow, text="Keep for next target", variable=self.keep_var, command=self._toggle_keep,
                       font=self.f_small, fg=MUTED, bg=CARD, selectcolor=INPUT, activebackground=CARD,
                       activeforeground=FG, bd=0, highlightthickness=0).pack(side="right")
        self._button(arow, "Reset", self._reset_adjust_clicked).pack(side="right", padx=(0, 8))

        # --- footer first, so the history card below can take whatever height is left
        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", side="bottom")
        self.status_lbl = self._label(foot, "", self.f_ui, MUTED, justify="left", anchor="w", wraplength=480)
        self.status_lbl.pack(fill="x")
        self.legend_lbl = self._label(foot, "", self.f_small, DIM, justify="left", anchor="w", wraplength=480)
        self.legend_lbl.pack(fill="x", pady=(4, 0))

        def rewrap(e: tk.Event) -> None:
            for lbl in (self.status_lbl, self.legend_lbl):
                lbl.configure(wraplength=max(e.width - 10, 200))
        foot.bind("<Configure>", rewrap)

        # --- recent targets and the accuracy test share the remaining height as two tabs
        lower = tk.Frame(outer, bg=CARD, highlightbackground=EDGE, highlightthickness=1, padx=12, pady=10)
        lower.pack(fill="both", expand=True, pady=(0, 10))
        tabs = tk.Frame(lower, bg=CARD)
        tabs.pack(fill="x")
        self.tab_btns: dict[str, tk.Label] = {}
        for key, text in (("history", "RECENT TARGETS"), ("accuracy", "ACCURACY TEST")):
            b = tk.Label(tabs, text=text, font=self.f_label, bg=CARD, cursor="hand2")
            b.pack(side="left", padx=(0, 18))
            b.bind("<Button-1>", lambda _e, k=key: self._show_tab(k))
            self.tab_btns[key] = b

        hist = tk.Frame(lower, bg=CARD)
        self._label(hist, "click one to fire on it again", self.f_small, DIM).pack(anchor="w")
        self.hist_list = tk.Listbox(hist, height=4, font=self.f_mono, bg=CARD, fg=FG, bd=0,
                                    highlightthickness=0, selectbackground=EDGE, selectforeground=AMBER,
                                    activestyle="none")
        self.hist_list.pack(fill="both", expand=True, pady=(4, 0))
        self.hist_list.bind("<<ListboxSelect>>", self._history_pick)

        acc = tk.Frame(lower, bg=CARD)
        abtns = tk.Frame(acc, bg=CARD)
        abtns.pack(fill="x", side="bottom", pady=(6, 0))
        self._button(abtns, "New test session", self._new_session).pack(side="left")
        self._button(abtns, "Open log folder", self._open_logs).pack(side="left", padx=6)
        self.acc_text = tk.Label(acc, text="", font=self.f_mono_small, fg=FG, bg=CARD, justify="left", anchor="nw")
        self.acc_text.pack(fill="both", expand=True, anchor="nw", pady=(4, 0))
        self.tab_frames = {"history": hist, "accuracy": acc}
        self._show_tab("history")

    def _place_window(self) -> None:
        geo = self.cfg.get("window")
        mons = screen.monitors()
        if geo and (m := re.fullmatch(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", geo)):
            x, y = int(m.group(3).lstrip("+")), int(m.group(4).lstrip("+"))
            if any(mon.contains(x + 60, y + 20) for mon in mons):
                self.root.geometry(geo)
                return
        # First run: the second monitor if there is one, otherwise the right edge of the main one.
        others = [m for m in mons if not m.primary]
        mon = others[0] if others else next((m for m in mons if m.primary), mons[0])
        wl, wt, wr, wb = mon.work
        w, h = min(540, wr - wl - 40), min(1500, wb - wt - 60)
        x = wl + 30 if others else wr - w - 20
        self.root.geometry(f"{w}x{h}+{x}+{wt + 30}")

    def _fit_height(self) -> None:
        """Grow a remembered window that is now too short for everything in it (e.g. after an update)."""
        self.root.update_idletasks()
        m = re.fullmatch(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", self.root.geometry())
        if not m:
            return
        w, h, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3).lstrip("+")), int(m.group(4).lstrip("+"))
        need = self.root.winfo_reqheight() + 120  # room for a few recent targets / accuracy lines
        if h >= need:
            return
        mon = screen.monitor_at(x + 20, y + 20)
        self.root.geometry(f"{w}x{max(h, min(need, mon.work[3] - y - 40))}+{x}+{y}")

    # ------------------------------------------------------------------ status
    def _status(self, text: str, color: str = MUTED) -> None:
        self.status_lbl.configure(text=text, fg=color)

    def _report_hotkeys(self) -> None:
        if not self.hotkeys.ready.is_set():
            self.root.after(200, self._report_hotkeys)
            return
        hk = self.cfg["hotkeys"]
        self.legend_lbl.configure(text=(f"{hk['gun']} read gun   ·   {hk['target']} read target   ·   "
                                        f"{hk['impact']} read where it landed (logs the shot, corrects aim)   ·   "
                                        f"{hk['dial']} check the SPH-2 sight   ·   "
                                        f"{hk['snapshot']} debug snapshot\n"
                                        "Hover the spot on the tactical map (or a coordinate line in chat) "
                                        "and press the key."))
        if self.hotkeys.failed:
            bad = "; ".join(f"{k}: {v}" for k, v in self.hotkeys.failed.items())
            self._status(f"Hotkey problem: {bad}. Change it in config.json and restart.", RED)
        elif not self.engine_ready:
            self._status("Loading the OCR engine…", AMBER)

    # ------------------------------------------------------------------ hotkeys & OCR worker
    def _on_hotkey(self, name: str) -> None:
        """Runs on the hotkey thread: capture right now, OCR later on the worker."""
        if self.engine_failed:
            self.results.put(("error", name, "the OCR engine isn't running (see above); type the coordinates instead"))
            return
        if name == "dial":  # the gun sight fills the main monitor, wherever the mouse is
            self._grab_sight()
            return
        try:
            x, y = screen.cursor_pos()
            mon = screen.monitor_at(x, y)
            shot = screen.grab(mon.rect)
        except Exception as e:  # noqa: BLE001 - surface anything to the status line
            self.results.put(("error", name, f"Screen capture failed: {e!r}"))
            return
        own = self._own_rect
        self.jobs.put({"kind": name, "cursor": (x, y), "shot": shot, "origin": mon.rect[:2],
                       "exclude": [own] if own else []})
        self.results.put(("busy", name))

    def _worker(self) -> None:
        try:
            ocr = Ocr()
            ocr.read(Image.new("RGB", (64, 64)))  # warm up the ONNX sessions
            reader = ReadoutReader(ocr, self.cfg.get("readout_memory"))
        except Exception:  # noqa: BLE001
            self.results.put(("engine_error", traceback.format_exc(limit=2)))
            return
        self.results.put(("engine_ready",))
        while (job := self.jobs.get()) is not None:
            try:
                if job["kind"] == "snapshot":
                    self.results.put(("snapshot", self._save_snapshot(ocr, reader, job)))
                    continue
                if job["kind"] == "sight":
                    self.results.put(("sight", read_sight(ocr, job["shot"])))
                    continue
                res = reader.read(job["shot"], job["origin"], job["cursor"], job["exclude"])
                self.results.put(("read", job["kind"], res, dict(reader.memory), job))
                if res.coord is None and self.cfg.get("save_failed_reads", True):
                    self._save_failed(job["shot"])
                if job["kind"] in ("gun", "target") and res.coord is not None:
                    # Heights land a moment after the coordinates. The label beside the cursor is
                    # the hovered spot's own height; for the gun, your HUD height is the fallback.
                    asl = reader.read_hover_asl(job["shot"], job["origin"], job["cursor"])
                    source = "hovered"
                    if asl is None and job["kind"] == "gun":
                        asl, source = reader.read_asl(job["shot"]), "your HUD"
                    if asl is not None:
                        self.results.put(("asl", job["kind"], asl, source))
            except Exception:  # noqa: BLE001
                self.results.put(("error", job["kind"], traceback.format_exc(limit=3)))

    def _save_snapshot(self, ocr: Ocr, reader: ReadoutReader, job: dict) -> str:
        cfgmod.DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        job["shot"].save(cfgmod.DEBUG_DIR / f"snapshot-{stamp}.png")
        probe = ReadoutReader(ocr, json.loads(json.dumps(reader.memory)))  # don't disturb what was learned
        res = probe.read(job["shot"], job["origin"], job["cursor"], job["exclude"])
        whole = ocr.read(job["shot"])
        info = {
            "cursor": job["cursor"], "monitor_origin": job["origin"], "size": job["shot"].size,
            "read": {"coord": [res.coord.x, res.coord.y] if res.coord else None, "text": res.text,
                     "box": res.box, "stage": res.region, "votes": res.votes, "ms": round(res.ms),
                     "message": res.message},
            "reader_memory": reader.memory,
            "seen_by_reader": res.seen,
            "whole_screen_ocr": [(b.text, round(b.score, 3), b.box) for b in whole],
        }
        path = cfgmod.DEBUG_DIR / f"snapshot-{stamp}.json"
        path.write_text(json.dumps(info, indent=1), encoding="utf-8")
        return str(path.with_suffix(".png"))

    def _save_failed(self, shot: Image.Image) -> None:
        cfgmod.DEBUG_DIR.mkdir(exist_ok=True)
        shot.save(cfgmod.DEBUG_DIR / f"failed-{datetime.now():%Y%m%d-%H%M%S}.png")
        old = sorted(cfgmod.DEBUG_DIR.glob("failed-*.png"))[:-MAX_FAILED_SAVES]
        for p in old:  # keep only the most recent few
            p.unlink(missing_ok=True)

    def _poll(self) -> None:
        if self._hwnd:
            self._own_rect = screen.window_rect(self._hwnd)
        try:
            while True:
                self._handle(self.results.get_nowait())
        except queue.Empty:
            pass
        self._ticks += 1
        if self._ticks % 15 == 0:  # ~0.6 s
            self._check_clipboard()
        self.root.after(40, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "engine_ready":
            self.engine_ready = True
            self.engine_lbl.configure(text="● OCR ready", fg=GREEN)
            if not self.hotkeys.failed:
                hk = self.cfg["hotkeys"]
                self._status(f"Ready. Open the tactical map, hover your gun and press {hk['gun']}, "
                             f"then hover the target and press {hk['target']}.", GREEN)
        elif kind == "engine_error":
            self.engine_failed = True
            self.engine_lbl.configure(text="● OCR failed", fg=RED)
            self._status("OCR engine failed to start; you can still type coordinates.\n" + msg[1], RED)
        elif kind == "busy":
            self._status(f"Reading {ROLE_LABEL.get(msg[1], msg[1])}…", AMBER)
        elif kind == "read":
            self._last_capture[msg[1]] = {**msg[4], "result": msg[2]}
            self._apply_read(msg[1], msg[2])
            self.cfg["readout_memory"] = msg[3]
        elif kind == "sight":
            self._on_sight(msg[1])
        elif kind == "asl":
            role, value, source = msg[1], msg[2], msg[3]
            self.asl[role] = float(value)
            self._log_elevation("hover" if source == "hovered" else "hud", role, float(value))
            self.asl_vars[role].set(f"{value:.0f}")
            self._refresh()
            if self.asl["gun"] is not None and self.asl["target"] is not None:
                self._status(f"{ROLE_LABEL[role]} height {value:.0f} m read from {source}; "
                             f"ΔZ {self._dz:+.0f} m is now in the solution.", FG)
        elif kind == "elev":
            if msg[1] == self._elev_key:
                self._elev_report = msg[2]
                auto = not self.cfg["terrain_map"] or self.cfg["terrain_auto"]
                if msg[3] and auto and msg[3] != self.cfg["terrain_map"]:
                    self.cfg["terrain_map"], self.cfg["terrain_auto"] = msg[3], True
                    self._terrain_key = None
                    self._elev_key = None
                    self._status(f"The heights the game printed match {msg[3].capitalize()}'s terrain: "
                                 "Terrain switched to that map.", GREEN)
                    self._refresh()
                if self.tab_frames["accuracy"].winfo_ismapped():
                    self._render_accuracy()
        elif kind == "terrain":
            if msg[1] == self._terrain_key:
                self._terrain_info = msg[2]
                self._render_terrain()
        elif kind == "snapshot":
            self._status(f"Saved debug snapshot: {msg[1]} (+ .json with everything OCR saw)", BLUE)
        elif kind == "error":
            self._status(f"{msg[1]}: {msg[2]}", RED)
            if msg[1] == "sight":
                self._dial_busy = False

    def _apply_read(self, role: str, res: ReadResult) -> None:
        if res.coord is None:
            self._status(f"{ROLE_LABEL[role]}: {res.message}", RED)
            self._cue("No reading.")
            return
        p = Point(res.coord.x, res.coord.y)
        # An explicit read always gets spoken, even if the numbers didn't change.
        self._last_spoken = None
        self._callout_prefix = "" if res.confident else "Check reading. "
        if role == "gun" and self.points["target"] is None:
            self._cue("Gun set." if res.confident else "Gun set. Check reading.")
        check = ""
        if not res.direct:
            check = f"  ⚠ {res.message}"
        elif not res.confident:
            alt = ", ".join(Point(x, y).text() for x, y in res.dissent[:2])
            check = (f"  ⚠ unconfirmed{f' (some reads saw {alt})' if alt else ''}: "
                     "check the digits against the thumbnail")
        self._set_thumb(role, res.crop)
        self.src_lbls[role].configure(
            text=f"read {datetime.now():%H:%M:%S}  ·  {res.region}  ·  {res.votes}/{res.reads} re-reads agree"
                 f"  ·  {res.ms:.0f} ms", fg=DIM if res.confident else AMBER)
        self.flag_lbls[role].grid(row=2 * ROLES.index(role), column=6, sticky="e")
        if role == "impact":
            self._apply_impact(p, confident=res.confident)
            if check:
                self._status(self.status_lbl.cget("text") + check, AMBER)
            return
        self.set_point(role, p)
        self._status(f"{ROLE_LABEL[role]} {p.text()}{check}", AMBER if check else FG)

    def _set_thumb(self, role: str, crop: Image.Image | None) -> None:
        if crop is None:
            self.thumb_lbls[role].configure(image="")
            return
        h = 26
        w = max(1, round(crop.width * h / max(crop.height, 1)))
        if w > 210:
            w, h = 210, max(1, round(crop.height * 210 / crop.width))
        self._thumbs[role] = ImageTk.PhotoImage(crop.resize((w, h), Image.LANCZOS), master=self.root)
        self.thumb_lbls[role].configure(image=self._thumbs[role])

    # ------------------------------------------------------------------ state changes
    @property
    def aim(self) -> Point | None:
        """Where to actually shoot: the target, shifted by any fire correction."""
        gun, target = self.points["gun"], self.points["target"]
        if target is None:
            return None
        if gun is None or self.adj == (0.0, 0.0):
            return target
        return shift_aim(gun, target, *self.adj, self.data.meters_per_unit)

    def set_point(self, role: str, p: Point | None) -> None:
        if role == "impact":
            if p is not None:
                self._apply_impact(p)
            return
        if p == self.points[role]:
            return
        self.points[role] = p
        if role in self.asl and self.asl[role] is not None:
            # A height belongs to the spot it was read at. Keeping the old one gave four
            # different targets the same ΔZ in real play; the new spot's own read follows.
            self.asl[role] = None
            self.asl_vars[role].set("")
        if role == "target" and p is not None:
            self._remember(p)
        # A correction belongs to one gun position; "keep" carries it to the next target.
        if role == "gun" or not self.keep_var.get():
            self.adj, self.adj_steps = (0.0, 0.0), 0
        self._sync_entries()
        self._refresh()

    def _apply_impact(self, impact: Point, confident: bool = True) -> None:
        gun, target = self.points["gun"], self.points["target"]
        if gun is None or target is None:
            self._status("Read your GUN and a TARGET before marking where a shell landed.", RED)
            return
        self.points["impact"] = impact
        aim = self.aim
        plan = self._plan()
        fired = plan.fire if plan else aim
        mpu = self.data.meters_per_unit
        long_m, right_m = miss_components(gun, aim, impact, mpu)
        logged = self._log_shot(gun, target, aim, impact, long_m, right_m, confident, plan)
        # Treat the miss as a steady bias: the gun dialed at `fired` landed at `impact`, so dial
        # that far the other way. The aim point is whatever the (relearned) trim turns into that.
        self.adj = self._adj_for_fire(corrected_aim(fired, target, impact))
        self.adj_steps += 1
        self._last_spoken, self._callout_prefix = None, "Corrected. "
        self._sync_entries()
        self._refresh()
        rng = "on range" if abs(long_m) < 0.5 else f"{abs(long_m):.0f} m {'LONG' if long_m > 0 else 'SHORT'}"
        side = "on line" if abs(right_m) < 0.5 else f"{abs(right_m):.0f} m {'RIGHT' if right_m > 0 else 'LEFT'}"
        note = " Logged for the accuracy test." if logged else ""
        self._status(f"Landed {rng}, {side} of the aim point. Aim moved to compensate; fire the new solution."
                     f"{note}", AMBER)

    def _adj_for_fire(self, want: Point) -> tuple[float, float]:
        """The aim correction whose fire point (aim + trim) lands on `want`."""
        gun, target = self.points["gun"], self.points["target"]
        mpu = self.data.meters_per_unit
        aim = want
        for _ in range(4):  # the trim barely changes over a few metres, so this settles at once
            self.adj = miss_components(gun, target, aim, mpu)
            plan = self._plan()
            if plan is None:
                break
            aim = Point(aim.x + want.x - plan.fire.x, aim.y + want.y - plan.fire.y)
        return miss_components(gun, target, aim, mpu)

    def _nudge(self, add_m: float, right_m: float) -> None:
        """A spotter's call: ADD/DROP moves the aim further/shorter, RIGHT/LEFT across the line."""
        if self.points["gun"] is None or self.points["target"] is None:
            self._status("Read your GUN and a TARGET first; corrections are along the line between them.", RED)
            return
        self.adj = (self.adj[0] + add_m, self.adj[1] + right_m)
        self.adj_steps += 1
        self._callout_prefix = "Corrected. "
        self._refresh()
        word = (f"{'ADD' if add_m > 0 else 'DROP'} {abs(add_m):.0f}" if add_m
                else f"{'RIGHT' if right_m > 0 else 'LEFT'} {abs(right_m):.0f}")
        self._status(f"{word}: aim moved. Fire the new solution.", AMBER)

    def _log_shot(self, gun: Point, target: Point, aim: Point, impact: Point, long_m: float, right_m: float,
                  confident: bool, plan: Plan | None) -> bool:
        """Record a real shot for the accuracy test: what we said to dial, and where it landed."""
        weapon = self.data.weapons[self.weapon_id]
        if plan is None or plan.arc is None:
            return False  # out of range: nothing was fired from our numbers
        sol, arc = plan.sol, plan.arc
        mpu = self.data.meters_per_unit
        self.shotlog.add(Shot(
            time=datetime.now().isoformat(timespec="seconds"), weapon=weapon.id, arc=arc.arc_id,
            distance_m=round(sol.distance_m, 1), azimuth_deg=round(sol.azimuth_deg, 2),
            mil=round((arc.min_mil + arc.max_mil) / 2, 1), dz_m=round(self._dz, 1),
            gun_x=gun.x, gun_y=gun.y, target_x=target.x, target_y=target.y,
            aim_x=round(aim.x, 4), aim_y=round(aim.y, 4), impact_x=impact.x, impact_y=impact.y,
            long_m=round(long_m, 1), right_m=round(right_m, 1),
            miss_m=round(distance_m(aim, impact, mpu), 1), to_target_m=round(distance_m(target, impact, mpu), 1),
            read_confident=confident, fire_x=round(plan.fire.x, 4), fire_y=round(plan.fire.y, 4),
            height_mil=round(arc.height_mil, 1), trim_add_m=round(plan.offsets[0], 1),
            trim_right_m=round(plan.offsets[1], 1), **self._dialed_fields(), **self._terrain_fields()))
        self._obs_cache = None  # the trim relearns with this shot
        self._render_accuracy()
        return True

    def _dialed_fields(self) -> dict:
        fired = self._fired_sight()
        if fired is None:
            return {}
        r, age = fired
        return {"sight_mil": round(r.mil, 1), "sight_heading": round(r.heading, 2), "sight_age_s": round(age, 1),
                "sight_stabilized": None if r.stabilized is None else float(r.stabilized),
                "tilt_left": r.tilt[0] if r.tilt else None, "tilt_right": r.tilt[1] if r.tilt else None}

    def _terrain_fields(self) -> dict:
        info = self._terrain_info
        if not info or info.get("dz") is None or self._terrain_key != self._terrain_want():
            return {}
        return {"terrain_dz": round(info["dz"], 1), "terrain_map": info["map"]}

    def _remember(self, p: Point) -> None:
        self.history = deque([h for h in self.history if h[1] != p], maxlen=self.history.maxlen)
        self.history.appendleft((datetime.now().strftime("%H:%M:%S"), p))

    def _commit_entries(self, role: str, explicit: bool = False) -> None:
        xs, ys = self.vars[(role, "x")].get(), self.vars[(role, "y")].get()
        for e in (self.entries[(role, "x")], self.entries[(role, "y")]):
            e.configure(highlightbackground=EDGE)
        if not xs.strip() and not ys.strip():
            if role != "impact" and self.points[role] is not None:
                self.set_point(role, None)
            return
        pair = best_coord(xs) or best_coord(ys)  # a whole "X80.07 Y70.54" pasted into one box
        x, y = (pair.x, pair.y) if pair else (parse_number(xs), parse_number(ys))
        if x is None or y is None:
            for axis, v in (("x", x), ("y", y)):
                if v is None:
                    self.entries[(role, axis)].configure(highlightbackground=RED)
            return
        p = Point(x, y)
        if role == "impact":
            if explicit and p != self.points["impact"]:
                self._set_thumb("impact", None)
                self.src_lbls["impact"].configure(text="typed", fg=DIM)
                self._apply_impact(p)
            return
        if p != self.points[role]:
            self._set_thumb(role, None)
            self.src_lbls[role].configure(text="typed", fg=DIM)
            self.set_point(role, p)

    def _commit_asl(self) -> None:
        for role, var in self.asl_vars.items():
            text = var.get().strip().rstrip("m ").strip()
            entry = self.asl_entries[role]
            if not text:
                self.asl[role] = None
                entry.configure(highlightbackground=EDGE)
                continue
            try:
                self.asl[role] = float(text.replace(",", "."))
                entry.configure(highlightbackground=EDGE)
            except ValueError:
                entry.configure(highlightbackground=RED)
        self._refresh()

    @property
    def _dz_raw(self) -> float:
        gun, target = self.asl["gun"], self.asl["target"]
        return 0.0 if gun is None or target is None else target - gun

    @property
    def _dz(self) -> float:
        """How far the target sits above the gun, in metres; 0 when unknown or implausible."""
        dz = self._dz_raw
        return 0.0 if abs(dz) > DZ_SANE else dz

    # -- trim: the gun position's own steady error, learned from its logged shots -------------
    def _obs(self) -> list[Obs]:
        if self._obs_cache is None:
            out = []
            for s in self.shotlog.shots:
                if not s.read_confident:
                    continue  # an unconfirmed impact read could teach the gun a misread
                hm = s.height_mil if s.height_mil is not None else \
                    height_mil_for(self.data.band(s.weapon, s.arc), s.distance_m, s.dz_m)
                gun, fire, mil = Point(s.gun_x, s.gun_y), Point(*s.fire), s.mil
                if s.dialed:
                    # Where the gun really pointed: the sight's heading and elevation, so a
                    # dialing slip is never learned as the gun's own error.
                    d = math.hypot(fire.x - gun.x, fire.y - gun.y)
                    th = math.radians(s.sight_heading)
                    fire, mil = Point(gun.x + d * math.sin(th), gun.y + d * math.cos(th)), s.sight_mil
                out.append(Obs(s.weapon, s.arc, gun, fire, Point(s.impact_x, s.impact_y), mil, hm))
            self._obs_cache = out
        return self._obs_cache

    def _plan(self) -> Plan | None:
        """Solve for the aim point, then move the fire point by the learned trim (if it's on)."""
        weapon = self.data.weapons[self.weapon_id]
        gun, aim = self.points["gun"], self.aim
        if gun is None or aim is None:
            return None
        mpu = self.data.meters_per_unit
        game = self.games.get(weapon.id)
        flat = solve(weapon, gun, aim, mpu, dz=self._dz, bands=self.data.height_bands, game=game)
        arc = self._chosen_arc(flat)
        if arc is None:
            return Plan(flat, None, None, (0.0, 0.0), aim)
        table_arc = next(a for a in weapon.arcs if a.id == arc.arc_id)
        trim = learn(self._obs(), weapon.id, table_arc, gun, flat.distance_m, mpu, range_fn=self._range)
        if not (self.trim_on and trim.active):
            return Plan(flat, arc, trim, (0.0, 0.0), aim)
        offsets = trim.offsets(flat.distance_m, (arc.min_mil + arc.max_mil) / 2, bool(arc.height_mil))
        fire = shift_aim(gun, aim, *offsets, mpu)
        sol = solve(weapon, gun, fire, mpu, dz=self._dz, bands=self.data.height_bands, game=game)
        return Plan(sol, self._chosen_arc(sol), trim, offsets, fire)

    def _log_elevation(self, source: str, role: str, asl: float) -> None:
        p = self.points.get(role)
        if p is not None and self.elevlog.add(source, role, p, asl, self.cfg["terrain_map"]) is not None:
            self._elev_key = None  # the comparison is out of date
            if not self.cfg["terrain_map"] or self.cfg["terrain_auto"]:
                self._render_accuracy()  # re-check which map this is

    def _elev_job(self, key: tuple) -> None:
        """Compare the game's heights with the terrain, and range misses with ground ΔZ (off the UI thread)."""
        try:
            maps = {}
            for m in available_maps():
                maps[m] = self._terrain_maps.get(m) or self._terrain_maps.setdefault(m, TerrainMap(m))
            chosen = self.cfg["terrain_map"]
            reads = [r for r in self.elevlog.readings if not r.map or not chosen or r.map == chosen]
            # Which map you're on is judged from this session's heights only: yesterday's
            # match may have been on another map. A map you picked yourself is never overruled.
            detected = None
            if not chosen or self.cfg["terrain_auto"]:
                session = [r for r in self.elevlog.readings if r.time >= self._started]
                detected = detect_map(session, maps)
            if chosen:
                fits = fit(reads, maps[chosen].height)
                map_id = chosen
            else:
                found = best_map(reads, maps)
                map_id, fits = found if found else ("", [])
            lines = []
            if fits:
                lines.append(f"TERRAIN vs THE GAME'S HEIGHTS ({map_id.capitalize()}, {sum(f.n for f in fits)} reads)")
                hover = next((f for f in fits if f.source == "hover" and f.n >= 2), None)
                for f in fits:
                    verdict = ("✓ terrain matches" if f.matches else
                               "need 3+ reads" if f.n < 3 else f"✗ typical read {f.mad:.0f} m off")
                    if f.rejected:
                        verdict += f" ({f.rejected} wild read{'s' if f.rejected > 1 else ''} set aside)"
                    if hover and f is not hover and f.n >= 2:
                        verdict += f", {f.offset - hover.offset:+.0f} m vs the hover labels"
                    lines.append(f"  {SOURCE_LABEL.get(f.source, f.source):16} {f.n:3} reads  game = terrain "
                                 f"{f.offset:+.1f} m ±{f.sd:.1f} (worst {f.worst:.1f})  {verdict}")
            elif not chosen:
                lines.append("TERRAIN vs THE GAME'S HEIGHTS: no heights read yet. Each ASL the app reads\n"
                             "(hover label, gun sight, HUD) is logged with its spot and compared here.")
            pairs = []
            if map_id:
                tm = maps[map_id]
                for sh in self.shotlog.shots:
                    if not sh.read_confident or (sh.height_mil or 0.0):
                        continue
                    if sh.terrain_map and sh.terrain_map != map_id:
                        continue
                    arc = next((a for a in self.data.weapons[sh.weapon].arcs if a.id == sh.arc), None) \
                        if sh.weapon in self.data.weapons else None
                    gun, impact = Point(sh.gun_x, sh.gun_y), Point(sh.impact_x, sh.impact_y)
                    hg, hi = tm.height(gun), tm.height(impact)
                    promised = self._range(arc, sh.sight_mil if sh.dialed else sh.mil) if arc else None
                    if hg is None or hi is None or promised is None:
                        continue
                    pairs.append((hi - hg, distance_m(gun, impact, self.data.meters_per_unit) - promised))
            rv = range_vs_ground(pairs)
            if rv:
                slope, se, icpt, n = rv
                shorter = -10 * slope  # metres shorter per 10 m the target sits higher
                verdict = ("not yet distinguishable from no effect" if abs(shorter) < 2 * 10 * se else
                           "as physics expects" if shorter > 0 else "OPPOSITE to physics: check heights and dialing")
                lines += ["", f"RANGE vs GROUND ΔZ ({map_id.capitalize()}, {n} flat shots)",
                          f"  each 10 m the target sits higher, shells land {abs(shorter):.0f} ±{10 * se:.0f} m "
                          f"{'shorter' if shorter >= 0 else 'LONGER'} (physics: ~10 shorter)",
                          f"  → {verdict}",
                          f"  on level ground they'd land {icpt:+.0f} m vs the table"]
            elif map_id and pairs:
                lines += ["", f"RANGE vs GROUND ΔZ: {len(pairs)} flat shot(s) so far, needs 4."]
            self.results.put(("elev", key, "\n".join(lines), detected))
        except Exception as e:  # noqa: BLE001 - offline or bad data: just no comparison
            self.results.put(("elev", key, f"TERRAIN vs GAME: unavailable ({type(e).__name__})", None))

    def _cycle_terrain(self) -> None:
        maps = [""] + available_maps()
        cur = self.cfg["terrain_map"] if self.cfg["terrain_map"] in maps else ""
        self.cfg["terrain_map"] = maps[(maps.index(cur) + 1) % len(maps)]
        self.cfg["terrain_auto"] = False  # your pick stands
        self._terrain_key, self._terrain_info = None, None
        self._refresh()

    def _terrain_want(self) -> tuple | None:
        m, gun, target = self.cfg["terrain_map"], self.points["gun"], self.points["target"]
        return (m, gun, target) if m and gun is not None else None

    def _request_terrain(self) -> None:
        """Look up ground heights off the UI thread: the first use of a spot downloads its chunk."""
        want = self._terrain_want()
        if want == self._terrain_key:
            return
        self._terrain_key, self._terrain_info = want, None
        if want is None:
            self._render_terrain()
            return
        self._render_terrain(loading=True)
        threading.Thread(target=self._terrain_job, args=(want,), daemon=True, name="terrain").start()

    def _terrain_job(self, key: tuple) -> None:
        map_id, gun, target = key
        info: dict = {"map": map_id}
        try:
            tm = self._terrain_maps.get(map_id) or self._terrain_maps.setdefault(map_id, TerrainMap(map_id))
            hg = tm.height(gun)
            if hg is None:
                info["error"] = f"your gun is outside {tm.label}'s terrain"
            else:
                ht = tm.height(target) if target is not None else None
                info["dz"] = None if ht is None else ht - hg
                az = azimuth_deg(gun, target) if target is not None else 0.0
                info["along"], info["across"] = _ground_slope(tm, gun, az)
                info["aimed"] = target is not None
        except Exception as e:  # noqa: BLE001 - no network, bad chunk: just no terrain
            info["error"] = f"terrain unavailable ({type(e).__name__})"
        self.results.put(("terrain", key, info))

    def _render_terrain(self, loading: bool = False) -> None:
        m = self.cfg["terrain_map"]
        self.terrain_btn.configure(text=f"Terrain: {m.capitalize() if m else 'off'}", fg=GREEN if m else MUTED)
        info = self._terrain_info
        if not m:
            self.terrain_lbl.configure(fg=DIM, text="pick the map you're on to see real ground heights")
            return
        if loading or (info is None and self._terrain_key is not None):
            self.terrain_lbl.configure(fg=DIM, text="looking up the ground…")
            return
        if info is None:
            self.terrain_lbl.configure(fg=DIM, text="read your gun to see the ground under it")
            return
        if "error" in info:
            self.terrain_lbl.configure(fg=AMBER, text=info["error"])
            return
        parts = []
        if info.get("dz") is not None:
            dz = info["dz"]
            parts.append(f"ground ΔZ {dz:+.0f} m (target {'above' if dz > 0 else 'below'} the gun)")
        across = info["across"]
        if info.get("aimed"):
            tilt = f"gun spot slopes {abs(across):.1f}° across the line of fire"
            if abs(across) >= 2:
                tilt += f" (right side {'high' if across > 0 else 'low'}: expect shots to pull " \
                        f"{'LEFT' if across > 0 else 'RIGHT'})"
            along = info["along"]
            if abs(along) >= 2:
                # Measured 2026-09-22: a spot sloping 4° down toward the targets landed every
                # flat shot 42-116 m short. Auto-trim learns it; this says it before shot one.
                parts.append(tilt + f"; tilts {abs(along):.1f}° {'DOWN' if along < 0 else 'UP'} toward the target: "
                             f"expect shots {'SHORT' if along < 0 else 'LONG'}")
            else:
                parts.append(tilt + f", {along:+.1f}° toward the target")
        colour = AMBER if abs(across) >= 2 or abs(info.get("along", 0.0)) >= 2 else MUTED
        self.terrain_lbl.configure(fg=colour, text="  ·  ".join(parts) + "  ·  info only, not applied to MIL")

    def _range(self, arc, mil: float) -> float | None:
        """Where a mil lands: the game's own table where its sight has been read, else the community table."""
        for g in self.games.values():
            if arc in g.weapon.arcs:
                r = g.range_at(arc, mil)
                if r is not None:
                    return r
        return range_at_mil(arc, mil)

    def _toggle_trim(self) -> None:
        self.trim_on = not self.trim_on
        self.cfg["auto_trim"] = self.trim_on
        self._refresh()

    def _trim_text(self, plan: Plan | None) -> tuple[str, str]:
        """(text, colour) describing the trim for the current gun spot."""
        t = plan.trim if plan else None
        if t is None:
            return "", DIM
        hk = self.cfg["hotkeys"]["impact"]
        if t.shots_az < 3:
            return f"TRIM  learning this gun spot: {t.shots_az}/3 impacts logged ({hk} on each)", DIM
        if not t.active:
            return f"TRIM  {t.shots_az} shots from this spot: no steady error, firing straight", MUTED
        parts = []
        if t.az_on:
            defl = t.deflection_deg((plan.arc.min_mil + plan.arc.max_mil) / 2) if plan.arc else 0.0
            tilt = f" (gun tilted ~{abs(t.cant_deg):.1f}°)" if t.cant_deg is not None else ""
            parts.append(f"throws {abs(defl):.1f}° {'right' if defl > 0 else 'left'} at this elevation{tilt}")
        if t.range_on:
            height_on = bool(plan.arc and plan.arc.height_mil)
            parts.append(f"lands {abs(t.range_m):.0f} m {'long' if t.range_m > 0 else 'short'} here"
                         + (" (not applied: height correction on)" if height_on else ""))
        head = f"TRIM  this gun spot ({t.shots_az} shots) " + "; ".join(parts)
        if not self.trim_on:
            return f"{head}\nauto-trim off: firing the raw table", MUTED
        add, right = plan.offsets
        moved = " · ".join(x for x in (
            f"{abs(right):.0f} m {'right' if right > 0 else 'left'}" if round(right) else "",
            f"{abs(add):.0f} m {'further' if add > 0 else 'shorter'}" if round(add) else "") if x)
        return f"{head}\n→ dialing {moved or 'unchanged'} of the aim point", AMBER

    def _render_trim(self, plan: Plan | None) -> None:
        self.trim_btn.configure(text=f"Auto-trim: {'on' if self.trim_on else 'off'}",
                                fg=GREEN if self.trim_on else MUTED)
        text, colour = self._trim_text(plan)
        self.trim_lbl.configure(text=text, fg=colour)

    def _sync_entries(self) -> None:
        for role in ROLES:
            p = self.points[role]
            for axis in ("x", "y"):
                self.vars[(role, axis)].set("" if p is None else f"{getattr(p, axis):.2f}")

    def _select_weapon(self, wid: str) -> None:
        self.weapon_id = wid
        self.cfg["weapon"] = wid
        for k, b in self.weapon_btns.items():
            b.configure(bg=AMBER if k == wid else CARD, fg=BG if k == wid else MUTED)
        self._refresh()

    def _toggle_topmost(self) -> None:
        self.cfg["always_on_top"] = bool(self.topmost_var.get())
        self.root.attributes("-topmost", self.cfg["always_on_top"])

    def _toggle_keep(self) -> None:
        self.cfg["keep_correction"] = bool(self.keep_var.get())

    # -- voice ------------------------------------------------------------------------
    def _toggle_voice(self) -> None:
        self.cfg["voice"]["enabled"] = bool(self.voice_var.get())
        if not self.voice_var.get():
            self.speaker.stop()

    def _set_volume(self, value: int) -> None:
        self.cfg["voice"]["volume"] = value
        self.vol_lbl.configure(text=f"{value}%")
        self.speaker.set_volume(value)

    def _set_rate(self, value: int) -> None:
        self.cfg["voice"]["rate"] = value
        self.rate_lbl.configure(text=f"{value:+d}")
        self.speaker.set_rate(value)

    def _test_voice(self, short: bool = False) -> None:
        if self.speaker.error:
            self._status(self.speaker.error, RED)
            return
        if short:
            self.speaker.say("Azimuth")
            return
        plan = self._plan()
        sol = plan.sol if plan else None
        self.speaker.say(self._callout_text(sol, plan.arc if plan else None)
                         or "Azimuth zero niner zero point zero. Elevation seven niner, low arc.")

    def _callout_text(self, sol: Solution | None, arc: ArcSolution | None) -> str:
        """The solution as a gunner wants to hear it: digit by digit, azimuth then elevation."""
        if sol is None:
            return ""
        if sol.status != "ok":
            word = "Too far" if sol.status == "too_far" else "Too close"
            return f"{word}, by {sol.off_by_m:.0f} meters."
        if arc is None:
            return ""
        weapon = self.data.weapons[self.weapon_id]
        parts = []
        if weapon.scope_range_lines:  # the mortar scope works in range
            parts.append(f"Range {sol.distance_m:.0f}.")
        parts.append(f"Azimuth {digits(f'{sol.azimuth_deg:05.1f}')}.")
        mil = round((arc.min_mil + arc.max_mil) / 2)
        arc_word = f", {arc.label.lower()} arc" if len(weapon.arcs) > 1 else ""
        parts.append(f"Elevation {digits(str(mil))}{arc_word}.")
        if self._dz and arc.mil_per_meter is None:
            parts.append("No height correction.")
        return " ".join(parts)

    def _queue_callout(self, text: str) -> None:
        """Speak a changed solution once things settle; a burst of updates says only the last."""
        if self._callout_job is not None:
            self.root.after_cancel(self._callout_job)
            self._callout_job = None
        if not text or not self.voice_var.get() or text == self._last_spoken:
            return

        def speak() -> None:
            self._callout_job = None
            self._last_spoken = text
            self.speaker.say(self._callout_prefix + text)
            self._callout_prefix = ""
        self._callout_job = self.root.after(150, speak)

    def _cue(self, text: str) -> None:
        """A short spoken confirmation that isn't a firing solution."""
        if self.voice_var.get():
            self.speaker.say(text)

    # -- sight check: read the gun sight ONCE per press, say how far to go -------------------
    # Nothing reads the game in the background and nothing is drawn over it. An app that
    # watches the game constantly or paints on top of it looks like a cheat to anti-cheat
    # software, whatever it actually does (WARDOGS runs a kernel-level one, 2026-09).
    DIAL_AZ_ON = 0.2  # degrees: about 7 m sideways at 2 km
    DIAL_EL_ON = 1.0  # mil

    def _check_sight(self) -> None:
        """The button: the same one-shot read as the hotkey."""
        if self._dial_busy:
            return
        self._dial_busy = True
        threading.Thread(target=self._grab_sight, daemon=True, name="sight-grab").start()

    def _grab_sight(self) -> None:
        """Screenshot the main monitor, where the game runs, once, for the OCR worker."""
        try:
            mon = next((m for m in screen.monitors() if m.primary), None) or screen.monitors()[0]
            self.jobs.put({"kind": "sight", "shot": screen.grab(mon.rect)})
        except Exception as e:  # noqa: BLE001
            self.results.put(("error", "sight", f"screen capture failed: {e!r}"))

    def _on_sight(self, r: SightReading) -> None:
        self._dial_busy = False
        if not r.found:
            hk = self.cfg["hotkeys"]["dial"]
            self._show_dial(f"SIGHT   no SPH-2 gun sight on screen: look through it and press {hk}", MUTED)
            self._say("No sight.")
            return
        if r.mil is not None and r.heading is not None:
            self._last_sight = (time.monotonic(), r)  # what the gun was laid at, for the next F9
        self._record_sight_rows(r)
        if r.asl is not None:
            self._log_elevation("sight", "gun", float(r.asl))
        if r.asl is not None and self.asl["gun"] != float(r.asl):  # the sight shows the gun's own height
            self.asl["gun"] = float(r.asl)
            self.asl_vars["gun"].set(str(r.asl))
            self._refresh()
        self._guide(r)

    def _guide(self, r: SightReading) -> None:
        plan = self._plan()
        if plan is None:
            self._show_dial("SIGHT   read a gun and a target first", MUTED)
            return
        sol, arc = plan.sol, plan.arc
        if arc is None:
            self._show_dial("SIGHT   target is out of range", RED)
            return
        az_err = None if r.heading is None else (sol.azimuth_deg - r.heading + 180) % 360 - 180
        el_err = None if r.mil is None else (arc.min_mil + arc.max_mil) / 2 - r.mil
        az_ok = az_err is not None and abs(az_err) <= self.DIAL_AZ_ON
        el_ok = el_err is not None and abs(el_err) <= self.DIAL_EL_ON
        parts = ["AZ ✓" if az_ok else "AZ ?" if az_err is None else f"{'▶' if az_err > 0 else '◀'} {abs(az_err):.1f}°",
                 "EL ✓" if el_ok else "EL ?" if el_err is None else f"{'▲' if el_err > 0 else '▼'} {abs(el_err):.0f} mil"]
        colour = GREEN if az_ok and el_ok else FG
        if r.tilt is not None:
            tl, tr = r.tilt
            parts.append("LEVEL ✓" if max(abs(tl), abs(tr)) <= LEVEL_OK else f"LEVEL L{tl:+.1f} R{tr:+.1f}")
        if r.stabilized is False:
            parts.append("UNSTABILIZED")
            colour = RED
        self._show_dial(f"SIGHT {datetime.now():%H:%M:%S}   " + "     ".join(parts), colour)

        # The whole correction in one sentence, turn first: press again after dialing.
        said = ["Not stabilized."] if r.stabilized is False else []
        if az_ok and el_ok:
            said.append("On target.")
        else:
            if not az_ok and az_err is not None:
                amount = f"{abs(az_err):.0f}" if abs(az_err) >= 10 else f"{abs(az_err):.1f}"
                said.append(f"{'Right' if az_err > 0 else 'Left'} {amount}.")
            if not el_ok and el_err is not None:
                said.append(f"{'Up' if el_err > 0 else 'Down'} {abs(el_err):.0f}.")
        self._say(" ".join(said))

    def _fired_sight(self) -> tuple[SightReading, float] | None:
        """The sight reading taken before this F9: what the gun was laid at when it fired."""
        if self._last_sight is None:
            return None
        age = time.monotonic() - self._last_sight[0]
        return (self._last_sight[1], age) if age <= SIGHT_FRESH_S else None

    def _say(self, phrase: str) -> None:
        if phrase and self.voice_var.get():
            self.speaker.say(phrase)

    def _show_dial(self, text: str, colour: str) -> None:
        self.dial_lbl.configure(text=text, fg=colour)

    def _record_sight_rows(self, r: SightReading) -> None:
        """Keep every (mil, range) row the sight prints: the game's own firing table."""
        new = []
        for row in r.table:
            if row in self._sight_rows or not self._sight_row_plausible(*row):
                continue
            # Only a row read the same way twice counts: one OCR slip would bend the table.
            self._row_counts[row] = self._row_counts.get(row, 0) + 1
            if self._row_counts[row] >= 2:
                new.append(row)
        if not new:
            return
        self._sight_rows.update(new)
        path = cfgmod.SIGHT_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(["time", "mil", "range_m", "gun_asl"])
            for mil, rng in new:
                w.writerow([datetime.now().isoformat(timespec="seconds"), mil, rng, r.asl if r.asl is not None else ""])
        game = self.games.get("sph2")
        if game is not None and game.add(new):
            self._refresh()  # the solution may now come from the game's own numbers

    # -- arcs -----------------------------------------------------------------------
    def _choose_arc(self, index: int) -> None:
        arcs = self.data.weapons[self.weapon_id].arcs
        if index < len(arcs):
            self.arc_choice[self.weapon_id] = arcs[index].id
            self._refresh()

    def _chosen_arc(self, sol: Solution | None) -> ArcSolution | None:
        """The arc you said you're firing, or the first that reaches if that one can't."""
        if sol is None or not sol.arcs:
            return None
        want = self.arc_choice.get(self.weapon_id)
        return next((a for a in sol.arcs if a.arc_id == want), sol.arcs[0])

    # -- clipboard --------------------------------------------------------------------
    def _toggle_clipboard(self) -> None:
        self.cfg["watch_clipboard"] = bool(self.clip_var.get())
        self._clip_seen = self._read_clipboard()  # don't act on whatever was already there

    def _read_clipboard(self) -> str | None:
        try:
            return self.root.clipboard_get()
        except tk.TclError:  # empty, or not text
            return None

    def _check_clipboard(self) -> None:
        text = self._read_clipboard()
        if not self._clip_primed:  # first look: remember what's already there, don't act on it
            self._clip_primed, self._clip_seen = True, text
            return
        if text == self._clip_seen:
            return
        self._clip_seen = text
        if not self.clip_var.get() or not text or text == self._own_clip:
            return
        # Only labelled pairs: a stray "12.50 3.99" on the clipboard must not become a target.
        coords = [c for c in find_coords(text) if c.labeled]
        if not coords:
            return
        c = coords[-1]  # the newest line when a whole chat log is copied
        self._set_thumb("target", None)
        self.src_lbls["target"].configure(text=f"from clipboard {datetime.now():%H:%M:%S}", fg=DIM)
        self.set_point("target", Point(c.x, c.y))
        self._status(f"TARGET X{c.x:.2f} Y{c.y:.2f} from the clipboard.", FG)

    def _copy_target(self) -> None:
        t = self.points["target"]
        if t is None:
            self._status("No target to copy yet.", RED)
            return
        text = f"x{t.x:.2f}, y{t.y:.2f}"  # the share format the game chat and other tools accept
        self._own_clip = text
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status(f"Copied {text} — paste it in squad chat for your team.", BLUE)

    # -- reads worth studying -------------------------------------------------------------
    def _flag_read(self, role: str) -> None:
        """Save the screenshot behind a wrong read so the reader can be fixed against it."""
        job = self._last_capture.get(role)
        if not job:
            return
        out = cfgmod.DEBUG_DIR / "misreads"
        out.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}-{role}"
        job["shot"].save(out / f"{stamp}.png")
        res: ReadResult = job["result"]
        info = {"role": role, "cursor": job["cursor"], "monitor_origin": job["origin"],
                "read": [res.coord.x, res.coord.y] if res.coord else None, "text": res.text, "box": res.box,
                "stage": res.region, "votes": res.votes, "dissent": res.dissent,
                "shown_now": [self.points[role].x, self.points[role].y] if self.points.get(role) else None}
        (out / f"{stamp}.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        self._status(f"Saved that {ROLE_LABEL[role]} read to debug/misreads/ — type the right numbers in, "
                     "and send me the folder when you've collected a few.", BLUE)

    # -- tabs and the accuracy test ------------------------------------------------------------
    def _show_tab(self, key: str) -> None:
        for k, frame in self.tab_frames.items():
            if k == key:
                frame.pack(fill="both", expand=True, pady=(6, 0))
            else:
                frame.pack_forget()
            self.tab_btns[k].configure(fg=AMBER if k == key else MUTED)
        if key == "accuracy":
            self._render_accuracy()

    def _sight_row_plausible(self, mil: float, rng: float) -> bool:
        """A range label that lost digits ("2,340 m" read as "340m") is nowhere near the table."""
        table = self._sight_table_range(mil)
        return table is None or abs(rng - table) <= SIGHT_ROW_SANE

    def _sight_table_range(self, mil: float) -> float | None:
        sph = self.data.weapons.get("sph2")
        arc = next((a for a in sph.arcs if a.id == ("high" if mil >= 605 else "low")), None) if sph else None
        return range_at_mil(arc, mil) if arc else None

    def _sight_vs_table(self) -> str:
        """How the SPH-2 sight's own table compares with the community one, per arc."""
        game = self.games.get("sph2")
        if game is None:
            return ""
        lines = []
        for arc in game.weapon.arcs:
            cov = game.coverage(arc)
            if cov is None:
                continue
            n, lo, hi, mean = cov
            lines.append(f"  {arc.label} arc: {n} rows, {lo:.0f}-{hi:.0f} mil — the game reaches {abs(mean):.0f} m "
                         f"{'FURTHER' if mean > 0 else 'SHORTER'} than the old table on average")
        if not lines:
            return ""
        return "GAME'S OWN SIGHT TABLE (used for MIL wherever it's been seen):\n" + "\n".join(lines)

    def _render_accuracy(self) -> None:
        key = (len(self.elevlog.readings), len(self.shotlog.shots), self.cfg["terrain_map"])
        if key != self._elev_key:
            self._elev_key = key
            threading.Thread(target=self._elev_job, args=(key,), daemon=True, name="elev").start()
        sums = self.shotlog.summaries()
        versus = self._sight_vs_table()
        if not sums:
            hk = self.cfg["hotkeys"]["impact"]
            self.acc_text.configure(fg=MUTED, text=(
                "No shots logged yet.\n\n"
                f"Fire the solution, then hover where the shell landed\non the map and press {hk}.\n"
                "Every impact becomes a data point: how far long or\nshort, left or right, of where "
                "the app aimed.\n\n"
                "After 3+ shots per weapon/arc, this shows whether the\nfiring tables run long or short "
                "in the live game." + (f"\n\n{versus}" if versus else "")
                + (f"\n\n{self._elev_report}" if self._elev_report else "")))
            return
        lines = [f"{'':14}{'shots':>5} {'range error':>13} {'left/right':>11} {'miss':>6}"]
        verdicts = []
        for s in sums:
            name = f"{self.data.weapons[s.weapon].label if s.weapon in self.data.weapons else s.weapon} {s.arc}"
            lines.append(f"{name[:14]:14}{s.shots:>5} {s.mean_long_m:+7.0f} ±{s.sd_long_m:<4.0f}"
                         f" {s.mean_right_m:+8.0f} m {s.median_miss_m:5.0f} m")
            if s.biased:
                verdicts.append(f"• {name}: lands {abs(s.mean_long_m):.0f} m "
                                f"{'LONG' if s.mean_long_m > 0 else 'SHORT'} on average (~{s.median_distance_m:.0f} m)\n"
                                f"  consistently, so it's the tables, not scatter.")
            elif s.shots < 3:
                verdicts.append(f"• {name}: {3 - s.shots} more shot(s) before this means anything.")
            elif s.sd_long_m > 30 and self._flat_share(s) > 0.5:
                # Big swings in range with sideways misses small: that's terrain, not the gun.
                verdicts.append(f"• {name}: range swings ±{s.sd_long_m:.0f} m shot to shot. Most were fired\n"
                                f"  without heights, and a target 50 m below the gun lands\n"
                                f"  ~100 m long, so fill in HEIGHT (or walk in with F9).")
            else:
                verdicts.append(f"• {name}: no consistent bias; the misses are scatter.")
        total = sum(s.shots for s in sums)
        lines += ["", *verdicts, "", f"{total} shots in {self.shotlog.path.name}  (range error: + long / − short)"]
        trim_text, _c = self._trim_text(self._plan_cache)
        if trim_text:
            lines += ["", trim_text]
        lines += ["", self._dialing_text()]
        if self._elev_report:
            lines += ["", self._elev_report]
        if versus:
            lines += ["", versus]
        self.acc_text.configure(text="\n".join(lines), fg=FG)

    def _dialing_text(self) -> str:
        """Dialed (what the sight showed) against told (what the app said): your part of the miss."""
        shots = [s for s in self.shotlog.shots if s.dialed]
        hk = self.cfg["hotkeys"]["dial"]
        if not shots:
            return (f"YOUR DIALING: not checked yet. Press {hk} (check sight) before you fire;\n"
                    "each F9 then records what the sight showed, so user error and gun error\n"
                    "can be told apart.")
        az = [(s.sight_heading - s.azimuth_deg + 180) % 360 - 180 for s in shots]
        el = [s.sight_mil - s.mil for s in shots]
        side = [s.distance_m * math.tan(math.radians(a)) for s, a in zip(shots, az)]
        rng = []
        for s in shots:
            arc = next((a for a in self.data.weapons[s.weapon].arcs if a.id == s.arc), None) \
                if s.weapon in self.data.weapons else None
            told, dialed = (self._range(arc, s.mil), self._range(arc, s.sight_mil)) if arc else (None, None)
            rng.append(dialed - told if told is not None and dialed is not None else 0.0)
        n = len(shots)
        lines = [f"YOUR DIALING ({n} shot{'s' if n != 1 else ''} with the sight seen at fire time)",
                 f"  azimuth   {sum(az) / n:+.2f}° average, worst {max(az, key=abs):+.2f}°  "
                 f"(= {sum(side) / n:+.0f} m sideways)",
                 f"  elevation {sum(el) / n:+.1f} mil average, worst {max(el, key=abs):+.1f}  "
                 f"(= {sum(rng) / n:+.0f} m in range)"]
        if all(abs(a) <= self.DIAL_AZ_ON + 0.05 for a in az) and all(abs(e) <= self.DIAL_EL_ON + 0.5 for e in el):
            lines.append("  → you dialed what you were told: the misses are the gun, not you.")
        else:
            lines.append(f"  → dialing accounts for ~{abs(sum(rng) / n):.0f} m of range and "
                         f"~{abs(sum(side) / n):.0f} m sideways on average.")
        tilted = [s for s in shots if s.tilt_left is not None]
        if tilted:
            worst = max(max(abs(s.tilt_left), abs(s.tilt_right)) for s in tilted)
            lines.append(f"  tilt pips seen on {len(tilted)}: worst {worst:.1f} dash off the middle mark.")
        return "\n".join(lines)

    def _flat_share(self, s) -> float:
        """Fraction of this weapon/arc's shots fired with no height difference entered."""
        shots = [x for x in self.shotlog.shots if (x.weapon, x.arc) == (s.weapon, s.arc)]
        return sum(1 for x in shots if x.dz_m == 0) / max(len(shots), 1)

    def _new_session(self) -> None:
        archived = self.shotlog.start_new_session()
        self._obs_cache = None
        self._render_accuracy()
        self._status(f"Started a new test session; the old log is kept as {archived.name}." if archived
                     else "Started a new test session.", BLUE)

    def _open_logs(self) -> None:
        folder = self.shotlog.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # the viewer's own file browser, on their click

    def _swap(self) -> None:
        g, t = self.points["gun"], self.points["target"]
        self.points["gun"], self.points["target"] = t, g
        g_img, t_img = self._thumbs.pop("gun", None), self._thumbs.pop("target", None)
        g_src, t_src = self.src_lbls["gun"].cget("text"), self.src_lbls["target"].cget("text")
        for role, img, src in (("gun", t_img, t_src), ("target", g_img, g_src)):
            if img:
                self._thumbs[role] = img
            self.thumb_lbls[role].configure(image=img or "")
            self.src_lbls[role].configure(text=src)
        self.asl["gun"], self.asl["target"] = self.asl["target"], self.asl["gun"]
        for role in ("gun", "target"):
            v = self.asl[role]
            self.asl_vars[role].set("" if v is None else f"{v:.0f}")
        self.adj, self.adj_steps = (0.0, 0.0), 0
        self._sync_entries()
        self._refresh()

    def _paste(self, role: str) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        c = best_coord(text)
        if c is None:
            self._status("Clipboard has no X/Y coordinates in it.", RED)
            return
        self._set_thumb(role, None)
        self.src_lbls[role].configure(text="pasted", fg=DIM)
        self.set_point(role, Point(c.x, c.y))

    def _reset_adjust_clicked(self) -> None:
        self.adj, self.adj_steps = (0.0, 0.0), 0
        self.points["impact"] = None
        self._set_thumb("impact", None)
        self.src_lbls["impact"].configure(text=" ")
        self._sync_entries()
        self._refresh()
        self._status("Adjustments cleared; aiming straight at the target.", MUTED)

    def _clear(self) -> None:
        for role in ROLES:
            self.points[role] = None
            self._set_thumb(role, None)
            self.src_lbls[role].configure(text=" ")
        for role in ("gun", "target"):
            self.asl[role] = None
            self.asl_vars[role].set("")
        for flag in self.flag_lbls.values():
            flag.grid_remove()
        self._last_capture.clear()
        self.adj, self.adj_steps = (0.0, 0.0), 0
        self._sync_entries()
        self._refresh()

    def _history_pick(self, _event=None) -> None:
        sel = self.hist_list.curselection()
        if sel and sel[0] < len(self.history):
            p = self.history[sel[0]][1]
            self._set_thumb("target", None)
            self.src_lbls["target"].configure(text="from recent targets", fg=DIM)
            self.set_point("target", p)

    # ------------------------------------------------------------------ rendering
    def _refresh(self) -> None:
        weapon = self.data.weapons[self.weapon_id]
        gun, target = self.points["gun"], self.points["target"]
        aim = self.aim
        sol: Solution | None = None
        plan: Plan | None = None
        arcs_expected = [a.label for a in weapon.arcs]
        for i, (col, name, val, note) in enumerate(self.arc_cols):
            if i < len(arcs_expected):
                col.pack(side="left", fill="x", expand=True)
                name.configure(text=arcs_expected[i])
                val.configure(text="—", fg=DIM)
                note.configure(text="")
            else:
                col.pack_forget()
        if gun is None or aim is None:
            self.az_val.configure(text="—", fg=DIM)
            self.az_sub.configure(text=" ")
            self.dist_val.configure(text="—", fg=DIM)
            self.dist_sub.configure(text=" ")
            missing = " & ".join(r for r, p in (("GUN", gun), ("TARGET", target)) if p is None)
            self.pill.configure(text=f"NEED {missing}", bg=EDGE, fg=MUTED)
            self.adjust_lbl.configure(text="")
            self.aim_lbl.configure(text="")
        else:
            plan = self._plan()
            sol = plan.sol
            self.az_val.configure(text=f"{sol.azimuth_deg:.1f}°", fg=FG)
            self.az_sub.configure(text=f"{compass_point(sol.azimuth_deg)}   ·   {azimuth_mil(sol.azimuth_deg):.0f} mil (6400)")
            self.dist_val.configure(text=f"{sol.distance_m:.0f} m", fg=FG)
            spread = spread_m(sol.distance_m, weapon.moa)
            extra = f"   ·   ±{spread:.0f} m spread" if spread and sol.status == "ok" else ""
            self.dist_sub.configure(text=f"ΔX {sol.dx_m:+.0f} m   ΔY {sol.dy_m:+.0f} m{extra}")
            found = {a.arc_id: a for a in sol.arcs}
            chosen = self._chosen_arc(sol)
            for i, arc in enumerate(weapon.arcs):
                _col, name, val, note = self.arc_cols[i]
                a = found.get(arc.id)
                picked = a is not None and chosen is not None and a.arc_id == chosen.arc_id
                if len(weapon.arcs) > 1:  # mark the arc you're firing; the other stays readable but quiet
                    name.configure(text=f"● {arc.label}  firing" if picked else f"{arc.label}  (click to fire)",
                                   fg=AMBER if picked else MUTED)
                val.configure(text=a.text() if a else "—", fg=(AMBER if picked else DIM_AMBER) if a else DIM)
                # Where the mil came from, and whether a height difference was applied.
                bits, colour = [], DIM
                if a and weapon.id in self.games:
                    bits.append("game's sight table" if a.source == "game" else "old table, unverified")
                    colour = GREEN if a.source == "game" else MUTED
                if a and self._dz and a.mil_per_meter is not None:
                    bits.append(f"incl. {a.height_mil:+.0f} for ΔZ")
                    colour = AMBER
                elif a and self._dz:
                    bits.append("flat only — no ΔZ data")
                    colour = RED
                note.configure(text=" · ".join(bits), fg=colour)
            if sol.status == "ok":
                self.pill.configure(text="IN RANGE", bg=GREEN, fg=BG)
            elif sol.status == "too_far":
                self.pill.configure(text=f"TOO FAR by {sol.off_by_m:.0f} m", bg=RED, fg=BG)
            else:
                self.pill.configure(text=f"TOO CLOSE by {sol.off_by_m:.0f} m", bg=RED, fg=BG)
            if self.adj != (0.0, 0.0):
                n = self.adj_steps
                self.adjust_lbl.configure(text=f"corrected ({n} step{'s' if n != 1 else ''})")
                self.aim_lbl.configure(text=f"AIM POINT  {aim.text()}   ({self._describe_adj()} of the target)")
            else:
                self.adjust_lbl.configure(text="")
                self.aim_lbl.configure(text="")
        self.adj_lbl.configure(text=f"aiming {self._describe_adj()} of the target" if self.adj != (0.0, 0.0)
                               else "no correction — aiming straight at the target",
                               fg=AMBER if self.adj != (0.0, 0.0) else DIM)
        self._plan_cache = plan
        self._render_trim(plan)
        self._request_terrain()
        self._render_tip(sol)
        self._render_height(sol)
        self._queue_callout(self._callout_text(sol, self._chosen_arc(sol)))
        self._render_history()

    def _describe_adj(self) -> str:
        further, right = self.adj
        parts = []
        if round(further):
            parts.append(f"{abs(further):.0f} m {'further' if further > 0 else 'shorter'}")
        if round(right):
            parts.append(f"{abs(right):.0f} m {'right' if right > 0 else 'left'}")
        return " · ".join(parts) or "on target"

    def _render_tip(self, sol: Solution | None) -> None:
        """Weapon-specific help reading the sight."""
        weapon = self.data.weapons[self.weapon_id]
        if weapon.id == "sph2":
            arc = self._chosen_arc(sol)
            if arc is not None and arc.source == "game":
                self.tip_lbl.configure(fg=DIM, text="MIL from the game's own sight table here. Level the SPH-2 "
                                                    "first: side tilt throws shots off.")
            elif arc is not None:
                hk = self.cfg["hotkeys"]["dial"]
                self.tip_lbl.configure(fg=AMBER, text=(
                    "MIL from the old community table here: it has run up to 14 mil off. Dial by the sight's RNG "
                    f"to the distance shown, then press {hk}: each check records the rows the sight shows, and "
                    "this switches to the game's own numbers."))
            else:
                self.tip_lbl.configure(text="Level the SPH-2 first: side tilt throws shots off.", fg=DIM)
            return
        if sol is None or sol.status != "ok" or not weapon.scope_range_lines:
            self.tip_lbl.configure(text="")
            return
        tips = []
        b = bracket(weapon.scope_range_lines, sol.distance_m)
        if b:
            lo, hi, frac = b
            tips.append(f"scope RNG lines {lo:.0f} → {hi:.0f}: {frac * 100:.0f}% of the way")
        mark = round(sol.azimuth_deg / 15) * 15 % 360
        diff = (sol.azimuth_deg - mark + 180) % 360 - 180
        tips.append(f"compass mark {mark:03.0f}°, then {abs(diff):.1f}° {'right' if diff > 0 else 'left'}")
        self.tip_lbl.configure(text="   ·   ".join(tips), fg=MUTED)

    def _render_height(self, sol: Solution | None) -> None:
        """Explain what the height difference is doing, or what it would be worth."""
        dz = self._dz
        known = [a for a in (sol.arcs if sol else ()) if a.mil_per_meter is not None]
        if abs(self._dz_raw) > DZ_SANE:
            self.height_lbl.configure(
                text=f"ΔZ {self._dz_raw:+.0f} m can't be right — check both heights; NOT applied", fg=RED)
        elif dz and known:
            a = known[0]
            text = f"ΔZ {dz:+.0f} m  →  {a.height_mil:+.0f} mil on {a.label}"
            colour = AMBER
            if a.extrapolated:
                text += "   beyond the ±40 m measured — treat as a guess"
                colour = RED
            self.height_lbl.configure(text=text, fg=colour)
        elif dz:
            self.height_lbl.configure(
                text=f"ΔZ {dz:+.0f} m  ·  no height data for this arc — walk it in with "
                     f"{self.cfg['hotkeys']['impact']}", fg=AMBER)
        elif known:
            a = known[0]
            self.height_lbl.configure(
                text=f"flat  ·  10 m of height ≈ {abs(10 * a.mil_per_meter):.0f} mil on {a.label}", fg=MUTED)
        else:
            self.height_lbl.configure(text="flat  ·  fill both to correct for a slope", fg=DIM)

    def _render_history(self) -> None:
        gun = self.points["gun"]
        self.hist_list.delete(0, "end")
        for stamp, p in self.history:
            line = f"{stamp}  {p.text():<16}"
            if gun is not None:
                line += f" {distance_m(gun, p, self.data.meters_per_unit):5.0f} m  {azimuth_deg(gun, p):5.1f}°"
            self.hist_list.insert("end", line)

    # ------------------------------------------------------------------ shutdown & errors
    def _on_tk_error(self, exc, val, tb) -> None:
        cfgmod.DEBUG_DIR.mkdir(exist_ok=True)
        with open(cfgmod.DEBUG_DIR / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{''.join(traceback.format_exception(exc, val, tb))}\n")
        self._status(f"Internal error: {val!r} (logged to debug/errors.log)", RED)

    def _quit(self) -> None:
        try:
            self.cfg["window"] = self.root.geometry()
            cfgmod.save(self.cfg)
        finally:
            self.speaker.close()
            self.hotkeys.stop()
            self.jobs.put(None)
            self.root.destroy()


def _ground_slope(tm: TerrainMap, gun: Point, az: float, half_m: float = 4.0) -> tuple[float, float]:
    """Ground slope under the gun in degrees: along the line of fire (+ = rising toward the
    target) and across it (+ = right side higher), over a vehicle-sized footprint."""
    th = math.radians(az)
    f, rt = (math.sin(th), math.cos(th)), (math.cos(th), -math.sin(th))
    d = half_m / 100

    def h(u: tuple[float, float], k: float) -> float:
        return tm.height(Point(gun.x + u[0] * k, gun.y + u[1] * k))

    along = math.degrees(math.atan((h(f, d) - h(f, -d)) / (2 * half_m)))
    across = math.degrees(math.atan((h(rt, d) - h(rt, -d)) / (2 * half_m)))
    return along, across


def main() -> None:
    screen.make_dpi_aware()
    root = tk.Tk()
    ArtyApp(root)
    root.mainloop()
