# Third-party notices

## Height correction (`data/height_correction.json`)

Condensed by `tools/extract_height_correction.py` from the terrain-correction
surface `data/ballistics/terrain-correction/low-main.json` in
apollyon-sys/wardogs-calculator (MIT, notice below). That project marks the
surface experimental and disabled by default, and so is anything derived from it
here. Its companion high-arc payload was not usable and is not included.

## Terrain heights (`data/terrain/*/manifest.json` and downloaded chunks)

The Terrain3D heightfields for Bakurani, Ozeti and Zestafona come from
apollyon-sys/wardogs-calculator (MIT, notice below): its manifests are included,
and its chunks are downloaded from that project's public release
(`assets.wardogs-artillery.com`) on first use, checked against the manifest's
SHA-256, and cached. Its authors note the absolute level is offset; only height
differences are used here.

## Firing tables (`data/weapons.json`)

The L81 Mortar and SPH-2 firing tables and the map bounds are community
measurements. They are published identically in these two MIT-licensed projects:

- https://github.com/acidtib/artydog (`apps/desktop/src/calculator/data/weapons.json`, `docs/BALLISTICS.md`)
- https://github.com/apollyon-sys/wardogs-calculator (`data/weapons.json`)

They are not official BULKHEAD or WARDOGS data.

```
MIT License

Copyright (c) 2026 acidtib

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

```
MIT License

Copyright (c) 2026 Apollyon

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## OCR (installed by `run.bat`, not bundled here)

- RapidOCR (`rapidocr_onnxruntime`), Apache-2.0, with the PaddleOCR PP-OCRv3
  models (Apache-2.0) that ship inside its wheel.
- ONNX Runtime, MIT. Pillow, MIT-CMU. NumPy, BSD-3-Clause. OpenCV, Apache-2.0.

WARDOGS is a trademark of its owners. This project is not affiliated with or
endorsed by BULKHEAD or Team17.
