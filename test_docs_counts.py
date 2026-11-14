#!/usr/bin/env python3
"""Fail when README.md's test numbers stop matching what the suites report.

The counts in README.md are measurements, not prose: every suite prints its own
PASS lines, run_tests.py counts them, and audit_test_checks.py counts the
`check()` call sites. This suite compares the README against those numbers, so a
suite that is added, renamed or grown cannot leave the list quietly wrong -- which
is exactly how it went stale before.

It also reads .github/workflows/tests.yml, because the interpreter matrix and the
job count are stated in the README's CI paragraph but belong to the workflow, and
they went stale there twice -- the matrix had gained 3.14 while the prose still
named three interpreters in two places.

Under run_tests.py it reuses the counts that run has already collected (the runner
exports them in RUN_TESTS_COUNTS), so it costs nothing. Run on its own it executes
each suite itself, which takes as long as the suites do.

    python test_docs_counts.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import run_tests

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SELF = Path(__file__).name
AUDIT = "audit_test_checks.py"

FAILED: list[str] = []

WORDS = {word: value for value, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
for tens_index, tens in enumerate("twenty thirty forty fifty sixty seventy eighty "
                                  "ninety".split(), start=2):
    WORDS[tens] = tens_index * 10
    WORDS.update({f"{tens}-{unit}": tens_index * 10 + WORDS[unit]
                  for unit in "one two three four five six seven eight nine".split()})


def check(cond: bool, label: str) -> None:
    """Record one verdict; a non-bool condition is a bug, not a pass."""
    if not isinstance(cond, bool):
        raise SystemExit("check() condition must be a bool, got %r" % (cond,))
    if not isinstance(label, str):
        raise SystemExit("check() label must be a string, got %r" % (label,))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def reported_counts(names: list[str]) -> dict[str, int]:
    """Each suite's PASS count: from the enclosing run when it is on offer, else by running it.

    Counting with run_tests' own PASS_LINE is deliberate -- the README quotes the
    numbers that table prints, so this has to use the same rule, all-check-PASSED
    summary line included in neither.
    """
    given = json.loads(os.environ.get("RUN_TESTS_COUNTS") or "{}")
    counts = {}
    for name in names:
        if name in given:
            counts[name] = int(given[name])
            continue
        done = subprocess.run(
            [sys.executable, name], cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        counts[name] = sum(1 for line in (done.stdout or "").splitlines()
                           if run_tests.PASS_LINE.match(line))
    return counts


def workflow_facts() -> tuple[list[str], list[str]]:
    """The interpreter matrix and job names, read from the workflow file.

    Parsed by hand rather than with a YAML library: this suite has no dependencies
    of its own, and the two shapes it needs are unambiguous -- matrix entries are
    the quoted versions in `python-version:` lines, job names are the keys at two
    spaces directly under `jobs:`.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    matrix = re.findall(r'-\s*python-version:\s*"([0-9]+\.[0-9]+)"', text)
    jobs, inside = [], False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        if line.strip() and not line.startswith(" "):
            break
        named = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if named:
            jobs.append(named.group(1))
    return matrix, jobs


def interpreter_lists(text: str) -> list[list[str]]:
    """Every bold list of interpreter versions in the README, as written."""
    found = re.findall(r"\*\*(?:Python )?(\d+\.\d+(?:(?:,| and)\s*\d+\.\d+)+)\*\*", text)
    return [[v for v in re.split(r",\s*|\s+and\s+", item) if v] for item in found]


def audited_call_sites() -> int | None:
    """The auditor's own total, read from the per-file counts it prints."""
    done = subprocess.run(
        [sys.executable, AUDIT], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    found = re.findall(r"\[(\d+) two-arg-or-more calls\]", done.stdout or "")
    return sum(int(n) for n in found) if found else None


def main() -> int:
    text = README.read_text(encoding="utf-8")
    suites = [name for name, _ in run_tests.SUITES if name != AUDIT]
    claims = {name: int(count) for name, count in
              re.findall(r"^python (\S+\.py)\s+#\s*(\d+) checks", text, re.M)}

    print("=== 1. the README list names exactly the suites that run ===")
    check(set(claims) == set(suites),
          "listed and run agree (missing %s, extra %s)"
          % (sorted(set(suites) - set(claims)) or "none",
             sorted(set(claims) - set(suites)) or "none"))

    print("\n=== 2. every per-suite count matches what the suite reports ===")
    # This file's own figure is the one number it cannot measure: its count is only
    # complete after this check has run. Its presence is still held to check 1.
    measured = reported_counts([name for name in suites if name != SELF])
    wrong = [(name, claims.get(name), count) for name, count in measured.items()
             if claims.get(name) != count]
    check(not wrong,
          "counts match for %d suite(s)%s"
          % (len(measured), "" if not wrong else ": " + ", ".join(
              "%s README %s vs %d" % (name, claim, count) for name, claim, count in wrong)))

    print("\n=== 3. the total is the sum of the parts ===")
    total = sum(measured.values()) + claims.get(SELF, 0)
    stated_total = re.search(r"run_tests\.py\s+#[^\n]*?(\d+) checks", text)
    check(stated_total is not None and int(stated_total.group(1)) == total,
          "README total %s equals the sum %d"
          % (stated_total.group(1) if stated_total else "missing", total))

    print("\n=== 4. the call-site figure matches the auditor ===")
    sites = audited_call_sites()
    stated_sites = re.search(r"\*\*(\d+) `check\(\)` call sites", text)
    check(sites is not None and stated_sites is not None
          and int(stated_sites.group(1)) == sites,
          "README %s equals the auditor's %s"
          % (stated_sites.group(1) if stated_sites else "missing", sites))

    print("\n=== 5. the prose count of suites matches the list ===")
    # The sentence wraps, so the count and the word need not be on one line.
    stated_word = re.search(r"runs the ([\w-]+)\s+suites", text)
    check(stated_word is not None and WORDS.get(stated_word.group(1).lower()) == len(suites),
          "README says %s suite(s), %d run"
          % (stated_word.group(1) if stated_word else "nothing", len(suites)))

    print("\n=== 6. the interpreter lists match the workflow's matrix ===")
    matrix, jobs = workflow_facts()
    listed = interpreter_lists(text)
    wrong = [entry for entry in listed if entry != matrix]
    check(bool(matrix) and bool(listed) and not wrong,
          "%d list(s) match %s%s"
          % (len(listed), ", ".join(matrix) or "nothing",
             "" if not wrong else "; disagreeing: " + str(wrong)))

    print("\n=== 7. the job count matches the workflow ===")
    counted = re.search(r"(\w+) identical jobs instead of (\w+)", text)
    legs = WORDS.get(counted.group(2).lower()) if counted else None
    doubled = WORDS.get(counted.group(1).lower()) if counted else None
    check(legs == len(matrix) and doubled == 2 * len(matrix) and len(jobs) == 1,
          "README says %s jobs instead of %s; the workflow runs %d job(s) across "
          "%d interpreter(s)"
          % (counted.group(1) if counted else "nothing",
             counted.group(2) if counted else "nothing", len(jobs), len(matrix)))

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
