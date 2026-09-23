"""Gun trim: the steady error of one gun position, learned from its own logged shots.

The firing tables assume a level gun on flat ground. Two errors they can't know
about showed up in real logged shots (2026-09-22, 13 SPH-2 shots from one spot):

* DEFLECTION from a canted gun. Parked on a side slope, the barrel's "up" is
  tilted, so raising it also swings the shot sideways by
  atan(tan(elevation) * sin(cant)). The miss grows with elevation: -0.4 deg at
  286 mil up to -2.8 deg at 571 mil from that one position (a ~3.6 deg cant),
  while the earlier session from other positions sat at ~0. A constant
  left/right offset fitted those shots with 0.61 deg of scatter; the cant
  model with 0.36. Because cant belongs to where the gun is parked, a trim is
  only ever learned from shots fired from the same spot.

* RANGE: where the shell lands versus where the table says the fired mil
  reaches. It mixes table error with the lie of the ground around the targets,
  so it's learned only from shots near the current distance, only from flat
  shots (no height correction applied), and it's switched off whenever a
  height correction is doing that job.

The trim is fitted to what the gun was DIALED to (the fire point) and where it
landed, never to the target, so it stays put once it starts being applied
instead of chasing its own corrections. Neither half is applied until it is
larger than twice its own standard error: 3 noisy shots should not move the gun.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .ballistics import Arc, HeightBand, Point, azimuth_deg, distance_m, range_at_mil

SAME_GUN_M = 50.0  # further than this and it's a new gun position with its own cant
MIN_SHOTS = 3
RANGE_WINDOW_M = 300.0  # range trim only from shots at roughly the same distance
# Weapons whose table mil is a true elevation angle (6400 per circle). SPH-2 low arc tops
# out near 605 mil = 34 deg, a sensible max-range angle for a draggy shell. The L81's
# numbers don't behave as angles, so its deflection is modelled as a plain offset.
ANGLE_MIL = {"sph2"}


@dataclass(frozen=True)
class Obs:
    """One logged shot, reduced to what the gun did."""
    weapon: str
    arc: str
    gun: Point
    fire: Point  # what the gun was dialed to
    impact: Point
    mil: float  # the elevation fired
    height_mil: float  # part of `mil` that was a height correction (0 = flat shot)


@dataclass(frozen=True)
class Trim:
    shots_az: int = 0
    k: float = 0.0  # deflection = atan(k * x), x = tan(elevation) (or 1 for a plain offset)
    k_se: float = 0.0
    angle_model: bool = True
    shots_range: int = 0
    range_m: float = 0.0  # mean landing minus the table's promise; negative = short
    range_se: float = 0.0

    @property
    def az_on(self) -> bool:
        return self.shots_az >= MIN_SHOTS and abs(self.k) > 2 * self.k_se

    @property
    def range_on(self) -> bool:
        return self.shots_range >= MIN_SHOTS and abs(self.range_m) > 2 * self.range_se

    @property
    def active(self) -> bool:
        return self.az_on or self.range_on

    @property
    def cant_deg(self) -> float | None:
        """The side tilt the deflection implies (+ = shots pulled right)."""
        return math.degrees(math.asin(max(-1.0, min(1.0, self.k)))) if self.angle_model else None

    def deflection_deg(self, mil: float) -> float:
        """How far right (+) of the dialed azimuth this gun throws at this elevation."""
        if not self.az_on:
            return 0.0
        return math.degrees(math.atan(self.k * _x(mil, self.angle_model)))

    def offsets(self, distance: float, mil: float, height_applied: bool) -> tuple[float, float]:
        """(add_m, right_m) to move the fire point by so the shell lands on the aim point."""
        add = -self.range_m if self.range_on and not height_applied else 0.0
        right = -distance * math.tan(math.radians(self.deflection_deg(mil)))
        return add, right


def _x(mil: float, angle_model: bool) -> float:
    return math.tan(mil * 2 * math.pi / 6400) if angle_model else 1.0


def learn(obs: list[Obs], weapon: str, arc: Arc, gun: Point, distance: float,
          meters_per_unit: float = 100.0, range_fn=range_at_mil) -> Trim:
    """Fit the trim for firing `weapon`/`arc` from `gun` at about `distance` metres.
    `range_fn(arc, mil)` is where the firing table says a mil lands (the game's own table
    where known), so the range trim is the gun's error, not the table's."""
    angle = weapon in ANGLE_MIL
    mine = [o for o in obs if o.weapon == weapon and o.arc == arc.id
            and distance_m(o.gun, gun, meters_per_unit) <= SAME_GUN_M]
    xs, ys = [], []
    for o in mine:
        daz = (azimuth_deg(o.gun, o.impact) - azimuth_deg(o.gun, o.fire) + 180) % 360 - 180
        xs.append(_x(o.mil, angle))
        ys.append(math.tan(math.radians(daz)))
    k = k_se = 0.0
    if len(xs) >= MIN_SHOTS:
        sxx = sum(x * x for x in xs)
        k = sum(x * y for x, y in zip(xs, ys)) / sxx
        resid = sum((y - k * x) ** 2 for x, y in zip(xs, ys)) / (len(xs) - 1)
        k_se = math.sqrt(resid / sxx)

    misses = []
    for o in mine:
        if o.height_mil:
            continue  # terrain was already being corrected; it would count twice
        if abs(distance_m(o.gun, o.fire, meters_per_unit) - distance) > RANGE_WINDOW_M:
            continue
        promised = range_fn(arc, o.mil)
        if promised is not None:
            misses.append(distance_m(o.gun, o.impact, meters_per_unit) - promised)
    rng = statistics.fmean(misses) if misses else 0.0
    rng_se = statistics.stdev(misses) / math.sqrt(len(misses)) if len(misses) > 1 else 0.0
    return Trim(len(xs), k, k_se, angle, len(misses), rng, rng_se)


def height_mil_for(band: HeightBand | None, distance: float, dz: float) -> float:
    """The height correction a shot got, for log rows written before it was recorded."""
    if band is None or not dz:
        return 0.0
    slope = band.slope(distance)
    return slope * dz if slope is not None else 0.0
