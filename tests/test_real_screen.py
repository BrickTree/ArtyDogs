"""Regression test on real WARDOGS screenshots.

The tactical map draws a crosshair through the cursor and labels the two rulers
separately, "y73.39" above and "x81.57" below, so the pair never shares a text
line. The first build only paired X with Y on one line and read nothing at all
in game; these crops keep that from coming back.
"""
import unittest
from pathlib import Path

from PIL import Image

from arty.ocr import Ocr, ReadoutReader

DATA = Path(__file__).parent / "data"
# (fixture, cursor inside the crop, what the map was showing)
SHOTS = [
    ("map_readout.png", (300, 220), (81.57, 73.39)),
    ("map_readout_2.png", (300, 220), (60.50, 71.77)),
]


class RealScreen(unittest.TestCase):
    def test_reads_the_split_readout(self):
        reader = ReadoutReader(Ocr())
        for name, cursor, expected in SHOTS:
            with self.subTest(shot=name):
                shot = Image.open(DATA / name)
                res = reader.read(shot, (0, 0), cursor)
                self.assertIsNotNone(res.coord, res.message)
                self.assertEqual((res.coord.x, res.coord.y), expected)
                self.assertTrue(res.confident, f"votes={res.votes} dissent={res.dissent}")
        # Two reads with the cursor in the same spot can't reveal the layout, but the
        # text height is learned from the first read and speeds up every later one.
        self.assertGreaterEqual(reader.memory.get("text_h", 0), 16)

    def test_x_whose_letter_is_hidden_is_placed_but_flagged(self):
        # A ping marker covers the "x" of "x98.76"; the number sits exactly where x goes.
        res = ReadoutReader(Ocr()).read(Image.open(DATA / "map_x_letter_lost.png"), (0, 0), (300, 220))
        self.assertEqual((res.coord.x, res.coord.y), (98.76, 62.03))
        self.assertFalse(res.confident)  # a human checks the thumbnail before firing
        self.assertIn("unreadable", res.message)

    def test_name_tag_over_x_explains_itself(self):
        # Hovering your own icon puts your name tag over the x label.
        res = ReadoutReader(Ocr()).read(Image.open(DATA / "map_x_covered_by_nametag.png"), (0, 0), (300, 220))
        self.assertIsNone(res.coord)
        self.assertIn("Only the y coordinate is visible", res.message)


class HoveredHeight(unittest.TestCase):
    """The map prints "RNG ... / ASL ..." beside the cursor when you hover some objects."""

    def test_reads_a_legible_label(self):
        reader = ReadoutReader(Ocr())
        self.assertEqual(reader.read_hover_asl(Image.open(DATA / "map_hover_asl_22.png"), (0, 0), (300, 220)), 22)

    def test_crosshair_through_the_digits_gives_nothing_not_a_guess(self):
        # "ASL 17 m" with the crosshair through it: unreadable, so no number at all.
        reader = ReadoutReader(Ocr())
        self.assertIsNone(reader.read_hover_asl(Image.open(DATA / "map_x_letter_lost.png"), (0, 0), (300, 220)))

    def test_no_label_no_height(self):
        reader = ReadoutReader(Ocr())
        self.assertIsNone(reader.read_hover_asl(Image.open(DATA / "map_readout.png"), (0, 0), (300, 220)))


if __name__ == "__main__":
    unittest.main()
