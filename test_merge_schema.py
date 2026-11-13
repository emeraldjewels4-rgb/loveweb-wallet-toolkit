#!/usr/bin/env python3
"""The SQL around merged_rows has to agree with itself, and no database is needed.

Four pieces of SQL describe one table, in two files: the CREATE and the ALTER that
back-fills a column in db.php, the INSERT in db_sync.php, and the queries the
`?action=status` and the preflight run against the same table. When they disagree
the failure is the worst shape available: the caller exports everything, builds
every row, uploads them, and only then gets a 503 -- after all the work, with
nothing to show which column was wrong. A column added to CREATE and forgotten in
the INSERT list does exactly that, and nothing in the tree looked at any of it,
because checking it "properly" would need MySQL.

So this parses the PHP string literals those statements are assembled from. It is
not a SQL parser and does not pretend to be: it understands the statements in this
repository and fails loudly when it cannot find one, so a rewrite cannot make a
check silently disappear.

    python test_merge_schema.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "php" / "db.php"
SYNC = ROOT / "php" / "db_sync.php"

TABLE = "merged_rows"

FAILED: list[str] = []

# Words that appear in these statements but are not columns: keywords, function
# names, and the table itself. A name outside this set that is also not a column is
# drift, which is the point of the scan.
SQL_WORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "BY", "AS", "AND", "OR", "ON",
    "NULL", "NOT", "DISTINCT", "ASC", "DESC", "LIMIT", "LIKE", "IN", "IS",
    "COUNT", "SUM", "MAX", "MIN", "COALESCE", "UTC_TIMESTAMP", "VALUES",
    "GREATEST", "INSERT", "INTO", "UPDATE", "DUPLICATE", "KEY", "CREATE",
    "TABLE", "IF", "EXISTS", "ALTER", "ADD", "COLUMN", TABLE.upper(),
    "MERGE_ROWS",
}

# Definitions that are not columns: indexes, keys, constraints.
NON_COLUMN = ("KEY", "UNIQUE", "PRIMARY", "INDEX", "CONSTRAINT", "FULLTEXT", "SPATIAL")


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


def php_literals(source: str) -> list[str]:
    """Every single- and double-quoted PHP literal, with its escapes resolved.

    A scanner rather than a regex over quote characters: db.php builds its status
    filter with " WHERE run_token = '" -- a double-quoted literal holding one
    apostrophe -- and a naive pattern would pair that apostrophe with the next one
    in the file and hand back a "literal" containing PHP code.
    """
    found: list[str] = []
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        if char in "\"'":
            quote, i, buf = char, i + 1, []
            while i < n:
                current = source[i]
                if current == "\\" and i + 1 < n:
                    buf.append(source[i:i + 2])
                    i += 2
                    continue
                if current == quote:
                    i += 1
                    break
                buf.append(current)
                i += 1
            text = "".join(buf)
            if quote == "'":
                # In single quotes PHP only knows \\ and \' -- everything else is
                # literal, so unescape in this order and leave the rest alone.
                text = text.replace("\\\\", "\x00").replace("\\'", "'").replace("\x00", "\\")
            else:
                text = text.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")
            found.append(text)
        elif source.startswith("//", i) or char == "#":
            while i < n and source[i] != "\n":
                i += 1
        elif source.startswith("/*", i):
            end = source.find("*/", i)
            i = n if end < 0 else end + 2
        else:
            i += 1
    return found


def function_body(source: str, name: str) -> str:
    """The text of one top-level function, up to its closing brace."""
    start = source.index(f"function {name}(")
    end = source.index("\n}", start)
    return source[start:end]


def statement(literals: list[str]) -> str:
    """The literals joined the way PHP's `.` joins them."""
    return "".join(literals)


def split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def columns_of(create_inner: str) -> dict[str, dict]:
    """Column name -> what the CREATE says about it."""
    columns: dict[str, dict] = {}
    for part in split_top_level(create_inner):
        words = part.split()
        if not words or words[0].upper().strip("`") in NON_COLUMN:
            continue
        upper = " " + part.upper() + " "
        columns[words[0].strip("`")] = {
            "type": words[1].upper() if len(words) > 1 else "",
            "not_null": "NOT NULL" in upper,
            "default": " DEFAULT " in upper,
            "auto": "AUTO_INCREMENT" in upper,
            "raw": part,
        }
    return columns


def keys_of(create_inner: str) -> dict[str, list[str]]:
    """Index name -> the columns it covers."""
    keys: dict[str, list[str]] = {}
    for part in split_top_level(create_inner):
        match = re.match(r"(?:UNIQUE\s+)?KEY\s+`?(\w+)`?\s*\(([^)]*)\)", part, re.I)
        if match:
            keys[match.group(1)] = [c.strip(" `") for c in match.group(2).split(",")]
    return keys


def identifiers(text: str) -> set[str]:
    """Names in a statement, with aliases and SQL words removed."""
    text = re.sub(r"\bAS\s+`?\w+`?", " ", text, flags=re.I)
    return {
        word for word in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)
        if word.upper() not in SQL_WORDS
    }


