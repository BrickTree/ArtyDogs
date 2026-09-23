"""Comparing the game's own height readouts with the terrain."""
import tempfile
import unittest
from pathlib import Path

from arty.ballistics import Point
from arty.elevcheck import ElevationLog, Reading, best_map, detect_map, fit, range_vs_ground


def ground(p: Point) -> float:
    """A made-up terrain: rises 2 m per map unit east, 1 m per unit north, 900 m below the game's datum."""
    return -900 + 2 * p.x + p.y


def other(p: Point) -> float:
    return -500 + 7 * p.y - 3 * p.x  # a different map


def reads(source, pts, offset, noise=()):
    noise = list(noise) or [0.0] * len(pts)
    return [Reading("t", source, "target", p, ground(p) + offset + n, "") for p, n in zip(pts, noise)]


PTS = [Point(10, 20), Point(40, 25), Point(70, 60), Point(90, 15), Point(55, 80)]


class Fits(unittest.TestCase):
    def test_a_matching_terrain_is_one_steady_offset(self):
        (f,) = fit(reads("hover", PTS, 942.0, (0.4, -0.6, 0.2, 0.9, -0.5)), ground)
        self.assertAlmostEqual(f.offset, 942.08, places=2)
        self.assertLess(f.sd, 1.0)
        self.assertTrue(f.matches)

    def test_sources_on_different_scales_show_up_as_different_offsets(self):
        fits = {f.source: f for f in fit(reads("hover", PTS, 942.0) + reads("sight", PTS[:3], 979.0), ground)}
        self.assertAlmostEqual(fits["sight"].offset - fits["hover"].offset, 37.0, places=6)
        self.assertTrue(fits["hover"].matches and fits["sight"].matches)

    def test_a_wrong_terrain_does_not_match(self):
        (f,) = fit(reads("hover", PTS, 942.0), other)
        self.assertFalse(f.matches)

    def test_the_heights_pick_the_map(self):
        map_id, fits = best_map(reads("hover", PTS, 942.0, (0.3, -0.2, 0.1, 0, 0.4)),
                                {"right": type("M", (), {"height": staticmethod(ground)}),
                                 "wrong": type("M", (), {"height": staticmethod(other)})})
        self.assertEqual(map_id, "right")

    def test_one_wild_read_is_set_aside(self):
        # 2026-09-22: one hover label sat ~450 m off every other read on the right map.
        rs = reads("hover", PTS, 942.0, (0.3, -0.2, 0.1, 0, 0.4)) + [Reading("t", "hover", "target", Point(5, 5), 5.0, "")]
        (f,) = fit(rs, ground)
        self.assertEqual((f.n, f.rejected), (5, 1))
        self.assertAlmostEqual(f.offset, 942.12, places=2)
        self.assertTrue(f.matches)

    def test_a_wild_read_cant_hand_the_win_to_the_wrong_map(self):
        maps = {"right": type("M", (), {"height": staticmethod(ground)}),
                "wrong": type("M", (), {"height": staticmethod(other)})}
        rs = reads("hud", PTS[:4], 943.0, (0.1, -0.1, 0.0, 0.1)) + reads("hover", PTS, 951.0, (9, -8, 10, -11, 3))
        rs.append(Reading("t", "hover", "target", Point(5, 5), 5.0, ""))
        self.assertEqual(detect_map(rs, maps), "right")

    def test_no_clear_winner_detects_nothing(self):
        twin = type("M", (), {"height": staticmethod(lambda p: ground(p) + 0.5)})
        maps = {"right": type("M", (), {"height": staticmethod(ground)}), "twin": twin}
        self.assertIsNone(detect_map(reads("hover", PTS, 942.0, (0.3, -0.2, 0.1, 0, 0.4)), maps))

    def test_off_map_points_are_skipped(self):
        fits = fit(reads("hover", PTS, 942.0), lambda p: None if p.x > 50 else ground(p))
        self.assertEqual(fits[0].n, 2)


class RangeVsGround(unittest.TestCase):
    def test_recovers_the_slope(self):
        pairs = [(dz, -1.1 * dz - 12) for dz in (-40, -25, -10, 0, 15, 30)]
        slope, se, icpt, n = range_vs_ground(pairs)
        self.assertAlmostEqual(slope, -1.1, places=6)
        self.assertAlmostEqual(icpt, -12, places=6)
        self.assertEqual(n, 6)

    def test_needs_spread_and_shots(self):
        self.assertIsNone(range_vs_ground([(1, 2), (2, 3), (3, 4)]))
        self.assertIsNone(range_vs_ground([(5, 1), (5, 2), (5, 3), (5, 4)]))


class Log(unittest.TestCase):
    def test_round_trip_and_no_repeats(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "elevations.csv"
            log = ElevationLog(path)
            self.assertIsNotNone(log.add("sight", "gun", Point(1, 2), 48, "bakurani"))
            self.assertIsNone(log.add("sight", "gun", Point(1, 2), 48, "bakurani"))  # same reading again
            self.assertIsNotNone(log.add("hover", "target", Point(3, 4), 22, ""))
            again = ElevationLog(path)
            self.assertEqual([(r.source, r.asl, r.map) for r in again.readings],
                             [("sight", 48.0, "bakurani"), ("hover", 22.0, "")])


if __name__ == "__main__":
    unittest.main()
