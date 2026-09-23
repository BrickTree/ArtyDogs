"""The game's own firing table, as the SPH-2 sight prints it.

The sight labels every 10 mil of its elevation ladder with the range that
elevation reaches ("1,020 mil" beside "2,130m"). Every sight check (F11) records
the rows it sees (logs/sight_table.csv), on top of the rows shipped with the app
(data/game_table_seed.csv). Those rows are the live game's numbers, and on
2026-09-22 they disagreed with the community table this app started from: the
low arc ran 65-71 m FURTHER at 30-50 mil (the table's mil ~14 too high around
1,300 m) and 21 m further at 320 mil, while the high arc matched to 3 m at
1,390-1,400 mil. The user found the same thing in play: dialing the sight's
range hit, dialing the table's mil didn't.

So wherever the sight has been seen, the game's rows decide the mil. The
difference between the two tables (game minus community range at the same mil)
is interpolated between observed rows no more than MAX_GAP_MIL apart, and held
for one ladder step (10 mil) beyond the outermost ones. Anywhere else the
community table is used, and the solution says so.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

from .ballistics import Arc, Weapon, range_at_mil

MAX_GAP_MIL = 100.0  # interpolate the table difference across gaps up to this; wider = unknown
EDGE_MIL = 10.0  # one ladder step either side of an observed row counts as covered
SANE_M = 150.0  # a row further than this from the community table is a misread ("2,340" -> "340")


class GameTable:
    def __init__(self, weapon: Weapon, rows=()) -> None:
        self.weapon = weapon
        self._seen: dict[str, dict[float, list[float]]] = {a.id: {} for a in weapon.arcs}
        self._cache: dict[str, list[tuple[float, float]]] = {}
        self.add(rows)

    @classmethod
    def load(cls, weapon: Weapon, *paths: Path) -> "GameTable":
        """Rows from each CSV (mil, range_m columns) that exists: the shipped seed, then your log."""
        rows = []
        for path in paths:
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        try:
                            rows.append((float(r["mil"]), float(r["range_m"])))
                        except (KeyError, ValueError):
                            continue
            except FileNotFoundError:
                continue
        return cls(weapon, rows)

    def arc_for(self, mil: float) -> Arc | None:
        """The arc whose table spans this mil (the SPH-2's arcs don't overlap)."""
        for a in self.weapon.arcs:
            mils = [m for _r, m in a.table]
            if min(mils) <= mil <= max(mils):
                return a
        return None

    def add(self, rows) -> int:
        """Take (mil, range) rows; returns how many were new and plausible."""
        added = 0
        for mil, rng in rows:
            arc = self.arc_for(mil)
            table = range_at_mil(arc, mil) if arc else None
            if table is None or abs(rng - table) > SANE_M:
                continue
            seen = self._seen[arc.id].setdefault(mil, [])
            if rng not in seen:
                seen.append(rng)
                added += 1
                self._cache.pop(arc.id, None)
        return added

    def rows(self, arc_id: str) -> list[tuple[float, float]]:
        """(mil, range) per observed mark; repeated reads of one mark use their median."""
        return sorted((m, statistics.median(r)) for m, r in self._seen.get(arc_id, {}).items())

    def _deltas(self, arc: Arc) -> list[tuple[float, float]]:
        if arc.id not in self._cache:
            pts = [(m, r - range_at_mil(arc, m)) for m, r in self.rows(arc.id)]
            self._cache[arc.id] = [p for i, p in enumerate(pts) if not _kinked(pts, i)]
        return self._cache[arc.id]

    def delta(self, arc: Arc, mil: float) -> float | None:
        """Game range minus community range at this mil, or None where the sight hasn't been seen."""
        pts = self._deltas(arc)
        if not pts:
            return None
        if mil < pts[0][0]:
            return pts[0][1] if pts[0][0] - mil <= EDGE_MIL else None
        if mil > pts[-1][0]:
            return pts[-1][1] if mil - pts[-1][0] <= EDGE_MIL else None
        for (m0, d0), (m1, d1) in zip(pts, pts[1:]):
            if m0 <= mil <= m1:
                if m1 - m0 > MAX_GAP_MIL:
                    # a wide gap: only the ladder step next to each side is known
                    if mil - m0 <= EDGE_MIL:
                        return d0
                    if m1 - mil <= EDGE_MIL:
                        return d1
                    return None
                return d0 if m1 == m0 else d0 + (mil - m0) * (d1 - d0) / (m1 - m0)
        return pts[0][1]  # exactly one row, exactly at it

    def range_at(self, arc: Arc, mil: float) -> float | None:
        d = self.delta(arc, mil)
        base = range_at_mil(arc, mil)
        return None if d is None or base is None else base + d

    def mil_for(self, arc: Arc, distance: float, step: float = 0.5) -> float | None:
        """The elevation that reaches `distance` by the game's own table, or None if uncovered."""
        pts = self._deltas(arc)
        if not pts:
            return None
        mils = sorted(m for _r, m in arc.table)
        lo, hi = max(mils[0], pts[0][0] - EDGE_MIL), min(mils[-1], pts[-1][0] + EDGE_MIL)
        best = None
        m, prev = lo, None
        while m <= hi + 1e-9:
            r = self.range_at(arc, m)
            if r is not None and prev is not None and prev[1] is not None:
                (m0, r0), r1 = prev, r
                if (r0 - distance) * (r1 - distance) <= 0 and r1 != r0:
                    cand = m0 + (distance - r0) * (m - m0) / (r1 - r0)
                    # low arc: the first crossing; high arc: the table is monotone too
                    best = cand
                    break
            prev = (m, r)
            m += step
        return best

    def coverage(self, arc: Arc) -> tuple[int, float, float, float] | None:
        """(rows kept, lowest mil, highest mil, mean metres further than the community table)."""
        pts = self._deltas(arc)
        if not pts:
            return None
        return len(pts), pts[0][0], pts[-1][0], statistics.fmean(d for _m, d in pts)


KINK_M = 15.0  # a row this far off the line through its close neighbours is a misread


def _kinked(pts: list[tuple[float, float]], i: int) -> bool:
    """The table difference changes smoothly (a few metres per 10 mil); a lone row that
    jumps away from both close neighbours is a misread label, not the game."""
    if 0 < i < len(pts) - 1:
        (m0, d0), (m, d), (m1, d1) = pts[i - 1], pts[i], pts[i + 1]
        if m1 - m0 <= 3 * EDGE_MIL:
            return abs(d - (d0 + (m - m0) * (d1 - d0) / (m1 - m0))) > KINK_M
    return False
