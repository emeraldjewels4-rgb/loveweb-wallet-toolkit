#!/usr/bin/env python3
"""The pages the route writes, and the client that walks them.

The export is two-phase now: ?action=tables says what there is, and one request
per page carries a cursor. A page is compact -- it is a row dump only
sync_database.py reads, and pretty-printing it was 58-74% more bytes and a fatal
at 80,000 rows. A status or a refusal stays indented, because a person reads those.

This suite drives the walk, not a copy of it: `walk_export()` is handed a fetch
that serves the pages export_probe recorded from the real route, so what is
asserted is the client's own loop -- the order of the requests, that it asks for
the next page with the cursor the last one returned, that it stops when the host
says there is nothing more, and that a reply claiming more with no cursor is named
rather than looped on forever.

It also holds the route check to its answers: a ping that names the file that is
here passes, and a stale route, a different file, a short copy and a contract
version below the client's floor are different messages rather than one "it
failed" -- including the two numbers, so the version cannot disagree with the file
it labels unnoticed.

    python test_sync_client.py
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import sync_database as client  # noqa: E402  (needs ROOT on the path first)

PROBE = ROOT / "tests" / "php" / "export_probe.php"
CLIENT = ROOT / "sync_database.py"
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


def listing_for(report: dict) -> dict:
    """The probe's phase one, cut down to the databases this suite serves pages for."""
    keep = {"journal", "cjd", "offline"}
    return {"databases": [entry for entry in report["listing"]["databases"]
                          if entry.get("database") in keep]}


def walk(report: dict, listing: dict, asks: list | None = None) -> tuple[dict, list]:
    """Walk the recorded pages through the client's own loop."""
    pages = report["pages"]
    single = report["single"]

    def fetch(label, table, after):
        if asks is not None:
            asks.append((label, table, after))
        if label == "journal":
            return pages["second"] if after == "2" else pages["first"]

        return single

    return client.walk_export(fetch, listing)



class RecordingHost(client.Host):
    """The transport, with every request recorded and the run stopped at the listing.

    A subclass rather than a mock of five module globals: whatever main() asks for
    next, it asks this object, and a fake that has not heard of it fails loudly in
    one place instead of silently serving a stale patch somewhere else.
    """

    key = "a-test-key"

    def __init__(self, ping: dict):
        super().__init__()
        self.ping_reply = ping
        self.asked: list[str] = []

    def ping(self):
        self.asked.append("db_sync.php?action=ping")
        return self.ping_reply, None

    def listing(self):
        self.asked.append("db_sync.php?action=tables")
        raise SystemExit("fake host: stopped at the listing")


def run_main(argv: list[str], ping: dict, asked: list[str]) -> tuple[object, str]:
    """client.main() with the host replaced by a recorder.

    `asked` records the requests that were made and then stops the run, so what is
    asserted is how far main() got -- past the route check or not -- without
    needing a fake for the merge. "The ping was skipped" is therefore a fact about
    requests rather than about the text on screen.
    """
    host = RecordingHost(ping)
    host.asked = asked
    saved = sys.argv
    sys.argv = ["sync_database.py"] + argv
    out = io.StringIO()
    code: object = 0
    try:
        with contextlib.redirect_stdout(out):
            client.main(host=host)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else exc.code
    finally:
        sys.argv = saved
    return code, out.getvalue()

def merged_text(document: dict) -> str:
    """What the client would merge from a document, as text, for comparison."""
    merged, _tables, _rows, _skipped = client.collect(document)
    return json.dumps(merged, sort_keys=True)


