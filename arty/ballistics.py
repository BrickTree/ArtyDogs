"""Firing-solution math for WARDOGS artillery.

Coordinates are the game's tactical-map units: 1 unit = 100 m, X grows east,
Y grows north (origin at the south-west corner of the world). Elevation is a
lookup in community-measured firing tables (data/weapons.json); there is no
physics model and the tables assume the gun and target are at the same height.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "weapons.json"
HEIGHT_FILE = Path(__file__).resolve().parent.parent / "data" / "height_correction.json"

# hypot() can land a float step off an exact table row or range limit.
RANGE_EPS = 1e-6

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def text(self) -> str:
        return f"X{self.x:.2f} Y{self.y:.2f}"


@dataclass(frozen=True)
class Arc:
    id: str
    label: str
    table: tuple[tuple[float, float], ...]  # (range_m, mil), ascending by range


@dataclass(frozen=True)
class Weapon:
    id: str
    label: str
    min_m: float
    max_m: float
    arcs: tuple[Arc, ...]
    moa: float | None = None  # accuracy; shells scatter over a radius that grows with range
    scope_range_lines: tuple[float, ...] = ()  # range labels printed in the sight (L81 RNG mode)


@dataclass(frozen=True)
class MapInfo:
    id: str
    label: str
    min_x: float
    max_x: float
    min_y: float
    max_y: float

    def contains(self, p: Point) -> bool:
        return self.min_x <= p.x <= self.max_x and self.min_y <= p.y <= self.max_y


@dataclass(frozen=True)
class HeightBand:
    """How many mil a metre of height difference is worth, by distance.

    Only covers part of one weapon's envelope; see data/height_correction.json.
    """
    weapon_id: str
    arc_id: str
    min_m: float
    max_m: float
    max_abs_dz: float
    slopes: tuple[tuple[float, float], ...]  # (distance, mil per metre), ascending

    def slope(self, distance: float) -> float | None:
        if not (self.min_m - RANGE_EPS <= distance <= self.max_m + RANGE_EPS):
            return None
        for (d0, s0), (d1, s1) in zip(self.slopes, self.slopes[1:]):
            if d0 <= distance <= d1:
                return s0 if d1 == d0 else s0 + (distance - d0) * (s1 - s0) / (d1 - d0)
        return self.slopes[0][1] if distance <= self.slopes[0][0] else self.slopes[-1][1]


@dataclass(frozen=True)
class ArcSolution:
    arc_id: str
    label: str
    min_mil: float  # including any height correction
    max_mil: float
    mil_per_meter: float | None = None  # height sensitivity here; None = no data for this arc
    height_mil: float = 0.0  # how much of the elevation above came from the height difference
    extrapolated: bool = False  # height difference is outside the range the data covers
    source: str = "table"  # "game": from the game's own sight table; "table": the community table

    def text(self) -> str:
        lo, hi = round(self.min_mil), round(self.max_mil)
        return f"{lo}" if lo == hi else f"{lo}-{hi}"


@dataclass(frozen=True)
class Solution:
    distance_m: float
    azimuth_deg: float
    dx_m: float
    dy_m: float
    status: str  # "ok", "too_close" or "too_far"
    off_by_m: float  # how far outside the weapon's range; 0 when in range
    arcs: tuple[ArcSolution, ...]

    @property
    def in_range(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class GameData:
    meters_per_unit: float
    weapons: dict[str, Weapon]
    maps: tuple[MapInfo, ...]
    height_bands: dict[tuple[str, str], HeightBand]

    def band(self, weapon_id: str, arc_id: str) -> HeightBand | None:
        return self.height_bands.get((weapon_id, arc_id))


def load_height_bands(path: Path = HEIGHT_FILE) -> dict[tuple[str, str], HeightBand]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    bands = {}
    for arc_id, a in raw.get("arcs", {}).items():
        bands[(a["weapon"], arc_id)] = HeightBand(
            a["weapon"], arc_id, float(a["minDistanceMeters"]), float(a["maxDistanceMeters"]),
            float(a["maxAbsDeltaZMeters"]), tuple((float(d), float(s)) for d, s in a["milPerMeter"]))
    return bands


def load_data(path: Path = DATA_FILE) -> GameData:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    weapons: dict[str, Weapon] = {}
    for w in raw["weapons"]:
        arcs = tuple(
            Arc(a["id"], a["label"], tuple(sorted((float(r), float(m)) for r, m in a["table"])))
            for a in w["arcs"]
        )
        weapons[w["id"]] = Weapon(w["id"], w["label"], float(w["range"]["minM"]), float(w["range"]["maxM"]),
                                  arcs, w.get("accuracyMoa"),
                                  tuple(float(v) for v in w.get("scopeRangeLines", ())))
    maps = tuple(
        MapInfo(m["id"], m["label"], m["bounds"]["minX"], m["bounds"]["maxX"], m["bounds"]["minY"], m["bounds"]["maxY"])
        for m in raw.get("maps", [])
    )
    return GameData(float(raw["metersPerUnit"]), weapons, maps, load_height_bands())


def distance_m(a: Point, b: Point, meters_per_unit: float = 100.0) -> float:
    return math.hypot(b.x - a.x, b.y - a.y) * meters_per_unit


def azimuth_deg(a: Point, b: Point) -> float:
    """Compass bearing from a to b: 0 = north (+Y), 90 = east (+X)."""
    deg = math.degrees(math.atan2(b.x - a.x, b.y - a.y))
    return (deg + 360.0) % 360.0 if deg < 0 else deg


def azimuth_mil(deg: float, per_circle: int = 6400) -> float:
    return deg * per_circle / 360.0


def compass_point(deg: float) -> str:
    """Eight-way compass sector, each centred on its letter (N is 337.5-22.5)."""
    return COMPASS[round((deg % 360.0) / 45.0) % len(COMPASS)]


def elevation_mil(table: tuple[tuple[float, float], ...], range_m: float) -> tuple[float, float] | None:
    """Linear interpolation between the bracketing rows; None outside the table.

    Returns (min_mil, max_mil); they differ only on an exact hit of a range that
    the table lists twice (SPH-2 high arc lists 610 and 620 mil at 2629 m).
    """
    if not math.isfinite(range_m):
        return None
    hits = [mil for r, mil in table if abs(r - range_m) <= RANGE_EPS]
    if hits:
        return min(hits), max(hits)
    for (r0, m0), (r1, m1) in zip(table, table[1:]):
        if r0 < range_m < r1:
            mil = m0 + (range_m - r0) * (m1 - m0) / (r1 - r0)
            return mil, mil
    return None


def spread_m(distance: float, moa: float | None) -> float | None:
    """Radius the shells scatter over at this range, from the gun's accuracy in MOA."""
    if not moa:
        return None
    return distance * math.tan(math.radians(moa / 60.0))


