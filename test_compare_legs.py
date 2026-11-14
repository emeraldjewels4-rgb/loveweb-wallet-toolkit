#!/usr/bin/env python3
"""Compare the comparator: the CI job that says the four legs agree.

compare_legs.py is the thing that now enforces the README's claim that every leg
reports the same counts, and an unexercised checker would fail open -- it exits 0
whenever it finds nothing, including when it understands nothing. These checks
drive it with hand-written leg files so that "identical" and "disagreed" are both
proven, not assumed.

    python test_compare_legs.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import compare_legs

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    """Record one verdict, refusing anything that could not be a real condition."""
    if not isinstance(cond, bool):
        raise SystemExit("check() condition must be a bool, got %r" % (cond,))
    if not isinstance(label, str):
        raise SystemExit("check() label must be a string, got %r" % (label,))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n--- {title}")


def leg(label: str, suites: dict[str, tuple[int, int]], checks: int | None = None) -> dict:
    return {
        "label": label,
        "checks": sum(passed for passed, _ in suites.values()) if checks is None else checks,
        "suites": dict(suites),
    }


def write(folder: Path, name: str, data: dict) -> str:
    payload = {
        "python": data["label"],
        "checks": data["checks"],
        "suites": [{"name": suite, "passed": passed, "failed": failed}
                   for suite, (passed, failed) in data["suites"].items()],
    }
    path = folder / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def run(paths: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = compare_legs.main(paths)
    return code, buffer.getvalue()


def main() -> int:
    print("compare_legs: identical legs pass, any disagreement is named")

    same = {"test_a.py": (10, 0), "test_b.py": (4, 0)}

    section("legs that agree")
    problems = compare_legs.differences([leg("3.11.9", same), leg("3.12.5", same)])
    check(problems == [], f"identical legs produce no disagreement ({len(problems)})")

    section("legs that do not")
    drifted = {"test_a.py": (9, 1), "test_b.py": (4, 0)}
    problems = compare_legs.differences([leg("3.11.9", same), leg("3.12.5", drifted)])
    check(any("test_a.py" in p for p in problems),
          f"a differing count names the suite ({problems[:1]})")
    check(any("3.11.9" in p and "3.12.5" in p for p in problems),
          "and names both legs that disagree")
    check(not any("test_b.py" in p for p in problems),
          "while a suite the legs agree on stays out of it")

    only_failed = {"test_a.py": (10, 0), "test_b.py": (3, 1)}
    check(any("test_b.py" in p for p in compare_legs.differences(
        [leg("3.11.9", same), leg("3.12.5", only_failed)])),
        "a count that moved from passes to failures is a difference too")

    short = {"test_a.py": (10, 0)}
    problems = compare_legs.differences([leg("3.11.9", same), leg("3.12.5", short)])
    check(any("did not run" in p and "test_b.py" in p for p in problems),
          f"a suite missing from one leg is reported ({problems[:1]})")

    check(any("totals differ" in p for p in compare_legs.differences(
        [leg("3.11.9", same, checks=14), leg("3.12.5", same, checks=13)])),
        "a total that disagrees with the rows is reported")

    section("the command line")
    with tempfile.TemporaryDirectory() as folder:
        here = Path(folder)
        a = write(here, "a.json", leg("3.11.9", same))
        b = write(here, "b.json", leg("3.12.5", same))
        code, out = run([a, b])
        check(code == 0 and "identical" in out,
              f"two agreeing files exit 0 (got {code})")

        c = write(here, "c.json", leg("3.13.2", drifted))
        code, out = run([a, c])
        check(code == 1 and "test_a.py" in out,
              f"a disagreeing leg exits 1 and names it (got {code})")

        code, _ = run([a])
        check(code == 2, f"one leg is not a comparison and exits 2 (got {code})")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
