"""The game's own firing table, from the rows the SPH-2 sight prints.

The rows are the real ones dial assist logged on 2026-09-22. On the low arc the
sight reaches 65-71 m further than the community table at 30-50 mil, so the
table's mil for ~1,300 m was ~14 mil too high: the user saw it as "dialing by
the sight's range hits, the mils are off".
"""
import unittest

from arty.ballistics import Point, elevation_mil, load_data, solve
from arty.gametable import GameTable

DATA = load_data()
SPH2 = DATA.weapons["sph2"]
LOW, HIGH = (next(a for a in SPH2.arcs if a.id == i) for i in ("low", "high"))
REAL = [(30, 1303), (50, 1399), (40, 1351), (320, 340), (320, 2340), (1400, 732), (1390, 777)]


class RealRows(unittest.TestCase):
    def setUp(self):
        self.g = GameTable(SPH2, REAL)

    def test_the_misread_row_is_dropped(self):
        self.assertNotIn((320.0, 340.0), self.g.rows("low"))  # "2,340m" read as "340m"
        self.assertEqual(len(self.g.rows("low")), 4)

    def test_the_sight_marks_come_back_exactly(self):
        self.assertAlmostEqual(self.g.mil_for(LOW, 1303), 30, places=6)
        self.assertAlmostEqual(self.g.mil_for(LOW, 1351), 40, places=6)
        self.assertAlmostEqual(self.g.mil_for(LOW, 2340), 320, places=6)
        self.assertAlmostEqual(self.g.mil_for(HIGH, 777), 1390, places=6)

    def test_the_old_table_was_14_mil_high_there(self):
        old = sum(elevation_mil(LOW.table, 1303)) / 2
        self.assertAlmostEqual(old - self.g.mil_for(LOW, 1303), 13.9, delta=0.2)

    def test_between_marks_it_interpolates(self):
        self.assertAlmostEqual(self.g.mil_for(LOW, 1375), 45, delta=0.3)

    def test_where_the_sight_was_never_seen_it_says_so(self):
        self.assertIsNone(self.g.mil_for(LOW, 1600))  # 100+ mil from any row
        self.assertIsNone(self.g.mil_for(LOW, 2000))

    def test_coverage(self):
        n, lo, hi, mean = self.g.coverage(LOW)
        self.assertEqual((n, lo, hi), (4, 30, 320))
        self.assertGreater(mean, 40)


class Solve(unittest.TestCase):
    GUN = Point(50, 50)

    def test_the_game_table_decides_where_it_covers(self):
        g = GameTable(SPH2, REAL)
        sol = solve(SPH2, self.GUN, Point(63.03, 50), game=g)  # 1303 m
        low = next(a for a in sol.arcs if a.arc_id == "low")
        self.assertEqual(low.source, "game")
        self.assertAlmostEqual(low.min_mil, 30, places=4)

    def test_elsewhere_the_old_table_and_it_says_so(self):
        g = GameTable(SPH2, REAL)
        sol = solve(SPH2, self.GUN, Point(70, 50), game=g)  # 2000 m: no rows near
        low = next(a for a in sol.arcs if a.arc_id == "low")
        self.assertEqual(low.source, "table")
        self.assertAlmostEqual(low.min_mil, sum(elevation_mil(LOW.table, 2000)) / 2, places=4)

    def test_height_correction_still_rides_on_top(self):
        g = GameTable(SPH2, REAL)
        flat = next(a for a in solve(SPH2, self.GUN, Point(63.51, 50), game=g,
                                     bands=DATA.height_bands).arcs if a.arc_id == "low")
        up = next(a for a in solve(SPH2, self.GUN, Point(63.51, 50), game=g, dz=20,
                                   bands=DATA.height_bands).arcs if a.arc_id == "low")
        self.assertEqual(up.source, "game")
        self.assertAlmostEqual(up.min_mil - flat.min_mil, up.height_mil, places=6)
        self.assertGreater(up.height_mil, 10)


class ShippedSeed(unittest.TestCase):
    """data/game_table_seed.csv ships with the app, so a new install starts on the game's numbers."""

    def test_seed_loads_and_covers_the_rows_seen(self):
        from arty.config import GAME_TABLE_SEED
        g = GameTable.load(SPH2, GAME_TABLE_SEED)
        self.assertAlmostEqual(g.mil_for(LOW, 1303), 30, places=6)
        self.assertAlmostEqual(g.mil_for(HIGH, 2130), 1020, places=6)
        self.assertGreaterEqual(len(g.rows("low")) + len(g.rows("high")), 8)

    def test_your_log_adds_to_the_seed(self):
        import tempfile
        from pathlib import Path
        from arty.config import GAME_TABLE_SEED
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "sight_table.csv"
            log.write_text("time,mil,range_m,gun_asl\nt,60,1447,\n", encoding="utf-8")
            g = GameTable.load(SPH2, GAME_TABLE_SEED, log, Path(d) / "missing.csv")
            self.assertIn((60.0, 1447.0), g.rows("low"))
            self.assertIn((30.0, 1303.0), g.rows("low"))


class Misreads(unittest.TestCase):
    def test_a_row_off_its_neighbours_is_ignored(self):
        # 1,351 misread as 1,391: 40 m off the smooth line through 30 and 50 mil
        g = GameTable(SPH2, [(30, 1303), (40, 1391), (50, 1399)])
        self.assertAlmostEqual(g.mil_for(LOW, 1351), 40, delta=0.3)

    def test_repeated_reads_use_the_median(self):
        g = GameTable(SPH2, [(30, 1303), (30, 1303), (30, 1305), (30, 1301)])
        self.assertEqual(g.rows("low"), [(30.0, 1303.0)])


if __name__ == "__main__":
    unittest.main()
