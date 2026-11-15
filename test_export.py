#!/usr/bin/env python3
"""The two-phase export: what there is, then one page at a time from a cursor.

The export used to be one request holding every row of every table, which is what
made it run out of memory at 80,000 rows. It is two actions now:

    GET ?action=tables                                  what there is, and no rows
    GET ?action=export&table=<database>.<table>&after=K  one page, ordered by key

`after` is the last key the previous page returned, so the cap cannot lose a row:
the walk resumes where it stopped. This suite drives both against a stub mysqli
that applies the cursor's WHERE, ORDER BY and LIMIT, so the three-request walk here
is the real SQL and not a re-description of it.

It also holds the two properties that were the reason the export exists at all:
encode_value() wraps a non-UTF-8 column instead of letting json_encode mangle it,
and the primary database's merged_rows is not exported while a neighbour's table of
the same name is.

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


def database(listing: dict, label: str) -> dict:
    """One database's entry in the phase-one reply, or an empty one."""
    for entry in listing.get("databases") or []:
        if entry.get("database") == label:
            return entry
    return {}


def table_of(listing: dict, label: str, table: str) -> dict:
    """One table's entry in the phase-one reply, or an empty one."""
    for entry in database(listing, label).get("tables") or []:
        if entry.get("table") == table:
            return entry
    return {}


