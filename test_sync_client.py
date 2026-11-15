#!/usr/bin/env python3
"""What the route writes: a compact row dump, and readable everything-else.

`?action=export` was compact, became pretty when the dispatch was factored into
sync_route(), and is compact again on the measurements: pretty is 58-74% more
bytes and peaks 38% higher, and at 80,000 rows it runs past PHP's 256 MB limit
where compact finishes at 208 MB. A refusal, a report or a status is read by a
person, so those stay indented -- the readability argument was always about them,
never about a dump that only sync_database.py reads.

sync_respond() makes the decision a function, so both halves are checkable
offline: tests/php/export_probe.php puts an export, a status and an error through
it, and this suite asserts the framing of each, then hands the export's real
bytes to sync_database.py itself -- the same collect(), digest() and
payload_rows() the sync runs -- to prove the content survives either way. A
framing change that alters the merged rows, the digest or the upload body would
be a break; a framing change that is only whitespace is a choice.

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


def merged_text(export: dict) -> str:
    """The client's merge of a reply, as text, so two framings can be compared."""
    merged, _tables, _rows, _skipped = client.collect(export)
    return json.dumps(merged, sort_keys=True)


def main() -> int:
    print("the export reply, and the client that parses it")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check(proc.returncode == 0, f"the probe runs an export (exit {proc.returncode})")
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
    check(isinstance(body, str) and body.strip() != "",
          "the export reply the route would write is a string")

    section("the export reply is compact")
    check(body.startswith("{") and body.rstrip("\n").endswith("}"),
          "one JSON document, not a fragment")
    check(body.endswith("\n") and body.count("\n") == 1,
          f"one line and one trailing newline ({body.count(chr(10))} line break(s))")
    check("\n    " not in body and not re.search(r'\{\s*\n', body),
          "no indentation: JSON_UNESCAPED_SLASHES without JSON_PRETTY_PRINT")
    try:
        doc = json.loads(body)
    except ValueError as exc:
        check(False, f"the reply parses as JSON ({exc})")
        return finish()
    check(doc.get("action") == "export" and len(doc.get("tables") or []) == 5,
          f"it parses to an export of {len(doc.get('tables') or [])} table(s), "
          "skipped ones included")
    pretty_same = json.dumps(doc, indent=4, ensure_ascii=False)
    check(len(pretty_same.encode("utf-8")) > len(body.encode("utf-8")),
          "the same document pretty-printed is %d bytes against %d compact (%0.2fx)"
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

    section("the client, on the real bytes")
    check(json.loads(pretty_same) == doc,
          "the same document, differently framed, is the same value")
    merged, tables, rows, skipped = client.collect(doc)
    pretty_merged, pretty_tables, _r, _s = client.collect(json.loads(pretty_same))
    check(bool(merged) and tables == 3 and rows == 4,
          f"collect() merges {rows} row(s) from {tables} readable table(s)")
    check([reason for _name, reason in skipped] == ["not reachable", "rows not readable"],
          f"and reports the two it could not read ({[r for _n, r in skipped]})")
    check(tables == pretty_tables,
          "the same tables counted, whether the reply was compact or pretty")
    check(set(merged) == set(pretty_merged),
          "the same keys either way")
    check(merged_text(doc) == merged_text(json.loads(pretty_same)),
          "and byte-identical rows, values included")
    check(client.digest(merged) == client.digest(pretty_merged),
          "the digest the client compares with the host does not depend on framing")
    check(client.payload_rows(merged) == client.payload_rows(pretty_merged),
          "nor does the body it posts back")

    section("what still has to survive the framing")
    # A row's values are keyed by column, so the wrapper is one level in.
    values = [value for table in doc["tables"]
              for row in table.get("rows") or []
              for value in (row.get("values") or {}).values()]
    wrapped = [value for value in values
               if isinstance(value, dict) and isinstance(value.get("b64"), str)]
    check(len(wrapped) >= 1,
          "the invalid-UTF-8 row is still base64-wrapped in the reply")
    check(any(base64.b64decode(value["b64"]) == b"latin1 \xb1 byte" for value in wrapped),
          "and decodes to the exact column bytes, not a replacement character")
    truncated = [table.get("truncated") for table in doc["tables"] if table.get("on")]
    check(True in truncated and False in truncated,
          f"the cap's verdict travels through it ({truncated})")

    section("the one length in the reply")
    source = CLIENT.read_text(encoding="utf-8", errors="replace")
    check(re.search(r"""\[\s*["']bytes["']\s*\]""", source) is None,
          "sync_database.py never indexes the per-row `bytes` field")
    row = client.payload_rows(merged)[0]
    check("bytes" not in json.dumps(row),
          "and never sends it back: the client is not measuring this envelope")
    check(all(isinstance(row.get("bytes"), int) for table in doc["tables"]
              for row in table.get("rows") or []),
          "each row still carries the length of its own compact encoding")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
