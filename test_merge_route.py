#!/usr/bin/env python3
"""The merge route, executed: a stub mysqli, the real writer, the real statuses.

merge.php holds the decision and test_merge_rows.py drives it with a recording
writer. That left the other half unexecuted: db_sync.php's route -- the
connection, the transaction, the prepared statement, the columns it writes and
the status it answers with. It could not be run because the file dispatches on
include and a request needs a server.

tests/php/merge_route_probe.php includes it as a library instead, hands
merge_rows() a `mysqli`-shaped stub, and prints what happened. What runs is the
same MysqliMergeWriter, the same merge_insert_sql(), the same merge_write() --
what answers is a stub instead of MySQL.

So this holds the route to: one statement whose 9 columns face 9 values with 7
placeholders, bind types that agree with both, a seen_in that keeps the larger
count across runs, a run token stamped on every row and replaced by the next
run, and the refusals -- 400 for a row the merge will not describe, 422 for a
key that does not match, 503 when the table cannot be prepared -- each rolled
back rather than half written.

What it does not prove: that MySQL evaluates GREATEST, or writes the row. The
stub models the statement's stated intent; only a server can confirm MySQL
agrees, and this repository still has not pointed a merge at one.

    python test_merge_route.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "tests" / "php" / "merge_route_probe.php"
FAILED: list[str] = []


def find_php() -> str | None:
    """PHP_BIN, then PATH, then the development copy. None means nothing to run."""
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


def digest_of(keys: list[str]) -> str:
    """The digest merged_status() computes: SHA-256 over sorted row keys."""
    running = hashlib.sha256()
    for key in sorted(keys):
        running.update(key.encode("utf-8"))
    return running.hexdigest()


def main() -> int:
    print("merge_rows(): the statement, the upsert, the run token and the refusals")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    # -n: the stub declares `class mysqli`, which cannot be done where the
    # extension is loaded, and a build with it statically linked is reported
    # rather than silently tested against the wrong thing.
    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    check(proc.returncode == 0, f"the probe executes merge_rows (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:400])
        return finish()
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        check(False, f"the probe answers with JSON (got {proc.stdout.strip()[:80]!r})")
        return finish()

    check("unavailable" not in report,
          "the stub stands in: this interpreter has no mysqli to shadow"
          + (f" ({report['unavailable']})" if "unavailable" in report else ""))
    first, second = report.get("first", {}), report.get("second", {})
    response = first.get("response", {})
    binds = first.get("binds", [])
    shape = report.get("shape", {})

    section("the statement the route sends")
    check(len(first.get("prepared", [])) == 1,
          f"one statement is prepared for the whole merge ({len(first.get('prepared', []))})")
    check(report.get("sql", "").startswith("INSERT INTO merged_rows"),
          "it inserts into merged_rows")
    check(len(shape.get("columns", [])) == 9 and len(shape.get("values", [])) == 9,
          "9 columns face 9 values (%d/%d)"
          % (len(shape.get("columns", [])), len(shape.get("values", []))))
    literals = {column: value for column, value in
                zip(shape.get("columns", []), shape.get("values", []))
                if value != "?"}
    check(sorted(literals) == ["first_seen", "last_seen"]
          and set(literals.values()) == {"UTC_TIMESTAMP()"},
          f"the two timestamps are literals, not bound ({sorted(literals)})")
    check(shape.get("placeholders") == 7,
          f"7 placeholders for the 7 bound columns ({shape.get('placeholders')})")

    section("the bind list agrees with the statement")
    disagreements = [d for bind in binds for d in bind.get("disagreements", [])]
    check(not disagreements,
          "every bind has a placeholder for it and a type for its value"
          + (f": {disagreements[:3]}" if disagreements else ""))
    check(all(bind.get("types") == "sssiiss" and len(bind.get("params", [])) == 7
              for bind in binds),
          "every row binds 'sssiiss' with 7 values")
    check(all(bind.get("param_types") == ["string", "string", "string", "int", "int",
                                          "string", "string"] for bind in binds),
          "the values are the types the bind string claims")
    params = [bind.get("params", []) for bind in binds]
    check(all(len(row[0]) == 64 and row[0] == row[0].lower() for row in params),
          "the key bound to key_sha256 is a lowercase hex digest")
    check(all(row[2] == "toolkit" for row in params),
          "a row that names no database goes to the primary label")
    check(all(row[6] == report.get("run1") for row in params),
          "every row is stamped with the run token")
    check(all(row[4] == len(row[5].encode("utf-8")) for row in params),
          "row_bytes is the byte length of the JSON stored beside it")

    section("one transaction around the rows")
    check(first.get("began") is True and first.get("committed") is True
          and first.get("rolled_back") is False,
          "the merge begins, writes, and commits")
    check(response.get("accepted") == 3 and response.get("received") == 3,
          f"three rows accepted ({response.get('accepted')}/{response.get('received')})")

    section("seen_in upserts instead of adding")
    stored = first.get("stored", {})
    check(len(stored) == 3, f"three rows stored, not six ({len(stored)})")
    check(sorted(row["seen_in"] for row in stored.values()) == [1, 2, 1000],
          "seen is clamped to 1..1000 before it is written")
    rerun = second.get("stored", {})
    check(len(rerun) == 3, "a second run over the same rows stores three, not six")
    check(all(rerun[key]["seen_in"] == max(stored[key]["seen_in"], 1)
              for key in stored),
          "GREATEST keeps the larger seen_in rather than the later, smaller one")
    check(all(rerun[key]["first_written"] == stored[key]["first_written"] for key in stored),
          "first_seen is not rewritten by the repeat")

    section("the run token names the last writer")
    check(all(row["run_token"] == report.get("run2") for row in rerun.values()),
          "the second run's token replaces the first on every row")
    merged = second.get("response", {}).get("merged", {})
    check(merged.get("run_token") == report.get("run2"),
          "the reply names the run it just wrote")
    check(merged.get("digest") == digest_of(list(rerun)),
          "the reply's digest is the SHA-256 of the keys this run wrote")
    check(merged.get("rows") == 3 and merged.get("totals", {}).get("rows") == 3,
          "the run's row count and the table's total are reported separately")

    section("what the route refuses")
    refusals = report.get("refusals", {})
    expected = {
        "bad table name": (400, "row table name is not a plain identifier"),
        "no values": (400, "a row without values describes nothing"),
        "bad source_db": (400, "row source_db is not a configured label"),
        "not an object": (400, "every row must be an object"),
    }
    for name, (status, message) in expected.items():
        got = refusals.get(name, {})
        check(got.get("status") == status and got.get("message") == message,
              f"{name}: answered {status} with its own message")
    mismatch = refusals.get("key mismatch", {})
    check(mismatch.get("status") == 422
          and mismatch.get("message") == "a row arrived under a key that does not describe it",
          "a key that does not describe the row is 422, not 400")
    check(all(not refusal.get("committed") and refusal.get("rolled_back") is True
              and not refusal.get("stored") for refusal in refusals.values()),
          "every refusal rolls back and leaves nothing behind")

    section("a table that cannot be prepared")
    failure = report.get("table_failure", {})
    check(failure.get("status") == 503
          and failure.get("message") == "could not prepare the merged_rows table",
          "the merge answers 503 instead of writing into a table it cannot read")
    check(failure.get("committed") is False and not failure.get("stored"),
          "and commits nothing")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
