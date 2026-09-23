"""Read the SPH-2 gunner sight: the elevation and range at the reticle, the heading, stability.

The sight labels its own scales ("1,020 mil" beside "RNG 2,130m", a heading tape
"255 | 270 | 286 W | 300"), so nothing has to be calibrated: two labelled marks on
a ladder fix its scale, and the value at the reticle is read off the line through
them. That keeps working when you zoom, which changes the spacing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .coords import find_asl
from .ocr import Ocr, _merge_lines

# Regions as fractions of the screen (left, top, right, bottom), measured on the
# 1920x1080 sight; generous, since labels move as you change elevation.
# Only the band around the reticle: two marks either side are all a ladder needs.
MIL_LADDER = (0.58, 0.22, 0.78, 0.78)
RNG_LADDER = (0.24, 0.22, 0.42, 0.78)
HEADING_TAPE = (0.20, 0.0, 0.80, 0.065)
STATUS_LINE = (0.36, 0.76, 0.64, 0.90)  # "Unstabilized" and "ASL 230"
MIL_STEP = 10  # the SPH-2 sight labels every 10 mil; lower marks are higher elevations
# The tilt pips beside the vehicle silhouette, in 1080p pixels from the screen's centre
# column: two scales of five dashes, each with a triangle riding on it. A level vehicle
# has both triangles on the middle dash.
PIP_ROWS = (870, 985)  # search band, y
PIP_DASH_DX = 75  # dash centre, left of centre (-) and right (+)
PIP_TRI_DX = (79, 85)  # the triangle sits on the outer side of its dashes

# "1,020 mil" / "980 mil"; a tick glyph in front often reads as a stray "1" ("11,020mil").
_MIL = re.compile(r"(\d)[,.](\d{3})\s*mil|(?<![\d,.])(\d{2,3})\s*mil", re.IGNORECASE)
# "2,130m" (OCR often gives "2.130m"); never the "m" of "mil".
_RNG = re.compile(r"(\d)[,.](\d{3})\s*m(?!il)|(?<![\d,.])(\d{3})\s*m(?!il)", re.IGNORECASE)
_HEADING = re.compile(r"(?<!\d)(\d{3})(?!\d)")


@dataclass
class SightReading:
    mil: float | None = None  # elevation at the reticle
    range_m: float | None = None  # the sight's own range for that elevation
    heading: float | None = None  # degrees, 0 = north
    stabilized: bool | None = None  # False while "Unstabilized" shows
    asl: int | None = None  # the gun's own height above sea level
    table: list[tuple[float, float]] = field(default_factory=list)  # (mil, range) pairs the sight printed
    # Where values sit on screen, for drawing markers: (value, pixel, value per pixel) lines.
    mil_scale: tuple[float, float, float] | None = None  # y
    heading_scale: tuple[float, float, float] | None = None  # x, on the unwrapped tape
    mil_label_x: float | None = None  # left edge of the mil labels
    # Tilt pips: how far each triangle sits below (+) or above (-) the middle dash, in dashes.
    tilt: tuple[float, float] | None = None
    pips: tuple[float, float, float, float] | None = None  # (centre y, left x, right x, dash spacing) px

    def mil_y(self, mil: float) -> float | None:
        return _pixel(self.mil_scale, mil)

    def heading_x(self, heading: float) -> float | None:
        if self.heading_scale is None:
            return None
        v0 = self.heading_scale[0]
        return _pixel(self.heading_scale, min((heading + k * 360 for k in (-1, 0, 1)), key=lambda v: abs(v - v0)))

    @property
    def found(self) -> bool:
        return self.mil is not None or self.heading is not None


def _value(m: re.Match[str]) -> float:
    return float(m.group(1) + m.group(2)) if m.group(1) else float(m.group(3))


def _crop(shot: Image.Image, frac: tuple[float, float, float, float]) -> tuple[Image.Image, int, int]:
    w, h = shot.size
    l, t, r, b = int(frac[0] * w), int(frac[1] * h), int(frac[2] * w), int(frac[3] * h)
    return shot.crop((l, t, r, b)), l, t


def _marks(ocr: Ocr, shot: Image.Image, frac, pattern: re.Pattern[str], axis: str, scale: float = 1.0):
    """(value, position) for every labelled mark in a region; position is y for ladders, x for the tape."""
    img, ox, oy = _crop(shot, frac)
    out = []
    for b in ocr.read(img, scale):
        m = pattern.search(b.text)
        if not m:
            continue
        cy = (b.box[1] + b.box[3]) / 2 + oy
        cx = (b.box[0] + b.box[2]) / 2 + ox
        out.append((_value(m), cy if axis == "y" else cx, b.text, b.box[0] + ox))
    return out


def line_fit(marks: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """(mean value, mean pixel, value per pixel) through labelled marks, or None if they
    don't sit on one straight scale (a misread label breaks the line)."""
    pts = sorted(set(marks), key=lambda p: p[1])
    if len(pts) < 2 or pts[-1][1] - pts[0][1] <= 10:
        return None
    n = len(pts)
    mean_p = sum(p for _v, p in pts) / n
    mean_v = sum(v for v, _p in pts) / n
    sxx = sum((p - mean_p) ** 2 for _v, p in pts)
    slope = sum((p - mean_p) * (v - mean_v) for v, p in pts) / sxx
    if slope == 0:
        return None
    if max(abs(v - (mean_v + slope * (p - mean_p))) for v, p in pts) > abs(slope) * 8 + 2:
        return None
    return mean_v, mean_p, slope


