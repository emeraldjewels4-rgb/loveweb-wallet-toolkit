#!/usr/bin/env python3
"""Tests for run_tests.py itself: the command everything else is trusted through.

A runner that reports success for a suite it never ran is worse than no runner, and
the ways it can do that look fine in a table: a missing file (counted as a skip), a
suite that hangs (counted as nothing), a closing "ALL CHECKS PASSED" line (counted as
a check), a --only that matched nothing (counted as everything passed). Each of those
is driven here over throwaway suites in a temporary directory, with the runner's ROOT
and SUITES swapped, so no project file is touched and the real suite list is never
re-entered.

The handoff to the docs check is driven the same way: RUN_TESTS_COUNTS has to hold
the counts measured so far by the time each later suite starts, and this file must
put the caller's value back afterwards -- the outer runner's own handoff is in that
variable while this suite runs.

    python test_run_tests.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import run_tests

FAILED: list[str] = []

# Throwaway suites. They are written into the temporary tree, so the names here are
# the only thing the runner sees -- nothing in this repository is involved.
PASSING = """\
print("  PASS  one")
print("  PASS  two")
print("ALL CHECKS PASSED")
"""

FAILING = """\
print("  PASS  one")
print("  FAIL  two")
raise SystemExit(1)
"""

SLEEPING = """\
import time
print("  PASS  slow", flush=True)
time.sleep(30)
"""

# Records what RUN_TESTS_COUNTS held when it started -- the handoff the docs check
# reads instead of running every suite a second time.
PROBE = """\
import os
import pathlib

