"""Ground heights from the map's terrain heightfield.

Offline: a synthetic map checks the decoding, the mirrored Y axis, bilinear
sampling and the checksum. If the real Bakurani chunks are already cached, the
real-data test checks them against an in-game height read (skipped otherwise, so
the suite never needs the network).
"""
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from arty.ballistics import Point
from arty.terrain import DATA_DIR, TerrainError, TerrainMap

SIDE = 511


def chunk_bytes(fn) -> bytes:
    """A chunk whose raw sample at vertex (x, y) is fn(x, y)."""
    return b"".join(struct.pack("<H", fn(x, y)) for y in range(SIDE) for x in range(SIDE))


class SyntheticMap(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        # One chunk (0,0) covering game X 0..10.2 and Y 0..10.2, Y mirrored like the real maps.
        self.raw = chunk_bytes(lambda x, y: x * 100)  # rises 100 raw units per vertex eastward
        entry = {"file": "chunks/0_0.bin", "bytes": len(self.raw), "minLocalZ": 0.0, "maxLocalZ": 65535 / 1000,
                 "sha256": hashlib.sha256(self.raw).hexdigest()}
        manifest = {"format": "wardogs-landscape-collision-u16-v1", "chunkXMin": 0, "chunkXMax": 0,
                    "chunkYMin": 0, "chunkYMax": 0, "chunkQuads": 510, "verticesPerSide": 511,
                    "globalQuadOffsetX": 0, "globalQuadOffsetY": 510, "gameUnitsToLandscapeQuads": 50,
                    "gameUnitsToLandscapeQuadsX": 50, "gameUnitsToLandscapeQuadsY": -50,
                    "worldZOffsetMeters": 0.5, "worldZScaleMetersPerLocalUnit": 9,
                    "coverage": {"gameXMin": 0, "gameXMax": 10.2, "gameYMin": 0, "gameYMax": 10.2},
                    "chunks": {"0,0": entry}}
        (root / "synth").mkdir()
        (root / "synth" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.fetched = []

        def fetch(url):
            self.fetched.append(url)
            return self.served

        self.served = self.raw
        self.tm = TerrainMap("synth", root, fetch=fetch)

    def tearDown(self):
        self.dir.cleanup()

    def test_decodes_heights(self):
        # Vertex x = 50 * gameX; raw = 100 * x; localZ = raw/65535 * 65.535 = raw/1000.
        # worldZ = 0.5 + 9 * localZ. At gameX 1.0: x=50, raw=5000, localZ=5, z=45.5.
        self.assertAlmostEqual(self.tm.height(Point(1.0, 5.0)), 45.5, places=3)
        # Between samples it interpolates: gameX 1.01 is vertex 50.5.
        self.assertAlmostEqual(self.tm.height(Point(1.01, 5.0)), 0.5 + 9 * 5.05, places=3)

    def test_differences_are_what_matters(self):
        dz = self.tm.height(Point(3.0, 2.0)) - self.tm.height(Point(1.0, 2.0))
        self.assertAlmostEqual(dz, 9 * 10.0, places=3)  # 100 vertices east = 10 localZ

    def test_y_axis_is_mirrored(self):
        # Game Y runs opposite the landscape rows: game Y 10 is row 10, game Y 0 is row 510.
        loc_top, loc_bottom = self.tm.locate(Point(1, 10)), self.tm.locate(Point(1, 0))
        self.assertAlmostEqual(loc_top.y, 10, places=6)
        self.assertAlmostEqual(loc_bottom.y, 510, places=6)

    def test_outside_coverage_is_none_not_a_guess(self):
        self.assertIsNone(self.tm.height(Point(11.0, 5.0)))

    def test_downloads_once_then_caches(self):
        self.tm.height(Point(1, 1))
        self.tm.height(Point(2, 2))
        self.assertEqual(len(self.fetched), 1)
        again = TerrainMap("synth", Path(self.dir.name), fetch=lambda url: self.fail("should use disk cache"))
        self.assertAlmostEqual(again.height(Point(1.0, 5.0)), 45.5, places=3)

    def test_a_corrupt_download_is_refused(self):
        self.served = self.raw[:-2] + b"\x00\x00"
        with self.assertRaises(TerrainError):
            self.tm.height(Point(1, 1))


@unittest.skipUnless((DATA_DIR / "bakurani" / "chunks" / "28_21.bin").exists(), "Bakurani chunks not cached")
class RealBakurani(unittest.TestCase):
    def test_matches_a_height_read_in_game(self):
        # 2026-09-22: the map's hover label gave the target 37 m below the gun (ΔZ -37).
        tm = TerrainMap("bakurani", fetch=lambda url: self.fail("chunks should be cached"))
        gun, impact = Point(126.06, 67.67), Point(100.82, 65.34)  # first logged shot
        self.assertAlmostEqual(tm.height(impact) - tm.height(gun), -37, delta=3)


if __name__ == "__main__":
    unittest.main()