def main() -> int:
    print(f"The SQL around {TABLE}: CREATE, ALTER, INSERT, status and preflight")

    db_source = DB.read_text(encoding="utf-8")
    sync_source = SYNC.read_text(encoding="utf-8")

    ensure = statement(php_literals(function_body(db_source, "ensure_merged_table")))
    merge = statement(php_literals(function_body(sync_source, "merge_rows")))
    status_body = function_body(db_source, "merged_status")
    report_body = function_body(sync_source, "database_report")

    section("the CREATE")
    create = re.search(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\)\s*ENGINE\s*=\s*InnoDB",
        ensure, re.S | re.I,
    )
    check(create is not None, "the CREATE statement can be found in ensure_merged_table")
    if create is None:
        return finish()
    columns = columns_of(create.group(2))
    check(create.group(1) == TABLE, f"it creates {TABLE} (found {create.group(1)})")
    check(len(columns) >= 9, f"it declares the {len(columns)} merged_rows column(s)")

    section("the INSERT")
    insert = re.search(
        r"INSERT INTO (\w+)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*ON DUPLICATE KEY UPDATE\s*(.*)",
        merge, re.S | re.I,
    )
    check(insert is not None, "the INSERT statement can be found in merge_rows")
    if insert is None:
        return finish()
    written = [name.strip(" `") for name in insert.group(2).split(",")]
    values = [value.strip() for value in split_top_level(insert.group(3))]
    check(insert.group(1) == TABLE, f"it writes to {TABLE} (found {insert.group(1)})")

    missing = [name for name in written if name not in columns]
    check(not missing,
          "every column it names exists in the CREATE"
          + (f" (unknown: {', '.join(missing)})" if missing else ""))

    auto = [name for name, spec in columns.items() if spec["auto"]]
    unwritten = [name for name in columns if name not in written and name not in auto]
    check(not unwritten,
          f"it writes every column that is not the auto-increment {auto or 'id'}"
          + (f" (unwritten: {', '.join(unwritten)})" if unwritten else ""))

    required = [name for name, spec in columns.items()
                if spec["not_null"] and not spec["default"] and not spec["auto"]]
    starved = [name for name in required if name not in written]
    check(not starved,
          f"the {len(required)} NOT NULL column(s) without a default are all supplied"
          + (f" (missing: {', '.join(starved)})" if starved else ""))

    check(len(values) == len(written),
          f"one value per column: {len(values)} value(s) for {len(written)} column(s)")

    bound = [name for name, value in zip(written, values) if value == "?"]
    check(len(bound) == values.count("?"),
          f"{values.count('?')} placeholder(s) line up with the columns they fill")
    check([name for name, value in zip(written, values) if value != "?"]
          == ["first_seen", "last_seen"],
          "first_seen and last_seen come from UTC_TIMESTAMP(), not from the caller")

    bind = re.search(r"bind_param\(\s*\$statement\s*,\s*'([sdib]+)'",
                     function_body(sync_source, "merge_rows"))
    check(bind is not None, "the bind_param type string can be found")
    if bind:
        check(len(bind.group(1)) == len(bound),
              f"one bind type per placeholder: {bind.group(1)!r} covers {len(bound)}")

    updated = [name.strip(" `") for name in
               re.findall(r"(\w+)\s*=\s*(?:VALUES\(|GREATEST\(|UTC_TIMESTAMP\()",
                          insert.group(4))]
    stray = [name for name in updated if name not in columns]
    check(bool(updated) and not stray,
          "the ON DUPLICATE KEY UPDATE targets are real columns"
          + (f" (unknown: {', '.join(stray)})" if stray else ""))

    section("the ALTER")
    alter = re.search(r"ALTER TABLE (\w+)\s+ADD COLUMN (\w+)\s+(\w+\(\d+\)[^,]*)",
                      ensure, re.S | re.I)
    check(alter is not None, "the ALTER statement can be found in ensure_merged_table")
    if alter:
        added, added_type = alter.group(2), " ".join(alter.group(3).split()).upper()
        check(added in columns, f"it adds {added}, which the CREATE declares too")
        check(added_type.startswith(columns.get(added, {}).get("type", "\x00")),
              f"both agree on its type ({added_type} vs "
              f"{columns.get(added, {}).get('type', 'absent')})")
        created_keys = keys_of(create.group(2))
        altered_key = re.search(r"ADD KEY (\w+)\s*\(([^)]*)\)", ensure, re.I)
        check(altered_key is not None and created_keys.get(altered_key.group(1))
              == [c.strip(" `") for c in altered_key.group(2).split(",")],
              "the ALTER's index is the one the CREATE declares ("
              + (f"{altered_key.group(1)} on {altered_key.group(2)})" if altered_key else "absent)"))

    section("the ?action=status queries")
    def is_statement(text: str) -> bool:
        return bool(re.search(r"\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY)\b", text, re.I))

    status_sql = [text for text in php_literals(status_body) if is_statement(text)]
    check(bool(status_sql), f"the {len(status_sql)} status statement fragment(s) can be found")
    named = set()
    for text in status_sql:
        named |= identifiers(text)
    strays = sorted(name for name in named if name not in columns)
    check(not strays,
          f"every column they name exists in the CREATE ({len(named)} name(s))"
          + (f" (unknown: {', '.join(strays)})" if strays else ""))
    check(any(re.search(r"\bWHERE\s+run_token\s*=", text, re.I) for text in status_sql),
          "the run filter is on the run_token column")
    check(all(TABLE in text for text in status_sql if "FROM" in text.upper()),
          f"they read from {TABLE}")

    section("the preflight")
    report_sql = statement(php_literals(report_body))
    check(f"SHOW TABLES LIKE '{TABLE}'" in report_sql,
          f"it asks whether {TABLE} exists")
    check(f"SHOW COLUMNS FROM {TABLE} LIKE 'run_token'" in report_sql,
          "it asks whether run_token is there, which the CREATE says it is")

    return finish()


def finish() -> int:
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
