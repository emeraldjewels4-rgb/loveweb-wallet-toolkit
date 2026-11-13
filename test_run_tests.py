#!/usr/bin/env python3
"""Tests for run_tests.py itself: the command everything else is trusted through.

A runner that reports success for a suite it never ran is worse than no runner, and
the ways it can do that look fine in a table: a missing file read as a skip, a suite
that hangs read as nothing, a closing "ALL CHECKS PASSED" line read as a check, a
--only that matched nothing read as everything passed. Each is driven here over
throwaway suites in one temporary directory, with the runner's ROOT and SUITES
swapped -- it reads suites as ROOT / name and returns from main(), so this drives the
real code path instead of a copy of it, and the real suite list is never re-entered.

The handoff to the docs check is driven the same way: RUN_TESTS_COUNTS must hold the
counts measured so far by the time a later suite starts, and this file must put the
caller's value back, because the outer run's own handoff is in that variable while
this suite runs.

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

# Throwaway suites, written into the temporary tree: these names are the only suites
# the runner sees, so nothing in this repository is involved.
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

# Records what RUN_TESTS_COUNTS held when it started: the handoff the docs check
# reads instead of running every suite a second time.
PROBE = """\
import os
import pathlib

counts = os.environ.get("RUN_TESTS_COUNTS", "")
pathlib.Path("counts.json").write_text(counts or "absent", encoding="utf-8")
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


def drive(tmp: Path, suites: list[tuple[str, str]], *argv: str) -> tuple[int, str]:
    """Run the real main() over these suites; return (exit code, its output).

    RUN_TESTS_COUNTS is emptied for the run and the caller's value put back, in both
    directions. Under the real runner that variable holds the outer handoff, and
    main() only overwrites it after the first suite -- so a probe would otherwise
    inherit counts this run never measured.
    """
    saved_root, saved_suites = run_tests.ROOT, run_tests.SUITES
    saved_env = os.environ.get("RUN_TESTS_COUNTS")
    run_tests.ROOT, run_tests.SUITES = tmp, list(suites)
    os.environ.pop("RUN_TESTS_COUNTS", None)
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


def probe(tmp: Path):
    """What the probe suite saw: parsed counts, or the raw text if it saw none.

    A handoff that never happened leaves "absent" behind, and returning it rather
    than raising turns that into a named failing check instead of a traceback.
    """
    path = tmp / "counts.json"
    text = path.read_text(encoding="utf-8") if path.is_file() else "(no probe output)"
    try:
        return json.loads(text)
    except ValueError:
        return text


def main() -> int:
    print("run_tests.py: what it counts, what it refuses, and what it exits with")

    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)

        section("a suite that passes")
        write(tmp, "test_ok.py", PASSING)
        code, out = drive(tmp, [("test_ok.py", "a suite that passes")])
        check(code == 0, f"exit code is 0 (got {code})")
        check("ALL SUITES PASSED (2 checks, 1 suite(s)" in out,
              "the total is 2 checks: the ALL CHECKS PASSED summary is not one")

        section("a suite that fails")
        write(tmp, "test_bad.py", FAILING)
        code, out = drive(tmp, [("test_bad.py", "one check fails")])
        check(code == 1, f"exit code is 1 (got {code})")
        check("- test_bad.py: exit 1, 1 failed check(s)" in out,
              "the failure list names it with the exit code and the failed count")
        check("FAIL  two" in out and "re-run alone: python test_bad.py" in out,
              "its output and the command to re-run it are printed")

        section("a suite file that is missing")
        code, out = drive(tmp, [("test_absent.py", "never written")])
        check(code == 1 and "MISSING" in out and "exit 127" in out,
              "a missing file fails the run with 127, never a skip")

        section("a suite that outlives its timeout")
        write(tmp, "test_slow.py", SLEEPING)
        started = time.time()
        code, out = drive(tmp, [("test_slow.py", "hangs")], "--timeout", "1")
        elapsed = time.time() - started
        check(code == 1 and "TIMEOUT: exit -9" in out and "timed out" in out,
              "it is reported as TIMEOUT with exit -9, not as a pass")
        check(elapsed < 20, f"the timeout was enforced, not waited out ({elapsed:.1f}s)")

        section("--only")
        write(tmp, "test_alpha.py", PASSING)
        write(tmp, "test_beta.py", PASSING)
        pair = [("test_alpha.py", "a"), ("test_beta.py", "b")]
        code, out = drive(tmp, pair, "--only", "beta")
        check(code == 0 and "running test_beta.py" in out
              and "running test_alpha.py" not in out,
              "only the matching suite runs, and a pass exits 0")
        code, out = drive(tmp, pair, "--only", "no-such-suite")
        check(code == 2 and "matched no suite" in out,
              "a match that selects nothing exits 2, not 0")

        section("--fail-fast")
        write(tmp, "test_first.py", FAILING)
        ordered = [("test_first.py", "fails"), ("test_alpha.py", "never reached")]
        code, out = drive(tmp, ordered, "--fail-fast")
        check(code == 1 and "--fail-fast: stopping here" in out
              and "running test_alpha.py" not in out,
              "it stops at the failure instead of running the next suite")
        code, out = drive(tmp, ordered)
        check(code == 1 and "running test_alpha.py" in out,
              "without the flag the next suite still runs")

        section("--verbose")
        # Output is captured per suite by default, so the flag is the only way a
        # suite's own lines reach the terminal -- under its heading, before the verdict.
        one = [("test_ok.py", "a suite that passes")]
        _, quiet = drive(tmp, one)
        code, loud = drive(tmp, one, "--verbose")
        heading = loud.find("running test_ok.py")
        echoed = loud.find("    PASS  one")
        verdict = loud.find("ok: exit 0")
        check(code == 0 and "    PASS  one" not in quiet
              and -1 < heading < echoed < verdict,
              "--verbose echoes the PASS lines under the heading, before the verdict")

        section("the handoff to the docs check")
        write(tmp, "test_probe.py", PROBE)
        drive(tmp, [("test_alpha.py", "2 checks"), ("test_beta.py", "2 more"),
                    ("test_probe.py", "records what it sees")])
        seen = probe(tmp)
        check(seen == {"test_alpha.py": 2, "test_beta.py": 2},
              f"each suite sees the counts measured before it ({seen})")

        # The sentinel stands in for the outer runner's handoff, which is this
        # process's real value: put that back rather than leaving the sentinel for
        # whatever runs next in here.
        ambient = os.environ.get("RUN_TESTS_COUNTS")
        try:
            os.environ["RUN_TESTS_COUNTS"] = '{"sentinel": 1}'
            drive(tmp, [("test_alpha.py", "a passing suite")])
            check(os.environ.get("RUN_TESTS_COUNTS") == '{"sentinel": 1}',
                  "and the caller's value is put back after the run")
        finally:
            if ambient is None:
                os.environ.pop("RUN_TESTS_COUNTS", None)
            else:
                os.environ["RUN_TESTS_COUNTS"] = ambient

    section("the real command's exit code")
    # The runner as a process: the exit status comes from `raise SystemExit(main())`,
    # which the in-process calls above cannot show.
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
