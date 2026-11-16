#!/usr/bin/env python3
"""A table that cannot be walked stops the run instead of merging a prefix.

Paging removed the old truncation: a table with a key is read to the end, one page
at a time, so the page size bounds a request and not the export. What remains is the
table with no primary key and no id -- there is no column to resume from, so it is
read once and the page says there was more. That page is a prefix, and a prefix
verifies perfectly: the digest compares the rows that were sent against the rows
that were stored, both true, while the rows past the page were never in the run.

So the run stops, names the table and how many rows it did get, and offers the two
ways forward. The client is driven, not re-implemented: main() runs here with its
network call replaced by the pages export_probe recorded, its snapshot pointed at a
temporary file, and its FTP upload skipped.

    python test_truncated_export.py
"""

from __future__ import annotations

import contextlib
import io
import hashlib
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


class RecordedHost(client.Host):
    """The transport, answered from recorded replies rather than a socket.

    Everything main() asks for goes through this one object, so a request added to
    the client either lands in a method here or raises -- instead of slipping past
    a patch of `client.call` that nobody updated.
    """

    def __init__(self, ping: dict, report: dict, listing: dict, snapshot: Path,
                 snapshot_in_place: bool = False):
        super().__init__(key="a-test-key", snapshot=snapshot)
        self.ping_reply = ping
        self.report_reply = report
        self.listing_reply = listing
        self.asked: list[str] = []

    def report(self):
        return {"databases": {"toolkit": {"merged_table": {
            "exists": True, "has_run_token": True,
            "privileges": {"alter": True, "create": True, "drop": True}}}}}

    def ping(self):
        return self.ping_reply, None

    def listing(self):
        return self.listing_reply

    def page(self, label, table, after, limit):
        # Matched on the URL the transport builds, so a wrong URL fails here.
        url = self.page_url(label, table, after, limit)
        self.asked.append(url)
        if "table=journal." in url:
            return self.report_reply["pages"]["second"] if "after=2" in url \
                else self.report_reply["pages"]["first"]
        if "table=cjd." in url:
            return self.report_reply["keyless_capped"]
        raise AssertionError("the client asked for something this test does not serve: %s" % url)


def drive(argv: list[str], report: dict, listing: dict, snapshot: Path) -> tuple[int, str]:
    """Run the client's main() with the host replaced by recorded replies."""
    asked: list[str] = []

    # The route check is a separate request on purpose (an HTTP failure there is a
    # message this run words, not call()'s generic one), so the fake host answers it
    # with what the file on disk would report.
    route_php = ROOT / "php" / "db_sync.php"
    ping = {
        "action": "ping",
        "route": "db_sync.php",
        "version": 3,
        "sha256": hashlib.sha256(route_php.read_bytes()).hexdigest(),
        "bytes": route_php.stat().st_size,
        "files": {name: {
            "sha256": hashlib.sha256((ROOT / "php" / name).read_bytes()).hexdigest(),
            "bytes": (ROOT / "php" / name).stat().st_size,
            "present": True,
        } for name in client.ROUTE_FILES},
    }

    host = RecordedHost(ping, report, listing, snapshot)
    saved = sys.argv
    sys.argv = ["sync_database.py"] + argv
    out = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out):
            client.main(host=host)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.argv = saved
    return code, out.getvalue()


def main() -> int:
    print("a table with no key to page by, driven through the client")

    section("the fixture has to be a page that cannot continue")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check(proc.returncode == 0, f"the probe records the pages (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:500])
        return finish()
    report = json.loads(proc.stdout)

    keyless = report["keyless_capped"]
    check(keyless.get("paged") is False and keyless.get("more") is True
          and keyless.get("cursor") is None,
          f"a keyless table's page with rows left over: paged=false, more=true "
          f"({len(keyless.get('rows') or [])} row(s) of 3)")

    keep = [entry for entry in report["listing"]["databases"]
            if entry.get("database") in {"journal", "cjd"}]
    listing = {"databases": keep}

    section("the walk names what it could not reach the end of")
    def fetch(label, table, after):
        if label == "journal":
            return report["pages"]["second"] if after == "2" else report["pages"]["first"]

        return keyless

    document, capped = client.walk_export(fetch, listing)
    check(capped == [("cjd.cjd_source", 1)],
          f"capped names the table and the row it got ({capped})")
    check(document["rows"] == 4,
          f"while the walkable table was read whole ({document['rows']} rows in all)")

    section("a keyless page refuses, and says why")
    # Under .sandbox so the snapshot path stays inside the repository: the client
    # prints it relative to ROOT, and the directory is absent in a fresh clone.
    (ROOT / ".sandbox").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".sandbox")) as folder:
        snapshot = Path(folder) / "db_merged.json"
        code, output = drive(["--dry-run", "--no-upload"], report, listing, snapshot)
        check(code == 1, f"the run exits 1 rather than printing a success (exit {code})")
        check("cjd.cjd_source is truncated: 1 row(s) came back" in output,
              "the reason names the table and the rows that came back")
        check("refusing to merge a prefix" in output,
              "and says refusing rather than skipped or warning")
        check("no key to page by" in output and "--allow-truncated" in output,
              "and gives both ways forward: a bigger --limit, or the flag")
        check(not snapshot.is_file(),
              "no snapshot was written, so the prefix did not replace the record")

        section("--allow-truncated merges the prefix, out loud")
        code, output = drive(["--dry-run", "--no-upload", "--allow-truncated"], listing=listing,
                             report=report, snapshot=snapshot)
        check(code == 0, f"the same export goes through when the flag is passed (exit {code})")
        check("merging a prefix anyway (--allow-truncated)" in output,
              "and the run says it is merging a prefix")
        check(snapshot.is_file() and json.loads(snapshot.read_text())["rows"] == 4,
              "the snapshot records the 4 rows that were actually there")

        section("a listing with only walkable tables needs no flag")
        walkable = {"databases": [entry for entry in keep if entry["database"] == "journal"]}
        fresh = Path(folder) / "full.json"
        code, output = drive(["--dry-run", "--no-upload"], report, walkable, fresh)
        check(code == 0, f"an export of walkable tables runs without the flag (exit {code})")
        check("truncated" not in output, "and nothing in it mentions truncation")
        check(json.loads(fresh.read_text())["rows"] == 3,
              "the snapshot holds all 3 rows, read across two pages")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
