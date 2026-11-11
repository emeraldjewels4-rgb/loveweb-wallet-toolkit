#!/usr/bin/env python3
"""Tests for the merge verification rule in sync_database.py.

The rule exists because `merged_rows` is a union over time: a row deleted from
its source stays in the table on purpose, carrying the token of the run that
wrote it. So the check cannot be "the table equals this export" -- that fails on
every run after any delete -- and it must not become "the count looks plausible"
either, which would accept a table that quietly lost rows.

These cases pin both halves: exact equality over this run's rows, and the two
accumulation failures that are real (fewer rows than were sent, fewer than an
earlier run had already stored). No network: the function under test is pure.

    python test_merge_verify.py
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


def scoped_for(keys):
    return {"rows": len(keys), "digest": s.digest(keys), "run_token": "a" * 32}


def main() -> int:
    print("=== 1. a run that stored exactly what was sent passes ===")
    local = {"k1", "k2", "k3"}
    ok, lines = s.verification(scoped_for(local), {"rows": 3}, local, s.digest(local), set())
    check(ok, "exact run and equal digest passes")
    check(any("this run stored 3 row(s)" in line for line in lines), "it reports what this run stored")
    check(any("table holds 3 row(s) in total" in line for line in lines), "and what the table holds")

    print("\n=== 2. rows removed from a source are tolerated ===")
    # Five stored, three sent: the two extras are rows an earlier run merged and
    # the sources no longer have. That is not a failure.
    ok, lines = s.verification(scoped_for(local), {"rows": 5}, local, s.digest(local),
                               {"k1", "k2", "k3", "gone1", "gone2"})
    check(ok, "an accumulated table larger than this export passes")
    check(any("+2 against this export" in line for line in lines), "and the surplus is reported")

    print("\n=== 3. the digest is still exact, not a count ===")
    other = {"k1", "k2", "k9"}
    ok, _ = s.verification(scoped_for(other), {"rows": 3}, local, s.digest(local), set())
    check(not ok, "a different set of the same size fails")
    ok, _ = s.verification({"rows": 3, "digest": s.digest(local)}, {"rows": 3}, local,
                           s.digest(local), set())
    check(ok, "the same set passes regardless of how the scoped reply is spelled")

    print("\n=== 4. a table that lost rows fails ===")
    ok, lines = s.verification(scoped_for(local), {"rows": 2}, local, s.digest(local), set())
    check(not ok, "fewer stored than sent fails")
    check(any("fewer rows than were just sent" in line for line in lines), "and says which way")

    ok, lines = s.verification(scoped_for({"k1"}), {"rows": 1}, {"k1"}, s.digest({"k1"}),
                               {"k1", "k2"})
    check(not ok, "a total below what an earlier run stored fails")
    check(any("lost rows an earlier run had stored" in line for line in lines),
          "and names the earlier run as the reason")
    ok, _ = s.verification(scoped_for({"k1"}), {"rows": 2}, {"k1"}, s.digest({"k1"}),
                           {"k1", "k2"})
    check(ok, "the same earlier keys at the right total pass, so the floor is not off by one")

    print("\n=== 5. a host that ignores the run token is refused, not assumed ===")
    ok, lines = s.verification({"rows": 0, "digest": None}, {"rows": 5}, local,
                               s.digest(local), set())
    check(not ok, "nothing recorded under the run fails")
    check(any("older db.php or db_sync.php" in line for line in lines),
          "and points at the deployed files rather than blaming the rows")

    print("\n=== 6. an unreadable total is reported, not invented ===")
    ok, lines = s.verification(scoped_for(local), {}, local, s.digest(local), set())
    check(ok, "the run's own equality still decides when the total is missing")
    check(any("accumulation is unchecked" in line for line in lines),
          "and the gap is stated rather than silently skipped")
    ok, _ = s.verification(scoped_for(other), {}, local, s.digest(local), set())
    check(not ok, "a bad digest still fails without a total to compare")

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
