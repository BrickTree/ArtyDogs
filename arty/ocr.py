"""Find and read the tactical map's coordinate readout on a screenshot.

The game prints the X/Y under the mouse cursor while the tactical map is open.
We do not know in advance where on screen that readout sits (next to the
cursor, or a fixed corner), so the reader searches outward from the cursor and
remembers where it found the text:

  1. the learned spot (fixed on screen, or at a fixed offset from the cursor)
  2. a window around the cursor
  3. the whole monitor

The closest coordinate-shaped text to the cursor wins, so hovering over a
squad-chat line from "Mark Coordinates" reads that line instead.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .coords import Coord, bare_number, find_asl, find_coords, loose_digits, single_axis, value_digits

Rect = tuple[int, int, int, int]  # left, top, right, bottom (right/bottom exclusive)


@dataclass(frozen=True)
class TextBox:
    text: str
    score: float
    box: Rect  # in the coordinates of the image that was read


class Ocr:
    """RapidOCR (PP-OCRv3 on onnxruntime), tuned for short horizontal HUD text."""

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        try:
            # Detect at the size we hand it (we upscale small crops ourselves);
            # HUD text is never rotated, so skip the angle classifier.
            self._engine = RapidOCR(use_angle_cls=False, det_model_path=None,
                                    det_limit_side_len=2600, det_limit_type="max")
        except Exception:
            self._engine = RapidOCR()
            self._engine.use_angle_cls = False

    def recognize_line(self, img: Image.Image) -> str:
        """Recognition only, for a crop already known to hold one line of text."""
        bgr = np.ascontiguousarray(np.asarray(img.convert("RGB"))[:, :, ::-1])
        res, _ = self._engine.text_recognizer([bgr])
        return res[0][0] if res else ""

    def read(self, img: Image.Image, scale: float = 1.0) -> list[TextBox]:
        src = img if scale == 1.0 else img.resize(
            (round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        bgr = np.ascontiguousarray(np.asarray(src.convert("RGB"))[:, :, ::-1])
        result, _ = self._engine(bgr)
        boxes = []
        for pts, text, score in result or []:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            boxes.append(TextBox(text, float(score), (
                math.floor(min(xs) / scale), math.floor(min(ys) / scale),
                math.ceil(max(xs) / scale), math.ceil(max(ys) / scale))))
        return boxes


@dataclass
class Candidate:
    coord: Coord
    text: str
    box: Rect  # screen coordinates
    bare: bool  # nothing but the coordinates, like the map readout (chat lines carry names)
    preferred: bool  # bare, or the cursor sits right on it
    rank: float  # lower is better within a preference tier
    text_h: int = 0  # height of one line of the text, not of a stacked pair
    inferred: str = ""  # an axis taken from its position because its label was unreadable


@dataclass
class ReadResult:
    coord: Coord | None
    text: str = ""
    box: Rect | None = None  # screen coordinates of the text that was read
    crop: Image.Image | None = None  # that text, for a thumbnail
    region: str = ""  # which search stage found it
    ms: float = 0.0
    message: str = ""
    votes: int = 0  # re-reads agreeing on both numbers
    reads: int = 0  # re-reads attempted
    dissent: list[tuple[float, float]] = field(default_factory=list)  # other values some reads produced
    mismatch: bool = False  # a re-read disagreed but was too mangled to quote
    direct: bool = True  # the map readout, or text right under the cursor (not a far-off chat line)
    seen: list[tuple[str, Rect]] = field(default_factory=list)  # everything OCR saw, screen coords

    @property
    def confident(self) -> bool:
        # A wrong coordinate wastes a shell or lands it on friendlies, so any
        # disagreement at all means a human should glance at the thumbnail. The game
        # always prints both labels; a missing one means something covered or mangled
        # the text, and every re-read of the same pixels would agree on the damage.
        return (self.coord is not None and self.direct and self.votes >= 3 and not self.dissent
                and not self.mismatch and not self.coord.repaired and self.coord.labeled)


def _merge_lines(boxes: list[TextBox]) -> list[tuple[str, Rect]]:
    """Join boxes that sit on one text line, in case OCR split "X80.07" from "Y70.54"."""
    rows: list[list[TextBox]] = []
    for b in sorted(boxes, key=lambda b: (b.box[1] + b.box[3]) / 2):
        cy, h = (b.box[1] + b.box[3]) / 2, b.box[3] - b.box[1]
        for row in rows:
            r = row[0].box
            if abs((r[1] + r[3]) / 2 - cy) < 0.5 * max(h, r[3] - r[1]):
                row.append(b)
                break
        else:
            rows.append([b])
    lines: list[tuple[str, Rect]] = []
    for row in rows:
        row.sort(key=lambda b: b.box[0])
        chunk = [row[0]]
        for prev, cur in zip(row, row[1:]):
            height = max(prev.box[3] - prev.box[1], cur.box[3] - cur.box[1])
            if cur.box[0] - prev.box[2] > 2.5 * height:
                lines.append(_join(chunk))
                chunk = []
            chunk.append(cur)
        lines.append(_join(chunk))
    return [ln for ln in lines if ln is not None]


def _join(chunk: list[TextBox]) -> tuple[str, Rect] | None:
    if len(chunk) < 2:
        return None
    return (" ".join(b.text for b in chunk),
            (min(b.box[0] for b in chunk), min(b.box[1] for b in chunk),
             max(b.box[2] for b in chunk), max(b.box[3] for b in chunk)))


def _digits_differ(read: str | None, winner: str) -> bool:
    """True when a re-read's digits contradict the winner's.

    A clipped read is a prefix ("202" of "2027") and proves nothing; a read that
    diverges ("204" against "2027") means one of them misread a digit.
    """
    if not read:
        return False
    return not (read.startswith(winner) or winner.startswith(read))


def _upscale(img: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return img
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)


def _text_mask(img: Image.Image, line_h: float) -> Image.Image:
    """Black text on white, keeping only thin bright strokes.

    A white top-hat (the image minus its opening) drops anything wider than a
    glyph stroke, so sunlit terrain and map labels fall away while the letters
    stay. Brightness alone can't do this: terrain is often as bright as the text.
    """
    g = ImageOps.grayscale(img)
    k = max(3, min(21, int(line_h // 4) | 1))  # wider than a stroke, narrower than a glyph
    opened = g.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))
    tophat = ImageChops.subtract(g, opened)
    peak = tophat.getextrema()[1] or 1
    cut = max(40, int(peak * 0.45))
    return tophat.point(lambda v: 0 if v >= cut else 255).convert("RGB")


def _without_vertical_lines(img: Image.Image) -> Image.Image:
    """Paint out columns bright from top to bottom: the map's crosshair running through a label."""
    a = np.asarray(ImageOps.grayscale(img)).astype(float)
    col = a.mean(axis=0)
    base = float(np.median(col))
    out = a.copy()
    for x in np.where(col > base + 0.6 * (a.max() - base))[0]:
        side = [c for c in (x - 3, x + 3) if 0 <= c < a.shape[1]]
        out[:, x] = a[:, side].mean(axis=1) if side else base
    return Image.fromarray(out.clip(0, 255).astype("uint8")).convert("RGB")


