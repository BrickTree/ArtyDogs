import unittest

from arty.ballistics import (Point, azimuth_deg, bracket, compass_point, corrected_aim, distance_m,
                             elevation_mil, load_data, miss_components, range_at_mil, shift_aim, solve, spread_m)

DATA = load_data()
MORTAR, SPH2 = DATA.weapons["mortar"], DATA.weapons["sph2"]


def arc(sol, arc_id):
    return next(a for a in sol.arcs if a.arc_id == arc_id).text()


def arc_of(sol, arc_id):
    return next(a for a in sol.arcs if a.arc_id == arc_id)


class ReferenceVectors(unittest.TestCase):
    """The vectors acidtib/artydog pins in docs/BALLISTICS.md."""

    def test_mortar(self):
        sol = solve(MORTAR, Point(50, 50), Point(53, 54))
        self.assertAlmostEqual(sol.distance_m, 500, places=6)
        self.assertAlmostEqual(sol.azimuth_deg, 36.87, places=2)
        self.assertTrue(sol.in_range)
        self.assertEqual(arc(sol, "single"), "461")

    def test_sph2_both_arcs(self):
        sol = solve(SPH2, Point(50, 50), Point(65, 50))
        self.assertAlmostEqual(sol.distance_m, 1500, places=6)
        self.assertAlmostEqual(sol.azimuth_deg, 90, places=6)
        self.assertEqual(arc(sol, "low"), "84")
        self.assertEqual(arc(sol, "high"), "1213")

    def test_sph2_below_low_arc_has_only_high(self):
        sol = solve(SPH2, Point(50, 50), Point(50, 60))  # 1000 m; low arc starts at 1181 m
        self.assertEqual([a.arc_id for a in sol.arcs], ["high"])


class Geometry(unittest.TestCase):
    def test_grid_unit_is_100m(self):
        self.assertEqual(distance_m(Point(0, 0), Point(1, 0)), 100)
        self.assertEqual(distance_m(Point(0, 0), Point(3, 4)), 500)

    def test_azimuth_clockwise_from_north(self):
        o = Point(0, 0)
        self.assertEqual(azimuth_deg(o, Point(0, 1)), 0)
        self.assertEqual(azimuth_deg(o, Point(1, 0)), 90)
        self.assertEqual(azimuth_deg(o, Point(0, -1)), 180)
        self.assertEqual(azimuth_deg(o, Point(-1, 0)), 270)

    def test_compass_points(self):
        self.assertEqual([compass_point(d) for d in (0, 22, 23, 90, 181, 359.9)], ["N", "N", "NE", "E", "S", "N"])

    def test_signed_deltas(self):
        sol = solve(MORTAR, Point(50, 50), Point(53, 46))
        self.assertAlmostEqual(sol.dx_m, 300, places=6)
        self.assertAlmostEqual(sol.dy_m, -400, places=6)


class RangeLimits(unittest.TestCase):
    def test_too_close_and_too_far(self):
        near = solve(MORTAR, Point(50, 50), Point(50, 50.5))
        self.assertEqual((near.status, round(near.off_by_m)), ("too_close", 82))
        self.assertEqual(near.arcs, ())
        far = solve(MORTAR, Point(50, 50), Point(50, 60))
        self.assertEqual((far.status, round(far.off_by_m)), ("too_far", 316))

    def test_exact_boundaries_are_in_range(self):
        self.assertTrue(solve(MORTAR, Point(0, 0), Point(0, 1.32)).in_range)
        self.assertTrue(solve(MORTAR, Point(0, 0), Point(0, 6.84)).in_range)


class Tables(unittest.TestCase):
    def test_duplicate_range_gives_span(self):
        high = next(a for a in SPH2.arcs if a.id == "high")
        self.assertEqual(elevation_mil(high.table, 2629), (610, 620))
        self.assertEqual(elevation_mil(high.table, 2629 * (1 + 4e-16)), (610, 620))

    def test_table_read_backwards(self):
        high = next(a for a in SPH2.arcs if a.id == "high")
        low = next(a for a in SPH2.arcs if a.id == "low")
        self.assertEqual(range_at_mil(high, 1020), 2098)  # the row the sight disagrees with (+32 m)
        self.assertEqual(range_at_mil(low, 100), 1576)
        self.assertAlmostEqual(range_at_mil(high, 1025), (2098 + 2072) / 2)
        self.assertIsNone(range_at_mil(low, 700))

    def test_interpolates_and_refuses_to_extrapolate(self):
        table = ((100.0, 900.0), (200.0, 800.0))
        self.assertEqual(elevation_mil(table, 150), (850, 850))
        self.assertIsNone(elevation_mil(table, 99))
        self.assertIsNone(elevation_mil(table, 201))
        self.assertIsNone(elevation_mil(table, float("nan")))


