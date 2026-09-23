"""End-to-end OCR on synthetic screens (slow: loads the OCR models, ~1 min)."""
import random
import unittest

from arty.ocr import Ocr, ReadoutReader
from tests.bench_reader import LAYOUTS, run
from tests.synth import make_case


class CoveredReadout(unittest.TestCase):
    def test_partly_covered_text_is_never_a_confident_wrong_read(self):
        # Our own window (or any other) over the start of "X137.68" leaves "7.68", which
        # every re-read of the same pixels agrees on. It must not come back as trusted.
        ocr = Ocr()
        rng = random.Random(11)
        for _ in range(6):
            case = make_case(rng, "cursor")
            clean = ReadoutReader(ocr).read(case.image, (0, 0), case.cursor)
            if clean.box is None:
                continue
            l, t, r, b = clean.box
            cover = (l - 200, t - 30, l + int((r - l) * 0.3), b + 30)  # hides the label and first digits
            res = ReadoutReader(ocr).read(case.image, (0, 0), case.cursor, exclude=[cover])
            got = (res.coord.x, res.coord.y) if res.coord else None
            with self.subTest(truth=case.truth, got=got):
                self.assertFalse(got is not None and got != case.truth and res.confident)


class Reader(unittest.TestCase):
    def test_synthetic_screens(self):
        report = run(n=10, seed=21)
        for layout in LAYOUTS:
            r = report[layout]
            with self.subTest(layout=layout):
                self.assertEqual(r["wrong_confident"], 0, r["failures"])  # never confidently wrong
                self.assertGreaterEqual(r["ok"], 9, r["failures"])
        self.assertEqual(report["split"]["mode"], "cursor")  # the real layout follows the cursor
        self.assertEqual(report["cursor"]["mode"], "cursor")
        self.assertEqual(report["fixed"]["mode"], "fixed")


if __name__ == "__main__":
    unittest.main()
