import tempfile
import unittest
from pathlib import Path

from arty.shotlog import Shot, ShotLog


def shot(long_m, right_m=0.0, arc="low", weapon="sph2", distance=1500.0):
    return Shot(time="2026-09-22T10:00:00", weapon=weapon, arc=arc, distance_m=distance, azimuth_deg=90.0,
                mil=84.0, dz_m=0.0, gun_x=50, gun_y=50, target_x=65, target_y=50, aim_x=65, aim_y=50,
                impact_x=65 + long_m / 100, impact_y=50, long_m=long_m, right_m=right_m,
                miss_m=abs(long_m), to_target_m=abs(long_m), read_confident=True)


class ShotLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "logs" / "shots.csv"

    def tearDown(self):
        self.dir.cleanup()

    def test_survives_a_restart(self):
        log = ShotLog(self.path)
        log.add(shot(12.0, -3.0))
        log.add(shot(-4.0, 1.5))
        again = ShotLog(self.path)
        self.assertEqual([s.long_m for s in again.shots], [12.0, -4.0])
        self.assertIs(again.shots[0].read_confident, True)

    def test_consistent_long_shots_are_flagged_as_bias(self):
        log = ShotLog(self.path)
        for m in (18, 22, 20, 19):
            log.add(shot(m))
        (s,) = log.summaries()
        self.assertEqual((s.weapon, s.arc, s.shots), ("sph2", "low", 4))
        self.assertAlmostEqual(s.mean_long_m, 19.75)
        self.assertTrue(s.biased)

    def test_scatter_is_not_bias(self):
        log = ShotLog(self.path)
        for m in (15, -18, 9, -7):
            log.add(shot(m))
        self.assertFalse(log.summaries()[0].biased)

    def test_two_shots_prove_nothing(self):
        log = ShotLog(self.path)
        log.add(shot(30))
        log.add(shot(31))
        self.assertFalse(log.summaries()[0].biased)

    def test_groups_by_weapon_and_arc(self):
        log = ShotLog(self.path)
        log.add(shot(5, arc="low"))
        log.add(shot(5, arc="high"))
        log.add(shot(5, weapon="mortar", arc="single", distance=400))
        self.assertEqual([(s.weapon, s.arc) for s in log.summaries()],
                         [("mortar", "single"), ("sph2", "high"), ("sph2", "low")])

    def test_new_session_keeps_the_old_log(self):
        log = ShotLog(self.path)
        log.add(shot(5))
        archived = log.start_new_session()
        self.assertTrue(archived.exists())
        self.assertEqual(log.shots, [])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
