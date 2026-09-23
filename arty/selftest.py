"""`WARDOGS-Arty.exe --selftest`: prove a build has everything it needs, without a click.

A packaged app can start fine and still be missing an OCR model or a data file
that only shows up on the first hotkey. This loads the firing tables, the height
data and the terrain manifests, starts the OCR engine and reads a rendered
coordinate readout, then writes the results to selftest.txt beside the app and
exits 0 (all good) or 1.
"""
from __future__ import annotations

import time
import traceback

from PIL import Image, ImageDraw, ImageFont

from . import __version__
from . import config as cfgmod


def run() -> int:
    lines, ok = [f"WARDOGS Arty {__version__} self-test"], True

    def step(name: str, fn) -> None:
        nonlocal ok
        t0 = time.perf_counter()
        try:
            detail = fn()
            lines.append(f"PASS {name} ({(time.perf_counter() - t0) * 1000:.0f} ms) {detail or ''}".rstrip())
        except Exception:  # noqa: BLE001 - report every failure, keep going
            ok = False
            lines.append(f"FAIL {name}\n{traceback.format_exc()}")

    def tables():
        from .ballistics import load_data
        d = load_data()
        return f"{len(d.weapons)} weapons, {len(d.height_bands)} height band(s)"

    def terrain():
        from .terrain import available_maps, TerrainMap
        maps = available_maps()
        for m in maps:
            TerrainMap(m)  # parses and validates the manifest
        if not maps:
            raise RuntimeError("no terrain manifests bundled")
        return ", ".join(maps)

    def ocr_read():
        from .ocr import Ocr, ReadoutReader
        img = Image.new("RGB", (600, 400), (48, 58, 44))
        font = ImageFont.truetype("arial.ttf", 20)
        d = ImageDraw.Draw(img)
        d.text((300, 180), "y73.39", font=font, fill=(235, 235, 225))
        d.text((325, 237), "x81.57", font=font, fill=(235, 235, 225))
        res = ReadoutReader(Ocr()).read(img, (0, 0), (300, 220))
        if res.coord is None or (res.coord.x, res.coord.y) != (81.57, 73.39):
            raise RuntimeError(f"read {res.coord} ({res.message})")
        return f"read x{res.coord.x} y{res.coord.y}"

    step("firing tables", tables)
    step("terrain manifests", terrain)
    step("OCR engine reads a coordinate readout", ocr_read)
    lines.append("RESULT " + ("OK" if ok else "FAILED"))
    (cfgmod.APP_DIR / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
