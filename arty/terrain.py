"""Ground height from the map's own terrain, so ΔZ comes from coordinates, not screen reads.

The heightfields are the apollyon-sys/wardogs-calculator Terrain3D datasets (MIT),
reconstructed from the game's landscape collision data for Bakurani, Ozeti and
Zestafona. Each map is 16x16 chunks of 511x511 u16 samples, 2 m apart. A chunk
decodes as localZ = min + raw/65535 * (max - min) using its manifest entry, then
worldZ = offset + localZ * scale.

The absolute level sits ~900 m below the game's ASL readout (the dataset's own
docs say so), so only DIFFERENCES are used: every ΔZ the app needs is a
difference. `datum` optionally shifts heights onto the in-game ASL scale when
one real ASL reading has been matched to a coordinate.

Manifests ship in data/terrain/<map>/manifest.json. Chunks (~0.5 MB each, 256 per
map) download on first use from the dataset's public release, are checked
against the manifest's SHA-256, and are cached. A gun and target need 1-4 chunks.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .ballistics import Point

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "terrain"
# Downloaded chunks: beside the manifests from source; beside the .exe in a packaged build,
# whose bundled data folder is read-only in spirit and replaced by every update.
CACHE_DIR = Path(sys.executable).resolve().parent / "terrain_cache" if getattr(sys, "frozen", False) else DATA_DIR
RELEASE = "https://assets.wardogs-artillery.com/releases/assets-v1/data/terrain/{map}/"
FORMAT = "wardogs-landscape-collision-u16-v1"


class TerrainError(RuntimeError):
    pass


@dataclass(frozen=True)
class Located:
    key: str
    x: float  # vertex coordinates inside the chunk
    y: float


class TerrainMap:
    def __init__(self, map_id: str, manifest_dir: Path = DATA_DIR, cache_dir: Path | None = None,
                 fetch=None) -> None:
        self.map_id = map_id
        path = manifest_dir / map_id / "manifest.json"
        self.m = json.loads(path.read_text(encoding="utf-8"))
        if self.m.get("format") != FORMAT or self.m.get("verticesPerSide") != 511 or self.m.get("chunkQuads") != 510:
            raise TerrainError(f"{map_id}: unsupported terrain manifest")
        default = CACHE_DIR if manifest_dir == DATA_DIR else manifest_dir
        self.cache_dir = (cache_dir or default) / map_id / "chunks"
        self._fetch = fetch or _download
        self._chunks: dict[str, bytes] = {}
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return self.map_id.capitalize()

    def covers(self, p: Point) -> bool:
        c = self.m["coverage"]
        return c["gameXMin"] <= p.x <= c["gameXMax"] and c["gameYMin"] <= p.y <= c["gameYMax"]

    def locate(self, p: Point) -> Located | None:
        if not self.covers(p):
            return None
        m = self.m
        qx = m["globalQuadOffsetX"] + p.x * m.get("gameUnitsToLandscapeQuadsX", m["gameUnitsToLandscapeQuads"])
        qy = m["globalQuadOffsetY"] + p.y * m.get("gameUnitsToLandscapeQuadsY", m["gameUnitsToLandscapeQuads"])
        n = m["chunkQuads"]
        cx = min(max(math.floor(qx / n), m["chunkXMin"]), m["chunkXMax"])
        cy = min(max(math.floor(qy / n), m["chunkYMin"]), m["chunkYMax"])
        key = f"{cx},{cy}"
        if key not in m["chunks"]:
            return None
        return Located(key, min(max(qx - cx * n, 0.0), n), min(max(qy - cy * n, 0.0), n))

    def chunk(self, key: str) -> bytes:
        """The raw chunk, from memory, the disk cache, or the release (verified)."""
        with self._lock:
            if key in self._chunks:
                return self._chunks[key]
        entry = self.m["chunks"][key]
        path = self.cache_dir / Path(entry["file"]).name
        data = path.read_bytes() if path.exists() else None
        if data is None or not _ok(data, entry):
            data = self._fetch(RELEASE.format(map=self.map_id) + entry["file"])
            if not _ok(data, entry):
                raise TerrainError(f"{self.map_id} chunk {key} failed its checksum")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with self._lock:
            self._chunks[key] = data
        return data

    def height(self, p: Point) -> float | None:
        """Ground height at a map coordinate (dataset datum), bilinear between the 2 m samples."""
        loc = self.locate(p)
        if loc is None:
            return None
        data, entry = self.chunk(loc.key), self.m["chunks"][loc.key]
        side = self.m["verticesPerSide"]
        lo, hi = entry["minLocalZ"], entry["maxLocalZ"]

        def z(x: int, y: int) -> float:
            raw = int.from_bytes(data[(y * side + x) * 2:(y * side + x) * 2 + 2], "little")
            local = lo + raw / 65535 * (hi - lo)
            return self.m["worldZOffsetMeters"] + local * self.m["worldZScaleMetersPerLocalUnit"]

        x0, y0 = min(int(loc.x), side - 1), min(int(loc.y), side - 1)
        x1, y1 = min(x0 + 1, side - 1), min(y0 + 1, side - 1)
        fx, fy = loc.x - x0, loc.y - y0
        top = z(x0, y0) + (z(x1, y0) - z(x0, y0)) * fx
        bottom = z(x0, y1) + (z(x1, y1) - z(x0, y1)) * fx
        return top + (bottom - top) * fy


def available_maps(manifest_dir: Path = DATA_DIR) -> list[str]:
    return sorted(p.parent.name for p in manifest_dir.glob("*/manifest.json"))


def _ok(data: bytes, entry: dict) -> bool:
    return len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "wardogs-arty"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()
