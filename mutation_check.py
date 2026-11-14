#!/usr/bin/env python3
"""Mutation check: do the suites actually bite?

A green suite is worth what it would catch. Each row in MUTATIONS is one small,
plausible break of the code -- a lost regex anchor, a disabled flag, a dropped JSON
option, a renamed column -- paired with the suite that is supposed to notice. The
rows are applied one at a time to a throwaway copy of the tree, the named suite runs
there, and a survivor (code broken, suite still green) is a failure of this check.

Two guards keep "caught" honest:

  * every named suite must pass unmutated in the same copy first, or a failure
    cannot be attributed to the mutation at all;
  * a row whose text no longer occurs exactly once is reported as stale, never
    quietly skipped -- rows rot when the code they point at changes.

Nothing in the working tree is modified: the copy is the only thing mutated.

    python mutation_check.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMEOUT = 600
SKIP = (".git", ".sandbox", "__pycache__", ".cloud-attachments", "*.pyc")
FAILED: list[str] = []


@dataclass(frozen=True)
class Mutation:
    """One edit a named suite must notice. `label` is what the report calls it."""

    file: str
    old: str
    new: str
    suite: str
    label: str


MUTATIONS: list[Mutation] = [
    Mutation(
        "run_tests.py",
        'PASS_LINE = re.compile(r"^\\s*PASS\\b")',
        'PASS_LINE = re.compile(r"PASS")',
        "test_run_tests.py",
        "run_tests: count PASS anywhere, so the summary counts too",
    ),
    Mutation(
        "run_tests.py",
        '        if result["rc"] != 0 and args.fail_fast:',
        "        if False:",
        "test_run_tests.py",
        "run_tests: --fail-fast stops nothing",
    ),
    Mutation(
        "php/keys.php",
        "JSON_UNESCAPED_SLASHES | JSON_PRESERVE_ZERO_FRACTION",
        "JSON_UNESCAPED_SLASHES",
        "test_row_key_parity.py",
        "keys.php: drop JSON_PRESERVE_ZERO_FRACTION (1.0 collides with 1)",
    ),
    Mutation(
        "php/merge.php",
        "const MERGE_BIND_TYPES = 'sssiiss';",
        "const MERGE_BIND_TYPES = 'sssissi';",
        "test_merge_rows.py",
        "merge.php: bind types widened to strings for seen and bytes",
    ),
    Mutation(
        "php/merge.php",
        "row_json, first_seen, last_seen, run_token)'",
        "row_json, last_seen, run_token)'",
        "test_merge_schema.py",
        "merge.php: INSERT drops first_seen out of the column list",
    ),
]


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


def child_env() -> dict:
    """The environment the suites get, with PHP resolvable outside the copy.

    The copy excludes .sandbox, so a suite that falls back to the development PHP
    there would find nothing. PHP_BIN is the suite's first choice, so point it at
    the real interpreter when the copy cannot reach one.
    """
    env = dict(os.environ)
    if not env.get("PHP_BIN") and shutil.which("php") is None:
        dev = ROOT / ".sandbox" / "php" / "php.exe"
        if dev.is_file():
            env["PHP_BIN"] = str(dev)
    return env


def run_suite(tree: Path, suite: str, env: dict) -> tuple[int, str, float]:
    """Run one suite in the copy; return (exit code, first FAIL line, seconds)."""
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(tree / suite)],
            cwd=str(tree), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {TIMEOUT}s", time.time() - started
    lines = (proc.stdout + "\n" + proc.stderr).splitlines()
    first_fail = next((line.strip() for line in lines
                       if line.lstrip().startswith("FAIL")), "")
    return proc.returncode, first_fail, time.time() - started


def main() -> int:
    print("mutations: each row breaks the code, and a named suite has to notice")

    tree = Path(tempfile.mkdtemp(prefix="mutation_check_")) / "tree"
    try:
        shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(*SKIP))
        env = child_env()

        section("a copy, and a baseline for every suite the table names")
        baselines: dict[str, bool] = {}
        for suite in dict.fromkeys(m.suite for m in MUTATIONS):
            code, first_fail, seconds = run_suite(tree, suite, env)
            baselines[suite] = code == 0
            note = f"  ({first_fail})" if first_fail else ""
            print(f"  {suite:<24} unmutated: exit {code}, {seconds:.1f}s{note}")
        check(all(baselines.values()),
              "every named suite passes unmutated in the copy")

        section("one mutation at a time")
        results: list[tuple[Mutation, str, str]] = []
        for mutation in MUTATIONS:
            path = tree / mutation.file
            original = path.read_text(encoding="utf-8")
            occurrences = original.count(mutation.old)
            verdict, evidence = "", ""
            if occurrences != 1:
                verdict = f"STALE: text occurs {occurrences} times"
            elif not baselines[mutation.suite]:
                verdict = "not attributable: baseline failed"
            else:
                path.write_text(original.replace(mutation.old, mutation.new, 1),
                                encoding="utf-8")
                try:
                    code, first_fail, seconds = run_suite(tree, mutation.suite, env)
                finally:
                    path.write_text(original, encoding="utf-8")
                if code != 0:
                    verdict, evidence = "caught", first_fail or f"exit {code}"
                else:
                    verdict = "SURVIVED"
            print(f"  {mutation.label:<56}  {mutation.suite:<22}  {verdict}"
                  + (f"  <- {evidence[:64]}" if evidence else ""))
            results.append((mutation, verdict, evidence))

        section("verdicts")
        for mutation, verdict, _ in results:
            if verdict == "caught":
                check(True, f"{mutation.label}: caught by {mutation.suite}")
            else:
                check(False, f"{mutation.label}: {verdict}")
    finally:
        shutil.rmtree(tree.parent, ignore_errors=True)

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