def main() -> int:
    print("the export's pages, and the client that walks them")

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

    replies = report.get("replies") or {}
    check(set(replies) == {"export", "status", "error"},
          f"the route's three reply shapes are captured ({sorted(replies)})")
    body = replies.get("export") or ""

    section("a page is compact")
    check(body.startswith("{") and body.rstrip("\n").endswith("}"),
          "one JSON document, not a fragment")
    check(body.endswith("\n") and body.count("\n") == 1,
          f"one line and one trailing newline ({body.count(chr(10))} line break(s))")
    check("\n    " not in body and not re.search(r"\{\s*\n", body),
          "no indentation: JSON_UNESCAPED_SLASHES without JSON_PRETTY_PRINT")
    try:
        page = json.loads(body)
    except ValueError as exc:
        check(False, f"the page parses as JSON ({exc})")
        return finish()
    check(page.get("action") == "export" and page.get("table") == "broadcast_journal",
          f"it parses to a page of {page.get('table')!r} from {page.get('database')!r}")
    pretty_same = json.dumps(page, indent=4, ensure_ascii=False)
    check(len(pretty_same.encode("utf-8")) > len(body.encode("utf-8")),
          "the same page pretty-printed is %d bytes against %d compact (%0.2fx)"
          % (len(pretty_same.encode("utf-8")), len(body.encode("utf-8")),
             len(pretty_same.encode("utf-8")) / len(body.encode("utf-8"))))

    section("every other reply is still pretty")
    status = replies.get("status") or ""
    error = replies.get("error") or ""
    check(status.count("\n") > 3 and "\n    " in status,
          f"a status reply is indented ({status.count(chr(10))} lines)")
    check(error.count("\n") > 1 and "\n    " in error,
          f"a refusal is indented too ({error.count(chr(10))} lines)")
    check(status.endswith("\n") and error.endswith("\n"),
          "both end with the one newline the route always writes")
    try:
        parsed = (json.loads(status), json.loads(error))
    except ValueError as exc:
        check(False, f"both parse as JSON ({exc})")
        return finish()
    check(parsed[0].get("action") == "status" and parsed[1].get("error") == "forbidden",
          "and both are the documents they claim to be")

    section("the walk asks for each page once")
    asks: list = []
    listing = listing_for(report)
    document, capped = walk(report, listing, asks)
    check(asks[:3] == [("journal", "broadcast_journal", None),
                       ("journal", "broadcast_journal", "2"),
                       ("cjd", "cjd_source", None)],
          f"a cursor walk: no cursor, then the key page one returned ({asks})")
    check(len(asks) == 3,
          "and it stops: two requests for the two-page table, one for the keyless one")
    check(capped == [],
          "nothing was capped, because the walk read past the first page")
    check(document["rows"] == 6 and len(document["tables"]) == 3,
          f"the document holds {document['rows']} row(s) across "
          f"{len(document['tables'])} table entries, the unread database included")

    journal = [row for table in document["tables"]
               if table.get("table") == "broadcast_journal"
               for row in table.get("rows") or []]
    check([row["values"]["id"] for row in journal] == ["1", "2", "3"],
          "the walked table is the whole table, in cursor order")

    section("the client merges it")
    merged, tables, rows, skipped = client.collect(document)
    check(bool(merged) and tables == 2 and rows == 6,
          f"collect() merges {rows} row(s) from {tables} readable table(s)")
    check([reason for _name, reason in skipped] == ["not reachable"],
          f"and reports the database it could not read ({[r for _n, r in skipped]})")
    check(len(merged) == len(journal) + 3,
          "the keyless table's 3 rows are in there too, each on its own key")

    section("the framing does not change the result")
    pretty_report = dict(report)
    pretty_report["pages"] = {name: json.loads(json.dumps(value, indent=4))
                              for name, value in report["pages"].items()}
    pretty_report["single"] = json.loads(json.dumps(report["single"], indent=4))
    pretty_document = walk(pretty_report, listing)[0]
    pretty_merged, _t, _r, _s = client.collect(pretty_document)
    check(set(merged) == set(pretty_merged), "the same keys, from either framing")
    check(merged_text(document) == merged_text(pretty_document),
          "byte-identical rows, values included")
    check(client.digest(merged) == client.digest(pretty_merged),
          "the digest the client compares with the host does not depend on framing")
    check(client.payload_rows(merged) == client.payload_rows(pretty_merged),
          "nor does the body it posts back")

    section("a reply that would truncate silently")
    broken = {"databases": [{"database": "journal", "on": 1, "error": None,
                             "tables": [{"table": "t", "columns": ["id"], "cursor": "id"}]}]}

    def stuck(_label, _table, _after):
        return {"rows": [], "more": True, "next": None}

    try:
        client.walk_export(stuck, broken)
        check(False, "a page claiming more with no cursor is refused")
    except ValueError as exc:
        check("says there is more but gave no cursor" in str(exc),
              f"a page claiming more with no cursor is named ({exc})")

    section("a table that changes while it is being walked")
    # An ALTER between two pages is not a truncation: nothing is missing and the
    # digest is self-consistent -- it just counts one row twice, because collect()
    # keys on the values and a new column makes a new key for the same row.
    changing = {"databases": [{"database": "journal", "on": 1, "error": None,
                              "tables": [{"table": "t", "columns": ["id", "amount"],
                                          "cursor": "id"}]}]}
    first_page = {"columns": ["id", "amount"], "rows": [{"key": "a"}],
                  "more": True, "next": 1}
    second_page = {"columns": ["id", "amount", "note"], "rows": [{"key": "b"}],
                   "more": False}

    def two_shapes(_label, _table, after):
        return second_page if after == 1 else first_page

    try:
        client.walk_export(two_shapes, changing)
        check(False, "a table whose columns change mid-walk is refused")
    except client.TableChanged as exc:
        check("journal.t changed while it was being read" in str(exc)
              and "['id', 'amount', 'note']" in str(exc)
              and "page 2" in str(exc),
              f"naming the table, the page and both column sets ({str(exc)[:48]}...)")

    def one_shape(_label, _table, _after):
        return dict(first_page, more=False)

    same, capped_same = client.walk_export(one_shape, changing)
    check(same["rows"] == 1 and capped_same == [],
          "while two pages of one shape are walked as before")

    # And the run words it the way every other refusal is worded, rather than
    # letting the exception out as a traceback.
    class ChangedHost(client.Host):
        key = "a-test-key"

        def listing(self):
            return changing

        def page(self, label, table, after, limit):
            return two_shapes(label, table, after)

    saved_argv = sys.argv
    sys.argv = ["sync_database.py", "--no-upload", "--skip-preflight",
                "--skip-route-check"]
    out = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(out):
            client.main(host=ChangedHost())
    except SystemExit as exc:
        code = exc.code
    except Exception as exc:  # a refusal that became a traceback is still a failure
        code = "%s: %s" % (type(exc).__name__, exc)
    finally:
        sys.argv = saved_argv
    text = out.getvalue()
    check(code == 1 and "journal.t changed while it was being read" in text
          and "refusing to merge" in text,
          f"and the run stops with exit 1, naming the table ({code})")

    section("a table changed before its first page is asked for")
    # The listing is phase one; an ALTER between it and page one used to be
    # adopted as the reference, so every later page agreed with the new shape.
    early = {"databases": [{"database": "journal", "on": 1, "error": None,
                            "tables": [{"table": "t", "columns": ["id"],
                                        "cursor": "id"}]}]}

    def one_page(_label, _table, _after):
        return {"columns": ["id", "amount"], "rows": [{"key": "a"}], "more": False}

    try:
        client.walk_export(one_page, early)
        check(False, "an ALTER between the listing and page one is refused")
    except client.TableChanged as exc:
        check("page 1" in str(exc) and "listing reported ['id']" in str(exc)
              and "['id', 'amount']" in str(exc),
              f"naming page one, the listing and the page ({str(exc)[:48]}...)")

    section("the same table in two databases")
    def two_databases(second_columns):
        return {"databases": [
            {"database": "journal", "on": 1, "error": None,
             "tables": [{"table": "t", "columns": ["id", "amount"], "cursor": "id"}]},
            {"database": "cjd", "on": 1, "error": None,
             "tables": [{"table": "t", "columns": second_columns, "cursor": "id"}]},
        ]}

    def shaped_page(label, _table, _after):
        # Each database serves its own shape, so this is a cross-database
        # difference rather than a table changing inside one walk.
        columns = ["id", "amount"] if label == "journal" else ["id", "amount", "note"]
        return {"columns": columns, "rows": [{"key": label}], "more": False}

    mixed, _capped_mixed = client.walk_export(
        shaped_page, two_databases(["id", "amount", "note"]))
    mixed_lines = client.shape_warnings(mixed["shapes"])
    check(mixed["rows"] == 2
          and mixed["shapes"] == {"t": {"journal": ["id", "amount"],
                                        "cjd": ["id", "amount", "note"]}}
          and len(mixed_lines) == 1
          and "t has columns" in mixed_lines[0] and "journal" in mixed_lines[0]
          and "cjd" in mixed_lines[0],
          f"a table with two column lists across databases is named, not refused "
          f"({mixed_lines[:1]})")

    def fixed_page(label, _table, _after):
        return {"columns": ["id", "amount"], "rows": [{"key": label}], "more": False}

    alike, _capped_alike = client.walk_export(fixed_page, two_databases(["id", "amount"]))
    check(alike["rows"] == 2
          and alike["shapes"] == {"t": {"journal": ["id", "amount"],
                                        "cjd": ["id", "amount"]}}
          and client.shape_warnings(alike["shapes"]) == [],
          "while the same columns in both databases are walked, recorded and not warned about")

    dropped: list = []
    warned, _capped_warn = client.walk_export(two_shapes, changing, guard="warn",
                                              changed=dropped)
    check(warned["rows"] == 0 and dropped == ["journal.t"],
          f"--alter-guard=warn drops the table and names it ({dropped})")

    unguarded, _capped_off = client.walk_export(two_shapes, changing, guard="off")
    check(unguarded["rows"] == 2,
          "--alter-guard=off merges both shapes rather than comparing them")

    section("a shape that changed between runs")
    lines = client.shape_drift({"t": {"journal": ["id", "amount"]}},
                              {"t": {"journal": ["id", "amount", "note"]}})
    check(lines == ["t in journal: added ['note'] since the last run"],
          f"the added column is named ({lines})")
    check(client.shape_drift({"t": {"journal": ["id", "amount"]}},
                             {"t": {"journal": ["id", "amount"]}}) == [],
          "and an unchanged table is silent")
    check(client.shape_drift({"t": {"journal": ["id", "amount"]}},
                             {"t": {"journal": ["id"]}})
          == ["t in journal: removed ['amount'] since the last run"],
          "a removed column is named too")
    check(client.shape_drift({"t": {"journal": ["id", "amount"]}},
                             {"t": {"journal": ["amount", "id"]}})
          == ["t in journal: reordered ['id', 'amount'] -> ['amount', 'id'] "
              "since the last run"],
          "a reorder is not silently treated as no change")

    section("the collision warning, driven through main()")
    # The walk records shapes; main() is what turns them into the advisory. A
    # listing with one table name in two databases is the end-to-end path: the
    # warning is printed once and the run finishes, since collect() keys on values
    # and the two shapes cannot collide.
    class CollisionHost(client.Host):
        def listing(self):
            return {"databases": [
                {"database": "journal", "on": 1, "error": None,
                 "tables": [{"table": "t", "columns": ["id", "amount"],
                             "cursor": "id"}]},
                {"database": "cjd", "on": 1, "error": None,
                 "tables": [{"table": "t", "columns": ["id", "amount", "note"],
                             "cursor": "id"}]},
            ]}

        def page(self, label, table, after, limit):
            columns = ["id", "amount"] if label == "journal" \
                else ["id", "amount", "note"]
            return {"columns": columns,
                    "rows": [{"key": label, "values": {"id": label}}],
                    "more": False}

    (ROOT / ".sandbox").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".sandbox")) as folder:
        snapshot_path = Path(folder) / "db_merged.json"
        host = CollisionHost(key="a-test-key", snapshot=snapshot_path)
        saved_argv = sys.argv
        sys.argv = ["sync_database.py", "--dry-run", "--no-upload",
                    "--skip-preflight", "--skip-route-check"]
        out = io.StringIO()
        code = 0  # main() returns normally on --dry-run; only a refusal exits
        try:
            with contextlib.redirect_stdout(out):
                client.main(host=host)
        except SystemExit as exc:
            code = exc.code
        finally:
            sys.argv = saved_argv
        text = out.getvalue()
        recorded = json.loads(snapshot_path.read_text())
    check(code == 0 and text.count("has columns") == 1
          and "export: warning:" in text
          and "merge: 2 distinct row(s)" in text
          and recorded["shapes"] == {"t": {"journal": ["id", "amount"],
                                          "cjd": ["id", "amount", "note"]}},
          f"main() warns once, merges both shapes and finishes ({code}, "
          f"{text.count('has columns')} warning(s))")

    section("what survives a page")
    values = [value for table in document["tables"]
              for row in table.get("rows") or []
              for value in (row.get("values") or {}).values()]
    wrapped = [value for value in values
               if isinstance(value, dict) and isinstance(value.get("b64"), str)]
    check(wrapped == [{"b64": base64.b64encode(b"latin1 \xb1 byte").decode()}],
          f"the invalid-UTF-8 column is still base64-wrapped ({wrapped})")
    check(any(isinstance(value, dict) and base64.b64decode(value["b64"]) == b"latin1 \xb1 byte"
              for value in wrapped),
          "and decodes to the exact column bytes, not a replacement character")
    check(all(table.get("truncated") is False for table in document["tables"]
              if table.get("on")),
          "every table read was read whole, so none is marked truncated")

    section("the route check, before a listing is asked for")
    # ?action=ping is the host's own account of which db_sync.php is running, so
    # the comparison is against this checkout's file, not a fixture.
    route_php = ROOT / "php" / "db_sync.php"
    ping = {
        "action": "ping",
        "route": "db_sync.php",
        "version": 3,
        "sha256": hashlib.sha256(route_php.read_bytes()).hexdigest(),
        "bytes": route_php.stat().st_size,
        # The route is four files, and the ping says which copies it is running.
        "files": {name: {
            "sha256": hashlib.sha256((route_php.parent / name).read_bytes()).hexdigest(),
            "bytes": (route_php.parent / name).stat().st_size,
            "present": True,
        } for name in client.ROUTE_FILES},
    }
    # A hand-bumped number is only worth checking if it is the same number on both
    # sides, so the client's floor is compared with the constant in the file.
    declared = re.search(r"const SYNC_ROUTE_VERSION = (\d+);",
                         route_php.read_text(encoding="utf-8"))
    check(declared is not None and int(declared.group(1)) == client.ROUTE_VERSION,
          f"the floor this client enforces is the route's own constant "
          f"({client.ROUTE_VERSION} vs {declared.group(1) if declared else 'missing'})")

    check(client.route_problems(ping, route_php) == [],
          "a ping that reports this file's sha256 and size passes")

    older = client.route_problems(dict(ping, version=client.ROUTE_VERSION - 1), route_php)
    check(len(older) == 1
          and "route version %d" % (client.ROUTE_VERSION - 1) in older[0]
          and "needs %d or later" % client.ROUTE_VERSION in older[0],
          f"a host below the floor is refused, naming both versions ({older[0][:46] if older else ''}...)")

    newer = client.route_problems(dict(ping, version=client.ROUTE_VERSION + 1), route_php)
    check(newer == [],
          "and a host ahead of the client is accepted, since the contract only grows")

    stale = client.route_problems({"action": "tables"}, route_php)
    check(len(stale) == 1 and "older than this client" in stale[0],
          f"a route with no ping is named as stale, not as a failure ({len(stale)} line)")

    other = client.route_problems(dict(ping, sha256="ab" * 32), route_php)
    check(len(other) == 1 and "different db_sync.php" in other[0]
          and "abababababab" in other[0],
          "a different file is named, with both hashes, so the upload is the suspect")

    short = client.route_problems(dict(ping, bytes=10), route_php)
    check(len(short) == 1 and "incomplete" in short[0] and "10 bytes" in short[0],
          f"a short copy is named as incomplete ({short[0].split(':')[0] if short else ''})")

    versionless = client.route_problems(dict(ping, version=None), route_php)
    check(len(versionless) == 1 and "no version" in versionless[0],
          "and a ping with no version cannot say what it is running, which is a problem")

    # One of the four files can be wrong while db_sync.php is perfect, which is
    # the case an entry-point hash cannot see: each file is named on its own.
    stale_merge = dict(ping, files=dict(ping["files"], **{
        "merge.php": {"sha256": "cd" * 32, "bytes": 12, "present": True}}))
    stale_lines = client.route_problems(stale_merge, route_php)
    check(len(stale_lines) == 1 and stale_lines[0].startswith("merge.php")
          and "cdcdcdcdcdcd" in stale_lines[0],
          f"a host running a different merge.php is named by that file ({stale_lines})")

    absent = dict(ping, files=dict(ping["files"], **{
        "keys.php": {"sha256": None, "bytes": None, "present": False}}))
    absent_lines = client.route_problems(absent, route_php)
    check(len(absent_lines) == 1 and "keys.php is not on the host" in absent_lines[0],
          "and one with no keys.php names it as missing, not as different")

    bare = {key: value for key, value in ping.items() if key != "files"}
    bare_lines = client.route_problems(bare, route_php)
    check(len(bare_lines) == 1 and "no file manifest" in bare_lines[0],
          "a ping with no manifest is refused: three of the four files would go uncompared")

    check(set(client.ROUTE_FILES) <= set(client.UPLOADS),
          f"and every file the route requires is one the sync uploads ({list(client.UPLOADS)})")

    # The whole point of asking first: nothing is read from a host that is not
    # running this file, so the check has to come before the listing request.
    # Asserted on the requests the run makes, not on the order of two strings in
    # the source: a reordering that still asks the listing first would pass a
    # grep and fail here.
    asked: list[str] = []
    run_main(["--no-upload", "--skip-preflight"], dict(ping), asked)
    check(asked == ["db_sync.php?action=ping", "db_sync.php?action=tables"],
          f"and main() asks for the ping before the listing ({asked})")

    section("a status from ?action=ping, told apart")
    # probe_route is the one place an HTTP failure becomes a sentence, so the four
    # answers are driven here rather than through main(). Each has a different
    # fix, and one generic line sent a reader to the wrong one -- a 404 read like
    # a malformed request when it means the toolkit was never deployed.
    saved_open = client.urllib.request.urlopen

    def probe_status(status, body=b"<html>no toolkit here</html>"):
        def fake_open(request, timeout=None):
            raise client.urllib.error.HTTPError(request.full_url, status, "x", {},
                                                io.BytesIO(body))
        client.urllib.request.urlopen = fake_open
        try:
            return client.probe_route(
                "https://host.invalid/toolkit/db_sync.php?action=ping", "a-test-key")
        finally:
            client.urllib.request.urlopen = saved_open

    payload, problem = probe_status(404)
    check(payload is None and problem.startswith("not deployed (HTTP 404)")
          and "no toolkit here" in problem,
          f"404 is a toolkit that was never deployed, and keeps the host's words "
          f"({problem[:44]}...)")

    payload, problem = probe_status(403)
    check(payload is None and problem.startswith("wrong key on the host (HTTP 403)"),
          "403 is db_sync.php refusing the key, not a route that is missing")

    payload, problem = probe_status(400)
    check(payload is None
          and problem.startswith("a route older than ?action=ping (HTTP 400)"),
          "400 is a db_sync.php that predates the ping")

    payload, problem = probe_status(
        500, b"<b>Fatal error</b>: require_once(): Failed opening required 'keys.php'")
    check(payload is None and problem.startswith("a file that landed broken (HTTP 500)")
          and "keys.php" in problem,
          "500 is a file that landed broken, with the host's fatal left on the line")

    # 502 is not 500, but both mean the same thing to a reader: the file is there
    # and running it failed. The status stays in the line because it says which
    # failure it was.
    payload, problem = probe_status(502)
    check(payload is None and problem.startswith("a file that landed broken (HTTP 502)"),
          "and the whole 5xx range is a file that landed broken, each with its own status")

    payload, problem = probe_status(429)
    check(payload is None and "has no name for" in problem,
          "while a status outside those families names its number instead of guessing")

    section("the one object main() asks for everything")
    # The transport exists so a new request cannot break three fakes at once. These
    # checks are about the object itself: what it defaults to, and the URL it builds
    # for a page -- which used to be assembled inline in main() and was untestable
    # without a whole fake host.
    plain = client.Host(key="a-key")
    # Constructed before any domain is named: if the base were resolved here,
    # importing the module would need SITE_DOMAIN and --help would need it too.
    # The domain below is this check's input, so this is the one check that names
    # a site -- nothing else in the suite needs one.
    unset = plain._base is None
    previous = os.environ.get("SITE_DOMAIN")
    os.environ["SITE_DOMAIN"] = "toolkit.example"
    try:
        default = plain.base
    finally:
        if previous is None:
            del os.environ["SITE_DOMAIN"]
        else:
            os.environ["SITE_DOMAIN"] = previous
    check(unset and default == "https://toolkit.example/toolkit/db_sync.php"
          and plain.key == "a-key" and plain.snapshot == client.SNAPSHOT,
          f"the default host builds its URL from SITE_DOMAIN on first use ({default.split('toolkit')[-1]})")

    # The refusal behind that lookup. Every other suite hands the client a valid
    # domain, so without these two nothing would notice if site_domain() started
    # guessing a host, or accepted a value it cannot build a URL from.
    def refusal(domain):
        previous = os.environ.get("SITE_DOMAIN")
        if domain is None:
            os.environ.pop("SITE_DOMAIN", None)
        else:
            os.environ["SITE_DOMAIN"] = domain
        try:
            client.site_domain()
        except SystemExit as exc:
            return str(exc.code)
        finally:
            if previous is None:
                os.environ.pop("SITE_DOMAIN", None)
            else:
                os.environ["SITE_DOMAIN"] = previous
        return None

    missing = refusal(None)
    check(missing is not None and missing.startswith("no SITE_DOMAIN")
          and "example.org" in missing,
          f"a run with no SITE_DOMAIN refuses, and names the bare form ({missing[:44]!r})")

    scheme, path = refusal("https://example.org"), refusal("example.org/toolkit")
    check(scheme is not None and path is not None
          and "bare domain" in scheme and "bare domain" in path
          and "'https://example.org'" in scheme and "'example.org/toolkit'" in path,
          f"and a scheme or a path is refused, quoting what it got ({scheme!r} / {path!r})")

    check(plain.page_url("db one", "a b", 41, 500).endswith(
              "?action=export&table=db%20one.a%20b&limit=500&after=41")
          and plain.page_url("db", "t", None, 20000).endswith(
              "?action=export&table=db.t&limit=20000"),
          "and a page URL quotes the table and asks for `after` only when there is one")

    # The failure this refactor exists to prevent: a request that bypasses the
    # transport and reads a module global. Every global it could reach is replaced
    # with one that raises, so a bypass fails here instead of in whichever suite
    # happened not to patch that name.
    def never(*args, **kwargs):
        raise AssertionError("main() used a module global instead of the host")

    saved = (client.call, client.probe_route, client.import_key, client.SNAPSHOT)
    client.call = client.probe_route = client.import_key = never
    client.SNAPSHOT = ROOT / "no-such-snapshot.json"
    try:
        asked: list[str] = []
        code, out = run_main(["--no-upload", "--skip-preflight"], dict(ping), asked)
    finally:
        client.call, client.probe_route, client.import_key, client.SNAPSHOT = saved
    check(asked == ["db_sync.php?action=ping", "db_sync.php?action=tables"],
          f"and with a host supplied no module-level request is reached ({asked})")

    section("--skip-route-check, for a host that predates the ping")
    # The oldest thing a pre-ping route can answer: it has no ping, so it would be
    # refused. The flag is the way past that, and it has to say it went past.
    stale = {"action": "tables"}

    asked: list[str] = []
    code, out = run_main(["--no-upload", "--skip-preflight", "--skip-route-check"], stale, asked)
    check("route: check skipped (--skip-route-check)" in out,
          "the skipped check says so, instead of passing for a check that ran")
    check(not any("action=ping" in url for url in asked),
          f"and no ping is asked for at all ({[url.rsplit('/', 1)[-1][:24] for url in asked]})")
    check(bool(asked) and "action=tables" in asked[0],
          "the run goes straight on to the listing")

    asked = []
    code, out = run_main(["--no-upload", "--skip-preflight"], stale, asked)
    check(code == 1 and "route: the host is not running this db_sync.php" in out,
          f"without the flag the same host is refused, as before ({code})")
    check(not any("action=tables" in url for url in asked),
          "and nothing is listed from it, which is what the flag overrides")

    section("the one length in a page")
    source = CLIENT.read_text(encoding="utf-8", errors="replace")
    check(re.search(r"""\[\s*["']bytes["']\s*\]""", source) is None,
          "sync_database.py never indexes the per-row `bytes` field")
    row = client.payload_rows(merged)[0]
    check("bytes" not in json.dumps(row),
          "and never sends it back: the client is not measuring this envelope")
    check(all(isinstance(row.get("bytes"), int) for table in document["tables"]
              for row in table.get("rows") or []),
          "each row still carries the length of its own compact encoding")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