def range_at_mil(arc: Arc, mil: float) -> float | None:
    """Read a firing table backwards: the range an arc reaches at a given elevation."""
    pts = sorted((m, r) for r, m in arc.table)
    for (m0, r0), (m1, r1) in zip(pts, pts[1:]):
        if m0 <= mil <= m1:
            return r0 if m1 == m0 else r0 + (mil - m0) * (r1 - r0) / (m1 - m0)
    return None


def solve(weapon: Weapon, gun: Point, target: Point, meters_per_unit: float = 100.0,
          dz: float = 0.0, bands: dict[tuple[str, str], HeightBand] | None = None, game=None) -> Solution:
    """`dz` is how far the target sits above the gun, in metres. `game` (a GameTable) supplies
    the game's own elevation wherever its sight has been read; elsewhere the table is used."""
    dist = distance_m(gun, target, meters_per_unit)
    # The tables overhang the usable envelope, so range limits come from the weapon.
    if dist + RANGE_EPS < weapon.min_m:
        status, off = "too_close", weapon.min_m - dist
    elif dist > weapon.max_m + RANGE_EPS:
        status, off = "too_far", dist - weapon.max_m
    else:
        status, off = "ok", 0.0
    arcs: list[ArcSolution] = []
    if status == "ok":
        for arc in weapon.arcs:
            el = elevation_mil(arc.table, dist)
            if el is None:
                continue
            gm = game.mil_for(arc, dist) if game is not None else None
            source = "table"
            if gm is not None:
                el, source = (gm, gm), "game"
            band = (bands or {}).get((weapon.id, arc.id))
            slope = band.slope(dist) if band else None
            applied = slope * dz if (slope is not None and dz) else 0.0
            beyond = bool(band and dz and abs(dz) > band.max_abs_dz)
            arcs.append(ArcSolution(arc.id, arc.label, el[0] + applied, el[1] + applied,
                                    slope, applied, beyond, source))
    return Solution(
        distance_m=dist,
        azimuth_deg=azimuth_deg(gun, target),
        dx_m=(target.x - gun.x) * meters_per_unit,
        dy_m=(target.y - gun.y) * meters_per_unit,
        status=status,
        off_by_m=off,
        arcs=tuple(arcs),
    )


def fire_axes(gun: Point, aim: Point) -> tuple[tuple[float, float], tuple[float, float]]:
    """Unit vectors (east, north) along the line of fire and to its right."""
    th = math.radians(azimuth_deg(gun, aim))
    return (math.sin(th), math.cos(th)), (math.cos(th), -math.sin(th))


def shift_aim(gun: Point, aim: Point, add_m: float = 0.0, right_m: float = 0.0,
              meters_per_unit: float = 100.0) -> Point:
    """Move the aim point further (add > 0) or shorter, and right (> 0) or left, in metres."""
    (ax, ay), (rx, ry) = fire_axes(gun, aim)
    return Point(aim.x + (ax * add_m + rx * right_m) / meters_per_unit,
                 aim.y + (ay * add_m + ry * right_m) / meters_per_unit)


def miss_components(gun: Point, aim: Point, point: Point, meters_per_unit: float = 100.0) -> tuple[float, float]:
    """Where `point` sits relative to `aim` seen from the gun, in metres: (long > 0, right > 0)."""
    (ax, ay), (rx, ry) = fire_axes(gun, aim)
    dx, dy = (point.x - aim.x) * meters_per_unit, (point.y - aim.y) * meters_per_unit
    return dx * ax + dy * ay, dx * rx + dy * ry


def bracket(lines: tuple[float, ...], value: float) -> tuple[float, float, float] | None:
    """The two marks either side of `value` and how far between them it sits (0-1)."""
    for lo, hi in zip(lines, lines[1:]):
        if lo <= value <= hi:
            return lo, hi, 0.0 if hi == lo else (value - lo) / (hi - lo)
    return None


def corrected_aim(aim: Point, target: Point, impact: Point) -> Point:
    """Walk the aim point onto the target after an observed impact.

    The shell aimed at `aim` landed at `impact`. Treating that miss as a steady
    bias (height difference, table error), aiming the same miss vector short of
    the target lands on it: new aim = aim + (target - impact).
    """
    return Point(aim.x + (target.x - impact.x), aim.y + (target.y - impact.y))
