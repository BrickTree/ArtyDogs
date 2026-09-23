"""Gun trim: a gun spot's steady error, learned from its own logged shots.

The real fixture is 13 SPH-2 shots from one gun spot (2026-09-22) that all went
left, more so the higher the barrel (-0.4 deg at 286 mil to -2.8 deg at 571 mil),
and ~31 m short. Earlier shots from other spots showed neither.
"""
import csv
import math
import random
import shutil
import statistics
import tempfile
import unittest
from pathlib import Path

from arty.ballistics import Point, azimuth_deg, distance_m, elevation_mil, load_data, range_at_mil
from arty.shotlog import FIELDS, ShotLog
from arty.trim import Obs, height_mil_for, learn

DATA = load_data()
SPH2 = DATA.weapons["sph2"]
LOW = next(a for a in SPH2.arcs if a.id == "low")
GUN = Point(126.06, 67.67)
REAL = Path(__file__).parent / "data" / "shots_canted_gun.csv"


def fire_shot(gun, distance, az, cant_deg=0.0, range_bias=0.0, noise=(0.0, 0.0), rng=None, height_mil=0.0):
    """Simulate one shot: the gun dials the table's mil for `distance` along `az`."""
    mil = sum(elevation_mil(LOW.table, distance)) / 2
    fire = Point(gun.x + distance * math.sin(math.radians(az)) / 100, gun.y + distance * math.cos(math.radians(az)) / 100)
    defl = math.degrees(math.atan(math.tan(mil * 2 * math.pi / 6400) * math.sin(math.radians(cant_deg))))
    n_az, n_rng = (rng.gauss(0, noise[0]), rng.gauss(0, noise[1])) if rng else (0.0, 0.0)
    landed = range_at_mil(LOW, mil) + range_bias + n_rng
    th = math.radians(az + defl + n_az)
    impact = Point(gun.x + landed * math.sin(th) / 100, gun.y + landed * math.cos(th) / 100)
    return Obs("sph2", "low", gun, fire, impact, mil + height_mil, height_mil)


class Learning(unittest.TestCase):
    def test_recovers_a_canted_gun_and_a_range_bias(self):
        rng = random.Random(4)
        obs = [fire_shot(GUN, d, 265 + rng.uniform(-5, 5), cant_deg=-3.6, range_bias=-30,
                         noise=(0.3, 12), rng=rng) for d in range(2300, 2620, 25)]
        t = learn(obs, "sph2", LOW, GUN, 2500)
        self.assertTrue(t.az_on and t.range_on)
        self.assertAlmostEqual(t.cant_deg, -3.6, delta=0.8)
        self.assertAlmostEqual(t.range_m, -30, delta=10)
        # The fix points the gun back the other way: right, and further.
        add, right = t.offsets(2500, 400, height_applied=False)
        self.assertGreater(add, 15)
        self.assertGreater(right, 30)

    def test_offsets_cancel_the_error_exactly_without_noise(self):
        obs = [fire_shot(GUN, d, 265, cant_deg=-3.0, range_bias=-25) for d in (2350, 2450, 2550, 2600)]
        t = learn(obs, "sph2", LOW, GUN, 2450)
        mil = sum(elevation_mil(LOW.table, 2450)) / 2
        self.assertAlmostEqual(t.deflection_deg(mil),
                               math.degrees(math.atan(math.tan(mil * 2 * math.pi / 6400) * math.sin(math.radians(-3.0)))),
                               places=4)
        self.assertAlmostEqual(t.range_m, -25, places=4)

    def test_pure_scatter_teaches_nothing(self):
        rng = random.Random(11)
        obs = [fire_shot(GUN, d, 90, noise=(0.4, 15), rng=rng) for d in (1500, 1700, 1900, 2100, 2300)]
        t = learn(obs, "sph2", LOW, GUN, 1900)
        self.assertFalse(t.active)
        self.assertEqual(t.offsets(1900, 200, False), (0.0, 0.0))

    def test_fewer_than_three_shots_applies_nothing(self):
        obs = [fire_shot(GUN, d, 265, cant_deg=-4, range_bias=-40) for d in (2400, 2500)]
        self.assertFalse(learn(obs, "sph2", LOW, GUN, 2450).active)

    def test_a_new_gun_spot_starts_from_scratch(self):
        # Cant belongs to where the gun is parked; 60 m away is somewhere else.
        obs = [fire_shot(GUN, d, 265, cant_deg=-4) for d in (2350, 2450, 2550)]
        moved = Point(GUN.x + 0.6, GUN.y)
        self.assertEqual(learn(obs, "sph2", LOW, moved, 2450).shots_az, 0)

    def test_height_corrected_shots_stay_out_of_the_range_trim(self):
        obs = [fire_shot(GUN, d, 265, range_bias=-150, height_mil=-34) for d in (2300, 2350, 2400)]
        obs += [fire_shot(GUN, d, 265, range_bias=-30) for d in (2450, 2500, 2550)]
        t = learn(obs, "sph2", LOW, GUN, 2450)
        self.assertEqual(t.shots_range, 3)
        self.assertAlmostEqual(t.range_m, -30, places=4)

    def test_range_trim_switches_off_under_a_height_correction(self):
        obs = [fire_shot(GUN, d, 265, range_bias=-30) for d in (2400, 2450, 2500)]
        t = learn(obs, "sph2", LOW, GUN, 2450)
        self.assertEqual(t.offsets(2450, 400, height_applied=True)[0], 0.0)


