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

    python test_sync_client.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
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

    return client.walk_export(fetch, listing, 2)


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
        client.walk_export(stuck, broken, 10)
        check(False, "a page claiming more with no cursor is refused")
    except ValueError as exc:
        check("says there is more but gave no cursor" in str(exc),
              f"a page claiming more with no cursor is named ({exc})")

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
