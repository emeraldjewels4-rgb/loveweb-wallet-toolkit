#!/usr/bin/env python3
"""A capped export is a prefix, so the sync refuses to merge it quietly.

`?action=export` stops at `cap + 1` rows per table and sets `truncated` when the
cap bit. Nothing read it. A table with 200,000 rows exported 20,000, the client
merged them, the digest check compared the rows it sent against the rows that
were stored, they matched, and the run printed "verified end to end". Every
sentence of that is true except the one a reader infers: that the table is in
the table.

So the flag is a refusal now, by name, before the snapshot is overwritten with a
prefix: which table, how many rows came back, and the two ways forward -- raise
`--limit`, or say `--allow-truncated` and merge what came knowing what did not.

The client is driven, not re-implemented: main() runs here with its network call
replaced by the export_probe's real capped export (cap 2 against a three-row
table), its snapshot pointed at a temporary file, and its FTP upload skipped. What
is asserted is the exit code, the reason printed, and that no snapshot was written
when the run refused.

    python test_truncated_export.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import sync_database as client  # noqa: E402  (needs ROOT on the path first)

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


def drive(argv: list[str], export_doc: dict, snapshot: Path) -> tuple[int, str]:
    """Run the client's main() with the host replaced by one export document."""
    asked: list[str] = []

    def fake_call(url, key, payload=None):
        asked.append(url)
        if "action=report" in url:
            # Enough for the ALTER preflight to pass, so the export is reached.
            return {"databases": {"toolkit": {"merged_table": {
                "exists": True, "has_run_token": True,
                "privileges": {"alter": True, "create": True, "drop": True}}}}}
        if "action=export" in url:
            return export_doc
        raise AssertionError("the client asked for something this test does not serve: %s" % url)

    saved = (client.call, client.SNAPSHOT, client.import_key, sys.argv)
    client.call, client.SNAPSHOT = fake_call, snapshot
    client.import_key = lambda: "a-test-key"
    sys.argv = ["sync_database.py"] + argv
    out = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out):
            client.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    finally:
        client.call, client.SNAPSHOT, client.import_key, sys.argv = saved
    return code, out.getvalue()


def main() -> int:
    print("a capped export, driven through the client that merges it")

    section("the fixture has to be a capped export")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check(proc.returncode == 0, f"the probe runs an export twice (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:500])
        return finish()
    report = json.loads(proc.stdout)

    capped = report["capped"]
    exact = report["exact"]
    flagged = {table["table"]: table["truncated"]
               for table in capped["tables"] if table.get("on")}
    check(flagged.get("broadcast_journal") is True
          and not any(value for table, value in flagged.items()
                      if table != "broadcast_journal"),
          f"the cap bit on the journal table and nothing else ({flagged})")
    check(client.capped_tables(exact) == [],
          "the same export at a cap that fits reports nothing truncated")

    section("the client names what it was given")
    names = client.capped_tables(capped)
    check(names == [("journal.broadcast_journal", 2)],
          f"capped_tables() names the table and what came back ({names})")
    check(exact.get("rows") > capped.get("rows"),
          f"the capped export carries {capped.get('rows')} row(s) where the full one "
          f"carries {exact.get('rows')}: a prefix is smaller, which is the point")

    section("a capped export refuses, and says why")
    # Under .sandbox so the snapshot path stays inside the repository: the client
    # prints it relative to ROOT, and a temporary directory elsewhere is not. The
    # directory is gitignored and absent in a fresh clone, which is how this first
    # failed on CI while passing on the machine that had one.
    (ROOT / ".sandbox").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".sandbox")) as folder:
        snapshot = Path(folder) / "db_merged.json"
        code, output = drive(["--dry-run", "--no-upload"], capped, snapshot)
        check(code == 1, f"the run exits 1 rather than printing a success (exit {code})")
        check("journal.broadcast_journal is truncated: 2 row(s) came back" in output,
              "the reason names the table and the rows that came back")
        check("refusing to merge a prefix" in output,
              "and says refusing rather than skipped or warning")
        check("--allow-truncated" in output and "50,000" in output,
              "and gives both ways forward: a bigger --limit, or the flag")
        check(not snapshot.is_file(),
              "no snapshot was written, so the prefix did not replace the record")

        section("--allow-truncated merges the prefix, out loud")
        code, output = drive(["--dry-run", "--no-upload", "--allow-truncated"], capped, snapshot)
        check(code == 0, f"the same export goes through when the flag is passed (exit {code})")
        check("merging a prefix anyway (--allow-truncated)" in output,
              "and the run says it is merging a prefix")
        check(snapshot.is_file() and json.loads(snapshot.read_text())["rows"] == 4,
              "the snapshot records the 4 rows that were actually there")

        section("an export that fits needs no flag")
        fresh = Path(folder) / "full.json"
        code, output = drive(["--dry-run", "--no-upload"], exact, fresh)
        check(code == 0, f"an uncapped export runs without --allow-truncated (exit {code})")
        check("truncated" not in output,
              "and nothing in it mentions truncation")
        check(json.loads(fresh.read_text())["rows"] == 5,
              "the snapshot holds all 5 rows of the full export")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