counts = os.environ.get("RUN_TESTS_COUNTS", "")
pathlib.Path("counts-%(slot)s.json").write_text(counts or "absent", encoding="utf-8")
print("  PASS  probe")
"""


def check(cond: bool, label: str) -> None:
    """Record one verdict, refusing anything that could not be a real condition.

    bool only: an int in this slot is almost always a count someone forgot to
    compare, and it would pass silently because every non-zero one is truthy.
    """
    if not isinstance(cond, bool):
        raise SystemExit("check() condition must be a bool, got %r" % (cond,))
    if not isinstance(label, str):
        raise SystemExit("check() label must be a string, got %r" % (label,))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n--- {title}")


def write(tmp: Path, name: str, body: str) -> None:
    (tmp / name).write_text(body, encoding="utf-8")


def probe_counts(tmp: Path, slot: str):
    """What the probe suite saw: the parsed counts, or the raw text it found.

    A handoff that never happened leaves "absent" here, so returning the text rather
    than raising turns that into a named failing check instead of a traceback.
    """
    path = tmp / f"counts-{slot}.json"
    text = path.read_text(encoding="utf-8") if path.is_file() else "(no probe output)"
    try:
        return json.loads(text)
    except ValueError:
        return text


def drive(tmp: Path, suites: list[tuple[str, str]], *argv: str,
          isolate_counts: bool = False) -> tuple[int, str]:
    """Run the real main() against these suites; return (exit code, its output).

    isolate_counts empties RUN_TESTS_COUNTS for the duration. Under the real runner
    that variable already holds the outer run's counts, and main() only overwrites it
    after the first suite -- so a probe that runs first would otherwise see them.
    Either way the caller's value is put back before returning: this suite runs in the
    middle of the outer handoff and must not disturb it.
    """
    saved_root, saved_suites = run_tests.ROOT, run_tests.SUITES
    saved_env = os.environ.get("RUN_TESTS_COUNTS")
    if isolate_counts:
        os.environ.pop("RUN_TESTS_COUNTS", None)
    run_tests.ROOT, run_tests.SUITES = tmp, list(suites)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = run_tests.main(list(argv))
    finally:
        run_tests.ROOT, run_tests.SUITES = saved_root, saved_suites
        if saved_env is None:
            os.environ.pop("RUN_TESTS_COUNTS", None)
        else:
            os.environ["RUN_TESTS_COUNTS"] = saved_env
    return code, buffer.getvalue()


def main() -> int:
    print("run_tests.py: what it counts, what it refuses, and what it exits with")

    section("counting a PASS line")
    check(run_tests.count(["  PASS  one", "  PASS  two", "ALL CHECKS PASSED"]) == (2, 0),
          "the closing ALL CHECKS PASSED line is not a check")
    check(run_tests.count(["  FAIL  one", "FAILED to start"]) == (0, 1),
          "FAILED is not read as a FAIL check (the word boundary holds)")
    check(run_tests.count(["  PASS  one", "  FAIL  two"]) == (1, 1),
          "one PASS and one FAIL are counted apart")

    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)

        section("a suite that passes")
        write(tmp, "test_ok.py", PASSING)
        code, out = drive(tmp, [("test_ok.py", "a suite that passes")])
        check(code == 0, f"exit code is 0 (got {code})")
        check("ALL SUITES PASSED (2 checks, 1 suite(s)" in out,
              "the total is 2 checks, not 3 (the summary line is excluded)")
        check("(3 checks" not in out, "and no total counts the summary as a check")

        section("a suite that fails")
        write(tmp, "test_bad.py", FAILING)
        code, out = drive(tmp, [("test_bad.py", "one check fails")])
        check(code == 1, f"exit code is 1 (got {code})")
        check("1 SUITE(S) FAILED" in out, "the summary says a suite failed")
        check("- test_bad.py: exit 1, 1 failed check(s)" in out,
              "and names it with the exit code and the failed count")
        check("re-run alone: python test_bad.py" in out,
              "its output is printed with the command to re-run it")
        check("FAIL  two" in out, "the failing check itself is shown, not just a count")

        section("a suite file that is missing")
        code, out = drive(tmp, [("test_absent.py", "never written")])
        check(code == 1, f"exit code is 1 (got {code})")
        check("MISSING" in out, "it is reported as MISSING")
        check("exit 127" in out, "and carries exit 127, so it cannot read as a skip")
        check("ALL SUITES PASSED" not in out, "the run cannot claim everything passed")

        section("a suite that outlives its timeout")
        write(tmp, "test_slow.py", SLEEPING)
        started = time.time()
        code, out = drive(tmp, [("test_slow.py", "hangs")], "--timeout", "1")
        elapsed = time.time() - started
        check(code == 1, f"exit code is 1 (got {code})")
        check("TIMEOUT: exit -9" in out, "it is reported as TIMEOUT with exit -9")
        check("timed out" in out, "the failure reason says timed out, not passed")
        check(elapsed < 20, f"the timeout was enforced, not waited out ({elapsed:.1f}s)")

        section("--only")
        write(tmp, "test_alpha.py", PASSING)
        write(tmp, "test_beta.py", PASSING)
        pair = [("test_alpha.py", "a"), ("test_beta.py", "b")]
        code, out = drive(tmp, pair, "--only", "beta")
        check(code == 0, f"exit code is 0 when the selected suite passes (got {code})")
        check("running test_beta.py" in out and "running test_alpha.py" not in out,
              "only the matching suite runs")
        code, out = drive(tmp, pair, "--only", "no-such-suite")
        check(code == 2, f"a match that selects nothing exits 2, not 0 (got {code})")
        check("matched no suite" in out and "test_alpha.py" in out,
              "and it lists what it does know about")

        section("--fail-fast")
        write(tmp, "test_first.py", FAILING)
        ordered = [("test_first.py", "fails"), ("test_alpha.py", "never reached")]
        code, out = drive(tmp, ordered, "--fail-fast")
        check(code == 1, f"exit code is 1 (got {code})")
        check("--fail-fast: stopping here" in out, "it says that it stopped early")
        check("running test_alpha.py" not in out, "the suite after the failure never ran")
        check("(1 suite(s) not run)" in out, "and the summary reports what did not run")
        code, out = drive(tmp, ordered)
        check(code == 1 and "running test_alpha.py" in out,
              "without the flag the next suite still runs")

    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        write(tmp, "test_alpha.py", PASSING)

        section("the handoff to the docs check")
        write(tmp, "test_probe_b.py", PROBE % {"slot": "b"})
        write(tmp, "test_probe_c.py", PROBE % {"slot": "c"})
        drive(tmp, [("test_alpha.py", "2 checks"),
                    ("test_probe_b.py", "runs second"),
                    ("test_probe_c.py", "runs third")], isolate_counts=True)
        seen_b = probe_counts(tmp, "b")
        seen_c = probe_counts(tmp, "c")
        check(seen_b == {"test_alpha.py": 2},
              f"a suite sees the counts measured before it ({seen_b})")
        check(seen_c == {"test_alpha.py": 2, "test_probe_b.py": 1},
              f"and the handoff accumulates as each suite finishes ({seen_c})")

        write(tmp, "test_probe_first.py", PROBE % {"slot": "first"})
        drive(tmp, [("test_probe_first.py", "runs first")], isolate_counts=True)
        first = probe_counts(tmp, "first")
        check(first == "absent",
              f"the first suite starts with no counts at all ({first})")

        # This suite runs under the real runner, so the ambient value is the outer
        # handoff: put it back exactly as it was found.
        ambient = os.environ.get("RUN_TESTS_COUNTS")
        try:
            os.environ["RUN_TESTS_COUNTS"] = '{"sentinel": 1}'
            drive(tmp, [("test_alpha.py", "a passing suite")])
            check(os.environ.get("RUN_TESTS_COUNTS") == '{"sentinel": 1}',
                  "drive puts the caller's value back after the run")
            os.environ.pop("RUN_TESTS_COUNTS", None)
            drive(tmp, [("test_alpha.py", "a passing suite")])
            check("RUN_TESTS_COUNTS" not in os.environ,
                  "and stays unset when it started unset")
        finally:
            if ambient is None:
                os.environ.pop("RUN_TESTS_COUNTS", None)
            else:
                os.environ["RUN_TESTS_COUNTS"] = ambient

    section("the real command's exit code")
    # The runner itself, as a process: --only with no match must exit 2 through
    # `raise SystemExit(main())`, which the in-process calls above cannot show.
    proc = subprocess.run(
        [sys.executable, str(Path(run_tests.__file__)), "--only", "no-such-suite-xyz"],
        capture_output=True, text=True, timeout=120,
    )
    check(proc.returncode == 2, f"python run_tests.py --only <no match> exits 2 (got {proc.returncode})")

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
