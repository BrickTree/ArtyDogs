import unittest

from arty.coords import best_coord, find_asl, find_coords, loose_digits, parse_number, value_digits
from arty.hotkeys import parse_hotkey


def pairs(text, strict=False):
    return [(c.x, c.y) for c in find_coords(text, strict)]


class Coordinates(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(pairs("X80.07 Y70.54"), [(80.07, 70.54)])
        self.assertEqual(pairs("X80.07Y70.54"), [(80.07, 70.54)])
        self.assertEqual(pairs("x100.05, y109.14"), [(100.05, 109.14)])
        self.assertEqual(pairs("X: 83,00  Y: 74,00"), [(83.0, 74.0)])
        self.assertEqual(pairs("[SQUAD] Wolf: X83.00 Y74.00"), [(83.0, 74.0)])

    def test_ocr_damage(self):
        self.assertEqual(pairs("X 110.07 111.03"), [(110.07, 111.03)])  # dropped Y label
        self.assertEqual(pairs("X 8 82.76 Y 47.10"), [(82.76, 47.10)])  # stray glyph
        c = best_coord("X8O.O7 Y70.54")  # letter O for zero
        self.assertEqual((c.x, c.y, c.repaired), (80.07, 70.54, True))
        c = best_coord("X8007 Y7054")  # lost decimal points
        self.assertEqual((c.x, c.y, c.repaired), (80.07, 70.54, True))

    def test_strict_rejects_clipped_numbers(self):
        self.assertEqual(pairs("X109.5 Y70.54", strict=True), [])
        self.assertEqual(pairs("1109.50 70.54", strict=True), [])
        self.assertEqual(pairs("X109.50 Y70.54", strict=True), [(109.5, 70.54)])

    def test_rejects_non_coordinates(self):
        self.assertEqual(pairs("Tower 3 500m"), [])
        self.assertEqual(pairs("X300.00 Y20.00"), [])  # off the 163.84-unit world

    def test_loose_digits_see_through_mangled_rereads(self):
        # What a background-stripped re-read produced when a map label covered the "4".
        self.assertEqual(loose_digits("X2047V56.77"), ("2047", "5677"))
        self.assertEqual(loose_digits("[SQUAD] Bravo: X39.83 Y69.02"), ("3983", "6902"))
        self.assertEqual(loose_digits("Tower 3"), (None, None))
        self.assertEqual([value_digits(v) for v in (20.47, 5.1, 0.0)], ["2047", "510", "0"])

    def test_reads_the_hud_altitude(self):
        # The "L" is faint and OCR drops or mangles it; the number is what counts.
        self.assertEqual([find_asl(t) for t in ("ASL 124", "ASL124", "AS 124", "ASL/ 124", "A5L 97", "ASI 7")],
                         [124, 124, 124, 124, 97, 7])
        self.assertEqual([find_asl(t) for t in ("GAS 12", "Toggle Legend", "")], [None, None, None])

    def test_parse_number(self):
        self.assertEqual([parse_number(s) for s in ("80.07", "80,07", "X80.07", " 7 ", "abc", "500.1")],
                         [80.07, 80.07, 80.07, 7.0, None, None])


class Hotkeys(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_hotkey("F8"), (0, 0x77))
        self.assertEqual(parse_hotkey("ctrl+shift+t"), (0x2 | 0x4, ord("T")))
        self.assertEqual(parse_hotkey("Alt+Numpad5"), (0x1, 0x65))
        for bad in ("", "Hyper+F1", "F99", "Ctrl+"):
            with self.assertRaises(ValueError):
                parse_hotkey(bad)


if __name__ == "__main__":
    unittest.main()
