"""Condense the community terrain-correction surfaces into mil-per-metre slopes.

The surfaces (apollyon-sys/wardogs-calculator, MIT) store, for every distance, the
height difference at which the dialled command changes by 10 mil. Within their
covered band that relationship is linear in dZ to well under a mil, so a slope per
distance reproduces the surface at a fraction of the size.

    python tools/extract_height_correction.py <dir-with-tc_*.json> [-o data/height_correction.json]

Source payloads: data/ballistics/terrain-correction/{low-main,high-v2}.json
Both are marked experimental by their authors; so is anything derived here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _fit_slope(dz: list[float], mil: list[float]) -> tuple[float, float] | None:
    """Least-squares mil-per-metre, plus the worst residual in mil."""
    if len(dz) < 3:
        return None
    a = np.polyfit(dz, mil, 1)
    worst = float(np.max(np.abs(np.polyval(a, dz) - mil)))
    return float(a[0]), worst


def low_arc(payload: dict, limit: float) -> list[list[float]]:
    rep = payload["representation"]
    nodes = rep["distanceNodes"]
    bnds = rep["boundariesMrad"]
    lo, hi = rep["minDeltaZMetersByBoundary"], rep["maxDeltaZMetersByBoundary"]
    out = []
    for i, dist in enumerate(nodes):
        dz, mil = [], []
        for b, mrad in enumerate(bnds):
            z = (lo[b][i] + hi[b][i]) / 2
            if abs(z) <= limit:
                dz.append(z)
                mil.append(float(mrad))
        fit = _fit_slope(dz, mil)
        if fit:
            out.append([float(dist), round(fit[0], 5), round(fit[1], 3)])
    return out


def high_arc(payload: dict, limit: float) -> list[list[float]]:
    rep = payload["representation"]
    curves: list[tuple[float, np.ndarray, np.ndarray]] = []
    for b in rep["boundaries"]:
        for seg in b.get("segments", []):
            d = np.asarray(seg["distanceNodes"], float)
            z = (np.asarray(seg["minDeltaZMeters"], float) + np.asarray(seg["maxDeltaZMeters"], float)) / 2
            order = np.argsort(d)
            curves.append((float(b["boundaryMrad"]), d[order], z[order]))
    dom = payload["domain"]
    out = []
    for dist in np.arange(dom["distanceMinMeters"], dom["distanceMaxMeters"] + 1, 4.0):
        dz, mil = [], []
        for mrad, d, z in curves:
            if d[0] <= dist <= d[-1]:
                value = float(np.interp(dist, d, z))
                if abs(value) <= limit:
                    dz.append(value)
                    mil.append(mrad)
        fit = _fit_slope(dz, mil)
        if fit:
            out.append([float(dist), round(fit[0], 5), round(fit[1], 3)])
    return out


def thin(rows: list[list[float]], step_m: float = 25.0) -> list[list[float]]:
    """Keep a slope roughly every `step_m`, always keeping both ends."""
    kept = [rows[0]]
    for row in rows[1:-1]:
        if row[0] - kept[-1][0] >= step_m:
            kept.append(row)
    kept.append(rows[-1])
    return [[r[0], r[1]] for r in kept]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path, help="directory holding tc_low-main.json and tc_high-v2.json")
    ap.add_argument("-o", "--out", type=Path, default=Path("data/height_correction.json"))
    args = ap.parse_args()

    low = json.loads((args.source / "tc_low-main.json").read_text(encoding="utf-8"))
    # The high-arc payload stores each 10-mil boundary over a ~20 m window of distance,
    # so any single distance has one to three points: too few to fit, and the attempt
    # lands 20+ mil out. Its authors mark it a disabled research candidate. Left out.
    arcs = {}
    for name, payload, fn in (("low", low, low_arc),):
        dom = payload["domain"]
        limit = min(abs(dom["deltaZMinMeters"]), abs(dom["deltaZMaxMeters"]))
        rows = fn(payload, limit)
        worst = max(r[2] for r in rows)
        arcs[name] = {
            "weapon": "sph2",
            "minDistanceMeters": rows[0][0],
            "maxDistanceMeters": rows[-1][0],
            "maxAbsDeltaZMeters": limit,
            "worstFitMil": round(worst, 2),
            "milPerMeter": thin(rows),
        }
        print(f"{name:5s}: {len(rows)} nodes -> {len(arcs[name]['milPerMeter'])} kept, "
              f"{rows[0][0]:.0f}-{rows[-1][0]:.0f} m, |dZ|<={limit:.0f} m, worst linear-fit error {worst:.2f} mil")
    out = {
        "schemaVersion": 1,
        "status": "EXPERIMENTAL - community estimate, not measured in game by this project",
        "source": ("Condensed from the terrain-correction surfaces in "
                   "github.com/apollyon-sys/wardogs-calculator (MIT), which their own release marks "
                   "experimental and disabled by default. Flat firing tables remain authoritative."),
        "note": "corrected_mil = flat_table_mil(distance) + milPerMeter(distance) * (target_height - gun_height)",
        "arcs": arcs,
    }
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
