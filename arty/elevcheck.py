"""Test the terrain against the game: every height the game prints, next to the terrain's.

The game shows heights in three places: the label beside the cursor on the map
("ASL 22 m", the hovered spot), the gun sight's status line (the gun), and your
HUD (you). Each read is logged with where it was taken. The terrain's absolute
level is offset from the game's, so a matching terrain shows up as the SAME
offset every time: `game = terrain + offset`, with little spread. A spread of a
few metres means the terrain can be trusted for ΔZ; a large or drifting one
means it can't, or that source reads a different spot than we think.

When no map was picked, every map is tried and the one whose offsets agree best
is reported: the heights themselves identify the map.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ballistics import Point

FIELDS = ["time", "source", "role", "x", "y", "asl", "map"]
SOURCE_LABEL = {"hover": "map hover label", "sight": "gun sight", "hud": "your HUD"}
MATCH_SD = 3.0  # offsets this consistent (m) mean the terrain matches that source
MIN_READS = 3


@dataclass(frozen=True)
class Reading:
    time: str
    source: str
    role: str
    point: Point
    asl: float
    map: str


OUTLIER_M = 25.0  # a read this far from the median offset is a misread (or a different spot), set aside


@dataclass(frozen=True)
class SourceFit:
    source: str
    n: int
    offset: float  # game height minus terrain height, averaged
    sd: float
    worst: float  # largest single departure from the average offset
    rejected: int = 0  # reads set aside as outliers
    mad: float = 0.0  # median distance from the median offset: the typical read's error

    @property
    def matches(self) -> bool:
        return self.n >= MIN_READS and self.mad <= MATCH_SD


class ElevationLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.readings: list[Reading] = []
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        self.readings.append(Reading(r["time"], r["source"], r["role"],
                                                     Point(float(r["x"]), float(r["y"])), float(r["asl"]), r["map"]))
                    except (KeyError, ValueError):
                        continue
        except FileNotFoundError:
            pass

    def add(self, source: str, role: str, point: Point, asl: float, map_id: str) -> Reading | None:
        last = next((r for r in reversed(self.readings) if r.source == source), None)
        if last and last.point == point and last.asl == asl:
            return None  # the sight repeats itself twice a second; one row per distinct reading
        r = Reading(datetime.now().isoformat(timespec="seconds"), source, role, point, float(asl), map_id)
        self.readings.append(r)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(FIELDS)
            w.writerow([r.time, source, role, point.x, point.y, asl, map_id])
        return r


def fit(readings: list[Reading], height) -> list[SourceFit]:
    """Offsets per source against one terrain (`height(point)` -> metres or None)."""
    by: dict[str, list[float]] = {}
    for r in readings:
        h = height(r.point)
        if h is not None:
            by.setdefault(r.source, []).append(r.asl - h)
    out = []
    for source, offs in sorted(by.items()):
        # One wild read (a hover label from 2026-09-22 sat 450 m off every other) must not
        # decide the verdict: set aside anything far from the median before averaging.
        med = statistics.median(offs)
        kept = [o for o in offs if abs(o - med) <= OUTLIER_M]
        if len(kept) * 2 <= len(offs):
            kept = offs  # scattered everywhere (a wrong map): nothing is an outlier, the spread is the answer
        mean = statistics.fmean(kept)
        out.append(SourceFit(source, len(kept), mean, statistics.stdev(kept) if len(kept) > 1 else 0.0,
                             max(abs(o - mean) for o in kept), len(offs) - len(kept),
                             statistics.median(abs(o - med) for o in offs)))
    return out


def _ranked(readings: list[Reading], maps: dict) -> list[tuple[float, str, list[SourceFit]]]:
    """Maps by how well the reads agree with their terrain: the read-weighted median error
    (robust: a wild read or a noisy source can't win or lose it on its own)."""
    scored = []
    for map_id, tm in maps.items():
        fits = fit(readings, tm.height)
        n = sum(f.n + f.rejected for f in fits)
        if n < MIN_READS:
            continue
        scored.append((sum(f.mad * (f.n + f.rejected) for f in fits) / n, map_id, fits))
    return sorted(scored, key=lambda t: t[0])


def best_map(readings: list[Reading], maps: dict) -> tuple[str, list[SourceFit]] | None:
    """The map whose terrain agrees best with these readings."""
    ranked = _ranked(readings, maps)
    return (ranked[0][1], ranked[0][2]) if ranked else None


DETECT_MAX_M = 8.0  # the winner's typical read must be this close to its terrain
DETECT_RATIO = 1.4  # and clearly closer than any other map's


def detect_map(readings: list[Reading], maps: dict) -> str | None:
    """The map these heights came from, or None unless it's clear-cut."""
    ranked = _ranked(readings, maps)
    if not ranked or ranked[0][0] > DETECT_MAX_M:
        return None
    if len(ranked) > 1 and ranked[1][0] < DETECT_RATIO * max(ranked[0][0], 0.5):
        return None
    return ranked[0][1]


def range_vs_ground(pairs: list[tuple[float, float]]) -> tuple[float, float, float, int] | None:
    """Least-squares (slope, slope SE, intercept, n) of range miss (m) on ground ΔZ (m)."""
    n = len(pairs)
    if n < 4:
        return None
    mx = statistics.fmean(x for x, _ in pairs)
    my = statistics.fmean(y for _, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx < 1e-9:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    resid = sum((y - my - slope * (x - mx)) ** 2 for x, y in pairs) / (n - 2)
    return slope, math.sqrt(resid / sxx), my - slope * mx, n