class HeightDifference(unittest.TestCase):
    """The SPH-2 low arc carries a measured height correction; nothing else does."""

    def solve_dz(self, dz, gun=Point(50, 50), target=Point(65, 50)):  # 1500 m east
        return solve(SPH2, gun, target, 100.0, dz=dz, bands=DATA.height_bands)

    def test_flat_solution_is_untouched(self):
        flat = self.solve_dz(0.0)
        self.assertEqual(arc(flat, "low"), "84")
        self.assertEqual(arc(flat, "high"), "1213")
        self.assertEqual(arc_of(flat, "low").height_mil, 0.0)

    def test_target_above_needs_more_elevation(self):
        flat = arc_of(self.solve_dz(0.0), "low")
        low = arc_of(self.solve_dz(40.0), "low")
        self.assertAlmostEqual(low.mil_per_meter, 0.68, places=1)
        self.assertAlmostEqual(low.height_mil, 27, delta=1)
        self.assertAlmostEqual(low.min_mil, flat.min_mil + low.height_mil, places=6)
        self.assertFalse(low.extrapolated)

    def test_below_and_above_are_symmetric(self):
        self.assertAlmostEqual(arc_of(self.solve_dz(30.0), "low").height_mil,
                               -arc_of(self.solve_dz(-30.0), "low").height_mil, places=6)

    def test_beyond_measured_band_is_flagged(self):
        self.assertTrue(arc_of(self.solve_dz(60.0), "low").extrapolated)

    def test_arcs_without_data_say_so(self):
        self.assertIsNone(arc_of(self.solve_dz(40.0), "high").mil_per_meter)
        self.assertEqual(arc_of(self.solve_dz(40.0), "high").height_mil, 0.0)
        mortar = solve(MORTAR, Point(50, 50), Point(53, 54), 100.0, dz=20.0, bands=DATA.height_bands)
        self.assertIsNone(arc_of(mortar, "single").mil_per_meter)

    def test_outside_the_covered_distances_there_is_no_slope(self):
        # The band covers 1283-2439 m; 1000 m is below it (high arc only anyway).
        near = solve(SPH2, Point(50, 50), Point(50, 60), 100.0, dz=20.0, bands=DATA.height_bands)
        self.assertTrue(all(a.mil_per_meter is None for a in near.arcs))


class Dispersion(unittest.TestCase):
    def test_matches_published_spreads(self):
        self.assertAlmostEqual(spread_m(685, MORTAR.moa), 10.0, delta=0.1)
        self.assertAlmostEqual(spread_m(2660, SPH2.moa), 7.7, delta=0.1)
        self.assertIsNone(spread_m(1000, None))


class LineOfFire(unittest.TestCase):
    gun = Point(50, 50)

    def test_add_moves_further_along_the_bearing(self):
        target = Point(53, 54)  # 500 m at 36.87 deg
        aim = shift_aim(self.gun, target, add_m=50)
        self.assertAlmostEqual(distance_m(self.gun, aim), 550, places=6)
        self.assertAlmostEqual(azimuth_deg(self.gun, aim), azimuth_deg(self.gun, target), places=6)

    def test_right_is_clockwise_of_the_line(self):
        north = Point(50, 55)
        aim = shift_aim(self.gun, north, right_m=30)
        self.assertAlmostEqual(aim.x, 50.3)  # firing north, right is east
        self.assertAlmostEqual(aim.y, 55.0)

    def test_components_undo_a_shift(self):
        target = Point(61.2, 43.9)
        for add, right in ((25, -10), (-40, 15), (0, 0)):
            moved = shift_aim(self.gun, target, add, right)
            long_m, right_m = miss_components(self.gun, target, moved)
            self.assertAlmostEqual(long_m, add, places=6)
            self.assertAlmostEqual(right_m, right, places=6)

    def test_bracket(self):
        lines = (132.0, 187.0, 240.0)
        self.assertEqual(bracket(lines, 200), (187.0, 240.0, (200 - 187) / 53))
        self.assertIsNone(bracket(lines, 100))


class FireAdjustment(unittest.TestCase):
    def test_correction_stored_along_the_line_reproduces_the_aim(self):
        # How the app stores an F9 correction: as further/right from the target.
        gun, target = Point(50, 50), Point(62, 58)
        impact = Point(62.25, 57.8)  # landed long and to the right
        new_aim = corrected_aim(target, target, impact)
        stored = miss_components(gun, target, new_aim)
        rebuilt = shift_aim(gun, target, *stored)
        self.assertAlmostEqual(rebuilt.x, new_aim.x, places=9)
        self.assertAlmostEqual(rebuilt.y, new_aim.y, places=9)

    def test_constant_bias_is_cancelled_in_one_step(self):
        target, bias = Point(80, 70), (0.3, -0.4)  # every shell lands 30 m east, 40 m south of the aim
        aim = target
        impact = Point(aim.x + bias[0], aim.y + bias[1])
        aim = corrected_aim(aim, target, impact)
        next_impact = Point(aim.x + bias[0], aim.y + bias[1])
        self.assertAlmostEqual(next_impact.x, target.x)
        self.assertAlmostEqual(next_impact.y, target.y)


if __name__ == "__main__":
    unittest.main()
