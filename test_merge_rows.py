#!/usr/bin/env python3
"""The merge, executed: merge_write()'s rules against a recording writer.

Until php/merge.php existed, the merge was only ever compared as text: db_sync.php
is a route and executes on include, so nothing could run merge_rows() without a
host, and no PHP route ran anywhere in CI. The decisions now live in merge.php and
tests/php/merge_probe.php drives them with a stub MergeWriter, which records what
a real one would have bound.

What this proves is what merge_rows hands to the database: one statement per
merge, the bind types and value types, the run token, the seen_in clamp, and the
statuses it refuses with. What it does not prove: that MySQL evaluates GREATEST
the way the stub models it, or that the row reaches the table -- those need a
server, which this repository still has not pointed at a real merge.

    python test_merge_rows.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "tests" / "php" / "merge_probe.php"
FAILED: list[str] = []


def find_php() -> str | None:
    """PHP_BIN, then PATH, then the development copy. None means nothing to run.

    The same three steps test_row_key_parity.py uses, so a machine that can run
    one can run the other.
    """
    named = os.environ.get("PHP_BIN")
    if named:
        found = shutil.which(named)
        if found:
            return found
    found = shutil.which("php")
    if found:
        return found
    dev = ROOT / ".sandbox" / "php" / "php.exe"
    return str(dev) if dev.is_file() else None


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


def finish() -> int:
    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


def inserts_of(result: dict) -> list[dict]:
    return [event for event in result.get("events", [])
            if isinstance(event, dict) and event.get("kind") == "insert"]


def main() -> int:
    print("merge_write(): what it binds, what it upserts, and what it refuses")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    proc = subprocess.run([php, str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    check(proc.returncode == 0, f"the probe executes merge_write (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", proc.stderr.strip()[:400])
        return finish()
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        check(False, f"the probe answers with JSON (got {proc.stdout.strip()[:80]!r})")
        return finish()

    three = report.get("three", {})
    events = three.get("events", [])
    inserts = inserts_of(three)
    sql = inserts[0]["sql"] if inserts else ""

    section("three rows, one statement")
    check(three.get("accepted") == 3 and len(three.get("keys", [])) == 3,
          f"all three rows are accepted and distinct ({three.get('accepted')})")
    check(len(inserts) == 3 and len({e["sql"] for e in inserts}) == 1,
          "every row goes through one shared statement")
    check(sql.startswith("INSERT INTO merged_rows"),
          "that statement inserts into merged_rows")
    check("ON DUPLICATE KEY UPDATE" in sql
          and "seen_in = GREATEST(seen_in, VALUES(seen_in))" in sql,
          "it upserts on the key, and seen_in only ever grows")
    check(all(f"{column} = VALUES({column})" in sql
              for column in ("row_bytes", "row_json", "source_db", "run_token")),
          "a repeat merge refreshes bytes, json, source and run token")

    section("the bind types")
    check(all(e["types"] == "sssiiss" for e in inserts),
          "'sssiiss' for every row: three strings, two ints, two strings")
    check(all(e["param_types"] == ["string", "string", "string", "int", "int", "string", "string"]
              for e in inserts),
          "the values fit it: key/table/source strings, seen and bytes ints, json and run string")
    check(all(e["params"][4] == len(e["params"][5].encode("utf-8")) for e in inserts),
          "row_bytes is the byte length of the JSON it stores, not a character count")
    check(all(e["params"][6] == "a" * 32 for e in inserts),
          "every row is stamped with the run token")
    keys = [e["params"][0] for e in inserts]
    check(len(set(keys)) == 3 and all(len(k) == 64 and k == k.lower() for k in keys),
          "three distinct lowercase-hex keys, computed here rather than taken from the caller")
    check(all(e["params"][2] == "toolkit" for e in inserts),
          "a row that names no database goes to the primary label")

    section("a second run over the same row")
    duplicate = report.get("duplicate", {})
    check(duplicate.get("first", {}).get("keys") == duplicate.get("second", {}).get("keys"),
          "the same row hashes to the same key both times")
    stored = duplicate.get("stored", {})
    check(len(stored) == 1, f"the repeat merges to one row, not two ({len(stored)})")
    only = next(iter(stored.values()), {})
    check(only.get("seen_in") == 3, "GREATEST keeps the larger seen_in (3), not the later 1")
    check(only.get("run_token") == "d" * 32,
          "the later run's token replaces the earlier one")

    section("the seen_in clamp")
    clamped = [e["params"][3] for e in inserts_of(report.get("clamped", {}))]
    check(clamped == [1, 1000, 1],
          f"seen is clamped to 1..1000 and defaults to 1 ({clamped})")

    section("the transaction")
    kinds = [event for event in events if isinstance(event, str)]
    check(kinds == ["begin", "commit"], f"one begin and one commit around the rows ({kinds})")

    section("the refusals")
    refusals = report.get("refusals", {})
    expected = {
        "not an object": (400, "every row must be an object"),
        "bad table name": (400, "row table name is not a plain identifier"),
        "no values": (400, "a row without values describes nothing"),
        "bad source_db": (400, "row source_db is not a configured label"),
        "key mismatch": (422, "a row arrived under a key that does not describe it"),
        "unencodable": (422, "a row could not be encoded"),
    }
    for name, (status, message) in expected.items():
        got = refusals.get(name, {})
        check(got.get("status") == status and got.get("message") == message,
              f"{name}: refused with {status} and its own message")
    check(all(r.get("events", [])[-1:] == ["rollback"] for r in refusals.values())
          and len(refusals) == len(expected),
          "every refusal rolls the transaction back")
    check(all(not r.get("stored") for r in refusals.values()),
          "and commits nothing")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
