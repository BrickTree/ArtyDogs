"""Pull X/Y tactical-map coordinates out of text.

Handles the game's cursor readout ("X80.07 Y70.54"), squad-chat lines from
Mark Coordinates, calculator share strings ("x100.05, y109.14"), and the usual
OCR damage: a dropped X or Y label, O/0 and l/1 swaps, decimal commas.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

WORLD_MAX = 163.84  # the world is 16384 m square; 1 unit = 100 m

# Characters OCR returns in place of digits, and what they should have been.
_DIGITISH = "0-9OoDQIl|iSsBZz"
_DIGIT_FIX = str.maketrans("OoDQIl|iSsBZz", "0000111155822")
_LX = r"(?<![A-Za-z])(?P<xl>[Xx])\s*[:=]?\s*"
_LY = r"(?<![A-Za-z])(?P<yl>[Yy])\s*[:=]?\s*"


def _num(decimals: str) -> str:
    # Never start or stop mid-number: a clipped "1109.50" must not read as 109.50.
    return rf"(?<![0-9])[{_DIGITISH}]{{1,3}}[.,][{_DIGITISH}]{{{decimals}}}(?![0-9])"


def _pair(decimals: str) -> re.Pattern[str]:
    # Either label may be missing: OCR drops single letters more often than digits.
    return re.compile(rf"(?:{_LX})?(?P<x>{_num(decimals)})\s*[,;/]?\s*(?:{_LY})?(?P<y>{_num(decimals)})")


def _axis(label: str, decimals: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z]){label}\s*[:=]?\s*({_num(decimals)})")


# The tactical map labels its crosshair rulers separately and puts y above x, so
# the two halves arrive in either order and often from different OCR boxes.
_AXIS_PATTERNS = {(a, d): _axis(lab, d) for a, lab in (("x", "[Xx]"), ("y", "[Yy]"))
                  for d in ("2", "1,2")}


_PAIR = _pair("1,2")
# The game prints two decimals, so a screen read with fewer was clipped or misread.
_PAIR_STRICT = _pair("2")
# Both labels present but the decimal point was lost ("X8007 Y7054").
_PAIR_NO_POINT = re.compile(rf"{_LX}(?P<x>\d{{3,5}})\s*[,;/]?\s*{_LY}(?P<y>\d{{3,5}})(?![0-9])")

_LOOKALIKES = str.maketrans({
    "×": "X", "Х": "X", "х": "x", "¥": "Y", "У": "Y", "у": "y",
    "·": ".", "。": ".", "˙": ".", "，": ",", "：": ":",
})


@dataclass(frozen=True)
class Coord:
    x: float
    y: float
    labeled: bool  # both the X and the Y label were read
    repaired: bool  # a character had to be guessed to read it
    start: int  # span in the normalised text
    end: int


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_LOOKALIKES)


def _number(raw: str) -> tuple[float, bool] | None:
    """Parse an OCR number token; returns (value, repaired) or None."""
    if sum(c.isdigit() for c in raw) < 2:
        return None
    fixed = raw.translate(_DIGIT_FIX).replace(",", ".")
    try:
        value = float(fixed)
    except ValueError:
        return None
    return value, fixed != raw.replace(",", ".")


def _plausible(v: float) -> bool:
    return 0.0 <= v <= WORLD_MAX + 1.0


@dataclass(frozen=True)
class AxisHit:
    axis: str  # "x" or "y"
    value: float
    repaired: bool
    start: int
    end: int


def axis_hits(text: str, axis: str, strict: bool = False) -> list[AxisHit]:
    """Every labelled value for one axis, e.g. the "y73.39" half of the readout."""
    out: list[AxisHit] = []
    for m in _AXIS_PATTERNS[(axis, "2" if strict else "1,2")].finditer(text):
        n = _number(m.group(1))
        if n is not None and _plausible(n[0]):
            out.append(AxisHit(axis, n[0], n[1], m.start(), m.end()))
    return out


# The map's distance ruler ("100M", "-100M", "1KM") sits close enough to the coordinate
# labels that OCR sometimes runs them together: "y77.78-100M".
_RULER_MARK = re.compile(r"[-–]?\s*\d+(?:[.,]\d+)?\s*K?M\b", re.IGNORECASE)


def single_axis(text: str, strict: bool = True) -> AxisHit | None:
    """One labelled value and nothing else, the way the map prints each ruler label."""
    t = normalize(text)
    hits = axis_hits(t, "x", strict) + axis_hits(t, "y", strict)
    if len(hits) != 1:
        return None
    hit = hits[0]
    rest = _RULER_MARK.sub("", t[:hit.start] + " " + t[hit.end:])
    return hit if len(rest.replace(" ", "")) <= 2 else None


def bare_number(text: str) -> float | None:
    """A two-decimal number on its own, e.g. an "x98.76" whose "x" OCR dropped."""
    t = _RULER_MARK.sub("", normalize(text)).strip()
    m = re.fullmatch(r"(\d{1,3}[.,]\d{2})", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if _plausible(v) else None


def _span_gap(a: AxisHit, b: AxisHit) -> int:
    return max(b.start - a.end, a.start - b.end, 0)


def find_coords(text: str, strict: bool = False) -> list[Coord]:
    """Every coordinate pair in `text`, in reading order.

    `strict` demands exactly two decimals, as the game prints them; use it for
    screen reads, where a shorter number means the text was clipped.
    """
    t = normalize(text)
    found: list[Coord] = []
    # Labelled values first, pairing each x with its nearest y whichever comes first:
    # the map prints "y73.39" above "x81.57", chat prints "X.. Y..".
    ys = axis_hits(t, "y", strict)
    taken: set[int] = set()
    for hx in axis_hits(t, "x", strict):
        free = [(i, h) for i, h in enumerate(ys) if i not in taken]
        if not free:
            break
        i, hy = min(free, key=lambda p: _span_gap(hx, p[1]))
        taken.add(i)
        found.append(Coord(hx.value, hy.value, True, hx.repaired or hy.repaired,
                           min(hx.start, hy.start), max(hx.end, hy.end)))
    for m in (_PAIR_STRICT if strict else _PAIR).finditer(t):
        if any(c.start < m.end() and m.start() < c.end for c in found):
            continue  # already read as a labelled pair
        nx, ny = _number(m.group("x")), _number(m.group("y"))
        if nx is None or ny is None or not (_plausible(nx[0]) and _plausible(ny[0])):
            continue
        found.append(Coord(nx[0], ny[0], bool(m.group("xl") and m.group("yl")),
                           nx[1] or ny[1], m.start(), m.end()))
    for m in _PAIR_NO_POINT.finditer(t):
        if any(c.start <= m.start() < c.end for c in found):
            continue
        x = float(m.group("x")[:-2] + "." + m.group("x")[-2:])
        y = float(m.group("y")[:-2] + "." + m.group("y")[-2:])
        if _plausible(x) and _plausible(y):
            found.append(Coord(x, y, True, True, m.start(), m.end()))
    found.sort(key=lambda c: c.start)
    return found


def best_coord(text: str, strict: bool = False) -> Coord | None:
    """The most trustworthy pair in `text`: labelled beats unlabelled, clean beats repaired."""
    coords = find_coords(text, strict)
    if not coords:
        return None
    return min(coords, key=lambda c: (not c.labeled, c.repaired, c.start))


# Deliberately sloppy: used only to notice when a re-read disagrees, never to read a value.
_LOOSE_X = re.compile(rf"[Xx]\s*[:=]?\s*([{_DIGITISH}][{_DIGITISH}.,]{{2,7}})")
_LOOSE_Y = re.compile(rf"[YyVv¥]\s*[:=]?\s*([{_DIGITISH}][{_DIGITISH}.,]{{2,7}})")


def loose_digits(text: str) -> tuple[str | None, str | None]:
    """The digit runs after the X and Y labels, e.g. "X2047V56.77" -> ("2047", "5677").

    Survives a lost decimal point or a Y misread as V, which the real parser rejects.
    """
    t = normalize(text)
    mx = _LOOSE_X.search(t)
    my = _LOOSE_Y.search(t)
    if mx and my and mx.start() < my.end() and my.start() < mx.end():
        my = _LOOSE_Y.search(t, mx.end())  # both claimed the same text; give it to x

    def digits(m: re.Match[str] | None) -> str | None:
        if m is None or sum(c.isdigit() for c in m.group(1)) < 2:
            return None
        return re.sub(r"\D", "", m.group(1).translate(_DIGIT_FIX)).lstrip("0") or "0"

    return digits(mx), digits(my)


# The HUD prints the vehicle's height above sea level, e.g. "ASL 124". The label is
# faint, so OCR often returns "AS 124" or "A5L 124"; the number is what matters.
# "1" is only part of the label when no digits follow it, or "AS 124" reads as 24.
# A full "ASL" may have a stray letter glued on from a neighbouring icon ("DASL22m").
_ASL = re.compile(r"(?:ASL|(?<![A-Za-z])A\s*[S5]\s*(?:L|I|1(?![0-9]))?)\s*[/|:=]?\s*(-?\d{1,4})(?![0-9])",
                  re.IGNORECASE)


def find_asl(text: str) -> int | None:
    m = _ASL.search(normalize(text))
    return int(m.group(1)) if m else None


def value_digits(v: float) -> str:
    """The digits a two-decimal readout shows for v: 20.47 -> "2047"."""
    return f"{v:.2f}".replace(".", "").lstrip("0") or "0"


def parse_number(text: str) -> float | None:
    """A single typed coordinate value such as '80.07', '80,07' or 'X80.07'."""
    t = normalize(text).strip().lstrip("XxYy:= ").replace(",", ".")
    if not re.fullmatch(r"\d{1,3}(?:\.\d*)?", t):
        return None
    v = float(t)
    return v if _plausible(v) else None
