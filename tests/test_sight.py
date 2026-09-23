"""The SPH-2 gunner sight, read from a real in-game screenshot (1920x1080).

At the moment of the snapshot the sight showed 1,020 mil and RNG 2,130 m at the
reticle, heading 286 W on the tape (286.7 between the 270 and 300 marks),
"Unstabilized" and ASL 230.
"""
import unittest
from pathlib import Path

from PIL import Image

from arty import sight
from arty.ocr import Ocr

DATA = Path(__file__).parent / "data"
OCR = None


def ocr() -> Ocr:
    global OCR
    OCR = OCR or Ocr()
    return OCR


def scrolled(shot: Image.Image, dy: int) -> Image.Image:
    """Move only the ladders, the way raising or lowering the barrel does."""
    out = shot.copy()
    out.paste(shot.crop((0, 200 + dy, shot.width, 800 + dy)), (0, 200))
    return out


class RealSight(unittest.TestCase):
    shot = Image.open(DATA / "sph2_sight.png")

    def test_reads_every_readout(self):
        r = sight.read_sight(ocr(), self.shot)
        self.assertAlmostEqual(r.mil, 1020, delta=0.5)
        self.assertAlmostEqual(r.range_m, 2130, delta=3)
        self.assertAlmostEqual(r.heading, 286.7, delta=0.3)
        self.assertIs(r.stabilized, False)
        self.assertEqual(r.asl, 230)
        self.assertIn((1020.0, 2130.0), r.table)

    def test_reads_between_the_marks(self):
        # 13.1 px per mil on this sight: 39 px down is ~3 mil more, 52 px up ~4 mil less.
        for dy, want in ((39, 1023.0), (-52, 1016.0)):
            with self.subTest(dy=dy):
                self.assertAlmostEqual(sight.read_sight(ocr(), scrolled(self.shot, dy)).mil, want, delta=0.6)


class Placement(unittest.TestCase):
    """Where a value sits on screen, so a marker can be drawn beside it."""

    def test_mil_and_heading_map_back_to_pixels(self):
        r = sight.read_sight(ocr(), RealSight.shot)
        self.assertAlmostEqual(r.mil_y(1020), 541, delta=3)  # the "1,020 mil" label's row
        self.assertAlmostEqual(r.mil_y(1030), 672, delta=3)
        self.assertAlmostEqual(r.heading_x(286.7), 960, delta=3)  # the reticle column
        self.assertAlmostEqual(r.heading_x(300), 1061, delta=4)  # the "300" mark
        self.assertLess(r.mil_label_x, 1240)


def pip_moved(shot: Image.Image, side: str, dy: int) -> Image.Image:
    """Move one tilt triangle up (-) or down (+), covering its old spot with nearby ground."""
    box = (872, 933, 884, 949) if side == "left" else (1036, 933, 1048, 949)
    cover = (848, 933, 860, 949) if side == "left" else (1060, 933, 1072, 949)
    out = shot.copy()
    tri = shot.crop(box)
    out.paste(shot.crop(cover), box[:2])
    out.paste(tri, (box[0], box[1] + dy))
    return out


class TiltPips(unittest.TestCase):
    """The two triangles beside the vehicle silhouette show the vehicle's tilt."""
    shot = Image.open(DATA / "sph2_sight.png")

    def test_reads_both_pips(self):
        (left, right), (cy, _lx, _rx, spacing) = sight.read_tilt(self.shot)
        # Both triangles sat 12 px below the middle dash; dashes are 16.75 px apart.
        self.assertAlmostEqual(left, 0.72, delta=0.05)
        self.assertAlmostEqual(right, 0.72, delta=0.05)
        self.assertAlmostEqual(cy, 929, delta=1)
        self.assertAlmostEqual(spacing, 16.75, delta=0.3)

    def test_a_moved_pip_reads_its_new_place(self):
        (left, right), _geo = sight.read_tilt(pip_moved(self.shot, "left", -17))
        self.assertAlmostEqual(left, 0.72 - 1.0, delta=0.08)
        self.assertAlmostEqual(right, 0.72, delta=0.05)
        (left, right), _geo = sight.read_tilt(pip_moved(self.shot, "right", -12))
        self.assertAlmostEqual(right, 0.0, delta=0.08)  # level on that side

    def test_no_sight_no_tilt(self):
        self.assertIsNone(sight.read_tilt(Image.open(DATA / "map_readout.png")))
        self.assertIsNone(sight.read_tilt(Image.new("RGB", (1920, 1080), (40, 60, 40))))

    def test_read_sight_includes_it(self):
        r = sight.read_sight(ocr(), self.shot)
        self.assertIsNotNone(r.tilt)
        self.assertAlmostEqual(r.tilt[0], 0.72, delta=0.05)


class Scales(unittest.TestCase):
    def test_value_between_marks(self):
        self.assertAlmostEqual(sight.value_at([(1010, 410), (1030, 672)], 541), 1020, delta=0.01)

    def test_one_mark_only_counts_on_the_line(self):
        self.assertEqual(sight.value_at([(1020, 542)], 540), 1020)
        self.assertIsNone(sight.value_at([(1020, 600)], 540))

    def test_a_misread_label_breaks_the_line(self):
        # "1,080" misread for "1,030": the marks no longer fit one scale, so no reading.
        self.assertIsNone(sight.value_at([(1010, 410), (1020, 541), (1080, 672)], 541))

    def test_heading_across_north(self):
        marks = sight._unwrap([(345.0, 800), (0.0, 914), (15.0, 1028)])
        self.assertAlmostEqual(sight.value_at(marks, 960) % 360, 6.05, delta=0.05)


if __name__ == "__main__":
    unittest.main()
