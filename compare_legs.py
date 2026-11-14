#!/usr/bin/env python3
"""Compare the per-suite counts each CI leg measured.

Every leg runs the same suites on a different interpreter, so every leg must
report the same numbers. That claim used to be verified by reading four logs;
this reads the JSON each leg writes (`run_tests.py --json`) and fails when one
leg disagrees with the rest.

    python compare_legs.py results/*.json

Exit 0 only when every leg ran the same suites and reported the same counts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    """One leg: its label, its per-suite (passed, failed), its total."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "label": str(data.get("python") or Path(path).stem),
        "checks": data.get("checks"),
        "suites": {row["name"]: (row.get("passed"), row.get("failed"))
                   for row in data.get("suites", [])},
    }


def differences(legs: list[dict]) -> list[str]:
    """Every way the legs disagree, as sentences naming the suite and the legs."""
    problems: list[str] = []
    every = set()
    for leg in legs:
        every |= set(leg["suites"])

    for leg in legs:
        missing = sorted(every - set(leg["suites"]))
        if missing:
            problems.append(f"{leg['label']} did not run: {', '.join(missing)}")

    for name in sorted(every):
        seen = [(leg["label"], leg["suites"].get(name)) for leg in legs]
        if len({value for _, value in seen}) > 1:
            problems.append(f"{name}: "
                            + ", ".join(f"{label} {value}" for label, value in seen))

    totals = [(leg["label"], leg["checks"]) for leg in legs]
    if len({value for _, value in totals}) > 1:
        problems.append("totals differ: "
                        + ", ".join(f"{label} {value}" for label, value in totals))
    return problems


def main(argv: list[str] | None = None) -> int:
    paths = list(argv if argv is not None else sys.argv[1:])
    if len(paths) < 2:
        print(f"need at least two legs to compare, got {len(paths)}", file=sys.stderr)
        return 2

    legs = [load(path) for path in paths]
    problems = differences(legs)

    print(f"{'leg':<12} {'suites':>6} {'checks':>7}")
    for leg in legs:
        print(f"{leg['label']:<12} {len(leg['suites']):>6} {str(leg['checks']):>7}")

    if problems:
        print(f"\n{len(problems)} disagreement(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"\nall {len(legs)} legs report identical per-suite counts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
