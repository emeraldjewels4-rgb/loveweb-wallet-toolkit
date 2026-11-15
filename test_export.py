#!/usr/bin/env python3
"""The export, executed: value encoding, the row cap, and what it refuses to lose.

export_all() is the read half of the sync and it had never run offline, because a
route file reads the configured databases on include. It takes its databases as an
argument now, so tests/php/export_probe.php drives the real function against a stub
mysqli: information_schema for the names, a SELECT per table, the rows as mysqli
would hand them back.

The value encoding is the part with a sharp edge. mysqli returns strings whatever
the column type, so a BLOB or a latin1 column is not valid UTF-8, and json_encode
replaces the bytes it cannot read -- silent corruption of the one kind this code
exists to prevent. encode_value() wraps those in base64 instead, and this asserts
both halves: what the export writes, and that the bytes survive the round trip.

The cap is asserted from the query text, not from the row count: "ask for cap + 1
and report truncated" is the mechanism, and a table of exactly cap rows must not
say it was cut.

    python test_export.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "tests" / "php" / "export_probe.php"
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


def entry(report: dict, label: str) -> dict:
    """The table entry for one database label, or {} when it has none."""
    for table in report.get("tables", []):
        if table.get("database") == label:
            return table
    return {}


def main() -> int:
    print("export_all(): what a value becomes, what the cap cuts, what it skips")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    check(proc.returncode == 0, f"the probe executes export_all (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:400])
        return finish()
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        check(False, f"the probe answers with JSON (got {proc.stdout.strip()[:80]!r})")
        return finish()

    check("unavailable" not in report,
          "the stub stands in: this interpreter has no mysqli to shadow")
    encoded = report.get("encoded", {})

    section("a value that is already text")
    check(encoded.get("null", {}).get("value") is None,
          "NULL stays null rather than becoming the string 'NULL'")
    check(encoded.get("text", {}).get("value") == "plain",
          "plain text is written as it arrived")
    check(encoded.get("multibyte", {}).get("value") == "ü €",
          "and so is valid multibyte text, unescaped")
    nested = encoded.get("nul", {}).get("value")
    check(isinstance(nested, str) and nested == "a\x00b",
          "a NUL byte is not a reason to wrap: the string is still valid UTF-8")

    section("a value that is not text at all")
    bad = encoded.get("invalid_utf8", {})
    wrapped = bad.get("value", {})
    check(isinstance(wrapped, dict) and set(wrapped) == {"b64"},
          f"invalid UTF-8 travels base64-wrapped ({wrapped})")
    check(base64.b64decode(wrapped.get("b64", "")) == b"latin1 \xb1 byte",
          "and the bytes decode back to exactly what the column held")
    check("\ufffd" not in str(bad.get("json")),
          "the JSON it becomes carries no replacement character")
    check(bad.get("round_trip") == base64.b64encode(b"latin1 \xb1 byte").decode(),
          "and re-encoding the decoded bytes gives the same base64 back")
    check(encoded.get("whole_float", {}).get("value") == "1"
          and encoded.get("int", {}).get("value") == "1",
          "numbers become strings: an export's values are what mysqli hands back")

    section("the cap")
    capped = report.get("capped", {})
    exact = report.get("exact", {})
    journal_capped = entry(capped, "journal")
    journal_exact = entry(exact, "journal")
    check(capped.get("cap") == 2 and exact.get("cap") == 3,
          "the cap the caller asked for is echoed in the reply")
    limits = [query for query in report.get("journal_queries", [])
              if "broadcast_journal` LIMIT" in query]
    check(limits == ["SELECT * FROM `broadcast_journal` LIMIT 3",
                     "SELECT * FROM `broadcast_journal` LIMIT 4"],
          f"each table is fetched as cap + 1 rows ({limits})")
    check(len(journal_capped.get("rows", [])) == 2
          and journal_capped.get("truncated") is True,
          "a table with more rows than the cap is cut and says so")
    check(len(journal_exact.get("rows", [])) == 3
          and journal_exact.get("truncated") is False,
          "a table with exactly cap rows is not reported as cut")

    section("what the cap counts")
    check(capped.get("rows") == 4,
          "the reply's row count is the rows exported, not the ones asked for "
          f"({capped.get('rows')}: 2 journal + 1 cjd + 1 neighbour)")
    check(exact.get("rows") == 5, "and it follows the cap (3 + 1 + 1)")

    section("the table this route writes")
    labels = [table.get("database") for table in capped.get("tables", [])]
    check("toolkit" not in labels,
          "the primary database's merged_rows is not exported: merging it would "
          "merge the previous merge")
    neighbour = entry(capped, "neighbour")
    check(neighbour.get("table") == "merged_rows"
          and len(neighbour.get("rows", [])) == 1,
          "a neighbour with a table of the same name is exported: the skip is the "
          "primary's own output, not the name")

    section("what the reply does not compute")
    check("merged" not in capped,
          "no merged status in an export: it costs a COUNT, a GROUP BY and a full "
          "ordered scan of merged_rows, on the route that skips that table")
    check(sorted(capped) == ["action", "cap", "generated_at", "rows", "tables"],
          f"and its fields are exactly the export's own ({sorted(capped)}), so a "
          "block creeping back fails here")

    section("a database that cannot be read")
    offline = entry(capped, "offline")
    check(offline.get("on") == 0 and offline.get("error") == "not reachable",
          "an unreachable database is reported as such instead of omitted")
    locked = entry(capped, "locked")
    check(locked.get("on") == 1 and locked.get("error") == "rows not readable"
          and locked.get("rows") == [],
          "a refused SELECT leaves the table listed and its rows empty")
    check(len(capped.get("tables", [])) == 5
          and len(journal_capped.get("rows", [])) == 2,
          "and neither one stops the tables that could be read")

    section("what a row carries")
    rows = journal_capped.get("rows", [])
    check(all(len(row.get("key", "")) == 64
              and row["key"] == row["key"].lower()
              and all(c in "0123456789abcdef" for c in row["key"])
              for row in rows),
          "every row carries a lowercase hex key")
    check(all(row.get("bytes") == len(json.dumps(row.get("values"), ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8"))
              for row in rows),
          "row_bytes is the byte length of the JSON it is stored with")
    check(sorted(journal_capped.get("columns", [])) == ["hash", "id"],
          "the column list comes from information_schema, not from the first row")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