def _pixel(scale: tuple[float, float, float] | None, value: float) -> float | None:
    return None if scale is None else scale[1] + (value - scale[0]) / scale[2]


def value_at(marks: list[tuple[float, float]], pos: float, max_gap: float = 6.0) -> float | None:
    """Read a linear scale at `pos` from its labelled marks (needs two, or one right at `pos`)."""
    pts = sorted(set(marks), key=lambda p: p[1])
    if len(pts) >= 2 and pts[-1][1] - pts[0][1] > 10:
        fit = line_fit(pts)
        return None if fit is None else fit[0] + fit[2] * (pos - fit[1])
    near = [v for v, p in pts if abs(p - pos) <= max_gap]
    return near[0] if near else None


def read_tilt(shot: Image.Image) -> tuple[tuple[float, float], tuple[float, float, float, float]] | None:
    """Read the two tilt pips: each triangle's offset from its scale's middle dash, in dashes.

    Pure pixels, no OCR: the dashes and triangles are small light-grey marks with no
    text. Returns None unless both scales show 3+ evenly spaced dashes and one triangle.
    """
    w, h = shot.size
    s = h / 1080
    cx = w / 2
    top, bottom = int(PIP_ROWS[0] * s), int(PIP_ROWS[1] * s)
    x_lo, x_hi = int(cx - (PIP_TRI_DX[1] + 6) * s), int(cx + (PIP_TRI_DX[1] + 6) * s)
    px = np.asarray(shot.crop((x_lo, top, x_hi, bottom)).convert("RGB")).astype(np.int16)
    light = (px.min(axis=2) > 130) & (px.max(axis=2) - px.min(axis=2) < 45)

    def rows_in(x_from: float, x_to: float) -> np.ndarray:
        a, b = int(round(x_from - x_lo)), int(round(x_to - x_lo)) + 1
        return light[:, max(a, 0):max(b, 1)].sum(axis=1)

    def clusters(counts: np.ndarray, min_count: int) -> list[tuple[float, int]]:
        """(weighted centre row, height) of each run of rows with enough light pixels."""
        out, start = [], None
        for y, c in enumerate(list(counts) + [0]):
            if c >= min_count and start is None:
                start = y
            elif c < min_count and start is not None:
                seg = counts[start:y].astype(float)
                out.append((top + start + float((np.arange(len(seg)) * seg).sum() / seg.sum()), y - start))
                start = None
        return out

    offsets, geo = [], []
    for side in (-1, 1):
        dash_x = cx + side * PIP_DASH_DX * s
        dashes = [y for y, n in clusters(rows_in(dash_x - 3 * s, dash_x + 3 * s), max(3, int(3 * s)))
                  if n <= 4 * s]
        if len(dashes) < 3:
            return None
        gaps = np.diff(dashes)
        spacing = float(np.median(gaps))
        if not (10 * s <= spacing <= 26 * s) or np.max(np.abs(gaps / spacing - np.round(gaps / spacing))) > 0.2:
            return None
        a, b = sorted((cx + side * PIP_TRI_DX[0] * s, cx + side * PIP_TRI_DX[1] * s))
        tris = [(y, n) for y, n in clusters(rows_in(a - 3 * s, b + 3 * s), 1) if 6 * s <= n <= 18 * s]
        if len(tris) != 1:
            return None
        # The scale is symmetric about its middle dash: the real dash nearest the centre
        # of the ones found.
        mid = min(dashes, key=lambda y: abs(y - (dashes[0] + dashes[-1]) / 2))
        offsets.append(round((tris[0][0] - mid) / spacing, 2))
        geo.append((mid, spacing))
    (mid_l, sp_l), (mid_r, sp_r) = geo
    return (offsets[0], offsets[1]), ((mid_l + mid_r) / 2, cx - PIP_TRI_DX[1] * s, cx + PIP_TRI_DX[1] * s,
                                      (sp_l + sp_r) / 2)


