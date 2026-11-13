#!/usr/bin/env python3
"""Unit tests for collect(): one entry per distinct key, and where each came from.

The local fake host exports the real snapshot, and that snapshot holds one row per
database, so the cross-database collapse is not reachable through it. This covers
that path directly with a two-source fixture: the same row seen in two databases
has to end up as a single entry that records both, while a row that differs by even
one value has to stay separate.

    python test_merge_collect.py
"""

from __future__ import annotations

import sys

from sync_database import collect

FAILED: list[str] = []


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


def export(*entries: dict) -> dict:
    """An export payload from table entries, so each case below reads as data."""
    return {"tables": list(entries)}


def table(database: str, name: str, *rows, on: bool = True, error=None) -> dict:
    """One table entry in the export's wire shape. A row is (key, values)."""
    entry = {"database": database, "table": name, "on": on,
             "rows": [{"key": key, "values": values} for key, values in rows]}
    if error is not None:
        entry["error"] = error
    return entry


def main() -> int:
    print("=== 1. the same row in two databases is one entry, with both sources ===")
    key, values = "k1", {"id": "1", "note": "alpha"}
    merged, tables, seen, _ = collect(export(
        table("toolkit", "journal", (key, values)),
        table("zipchain", "journal", (key, values)),
    ))
    # Two entries, both readable, is what makes tables == 2; a skip would show as
    # fewer, so "nothing was skipped" here would be a restatement, not a check.
    check(tables == 2 and seen == 2, "both tables were read and both rows seen")
    check(len(merged) == 1, "the shared row collapsed to one entry")
    entry = merged.get(key, {})
    check(entry.get("seen") == {"toolkit.journal": 1, "zipchain.journal": 1},
          "the entry records each source once (%r)" % (entry.get("seen"),))
    check(entry.get("source_db") == "toolkit",
          "the first database seen is kept as the source")
    check(entry.get("values") == values,
          "the row's own values are kept, not merged with the other copy")

    print("\n=== 2. a repeat inside one table is counted against that source ===")
    merged, _, seen, _ = collect(export(
        table("toolkit", "journal", ("k1", {"id": "1"}), ("k1", {"id": "1"})),
    ))
    check(len(merged) == 1 and seen == 2, "two rows, one entry")
    check(merged.get("k1", {}).get("seen") == {"toolkit.journal": 2},
          "the duplicate is counted twice, not folded into the count")

    print("\n=== 3. rows that differ stay apart, even across databases ===")
    merged, _, seen, _ = collect(export(
        table("toolkit", "journal", ("k1", {"id": "1"})),
        table("zipchain", "journal", ("k2", {"id": "2"})),
    ))
    check(sorted(merged) == ["k1", "k2"] and seen == 2,
          "two keys that share nothing stay two entries, both counted (%r, seen %d)"
          % (sorted(merged), seen))
    check(merged.get("k2", {}).get("source_db") == "zipchain",
          "each entry keeps its own database")

    print("\n=== 4. an unreadable database is reported, not silently dropped ===")
    _, tables, _, skipped = collect(export(
        table("toolkit", "journal", ("k1", {"id": "1"})),
        table("toolkit_archive", "journal", on=False, error="rows not readable"),
    ))
    check(tables == 1, "only the readable table counts as read")
    check(skipped == [("toolkit_archive", "rows not readable")],
          "the skip names the database and the reason (%r)" % (skipped,))

    print("\n=== 5. a readable table that failed names the table, not just the database ===")
    merged, tables, _, skipped = collect(export(
        table("toolkit", "broken", error="select refused"),
    ))
    check(tables == 0 and merged == {}, "a table that errored contributes nothing")
    check(skipped == [("toolkit.broken", "select refused")],
          "the skip names database.table (%r)" % (skipped,))

    print("\n=== 6. a table entry with no name cannot be counted ===")
    _, tables, seen, skipped = collect(export(
        table("toolkit", "", ("k1", {"id": "1"})),
    ))
    check(tables == 0 and seen == 0, "nothing is read from a nameless table entry")
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
