#!/usr/bin/env python3
"""Run every test suite and the check-order audit in one command.

Exits 0 only if every suite exits 0. One command instead of seven, because a
suite that nobody remembers to run is a suite that silently rots.

    python run_tests.py                  # run everything, print a summary table
    python run_tests.py --verbose        # stream each suite's output live
    python run_tests.py --only seed      # just the suites whose name matches
    python run_tests.py --fail-fast      # stop at the first failure
    python run_tests.py --json out.json  # also write those numbers as JSON

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
import json
import os
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
    ("test_no_secrets.py", "tracked files hold no key, password or wallet export"),
    ("test_merge_shape.py", "throwaway git repositories, no network"),
    ("test_merge_verify.py", "pure verification rule, no network"),
    ("test_merge_preflight.py", "pure privilege preflight, no network"),
    ("test_merge_collect.py", "pure row collapsing and provenance, no network"),
    ("test_merge_schema.py", "merged_rows SQL shapes agree, parsed without a database"),
    ("test_merge_rows.py", "the merge executed: bind types, upsert, refusals, no database"),
    ("test_merge_route.py", "the route executed: stub mysqli, statement, statuses"),
    ("test_export.py", "the export executed: value encoding, the cap, what it skips"),
    ("test_sync_route.py", "the dispatch executed: the guard, the body checks, 405/413"),
    ("test_row_key_parity.py", "PHP row_key() against the same formula in Python"),
    ("test_check_funding.py", "fake providers, no network"),
    ("test_watch_only.py", "zpub derivation against the BIP84 vectors"),
    ("test_gen_wallets.py", "derivation vectors, encryption, file guards"),
    ("test_export_holders.py", "fake JSON-RPC chain"),
    ("test_seed_pool.py", "mock QuickSwap router on a local chain"),
    ("test_update_toolkit.py", "fake git runner, no repository or network"),
    ("test_broadcast.py", "in-memory EVM, simulated endpoint failures"),
    ("test_distribute.py", "real signed transactions on a local chain"),
    ("test_distribute_token.py", "real ERC-20 transfers on a local chain"),
    ("test_mint.py", "in-memory EVM"),
    ("test_token.py", "in-memory EVM"),
    ("test_run_tests.py", "the runner itself: what it counts, refuses and exits with"),
    ("test_compare_legs.py", "the CI leg comparator: identical legs pass, drift is named"),
    ("test_suite_output.py", "every suite prints PASS only from check(), one summary"),
    ("test_php_surface.py", "which functions in php/ no test has ever named"),
    ("test_sync_client.py", "the export reply is compact; the client still parses it"),
    ("mutation_check.py", "every named mutation must be caught by its suite"),
    ("audit_test_checks.py", "check-order audit (no suite of its own)"),
    # Last on purpose: it compares README's numbers against the counts above, and
    # reads them from RUN_TESTS_COUNTS rather than running every suite again.
    ("test_docs_counts.py", "README counts vs what the suites report"),
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


def write_json(path: str, rows: list[dict], total_pass: int, total_fail: int,
               total_seconds: float, started_at: str, listed: int) -> None:
    """The numbers the table prints, for something other than a person to read.

    Written whatever the verdict, because a leg that failed is exactly the one
    whose numbers are worth comparing.
    """
    payload = {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "started_at": started_at,
        "seconds": round(total_seconds, 3),
        "suites_listed": listed,
        "suites_run": len(rows),
        "checks": total_pass,
        "failed_checks": total_fail,
        "suites": [
            {
                "name": row["name"],
                "exit": row["rc"],
                "passed": row["passed"],
                "failed": row["failed"],
                "seconds": round(row["seconds"], 3),
                "timed_out": row["timed_out"],
            }
            for row in rows
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def count(lines: list[str]) -> tuple[int, int]:
    passed = sum(1 for line in lines if PASS_LINE.match(line))
    failed = sum(1 for line in lines if FAIL_LINE.match(line))
    return passed, failed


BASELINE_NAME = "counts-baseline.json"


def baseline_path() -> Path:
    """The committed baseline, beside this file so a copy of the tree carries one."""
    return ROOT / BASELINE_NAME


def load_baseline(path: Path) -> dict[str, int] | None:
    """Suite name -> the count it last reported, or None when there is no file.

    A baseline that will not parse is reported and treated as absent: skipping the
    comparison without saying so is the failure this exists to catch.
    """
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(name): int(count) for name, count in payload["suites"].items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"  BASELINE  {path.name} cannot be read ({exc}); counts not compared")
        return None


def baseline_report(measured: dict[str, int], baseline: dict[str, int],
                    full: bool) -> dict[str, list]:
    """What moved, in the four ways a count can: down, up, new, gone.

    `gone` only counts on a full run: under --only or --fail-fast the suites that
    did not run are the ones nobody asked for, not ones that disappeared.
    """
    return {
        "drops": sorted((name, baseline[name], count) for name, count in measured.items()
                        if name in baseline and count < baseline[name]),
        "rises": sorted((name, baseline[name], count) for name, count in measured.items()
                        if name in baseline and count > baseline[name]),
        "added": sorted(name for name in measured if name not in baseline),
        "gone": sorted(name for name in baseline if name not in measured) if full else [],
    }


def write_baseline(measured: dict[str, int], path: Path) -> None:
    """Record what this run measured as the counts every later run is held to."""
    payload = {"version": 1,
               "suites": {name: measured[name] for name in sorted(measured)}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def report_baseline(drops: list[tuple[str, int, int]], gone: list[str],
                   baseline: dict[str, int]) -> None:
    """Say which suites stopped checking things, and how to accept that on purpose."""
    print(f"\n{len(drops) + len(gone)} SUITE(S) BELOW THE BASELINE:")
    for name, was, now in drops:
        print(f"  - {name}: {was} -> {now}")
    for name in gone:
        print(f"  - {name}: {baseline[name]} -> not run at all")
    print("  a suite that checks fewer things can still exit 0, so this is what")
    print("  notices. If the drop is intended, record it:")
    print("  python run_tests.py --write-baseline")


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
    parser.add_argument("--json", metavar="PATH",
                        help="also write the per-suite counts and timings to PATH")
    parser.add_argument("--write-baseline", action="store_true",
                        help="record this run's counts as the new counts-baseline.json")
    args = parser.parse_args(argv)

    selected = [(p, d) for p, d in SUITES
                if not args.only or any(s.lower() in p.lower() for s in args.only)]
    if not selected:
        print(f"Error: --only {args.only} matched no suite. "
              f"Known: {', '.join(p for p, _ in SUITES)}")
        return 2

    rows: list[dict] = []
    measured: dict[str, int] = {}
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
        # Published to the suites still to run, so the docs check can compare README
        # against the numbers this run actually measured instead of repeating them.
        measured[name] = result["passed"]
        os.environ["RUN_TESTS_COUNTS"] = json.dumps(measured)
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
    if args.json:
        write_json(args.json, rows, total_pass, total_fail, total_seconds,
                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_all)),
                   len(SUITES))
    # The counts this run measured, against the counts it last measured -- not
    # against prose somebody has to remember to edit. A suite that silently stops
    # checking things exits 0 either way; this is what notices.
    drops: list[tuple[str, int, int]] = []
    gone: list[str] = []
    baseline: dict[str, int] = {}
    red = [r for r in rows if r["rc"] != 0]
    # A baseline records what a green run measured. Writing one from a red run
    # records the counts a failure produced -- 31 instead of 32 because a check was
    # failing, 4 instead of 11 because the harness could not run -- and then holds
    # every later run to that.
    if args.write_baseline and red:
        print(f"\nbaseline NOT written: {len(red)} suite(s) failed, and a baseline")
        print("  records a green run. Fix them, then record:")
        print("  python run_tests.py --write-baseline")
    elif args.write_baseline:
        write_baseline(measured, baseline_path())
        print(f"\nbaseline written: {len(measured)} suite(s), "
              f"{sum(measured.values())} checks -> {baseline_path().name}")
    else:
        baseline = load_baseline(baseline_path())
        if baseline is None:
            print(f"\nno {baseline_path().name} under {ROOT}: counts not compared")
        else:
            moved = baseline_report(measured, baseline,
                                    full=not args.only and not args.fail_fast)
            drops = moved["drops"]
            gone = moved["gone"]
            if any(moved.values()):
                print(f"\nCOUNTS AGAINST {baseline_path().name}")
                for name, was, now in moved["rises"]:
                    print(f"  ABOVE  {name}: {was} -> {now}"
                          f"  (record with --write-baseline)")
                for name in moved["added"]:
                    print(f"  NEW    {name}: {measured[name]}"
                          f"  (record with --write-baseline)")
                for name in moved["gone"]:
                    print(f"  GONE   {name}: baseline says {baseline[name]}"
                          f", this run did not include it")
            else:
                print(f"\ncounts as the baseline has them: {len(measured)} suite(s)")

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
        if drops or gone:
            report_baseline(drops, gone, baseline)
        return 1

    if drops or gone:
        report_baseline(drops, gone, baseline)
        return 1

    print(f"\nALL SUITES PASSED ({total_pass} checks, {len(rows)} suite(s), "
          f"{total_seconds:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
