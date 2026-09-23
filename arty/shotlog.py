"""A record of real shots, so the firing solutions can be checked against the game.

Every impact you read with F9 becomes one row: what the app told you to dial and
where the shell actually landed, split into range error (long/short) and
deflection (left/right) along the line of fire. Averaged per weapon and arc,
that shows whether the tables run long or short in the live game.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime
from pathlib import Path


@dataclass
class Shot:
    time: str
    weapon: str
    arc: str
    distance_m: float  # gun to the aim point
    azimuth_deg: float
    mil: float  # the elevation the app gave for that arc
    dz_m: float  # height difference used (0 = flat)
    gun_x: float
    gun_y: float
    target_x: float
    target_y: float
    aim_x: float
    aim_y: float
    impact_x: float
    impact_y: float
    long_m: float  # how far past the aim point it landed; negative = short
    right_m: float  # how far right of the line of fire; negative = left
    miss_m: float  # straight-line miss from the aim point
    to_target_m: float  # straight-line miss from the target itself
    read_confident: bool  # whether the impact coordinates were a confirmed read
    # Added with gun trim; older logs lack them (None = "not recorded", filled in on load).
    fire_x: float | None = None  # what the gun was actually dialed to: the aim point plus any trim
    fire_y: float | None = None
    height_mil: float | None = None  # part of `mil` that was a height correction
    trim_add_m: float = 0.0  # learned trim applied to this shot, metres further
    trim_right_m: float = 0.0  # and metres right
    # What the gun sight actually showed at the last dial-assist reading before the impact
    # was marked (dial assist on): what you DIALED, against what the app told you.
    sight_mil: float | None = None
    sight_heading: float | None = None
    sight_age_s: float | None = None  # how old that reading was when F9 was pressed
    sight_stabilized: float | None = None  # 1 / 0
    tilt_left: float | None = None  # tilt pips, dashes below (+) the middle mark
    tilt_right: float | None = None
    terrain_dz: float | None = None  # target minus gun ground height from the map's terrain
    terrain_map: str = ""

    @property
    def dialed(self) -> bool:
        return self.sight_mil is not None and self.sight_heading is not None

    @property
    def fire(self) -> tuple[float, float]:
        return (self.aim_x if self.fire_x is None else self.fire_x,
                self.aim_y if self.fire_y is None else self.fire_y)


FIELDS = [f.name for f in fields(Shot)]
OPTIONAL = {f.name for f in fields(Shot) if f.default is not MISSING}


@dataclass(frozen=True)
class Summary:
    weapon: str
    arc: str
    shots: int
    mean_long_m: float
    sd_long_m: float
    mean_right_m: float
    median_miss_m: float
    median_distance_m: float

    @property
    def biased(self) -> bool:
        """Mean range error beyond two standard errors: the tables, not luck (needs 3+ shots)."""
        if self.shots < 3:
            return False
        return abs(self.mean_long_m) > 2 * self.sd_long_m / math.sqrt(self.shots)


class ShotLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.shots: list[Shot] = self._load()

    def _load(self) -> list[Shot]:
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except FileNotFoundError:
            return []
        out = []
        for row in rows:
            try:
                out.append(Shot(**{k: _coerce(k, row[k]) for k in FIELDS
                                   if not (k in OPTIONAL and row.get(k) in (None, ""))}))
            except (KeyError, ValueError):
                continue  # a hand-edited or older row; skip rather than refuse the whole log
        return out

    def add(self, shot: Shot) -> None:
        self.shots.append(shot)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        if not new:
            self._upgrade_header()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(asdict(shot))

    def _upgrade_header(self) -> None:
        """A log from an older version gets the new columns (blank) before a row is added,
        so every row stays under the right heading. The old file is kept as .bak."""
        with open(self.path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames == FIELDS:
                return
            rows = list(reader)
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        backup.write_bytes(self.path.read_bytes())
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    def start_new_session(self) -> Path | None:
        """Set the current log aside (renamed, never deleted) and start an empty one."""
        if not self.path.exists():
            return None
        archived = self.path.with_name(f"{self.path.stem}-{datetime.now():%Y%m%d-%H%M%S}{self.path.suffix}")
        self.path.rename(archived)
        self.shots = []
        return archived

    def summaries(self) -> list[Summary]:
        groups: dict[tuple[str, str], list[Shot]] = {}
        for s in self.shots:
            groups.setdefault((s.weapon, s.arc), []).append(s)
        out = []
        for (weapon, arc), shots in sorted(groups.items()):
            longs = [s.long_m for s in shots]
            out.append(Summary(
                weapon, arc, len(shots), statistics.fmean(longs),
                statistics.stdev(longs) if len(longs) > 1 else 0.0,
                statistics.fmean(s.right_m for s in shots),
                statistics.median(s.miss_m for s in shots),
                statistics.median(s.distance_m for s in shots)))
        return out


def _coerce(name: str, value: str):
    if name in ("time", "weapon", "arc", "terrain_map"):
        return value
    if name == "read_confident":
        return value.strip().lower() in ("true", "1", "yes")
    return float(value)
