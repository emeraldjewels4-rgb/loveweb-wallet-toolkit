#!/usr/bin/env python3
"""Run every test suite and the check-order audit in one command.

Exits 0 only if every suite exits 0. One command instead of seven, because a
suite that nobody remembers to run is a suite that silently rots.

    python run_tests.py                  # run everything, print a summary table
    python run_tests.py --verbose        # stream each suite's output live
    python run_tests.py --only seed      # just the suites whose name matches
    python run_tests.py --fail-fast      # stop at the first failure

Counts come from lines that BEGIN with PASS or FAIL, not from `grep -c PASS`:
several suites end with "ALL CHECKS PASSED", and that closing line is not a
check. Counting naively inflated every total by exactly one, which is how this
detail was found.

Output is captured per suite rather than streamed by default, so one noisy
suite cannot bury the result of the next. A failing suite has its tail printed
under the table -- the failure, its traceback, and the command to re-run it
alone -- so the runner is an entry point, not a place that hides evidence.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Ordered cheapest-first: a syntax error in a small suite should surface before
# a 13-second EVM run. Each entry is (path, what it runs against).
SUITES: list[tuple[str, str]] = [
    ("test_payment_request.py", "pure local computation"),
    ("test_check_funding.py", "fake providers, no network"),
    ("test_watch_only.py", "zpub derivation against the BIP84 vectors"),
    ("test_gen_wallets.py", "derivation vectors, encryption, file guards"),
    ("test_export_holders.py", "fake JSON-RPC chain"),
    ("test_seed_pool.py", "mock QuickSwap router on a local chain"),
    ("test_distribute.py", "real signed transactions on a local chain"),
    ("test_distribute_token.py", "real ERC-20 transfers on a local chain"),
    ("test_mint.py", "in-memory EVM"),
    ("test_token.py", "in-memory EVM"),
    ("audit_test_checks.py", "check-order audit (no suite of its own)"),
]

PASS_LINE = re.compile(r"^\s*PASS\b")
FAIL_LINE = re.compile(r"^\s*FAIL\b")
TAIL_LINES = 40


def run(path: Path, timeout: int) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "rc": proc.returncode,
            "out": proc.stdout or "",
            "err": proc.stderr or "",
            "seconds": time.time() - started,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {"rc": -9, "out": out, "err": err,
                "seconds": time.time() - started, "timed_out": True}


def count(lines: list[str]) -> tuple[int, int]:
    passed = sum(1 for line in lines if PASS_LINE.match(line))
    failed = sum(1 for line in lines if FAIL_LINE.match(line))
    return passed, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every test suite plus the check-order audit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--only", action="append", metavar="SUBSTRING",
                        help="run only suites whose filename matches (repeatable)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="stream each suite's output as it runs")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop at the first failing suite")
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-suite timeout in seconds (default 900)")
    args = parser.parse_args(argv)

    selected = [(p, d) for p, d in SUITES
                if not args.only or any(s.lower() in p.lower() for s in args.only)]
    if not selected:
        print(f"Error: --only {args.only} matched no suite. "
              f"Known: {', '.join(p for p, _ in SUITES)}")
        return 2

    rows: list[dict] = []
    started_all = time.time()

    for name, description in selected:
        path = ROOT / name
        print(f"running {name} ({description})...", flush=True)
        if not path.is_file():
            rows.append({"name": name, "description": description, "rc": 127,
                         "passed": 0, "failed": 0, "seconds": 0.0,
                         "out": "", "err": f"missing file: {path}", "timed_out": False})
            print("  MISSING")
            if args.fail_fast:
                break
            continue

        result = run(path, args.timeout)
        result["name"] = name
        result["description"] = description
        lines = (result["out"] + "\n" + result["err"]).splitlines()
        result["passed"], result["failed"] = count(lines)
        if args.verbose:
            for line in lines:
                print(f"    {line}")
        verdict = "ok" if result["rc"] == 0 else ("TIMEOUT" if result["timed_out"] else "FAILED")
        print(f"  {verdict}: exit {result['rc']}, {result['passed']} pass, "
              f"{result['failed']} fail, {result['seconds']:.1f}s", flush=True)
        rows.append(result)
        if result["rc"] != 0 and args.fail_fast:
            print("  --fail-fast: stopping here")
            break

    total_seconds = time.time() - started_all
    width = max(len(r["name"]) for r in rows)
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'suite'.ljust(width)}  {'exit':>4}  {'pass':>5}  {'fail':>5}  {'time':>6}")
    for r in rows:
        print(f"{r['name'].ljust(width)}  {r['rc']:>4}  {r['passed']:>5}  "
              f"{r['failed']:>5}  {r['seconds']:>5.1f}s")

    total_pass = sum(r["passed"] for r in rows)
    total_fail = sum(r["failed"] for r in rows)
    failed_suites = [r for r in rows if r["rc"] != 0]
    skipped = len(SUITES) - len(rows)
    print("-" * 78)
    print(f"{'TOTAL'.ljust(width)}  {'':>4}  {total_pass:>5}  {total_fail:>5}  "
          f"{total_seconds:>5.1f}s"
          + (f"   ({skipped} suite(s) not run)" if skipped else ""))

    if failed_suites:
        print(f"\n{len(failed_suites)} SUITE(S) FAILED:")
        for r in failed_suites:
            reason = "timed out" if r["timed_out"] else f"exit {r['rc']}"
            print(f"  - {r['name']}: {reason}, {r['failed']} failed check(s)")
        for r in failed_suites:
            body = (r["out"] + ("\n" + r["err"] if r["err"].strip() else "")).rstrip()
            tail = body.splitlines()[-TAIL_LINES:]
            print(f"\n----- last {len(tail)} line(s) of {r['name']} -----")
            for line in tail:
                print(f"  {line}" if line.strip() else "")
            print(f"  re-run alone: python {r['name']}")
        return 1

    print(f"\nALL SUITES PASSED ({total_pass} checks, {len(rows)} suite(s), "
          f"{total_seconds:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