def _tape_marks(ocr: Ocr, shot: Image.Image) -> list[tuple[float, float]]:
    """(heading, x) for each printed mark on the compass tape.

    The strip is so wide and thin that OCR reads it as one run of text with no
    positions, so it is read in short overlapping tiles. The boxed "286 W" in the
    middle is the heading rounded down, not a mark, so it is left out of the fit.
    """
    w, h = shot.size
    l, t, r, b = (int(HEADING_TAPE[0] * w), int(0.014 * h), int(HEADING_TAPE[2] * w), int(0.054 * h))
    tile, step = int(0.125 * w), int(0.083 * w)
    marks: dict[float, list[float]] = {}
    for x0 in range(l, r - step, step):
        for bx in ocr.read(shot.crop((x0, t, min(x0 + tile, r), b)), 2.0):
            text = bx.text.strip()
            if re.search(r"[NESWnesw]", text):  # the boxed current heading, e.g. "286 W"
                continue
            m = _HEADING.fullmatch(text)
            if m and 0 <= int(m.group(1)) < 360:
                marks.setdefault(float(m.group(1)), []).append(x0 + (bx.box[0] + bx.box[2]) / 2)
    return [(v, sum(xs) / len(xs)) for v, xs in marks.items()]


def _unwrap(tape: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """345 | 000 | 015 on one tape is 345 | 360 | 375."""
    if tape and max(v for v, _ in tape) - min(v for v, _ in tape) > 180:
        return [(v + 360 if v < 180 else v, x) for v, x in tape]
    return tape


def read_sight(ocr: Ocr, shot: Image.Image) -> SightReading:
    w, h = shot.size
    centre_y, centre_x = h / 2, w / 2
    reading = SightReading()

    mil_marks = [(v, y, x) for v, y, _t, x in _marks(ocr, shot, MIL_LADDER, _MIL, "y", 1.5) if 0 <= v <= 1600]
    mils = [(v, y) for v, y, _x in mil_marks]
    rngs = [(v, y) for v, y, _t, _x in _marks(ocr, shot, RNG_LADDER, _RNG, "y", 1.5) if 50 <= v <= 5000]
    reading.mil = value_at(mils, centre_y)
    reading.mil_scale = line_fit(mils)
    reading.mil_label_x = min((x for _v, _y, x in mil_marks), default=None)
    if reading.mil is None and len(mils) == 1 and len(rngs) >= 2:
        # The ladders share rows, one per MIL_STEP. The range ladder usually reads cleanly,
        # so its row spacing places a lone mil mark relative to the reticle.
        ys = sorted(y for _v, y in rngs)
        row = sorted(b - a for a, b in zip(ys, ys[1:]))[len(ys) // 2 - 1 if len(ys) > 2 else 0]
        if row > 10:
            v, y = mils[0]
            reading.mil = v + (centre_y - y) / row * MIL_STEP
    reading.range_m = value_at(rngs, centre_y)
    # Marks on the two ladders at the same height are one row of the game's range table.
    reading.table = sorted({(mv, rv) for mv, my in mils for rv, ry in rngs if abs(my - ry) <= 12})

    tape = _unwrap(_tape_marks(ocr, shot))
    heading = value_at(tape, centre_x)
    reading.heading_scale = line_fit(tape)
    reading.heading = None if heading is None else heading % 360

    status, _ox, _oy = _crop(shot, STATUS_LINE)
    boxes = ocr.read(status, 1.5)
    words = " ".join(b.text.lower() for b in boxes)
    reading.asl = next((v for text in [b.text for b in boxes] + [t for t, _ in _merge_lines(boxes)]
                        if (v := find_asl(text)) is not None), None)
    if "unstab" in words:
        reading.stabilized = False
    elif "stabil" in words:
        reading.stabilized = True
    tilt = read_tilt(shot)
    if tilt is not None:
        reading.tilt, reading.pips = tilt
    return reading
