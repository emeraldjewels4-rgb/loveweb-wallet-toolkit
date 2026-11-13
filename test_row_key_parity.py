#!/usr/bin/env python3
"""Row keys must come out of PHP and out of Python byte for byte the same.

PHP computes every production key: db_sync.php requires keys.php, and a row filed
under a key the host would not have computed is refused. Python needs the same
formula for two reasons -- the local merge harness re-derives keys to stand in for
the host, and a second implementation is the only way to notice when the first one
changes. A PHP upgrade that reformatted numbers, or an edit that dropped an
encoding flag, would otherwise surface as rows that "already exist" under different
keys in a table that is already full, long after the run that wrote them.

The table below runs value types rather than one string row, because a key is
SHA-256 over encoded values and the encoding is where two languages drift: JSON
numbers, booleans, nulls, empty strings, non-ASCII and astral text, delimiters
inside a value, and a JSON document stored as a string. Two of them are the cases a
merge actually hinges on -- a value of `1` in one database against `"1"` in another,
and an integer `1` against a float `1.0`: each pair must key differently, or two
distinct rows collapse into one.

Two encodings genuinely differ and are declared at the bottom rather than tolerated:
PHP writes exponent-form floats as `1.0e+20` where Python writes `1e+20`. No
production key is affected -- mysqli hands back strings, so floats never reach the
PHP side of a real merge -- but the divergence is asserted so that a change in
either direction is a failing check rather than a surprise.

    python test_row_key_parity.py

PHP comes from PHP_BIN, then PATH, then the development copy at
.sandbox/php/php.exe. With no interpreter this suite fails instead of passing:
nothing about the format has been verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

FAILED: list[str] = []

# One PHP process, every case, so the suite costs one interpreter start (~50 ms)
# rather than one per row.
BRIDGE = """<?php
declare(strict_types=1);
require getenv('REPO') . '/php/keys.php';

