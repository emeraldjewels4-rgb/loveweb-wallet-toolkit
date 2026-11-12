#!/usr/bin/env python3
"""Tests for the pre-merge privilege check in sync_database.py.

The check exists because the first merge after a deploy has to ALTER merged_rows
to add run_token, and an account without ALTER fails with a generic 503 only
after the rows are built and posted. These cases pin the decision: refuse exactly
when the column is missing and the grant is missing, and never refuse for a
privilege that is not needed yet.

    python test_merge_preflight.py
"""

from __future__ import annotations

import sys

import sync_database as s

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def report(exists=True, has_run=False, alter=False, drop=False, create=False, database="db_x"):
    return {"databases": {s.PRIMARY_LABEL: {
        "database": database,
        "merged_table": {
            "database": database,
            "table": "merged_rows",
            "exists": exists,
            "has_run_token": has_run,
            "needs_alter": exists is True and has_run is False,
            "privileges": {"alter": alter, "drop": drop, "create": create,
                           "insert": True, "select": True},
        },
    }}}


def main() -> int:
    print("=== 1. the failure this exists to name ===")
    ok, lines = s.schema_preflight(report(exists=True, has_run=False, alter=False))
    check(not ok, "no run_token and no ALTER is refused")
    text = " ".join(lines)
    check("ALTER" in text and "run_token" in text, "the reason names both")
    check("503" in text, "and says what would have happened instead")
    check("db_x.merged_rows" in text, "and which table to grant it on")

    print("\n=== 2. the same state with the grant present ===")
    ok, lines = s.schema_preflight(report(exists=True, has_run=False, alter=True))
    check(ok, "ALTER is enough to proceed")

    print("\n=== 3. an already-migrated table needs no grant ===")
    ok, lines = s.schema_preflight(report(exists=True, has_run=True, alter=False))
    check(ok, "run_token present passes without ALTER")
    check(not any("cannot be added" in line for line in lines), "and nothing claims it is missing")

    print("\n=== 4. a missing table needs CREATE, not ALTER ===")
    ok, lines = s.schema_preflight(report(exists=False, has_run=None, create=False))
    check(not ok, "no table and no CREATE is refused")
    check(any("CREATE" in line for line in lines), "the reason names CREATE")
    ok, _ = s.schema_preflight(report(exists=False, has_run=None, create=True))
    check(ok, "CREATE is enough")

    print("\n=== 5. DROP is only warned about, never a refusal ===")
    ok, lines = s.schema_preflight(report(exists=True, has_run=True, drop=False))
    check(ok, "a missing DROP does not block a merge")
    check(any("DROP is not granted" in line for line in lines), "it is reported instead")

    print("\n=== 6. an old route that does not report the table is not a refusal ===")
    ok, lines = s.schema_preflight({"databases": {s.PRIMARY_LABEL: {"database": "db_x"}}})
    check(ok, "the merge proceeds and finds out")
    check(any("did not report merged_rows" in line for line in lines), "and says why")
    ok, lines = s.schema_preflight(None)
    check(ok, "even an empty report is not a hard stop")

    print("\n=== 7. a boolean-ish 'exists' cannot be mistaken for a grant ===")
    ok, _ = s.schema_preflight(report(exists=None, has_run=None, alter=False, create=False))
    check(ok, "unknown table state does not refuse on privileges alone")

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