def _rect_distance(p: tuple[int, int], r: Rect) -> float:
    dx = max(r[0] - p[0], 0, p[0] - r[2])
    dy = max(r[1] - p[1], 0, p[1] - r[3])
    return math.hypot(dx, dy)


def _clip(r: Rect, bounds: Rect) -> Rect | None:
    c = (max(r[0], bounds[0]), max(r[1], bounds[1]), min(r[2], bounds[2]), min(r[3], bounds[3]))
    return c if c[2] - c[0] >= 16 and c[3] - c[1] >= 16 else None


class ReadoutReader:
    # Search window around the cursor: left, top, right, bottom offsets in px.
    NEAR = (-460, -240, 460, 280)
    # Padding around a learned readout spot; wide, because the number can grow, and
    # tall, because the map stacks its "y" label above its "x" label.
    PAD_X, PAD_Y = 90, 55
    # Crops smaller than this get upscaled before OCR; small HUD text reads far
    # better at 2-3x.
    SMALL_AREA = 1_200_000
    # Text at least this tall reads fine at native resolution, which is much faster.
    CRISP_TEXT = 16
    # How close a chat-style line must be to the cursor to be read instead of the map readout.
    FALLBACK_RADIUS = 150

    def __init__(self, ocr: Ocr, memory: dict | None = None) -> None:
        self.ocr = ocr
        # {"mode": "cursor" | "fixed" | None, "last": {"box": [...], "cursor": [...]}}
        self.memory: dict = dict(memory or {})

    # -- search regions ---------------------------------------------------------
    def _learned_region(self, cursor: tuple[int, int]) -> Rect | None:
        last = self.memory.get("last")
        mode = self.memory.get("mode")
        if not last or mode not in ("cursor", "fixed"):
            return None
        l, t, r, b = last["box"]
        if mode == "cursor":
            dx, dy = cursor[0] - last["cursor"][0], cursor[1] - last["cursor"][1]
            l, t, r, b = l + dx, t + dy, r + dx, b + dy
        return (l - self.PAD_X, t - self.PAD_Y, r + self.PAD_X, b + self.PAD_Y)

    def _regions(self, cursor: tuple[int, int], monitor: Rect) -> list[tuple[str, Rect]]:
        out: list[tuple[str, Rect]] = []
        learned = self._learned_region(cursor)
        if learned and (c := _clip(learned, monitor)):
            out.append(("learned spot", c))
        n = self.NEAR
        near = _clip((cursor[0] + n[0], cursor[1] + n[1], cursor[0] + n[2], cursor[1] + n[3]), monitor)
        if near:
            out.append(("near cursor", near))
        out.append(("whole screen", monitor))
        return out

    # -- reading -------------------------------------------------------------------
    @property
    def _crisp(self) -> bool:
        """True once we've seen how tall this game's readout actually is."""
        return (self.memory.get("text_h") or 0) >= self.CRISP_TEXT

    def _scale_for(self, region: Rect) -> float:
        area = (region[2] - region[0]) * (region[3] - region[1])
        if self._crisp:
            return 2.0 if area <= 120_000 else 1.0
        if area <= 120_000:
            return 3.0
        if area <= self.SMALL_AREA:
            return 2.0
        return 1.0

    @staticmethod
    def _clipped(box: Rect, region: Rect, monitor: Rect) -> bool:
        """Text touching a crop edge may be cut off ("09.50" of "109.50").

        Edges that are also the monitor's edge don't count: nothing is drawn past them.
        """
        l, t, r, b = box
        w, h = region[2] - region[0], region[3] - region[1]
        return ((region[0] > monitor[0] and l <= 2) or (region[1] > monitor[1] and t <= 1)
                or (region[2] < monitor[2] and r >= w - 2) or (region[3] < monitor[3] and b >= h - 1))

    @staticmethod
    def _touches_mask(box: Rect, masked: list[Rect]) -> bool:
        """Text running into a window we blanked out may be cut off there ("7.68" of "137.68")."""
        l, t, r, b = box
        return any(l - 4 < mr and r + 4 > ml and t < mb and b > mt for ml, mt, mr, mb in masked)

    def _candidates(self, boxes: list[TextBox], region: Rect, monitor: Rect,
                    cursor: tuple[int, int], masked: list[Rect] | None = None) -> list[Candidate]:
        ox, oy = region[0], region[1]
        masked = masked or []
        items = [(b.text, b.box) for b in boxes] + _merge_lines(boxes)
        best: dict[tuple[float, float], Candidate] = {}
        for text, (l, t, r, b) in items:
            if self._clipped((l, t, r, b), region, monitor):
                continue
            screen_box = (l + ox, t + oy, r + ox, b + oy)
            if self._touches_mask(screen_box, masked):
                continue
            dist = _rect_distance(cursor, screen_box)
            for c in find_coords(text, strict=True):
                # Chat lines carry a channel and a name; the readout is bare numbers.
                # Only read a chat line when the cursor is right on it.
                extra = len(text.replace(" ", "")) - len(text[c.start:c.end].replace(" ", ""))
                bare = extra <= 2
                preferred = bare or dist <= 6
                rank = dist + (0 if c.labeled else 250) + (150 if c.repaired else 0) + 6 * max(extra, 0)
                key = (c.x, c.y)
                if key not in best or (not preferred, rank) < (not best[key].preferred, best[key].rank):
                    best[key] = Candidate(c, text, screen_box, bare, preferred, rank, b - t)
        for cand in self._axis_pairs(boxes, region, monitor, cursor, masked):
            key = (cand.coord.x, cand.coord.y)
            if key not in best or cand.rank < best[key].rank:
                best[key] = cand
        return sorted(best.values(), key=lambda c: (not c.preferred, c.rank))

    # How far apart, in pixels, the two halves of the readout may sit.
    PAIR_SPAN = 280

    def _axis_pairs(self, boxes: list[TextBox], region: Rect, monitor: Rect,
                    cursor: tuple[int, int], masked: list[Rect] | None = None) -> list[Candidate]:
        """Pair a lone "x81.57" with a lone "y73.39" nearby.

        The tactical map labels its two crosshair rulers separately, y above x,
        so the halves arrive as different OCR boxes and never share a line.
        """
        ox, oy = region[0], region[1]
        halves: dict[str, list[tuple[float, bool, TextBox]]] = {"x": [], "y": []}
        for b in boxes:
            if self._clipped(b.box, region, monitor):
                continue
            l, t, r, bt = b.box
            if self._touches_mask((l + ox, t + oy, r + ox, bt + oy), masked or []):
                continue
            hit = single_axis(b.text)
            if hit is not None:
                halves[hit.axis].append((hit.value, hit.repaired, b))
        out: list[Candidate] = []
        for vx, rx, bx in halves["x"]:
            for vy, ry, by in halves["y"]:
                gap = math.hypot((bx.box[0] + bx.box[2] - by.box[0] - by.box[2]) / 2,
                                 (bx.box[1] + bx.box[3] - by.box[1] - by.box[3]) / 2)
                if gap > self.PAIR_SPAN:
                    continue
                out.append(self._pair_candidate(vx, rx, bx, vy, ry, by, region, cursor, gap))
        if not out:
            out = self._inferred_pairs(boxes, halves, region, monitor, cursor, masked or [])
        return out

    def _pair_candidate(self, vx: float, rx: bool, bx: TextBox, vy: float, ry: bool, by: TextBox,
                        region: Rect, cursor: tuple[int, int], gap: float, inferred: str = "") -> Candidate:
        ox, oy = region[0], region[1]
        box = (min(bx.box[0], by.box[0]) + ox, min(bx.box[1], by.box[1]) + oy,
               max(bx.box[2], by.box[2]) + ox, max(bx.box[3], by.box[3]) + oy)
        text = " ".join(b.text for b in sorted((bx, by), key=lambda b: b.box[1]))
        coord = Coord(vx, vy, not inferred, rx or ry, 0, len(text))
        # Prefer the pair nearest the cursor, then the tightest.
        rank = _rect_distance(cursor, box) + 0.5 * gap + (150 if coord.repaired else 0) + (200 if inferred else 0)
        line_h = min(bx.box[3] - bx.box[1], by.box[3] - by.box[1])
        return Candidate(coord, text, box, True, True, rank, line_h, inferred)

    @staticmethod
    def _x_sits_under_y(by: Rect, bx: Rect) -> bool:
        """The map prints x about one line-height right of y and two to three lines below it."""
        h = by[3] - by[1]
        dx, dy = bx[0] - by[0], bx[1] - by[1]
        return 0.3 * h <= dx <= 2.2 * h and 1.8 * h <= dy <= 3.8 * h

    def _inferred_pairs(self, boxes: list[TextBox], halves: dict, region: Rect, monitor: Rect,
                        cursor: tuple[int, int], masked: list[Rect]) -> list[Candidate]:
        """A label whose letter OCR lost ("98.76" for "x98.76"), placed exactly where that axis goes.

        Only ever offered unconfirmed: the position says which axis it is, but a human
        should glance at the thumbnail before firing on it.
        """
        ox, oy = region[0], region[1]
        bare = []
        for b in boxes:
            v = bare_number(b.text)
            l, t, r, bt = b.box
            if v is not None and not self._clipped(b.box, region, monitor) \
                    and not self._touches_mask((l + ox, t + oy, r + ox, bt + oy), masked):
                bare.append((v, b))
        out = []
        for vy, ry, by in halves["y"]:
            for vx, bx in bare:
                if self._x_sits_under_y(by.box, bx.box):
                    out.append(self._pair_candidate(vx, False, bx, vy, ry, by, region, cursor, 0.0, "x"))
        for vx, rx, bx in halves["x"]:
            for vy, by in bare:
                if self._x_sits_under_y(by.box, bx.box):
                    out.append(self._pair_candidate(vx, rx, bx, vy, False, by, region, cursor, 0.0, "y"))
        return out

    # Re-read variants: (name, upscale, isolate the text colour first). Different
    # scales and preprocessing fail differently, so agreement between them means more
    # than agreement between near-identical passes. Crisp text needs less blowing up.
    # The masked pass is the only one that sees through a label drawn across a digit,
    # and it needs the upscale to work, so it stays at 2x whatever the text size.
    RECHECKS_SMALL = (("1x", 1.0, False), ("2x", 2.0, False), ("3x", 3.0, False), ("mask", 2.0, True))
    RECHECKS_CRISP = (("1x", 1.0, False), ("1.5x", 1.5, False), ("2x", 2.0, False), ("mask", 2.0, True))

    @property
    def _rechecks(self) -> tuple[tuple[str, float, bool], ...]:
        return self.RECHECKS_CRISP if self._crisp else self.RECHECKS_SMALL

    def _confirm(self, shot: Image.Image, origin: tuple[int, int],
                 cand: Candidate) -> tuple[Coord, int, list[tuple[float, float]]]:
        """Re-read just the chosen text several ways and vote.

        One OCR pass occasionally swaps a digit (a "9" crossed by a grid line read
        as "5"). Returns the voted coordinate, how many clean re-reads agree with it
        exactly, and every other value any read suggested, however mangled.
        """
        ox, oy = origin
        l, t, r, b = cand.box
        # Pad by one line of text, not by the height of a stacked pair.
        line = cand.text_h or (b - t)
        crop = shot.crop((max(l - ox - 2 * line, 0), max(t - oy - line, 0),
                          min(r - ox + 2 * line, shot.width), min(b - oy + line, shot.height)))
        rereads: list[tuple[float, float]] = []
        sloppy: list[tuple[str | None, str | None]] = []
        for _name, scale, mask in self._rechecks:
            img = _text_mask(_upscale(crop, scale), line * scale) if mask else crop
            boxes = self.ocr.read(img, 1.0 if mask else scale)
            text = " ".join(bx.text for bx in sorted(boxes, key=lambda bx: bx.box[0]))
            # Only fully labelled re-reads vote: "y73.39 81.57" with the x dropped would
            # otherwise parse as a swapped pair.
            clean = [c for c in find_coords(text, strict=True) if not c.repaired and c.labeled]
            if clean:
                rereads.append((clean[0].x, clean[0].y))
            sloppy.append(loose_digits(text))
        first = (cand.coord.x, cand.coord.y)
        x = Counter([first[0]] + [p[0] for p in rereads]).most_common(1)[0][0]
        y = Counter([first[1]] + [p[1] for p in rereads]).most_common(1)[0][0]
        votes = sum(p == (x, y) for p in rereads)
        dissent = {p for p in rereads + [first] if p != (x, y)}
        # A re-read too mangled to parse still counts against us if its digits differ
        # ("X2047V56.77" says 20.47 even with the point lost and Y read as V).
        wx, wy = value_digits(x), value_digits(y)
        mismatch = False
        for dx, dy in sloppy:
            bad_x, bad_y = _digits_differ(dx, wx), _digits_differ(dy, wy)
            if not (bad_x or bad_y):
                continue
            # Quote the alternative only when its digits came out whole.
            whole = (not bad_x or len(dx) == len(wx)) and (not bad_y or len(dy) == len(wy))
            if whole:
                dissent.add((int(dx) / 100 if bad_x else x, int(dy) / 100 if bad_y else y))
            else:
                mismatch = True  # disagreed, but too chewed up to quote a value
        repaired = cand.coord.repaired and votes == 0
        return (Coord(x, y, cand.coord.labeled, repaired, cand.coord.start, cand.coord.end), votes,
                sorted(dissent), mismatch)

    # The vehicle HUD sits along the bottom of the screen.
    ASL_STRIP_TOP = 0.72
    def read_asl(self, shot: Image.Image) -> int | None:
        """Your own height above sea level, which the HUD shows while the map is open.

        Read twice at different scales and only believed when both agree: a misread
        altitude would quietly bend every height-corrected solution.
        """
        top = int(shot.height * self.ASL_STRIP_TOP)
        strip = shot.crop((0, top, shot.width, shot.height))
        seen = []
        for scale in (1.0, 1.5):
            boxes = self.ocr.read(strip, scale)
            for text, _box in [(b.text, b.box) for b in boxes] + _merge_lines(boxes):
                found = find_asl(text)
                if found is not None:
                    seen.append(found)
                    break
        return seen[0] if seen and len(set(seen)) == 1 else None

    # Around the cursor, where the map prints "RNG 2,142 m" over "ASL 17 m" for a hovered object.
    HOVER_NEAR = (-260, -160, 80, 40)

    def read_hover_asl(self, shot: Image.Image, origin: tuple[int, int], cursor: tuple[int, int]) -> int | None:
        """Height of the hovered object, from the small label the map shows beside the cursor.

        The "RNG" line reads well, so it locates the "ASL" line under it. That line is
        tiny and often crossed by the crosshair, so it is read several ways and only
        believed when at least two readings agree and none disagrees: a wrong height
        would quietly bend the elevation, a missing one only means typing it.
        """
        ox, oy = origin
        n = self.HOVER_NEAR
        reg = (max(cursor[0] + n[0] - ox, 0), max(cursor[1] + n[1] - oy, 0),
               min(cursor[0] + n[2] - ox, shot.width), min(cursor[1] + n[3] - oy, shot.height))
        if reg[2] - reg[0] < 40 or reg[3] - reg[1] < 40:
            return None
        rng = [b for b in self.ocr.read(shot.crop(reg), 3.0) if "RNG" in b.text.upper()]
        if not rng:
            return None
        l, t, r, b = rng[0].box
        h = b - t
        line = shot.crop((reg[0] + l - 6, reg[1] + b - 1, reg[0] + r + 12, reg[1] + b + h + 5))
        clean = _without_vertical_lines(line)
        reads = []
        for img in (line, clean):
            for scale in (3.0, 4.0, 6.0):
                g = ImageOps.autocontrast(ImageOps.grayscale(_upscale(img, scale)), cutoff=2).convert("RGB")
                reads.append(find_asl(self.ocr.recognize_line(g)))
                reads.append(find_asl(" ".join(bx.text for bx in self.ocr.read(g))))
        tally = Counter(v for v in reads if v is not None).most_common()
        if not tally:
            return None
        value, votes = tally[0]
        runner_up = tally[1][1] if len(tally) > 1 else 0
        # A strong majority: one pass dropping a digit ("ASL 2m" for "ASL 22 m") mustn't veto
        # seven clean reads, but two passes genuinely split between numbers must.
        return value if votes >= 4 and votes >= 3 * runner_up else None

    def _learn(self, box: Rect, cursor: tuple[int, int], text_h: int = 0) -> None:
        last = self.memory.get("last")
        self.memory["last"] = {"box": list(box), "cursor": list(cursor)}
        if text_h:
            self.memory["text_h"] = text_h
        if not last:
            return
        dcx, dcy = cursor[0] - last["cursor"][0], cursor[1] - last["cursor"][1]
        dbx, dby = box[0] - last["box"][0], box[1] - last["box"][1]
        if math.hypot(dcx, dcy) < 40:
            return  # cursor barely moved; can't tell the two layouts apart
        if math.hypot(dbx - dcx, dby - dcy) < 16:
            self.memory["mode"] = "cursor"
        elif math.hypot(dbx, dby) < 16:
            self.memory["mode"] = "fixed"

    def read(self, shot: Image.Image, origin: tuple[int, int], cursor: tuple[int, int],
             exclude: list[Rect] | None = None) -> ReadResult:
        """Read the coordinate readout nearest `cursor`.

        `shot` is a capture of one monitor whose top-left sits at screen
        coordinate `origin`; `cursor` and `exclude` are screen coordinates.
        """
        t0 = time.perf_counter()
        ox, oy = origin
        monitor = (ox, oy, ox + shot.width, oy + shot.height)
        if exclude:
            shot = shot.copy()
            draw = ImageDraw.Draw(shot)
            for l, t, r, b in exclude:  # never read our own window
                draw.rectangle((l - ox, t - oy, r - ox - 1, b - oy - 1), fill=(0, 0, 0))
        if shot.convert("L").getextrema()[1] < 12:
            return ReadResult(None, ms=(time.perf_counter() - t0) * 1000,
                              message="Screen capture came back black. Run WARDOGS in Borderless/Windowed "
                                      "fullscreen so other programs can see it.")
        seen: list[tuple[str, Rect]] = []
        fallback: tuple[Candidate, str] | None = None
        for name, region in self._regions(cursor, monitor):
            crop = shot.crop((region[0] - ox, region[1] - oy, region[2] - ox, region[3] - oy))
            boxes = self.ocr.read(crop, self._scale_for(region))
            seen.extend((b.text, (b.box[0] + region[0], b.box[1] + region[1],
                                  b.box[2] + region[0], b.box[3] + region[1])) for b in boxes)
            cands = self._candidates(boxes, region, monitor, cursor, exclude)
            if not cands:
                continue
            if cands[0].preferred:
                return self._finish(shot, origin, cursor, cands[0], name, t0, seen)
            # Only chat-style text so far; the readout itself may be further out.
            fallback = fallback or (cands[0], name)
        if fallback and _rect_distance(cursor, fallback[0].box) <= self.FALLBACK_RADIUS:
            return self._finish(shot, origin, cursor, fallback[0], fallback[1], t0, seen)
        if fallback:
            # Coordinates exist on screen but nowhere near the cursor. Handing back a
            # teammate's old chat line would put shells on the wrong square.
            return ReadResult(None, region="", ms=(time.perf_counter() - t0) * 1000,
                              message="Couldn't find the map's coordinate readout. The only coordinates on "
                                      "screen are far from the cursor (a chat line?) - hover them if you "
                                      "meant those.", seen=seen)
        near = {hit.axis for text, box in seen if _rect_distance(cursor, box) <= 250
                and (hit := single_axis(text)) is not None}
        if len(near) == 1:
            found = near.pop()
            missing = "x" if found == "y" else "y"
            return ReadResult(None, region="", ms=(time.perf_counter() - t0) * 1000, seen=seen, message=(
                f"Only the {found} coordinate is visible: something is covering the {missing} label, usually "
                "your own name tag or an icon's info card. Move the cursor a little off the icon and press again."))
        return ReadResult(None, region="", ms=(time.perf_counter() - t0) * 1000,
                          message="No X/Y coordinates found on screen. Is the tactical map open with "
                                  "the cursor over it?", seen=seen)

    def _finish(self, shot: Image.Image, origin: tuple[int, int], cursor: tuple[int, int],
                cand: Candidate, region: str, t0: float, seen: list[tuple[str, Rect]]) -> ReadResult:
        if cand.inferred:
            # No vote: re-reads missing the same letter would parse the pair the wrong way round.
            coord, votes, dissent, mismatch = cand.coord, 0, [], False
        else:
            coord, votes, dissent, mismatch = self._confirm(shot, origin, cand)
        if cand.bare:  # learn where the readout lives; chat lines move as chat scrolls
            self._learn(cand.box, cursor, cand.text_h)
        ox, oy = origin
        l, t, r, b = cand.box
        thumb = shot.crop((max(l - ox - 4, 0), max(t - oy - 3, 0),
                           min(r - ox + 4, shot.width), min(b - oy + 3, shot.height)))
        message = "" if cand.preferred else ("The map readout wasn't found, so this is the nearest coordinate "
                                             "line (chat?). Make sure it's the one you meant.")
        if cand.inferred:
            message = (f"The '{cand.inferred}' label was unreadable, so {cand.inferred} was taken from where it sits; "
                       "check it against the thumbnail.")
        return ReadResult(coord, cand.text, cand.box, thumb, region, (time.perf_counter() - t0) * 1000,
                          message, votes, len(self._rechecks), dissent, mismatch, cand.preferred, seen)
