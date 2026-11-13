#!/usr/bin/env python3
"""Unit tests for collect(): one entry per distinct key, and where each came from.

The local fake host exports the real snapshot now, and that snapshot holds one row
per database, so the cross-database collapse is no longer reachable through it.
This covers that path directly with a two-source fixture: the same row seen in two
databases has to end up as a single entry that records both, and a row that differs
by even one value has to stay separate.

    python test_merge_collect.py
"""

from __future__ import annotations

import sys

from sync_database import collect

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if not isinstance(cond, bool) and not isinstance(cond, int):
        raise SystemExit("check() got a non-condition %r as its condition" % (cond,))
    if not isinstance(label, str):
        raise SystemExit("check() got a non-string label: %r" % (label,))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def table(database: str, name: str, rows: list, on: bool = True, error=None) -> dict:
    entry = {"database": database, "table": name, "on": on, "rows": rows}
    if error is not None:
        entry["error"] = error
    return entry


def row(key: str, **values) -> dict:
    return {"key": key, "values": values}


def main() -> int:
    print("=== 1. the same row in two databases is one entry, with both sources ===")
    merged, tables, seen, skipped = collect({"tables": [
        table("toolkit", "journal", [row("k1", id="1", note="alpha")]),
        table("zipchain", "journal", [row("k1", id="1", note="alpha")]),
    ]})
    check(tables == 2, "both table entries were counted as read")
    check(seen == 2, "both rows were seen")
    check(len(merged) == 1, "the shared row collapsed to one entry")
    check(merged["k1"]["seen"] == {"toolkit.journal": 1, "zipchain.journal": 1},
          "and that entry records each source once (%r)" % (merged["k1"]["seen"],))
    check(merged["k1"]["source_db"] == "toolkit",
          "the first database seen is kept as the source")
    check(merged["k1"]["values"] == {"id": "1", "note": "alpha"},
          "the row's own values are kept, not merged with the other copy")
    check(skipped == [], "nothing was skipped")

    print("\n=== 2. a repeat inside one table is counted against that source ===")
    merged, tables, seen, skipped = collect({"tables": [
        table("toolkit", "journal", [row("k1", id="1"), row("k1", id="1")]),
    ]})
    check(len(merged) == 1 and seen == 2, "two rows, one entry")
    check(merged["k1"]["seen"] == {"toolkit.journal": 2},
          "the duplicate is counted twice, not folded into the count")

    print("\n=== 3. rows that differ stay apart, even across databases ===")
    merged, tables, seen, skipped = collect({"tables": [
        table("toolkit", "journal", [row("k1", id="1")]),
        table("zipchain", "journal", [row("k2", id="2")]),
    ]})
    check(len(merged) == 2 and seen == 2, "two keys that share nothing stay two entries")
    check(set(merged) == {"k1", "k2"}, "both keys are present")
    check(merged["k2"]["source_db"] == "zipchain", "each entry keeps its own database")

    print("\n=== 4. an unreadable database is reported, not silently dropped ===")
    merged, tables, seen, skipped = collect({"tables": [
        table("toolkit", "journal", [row("k1", id="1")]),
        table("toolkit_archive", "journal", [], on=False, error="rows not readable"),
    ]})
    check(tables == 1, "only the readable table counts as read")
    check(skipped == [("toolkit_archive", "rows not readable")],
          "the skip names the database and the reason (%r)" % (skipped,))

    print("\n=== 5. a readable table that failed names the table, not just the database ===")
    merged, tables, seen, skipped = collect({"tables": [
        table("toolkit", "broken", [], error="select refused"),
    ]})
    check(tables == 0 and merged == {}, "a table that errored contributes nothing")
    check(skipped == [("toolkit.broken", "select refused")],
          "the skip names database.table (%r)" % (skipped,))

    print("\n=== 6. a table entry with no name cannot be counted ===")
    merged, tables, seen, skipped = collect({"tables": [
        {"database": "toolkit", "table": "", "on": 1, "rows": [row("k1", id="1")]},
    ]})
    check(tables == 0 and seen == 0, "nothing is read from it")
    check(skipped == [("toolkit", "not readable")],
          "and it is reported with the fallback reason (%r)" % (skipped,))

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
