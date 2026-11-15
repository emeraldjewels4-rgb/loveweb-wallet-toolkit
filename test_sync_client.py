#!/usr/bin/env python3
"""The export reply as the route writes it, fed to the client that has to read it.

`?action=export` used to be compact -- JSON_UNESCAPED_SLASHES and nothing else --
and it became pretty-printed when the dispatch was factored into sync_route().
Pretty JSON is still JSON, so nothing was expected to break; nothing checked
either, and "expected" is how a sync stops working at three in the morning.

sync_respond() is that echo as a function now, so the real bytes are available
offline: tests/php/export_probe.php runs an export and puts the reply through it,
and this suite hands that string to sync_database.py itself -- the same call(),
collect(), digest() and payload_rows() the sync runs -- rather than to a
lookalike.

What is asserted is that the framing does not matter and the content does:
the client parses the pretty bytes and the compact ones to the same document
value, merges the same rows, computes the same digest over the same keys, and
builds the identical upload body. The per-row `bytes` count is the one length in
the reply, it is the length of that row's own compact encoding, and the client is
asserted not to read or send it -- so the envelope's size is nobody's input.

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

    envelope = report.get("envelope")
    check(isinstance(envelope, str) and envelope.strip() != "",
          "the reply the route would write is a string")

    section("what the route writes")
    check(envelope.startswith("{") and envelope.rstrip("\n").endswith("}"),
          "one JSON document, not a fragment")
    check(envelope.endswith("\n"),
          "it ends with a newline, so a terminal prompt does not land on the brace")
    try:
        pretty = json.loads(envelope)
    except ValueError as exc:
        check(False, f"the reply parses as JSON ({exc})")
        return finish()
    check(pretty.get("action") == "export" and len(pretty.get("tables") or []) == 5,
          f"it parses to an export of {len(pretty.get('tables') or [])} table(s), "
          "skipped ones included")
    lines = envelope.count("\n")
    check(lines > 20, f"the refactor's change is real: {lines} lines, pretty-printed")
    check("    " in envelope, "with the indentation JSON_PRETTY_PRINT emits")

    section("the client, on the real bytes")
    compact = json.dumps(pretty, separators=(",", ":"), ensure_ascii=False)
    check(json.loads(compact) == pretty,
          f"the same document, %d bytes compact against %d pretty"
          % (len(compact.encode("utf-8")), len(envelope.encode("utf-8"))))

    pretty_merged, tables, rows, skipped = client.collect(pretty)
    compact_merged, compact_tables, _r, _s = client.collect(json.loads(compact))
    check(bool(pretty_merged) and tables == 3 and rows == 4,
          f"collect() merges {rows} row(s) from {tables} readable table(s)")
    check([reason for _name, reason in skipped] == ["not reachable", "rows not readable"],
          f"and reports the two it could not read ({[r for _n, r in skipped]})")
    check(tables == compact_tables,
          "the same tables counted, whether the reply was pretty or compact")
    check(set(pretty_merged) == set(compact_merged),
          "the same keys either way")
    check(merged_text(pretty) == merged_text(json.loads(compact)),
          "and byte-identical rows, values included")
    check(client.digest(pretty_merged) == client.digest(compact_merged),
          "the digest the client compares with the host does not depend on framing")
    check(client.payload_rows(pretty_merged) == client.payload_rows(compact_merged),
          "nor does the body it posts back")

    section("what still has to survive the framing")
    # A row's values are keyed by column, so the wrapper is one level in.
    values = [value for table in pretty["tables"]
              for row in table.get("rows") or []
              for value in (row.get("values") or {}).values()]
    wrapped = [value for value in values
               if isinstance(value, dict) and isinstance(value.get("b64"), str)]
    check(len(wrapped) >= 1,
          "the invalid-UTF-8 row is still base64-wrapped in the pretty reply")
    check(any(base64.b64decode(value["b64"]) == b"latin1 \xb1 byte" for value in wrapped),
          "and decodes to the exact column bytes, not a replacement character")
    truncated = [table.get("truncated") for table in pretty["tables"] if table.get("on")]
    check(True in truncated and False in truncated,
          f"the cap's verdict travels through it ({truncated})")

    section("the one length in the reply")
    source = CLIENT.read_text(encoding="utf-8", errors="replace")
    check(re.search(r"""\[\s*["']bytes["']\s*\]""", source) is None,
          "sync_database.py never indexes the per-row `bytes` field")
    row = client.payload_rows(pretty_merged)[0]
    check("bytes" not in json.dumps(row),
          "and never sends it back: the client is not measuring this envelope")
    check(all(isinstance(row.get("bytes"), int) for table in pretty["tables"]
              for row in table.get("rows") or []),
          "each row still carries the length of its own compact encoding")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