def main() -> int:
    print("the two-phase export: a listing, then pages from a cursor")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check(proc.returncode == 0, f"the probe walks an export (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:500])
        return finish()
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        check(False, f"the probe answers with JSON (got {proc.stdout.strip()[:80]!r})")
        return finish()

    check("unavailable" not in report,
          "the stub stands in: this interpreter has no mysqli to shadow")

    section("what a value becomes on its way out")
    encoded = report.get("encoded", {})
    check(encoded.get("null", {}).get("value") is None,
          "NULL stays null rather than becoming the string 'NULL'")
    check(encoded.get("text", {}).get("value") == "plain",
          "plain text is written as it arrived")
    check(encoded.get("multibyte", {}).get("value") == "ü €",
          "and so is valid multibyte text, unescaped")
    check(encoded.get("nul", {}).get("value") == "a\x00b",
          "a NUL byte is not a reason to wrap: the string is still valid UTF-8")
    bad = encoded.get("invalid_utf8", {})
    check(bad.get("value") == {"b64": base64.b64encode(b"latin1 \xb1 byte").decode()},
          f"invalid UTF-8 travels base64-wrapped ({bad.get('value')})")
    check(bad.get("round_trip") == base64.b64encode(b"latin1 \xb1 byte").decode(),
          "and the bytes decode back to exactly what the column held")
    check("\ufffd" not in str(bad.get("json")),
          "the JSON it becomes carries no replacement character")
    check(encoded.get("whole_float", {}).get("value") == "1"
          and encoded.get("int", {}).get("value") == "1",
          "numbers become strings: a page's values are what mysqli hands back")

    section("phase one: what there is, with no rows")
    listing = report.get("listing", {})
    check(listing.get("action") == "tables" and len(listing.get("databases") or []) == 7,
          f"one entry per configured database ({len(listing.get('databases') or [])})")
    check(all("rows" not in entry for entry in listing.get("databases") or []),
          "the listing carries tables and columns, not rows: it stays small however "
          "large the databases are")
    journal = table_of(listing, "journal", "broadcast_journal")
    check(journal.get("cursor") == "id" and journal.get("columns") == ["id", "hash"],
          f"a table says its columns and the key a page is ordered by ({journal.get('cursor')})")
    primary = database(listing, "toolkit")
    check(primary.get("on") == 1 and not (primary.get("tables") or []),
          "the primary database's merged_rows is not offered for export: merging it "
          "would merge the previous merge")
    neighbour = table_of(listing, "neighbour", "merged_rows")
    check(bool(neighbour),
          "a neighbour with a table of the same name is offered: the skip is the "
          "primary's own output, not the name")
    cjd = table_of(listing, "cjd", "cjd_source")
    check(cjd.get("cursor") is None,
          "a table with no primary key and no id cannot be walked, and says so")
    offline = database(listing, "offline")
    check(offline.get("on") == 0 and offline.get("error") == "not reachable",
          "an unreachable database is reported instead of omitted")

    section("phase two: the walk, one page at a time")
    pages = report.get("pages", {})
    first, second, third = pages.get("first", {}), pages.get("second", {}), pages.get("third", {})
    check([row["values"]["id"] for row in first.get("rows") or []] == ["1", "2"]
          and first.get("more") is True and first.get("next") == "2",
          f"page one: 2 rows, more, and the key to continue from "
          f"({[r['values']['id'] for r in first.get('rows') or []]}, next {first.get('next')!r})")
    check([row["values"]["id"] for row in second.get("rows") or []] == ["3"]
          and second.get("more") is False and second.get("next") is None,
          "page two: the last row, and no more to ask for")
    walked = [row["values"]["id"] for page in (first, second) for row in page.get("rows") or []]
    check(walked == ["1", "2", "3"],
          f"the pages together are the whole table, each row once ({walked})")
    check(third.get("rows") == [] and third.get("more") is False,
          "asking past the last key returns nothing instead of page one again")
    check(first.get("cursor") == "id" and first.get("paged") is True,
          "the page names the cursor it was ordered by")

    queries = [query for query in report.get("journal_queries") or []
               if "broadcast_journal` " in query]
    check(queries[:2] == [
        "SELECT * FROM `broadcast_journal` ORDER BY `id` ASC LIMIT 3",
        "SELECT * FROM `broadcast_journal` WHERE `id` > '2' ORDER BY `id` ASC LIMIT 3",
    ], f"the cursor is the WHERE, not a re-read: {queries[:2]}")
    check(all("LIMIT 3" in query for query in queries[:2]),
          "and each page asks for cap + 1 rows, so 'more' is answered by the database")

    section("a page that cannot be walked")
    single = report.get("single", {})
    check(single.get("paged") is False and single.get("more") is False,
          "a keyless table is read once and reports paged: false")
    wrapped = [row["values"].get("content") for row in single.get("rows") or []
               if isinstance(row["values"].get("content"), dict)]
    check(len(single.get("rows") or []) == 3
          and wrapped == [{"b64": base64.b64encode(b"latin1 \xb1 byte").decode()}],
          f"and its binary column still travels wrapped, in a page ({len(single.get('rows') or [])} rows)")

    section("what a page refuses")
    refused = report.get("refusals", {})
    check(refused.get("no database", {}).get("status") == 404
          and "no database by that name" in (refused.get("no database", {}).get("message") or ""),
          "a database label nobody configured: 404, by name")
    check(refused.get("no table", {}).get("status") == 404
          and "no table by that name" in (refused.get("no table", {}).get("message") or ""),
          "a table the database does not have: 404, by name")
    check(refused.get("offline", {}).get("status") == 503
          and refused.get("offline", {}).get("message") == "that database is not reachable",
          "an unreachable database: 503, rather than an empty page that looks complete")
    check(refused.get("refused select", {}).get("status") == 503
          and refused.get("refused select", {}).get("message") == "that table is not readable",
          "a SELECT the account may not run: 503, named for what failed")

    section("what a row carries")
    rows = first.get("rows") or []
    check(all(len(row.get("key", "")) == 64 and row["key"] == row["key"].lower()
              and all(char in "0123456789abcdef" for char in row["key"]) for row in rows),
          "every row carries a lowercase hex key")
    check(all(row.get("bytes") == len(json.dumps(row["values"], separators=(",", ":")))
              for row in rows),
          "bytes is the byte length of the values as JSON")
    check(set(rows[0].get("values", {})) == {"id", "hash"},
          "and the row holds the table's columns, from information_schema")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