$payload = json_decode((string) stream_get_contents(STDIN), true);
$keys = array();
foreach ($payload['cases'] as $case) {
    $keys[] = row_key((string) $case['table'], $case['values']);
}
echo json_encode($keys), "\\n";
"""

# (label, table, values) -- must agree byte for byte on both sides.
PARITY: list[tuple[str, str, dict]] = [
    ("an integer", "ledger", {"amount": 1}),
    ("zero", "ledger", {"amount": 0}),
    ("a negative integer", "ledger", {"amount": -7}),
    ("a float that is whole", "ledger", {"amount": 1.0}),
    ("a float with a fraction", "ledger", {"amount": 0.1}),
    ("negative zero", "ledger", {"amount": -0.0}),
    ("a boolean true", "ledger", {"flag": True}),
    ("a boolean false", "ledger", {"flag": False}),
    ("null", "ledger", {"note": None}),
    ("an empty string", "ledger", {"note": ""}),
    ("latin-1 range text", "ledger", {"note": "café"}),
    ("CJK text", "ledger", {"note": "日本語"}),
    ("an astral character", "ledger", {"note": "\U0001F600"}),
    ("an astral ZWJ sequence", "ledger", {"note": "\U0001F469\u200D\U0001F4BB"}),
    ("a slash", "ledger", {"url": "a/b"}),
    ("quotes and a backslash", "ledger", {"note": 'he said "hi" \\ ok'}),
    ("a tab and a newline", "ledger", {"note": "x\ty\nz"}),
    ("the text of a unicode escape", "ledger", {"note": "\\u007f"}),
    ("DEL, the one character the encoders spell differently", "ledger", {"note": "\x7f"}),
    ("a C0 control character", "ledger", {"note": "\x01"}),
    ("JSON stored inside a string", "ledger", {"payload": '{"b":1,"c":[2,3]}'}),
    ("an integer past 2**53", "ledger", {"id": 9007199254740993}),
    ("a base64-wrapped BLOB", "ledger", {"blob": {"b64": "AAEC/w=="}}),
    ("five columns of mixed types", "journal",
     {"id": 4, "amount": 0.5, "note": "é", "flag": True, "gone": None}),
]


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


def section(title: str) -> None:
    print(f"\n--- {title}")


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


def php_row_keys(php: str, cases: list[tuple[str, str, dict]]) -> dict:
    """Every key PHP computes for these cases, by label."""
    payload = json.dumps({"cases": [{"table": t, "values": v} for _, t, v in cases]})
    with tempfile.TemporaryDirectory() as tmp:
        bridge = Path(tmp) / "bridge.php"
        bridge.write_text(BRIDGE, encoding="utf-8")
        proc = subprocess.run(
            [php, str(bridge)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ, REPO=str(ROOT)),
        )
    if proc.returncode != 0:
        raise SystemExit("php failed (%d): %s" % (proc.returncode, proc.stderr[-600:]))
    keys = json.loads(proc.stdout)
    return {label: key for (label, _, _), key in zip(cases, keys)}


def python_row_key(table: str, values: dict) -> str:
    """PHP's formula in Python: sorted keys, compact ASCII JSON, sha256.

    U+007F is the only character the two encoders spell differently: json_encode
    leaves DEL as the raw byte, json.dumps escapes it as \\u007f. Both spellings are
    JSON-equivalent but the hash is over the spelling, so the escape is undone to
    match PHP. The lookbehind keeps text that literally contains a backslash-u007f
    sequence intact -- that arrives as \\\\u007f and must not be rewritten.
    """
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload = re.sub(r"(?<!\\)\\u007f", "\x7f", payload)
    return hashlib.sha256((table + "\n" + payload).encode("utf-8")).hexdigest()


def main() -> int:
    print("Row keys: PHP's row_key() against the same formula in Python")

    section("a PHP interpreter to compare against")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        print("\n" + "=" * 60)
        print("1 CHECK(S) FAILED:")
        print("  - php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
        return 1
    print(f"  using {php}")

    section("PHP computed a key for every case")
    from_php = php_row_keys(php, PARITY)
    check(len(from_php) == len(PARITY), f"{len(PARITY)} key(s) returned, one per case")

    section("both sides agree, byte for byte")
    for label, table, values in PARITY:
        ours = python_row_key(table, values)
        theirs = from_php.get(label, "")
        check(ours == theirs, f"{label}: {ours[:16]}")

    section("values that must NOT share a key")
    one = python_row_key("ledger", {"amount": 1})
    check(one != python_row_key("ledger", {"amount": "1"}),
          "the integer 1 and the string \"1\" key differently")
    check(one != python_row_key("ledger", {"amount": 1.0}),
          "the integer 1 and the float 1.0 key differently")
    check(python_row_key("journal", {"id": 1}) != python_row_key("ledger", {"id": 1}),
          "the same values in two tables key differently")
    check(python_row_key("ledger", {"a": 1, "b": 2}) == python_row_key("ledger", {"b": 2, "a": 1}),
          "column order does not change the key")

    section("declared divergences: exponent-form floats")
    # PHP writes 1.0e+20 / 1.0e-7, Python 1e+20 / 1e-07 -- the short form differs, so
    # the hashes do. Asserted as a difference, not as agreement, so that removing it
    # (or a PHP upgrade silently reconciling it) is visible here.
    for label, value in [("a large exponent", 1e20), ("a small exponent", 1e-7)]:
        ours = python_row_key("ledger", {"amount": value})
        theirs = php_row_keys(php, [(label, "ledger", {"amount": value})])[label]
        check(ours != theirs, f"{label} keys differently in the two encoders, as documented")

    section("the function the route actually calls is the one under test")
    sources = sorted(p.relative_to(ROOT).as_posix()
                     for p in (ROOT / "php").glob("*.php")
                     if "function row_key" in p.read_text(encoding="utf-8"))
    check(sources == ["php/keys.php"], f"row_key is defined once, in php/keys.php ({sources})")
    route = (ROOT / "php" / "db_sync.php").read_text(encoding="utf-8")
    check("require_once __DIR__ . '/keys.php';" in route,
          "db_sync.php requires keys.php, so the route keys with the tested function")

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