class RealShots(unittest.TestCase):
    """Each real shot predicted from the other twelve: out of sample, not fitted to itself."""

    def setUp(self):
        band = DATA.band("sph2", "low")
        self.obs = []
        with open(REAL, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.obs.append(Obs("sph2", r["arc"], Point(float(r["gun_x"]), float(r["gun_y"])),
                                    Point(float(r["aim_x"]), float(r["aim_y"])),
                                    Point(float(r["impact_x"]), float(r["impact_y"])), float(r["mil"]),
                                    height_mil_for(band, float(r["distance_m"]), float(r["dz_m"]))))

    def test_leave_one_out_first_shots_land_much_closer(self):
        before, after = [], []
        for i, o in enumerate(self.obs):
            if o.height_mil:
                continue
            dist = distance_m(o.gun, o.fire)
            t = learn(self.obs[:i] + self.obs[i + 1:], "sph2", LOW, o.gun, dist)
            daz = (azimuth_deg(o.gun, o.impact) - azimuth_deg(o.gun, o.fire) + 180) % 360 - 180
            side = dist * math.tan(math.radians(daz))
            side_fix = side - dist * math.tan(math.radians(t.deflection_deg(o.mil)))
            rng_miss = distance_m(o.gun, o.impact) - range_at_mil(LOW, o.mil)
            before.append(math.hypot(side, rng_miss))
            after.append(math.hypot(side_fix, rng_miss - (t.range_m if t.range_on else 0.0)))
        self.assertEqual(len(before), 11)
        self.assertGreater(statistics.median(before), 60)  # ~77 m as fired
        self.assertLess(statistics.median(after), 25)  # ~13 m with the trim

    def test_the_real_gun_was_tilted_about_three_and_a_half_degrees(self):
        t = learn(self.obs, "sph2", LOW, GUN, 2500)
        self.assertTrue(t.az_on)
        self.assertAlmostEqual(t.cant_deg, -3.6, delta=0.5)
        self.assertAlmostEqual(t.range_m, -30, delta=8)


class OldLogs(unittest.TestCase):
    def test_an_old_log_is_upgraded_not_misaligned(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "shots.csv"
            shutil.copy(REAL, path)  # written before the trim columns existed
            log = ShotLog(path)
            self.assertEqual(len(log.shots), 13)
            self.assertIsNone(log.shots[0].height_mil)
            self.assertEqual(log.shots[0].fire, (log.shots[0].aim_x, log.shots[0].aim_y))
            new = log.shots[0]
            new.fire_x, new.fire_y, new.height_mil, new.trim_right_m = 1.0, 2.0, -3.0, 44.0
            log.add(new)
            again = ShotLog(path)
            self.assertEqual(len(again.shots), 14)
            self.assertEqual(again.shots[-1].fire, (1.0, 2.0))
            self.assertEqual(again.shots[-1].trim_right_m, 44.0)
            self.assertEqual(again.shots[0].long_m, log.shots[0].long_m)  # old rows kept their values
            with open(path, newline="", encoding="utf-8") as f:
                self.assertEqual(next(csv.reader(f)), FIELDS)
            self.assertTrue(path.with_suffix(".csv.bak").exists())


if __name__ == "__main__":
    unittest.main()
