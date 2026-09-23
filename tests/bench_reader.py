"""Benchmark the readout reader on synthetic screens: accuracy, wrong reads, latency.

    .venv\\Scripts\\python.exe -m tests.bench_reader [cases-per-layout]

"wrong+confident" is the number that matters most: a wrong read the app would
present without a warning.
"""
from __future__ import annotations

import random
import statistics
import sys
from collections import Counter

from arty.ocr import Ocr, ReadoutReader
from tests.synth import make_case

LAYOUTS = ("split", "cursor", "fixed", "chat")  # "split" is the real WARDOGS layout


def run(n: int = 30, seed: int = 5, ocr: Ocr | None = None) -> dict:
    ocr = ocr or Ocr()
    rng = random.Random(seed)
    report = {}
    for layout in LAYOUTS:
        reader = ReadoutReader(ocr)  # fresh memory per layout, so learning is exercised
        ok = wrong = miss = confident = wrong_confident = 0
        times, stages, failures = [], Counter(), []
        for _ in range(n):
            case = make_case(rng, layout)
            res = reader.read(case.image, (0, 0), case.cursor)
            times.append(res.ms)
            stages[res.region or "none"] += 1
            confident += res.confident
            if res.coord is None:
                miss += 1
                failures.append((case.meta, case.truth, "MISS", [t for t, _ in res.seen][:6]))
            elif (res.coord.x, res.coord.y) == case.truth:
                ok += 1
            else:
                wrong += 1
                wrong_confident += res.confident
                failures.append((case.meta, case.truth, (res.coord.x, res.coord.y), res.text,
                                 f"votes={res.votes}"))
        report[layout] = dict(ok=ok, wrong=wrong, miss=miss, n=n, confident=confident,
                              wrong_confident=wrong_confident, stages=dict(stages),
                              median_ms=statistics.median(times), max_ms=max(times),
                              mode=reader.memory.get("mode"), failures=failures)
    return report


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    for layout, r in run(n).items():
        print(f"[{layout:6s}] exact {r['ok']}/{r['n']}  wrong {r['wrong']} (confident {r['wrong_confident']})  "
              f"miss {r['miss']}  confident {r['confident']}/{r['n']}  median {r['median_ms']:.0f} ms  "
              f"max {r['max_ms']:.0f} ms  learned={r['mode']}  stages={r['stages']}")
        for f in r["failures"][:8]:
            print("     ", f)
