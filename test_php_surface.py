#!/usr/bin/env python3
"""What in php/ has never been named by a test, and what just joined it.

Eleven PHP files, and until this existed the untested half of them was invisible:
merge.php, db_sync.php and keys.php had probes and the rest had only ever run on
a host. That came to light by reading the directory by hand, which is not a check.

So: every function, class, interface and trait declared at column 0 in a tracked
php/ file is a symbol. A symbol is covered when a probe or a suite names it, or
when something covered calls it -- `refuse()` is never named by a test, but every
route refusal goes through it, so counting only direct mentions would report it
as untested when it is exercised constantly.

What is left is the honest gap, recorded in php-untested.txt with a reason each.
The list is the check: a symbol that is neither covered nor recorded fails the
suite by name, and a recorded symbol that has since been covered fails too, so
the file can only shrink deliberately. That makes "a new function landed with no
test naming it" a red build instead of a discovery.

Run it after adding a probe: `python test_php_surface.py --write-allowlist`.

    python test_php_surface.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWLIST = ROOT / "php-untested.txt"
DECLARATION = re.compile(
    r"^(?:final\s+|abstract\s+)?(?:function|class|interface|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# Identical helpers declared in two entry scripts. Separate files, so nothing
# fatals today; pinned so a third one fails the run instead of accumulating.
KNOWN_DUPLICATES = [("h", ["php/chain.php", "php/index.php"]),
                    ("refuse", ["php/cjd_store.php", "php/db_sync.php"])]
FAILED: list[str] = []


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


def tracked(*patterns: str) -> list[Path]:
    """Files git tracks under these patterns.

    Tracked only, on purpose: php/config.local.php holds the host's credentials,
    is not in the repository, and must never be read by a test. It also means a
    probe nobody has added yet does not count -- which is the state CI sees, and
    the state that has to be true for the coverage to mean anything.
    """
    proc = subprocess.run(["git", "ls-files", *patterns], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        return []
    return [ROOT / line for line in proc.stdout.splitlines() if line.strip()]


def surface(files: list[Path]) -> dict[str, list[str]]:
    """Every column-0 declaration in these files, mapped to the files declaring it."""
    found: dict[str, list[str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in DECLARATION.finditer(text):
            name = match.group(1)
            try:
                relative = path.relative_to(ROOT).as_posix()
            except ValueError:  # a tree outside this one, which is how the scan is tested
                relative = path.name
            found.setdefault(name, [])
            if relative not in found[name]:
                found[name].append(relative)
    return found


def calls_of(files: list[Path], symbols: dict[str, list[str]]) -> dict[str, set[str]]:
    """What each symbol calls, from its declaration to the next one."""
    edges: dict[str, set[str]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        marks = [(match.start(), match.group(1)) for match in DECLARATION.finditer(text)]
        for index, (position, name) in enumerate(marks):
            # The body ends at the closing brace that starts a line, which is where a
            # top-level declaration ends in this codebase. Taking everything up to the
            # next declaration instead would attribute a file's trailing route code to
            # its last function, and quietly mark callees in that code as covered.
            closing = text.find("\n}\n", position)
            body = text[position:closing + 1 if closing != -1 else len(text)]
            edges.setdefault(name, set()).update(
                called for called in CALL.findall(body)
                if called != name and called in symbols
            )
    return edges


def named_in_tests(symbols: dict[str, list[str]], test_files: list[Path]) -> set[str]:
    """Symbols some probe or suite mentions, in code or in prose."""
    blob = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in test_files)
    return {name for name in symbols
            if re.search(r"\b%s\b" % re.escape(name), blob) is not None}


def covered(symbols: dict[str, list[str]], edges: dict[str, set[str]],
            roots: set[str]) -> set[str]:
    """The roots, plus everything they reach by calling it."""
    reached: set[str] = set()
    queue = [name for name in roots if name in symbols]
    while queue:
        name = queue.pop()
        if name in reached:
            continue
        reached.add(name)
        queue.extend(called for called in edges.get(name, ()) if called not in reached)
    return reached


def untested(files: list[Path], test_files: list[Path]) -> tuple[
        dict[str, list[str]], set[str], set[str], set[str]]:
    """(symbols, untested, named, reached) for a tree."""
    symbols = surface(files)
    edges = calls_of(files, symbols)
    named = named_in_tests(symbols, test_files)
    reached = covered(symbols, edges, named)
    return symbols, set(symbols) - reached, named, reached


def read_allowlist(path: Path) -> dict[str, str]:
    """symbol -> note, from a file of `symbol` or `symbol  # note` lines."""
    entries: dict[str, str] = {}
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        name, _, note = text.partition("#")
        entries[name.strip()] = note.strip()
    return entries


def write_allowlist(path: Path, names: set[str], notes: dict[str, str]) -> None:
    """Record the current gap, keeping any note a symbol already had."""
    header = [
        "# Functions, classes, interfaces and traits in php/ that no probe or suite",
        "# names, and that nothing a test names can reach either -- the surface where a",
        "# bug is found by a user instead of by CI.",
        "#",
        "# test_php_surface.py fails when a symbol is here and covered (the note is",
        "# stale) and when a symbol is covered by nothing and missing from here (it just",
        "# landed untested). Rewrite deliberately:",
        "#",
        "#     python test_php_surface.py --write-allowlist",
        "",
    ]
    body = []
    for name in sorted(names):
        note = notes.get(name, "").strip()
        body.append(f"{name}  # {note}" if note else name)
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print("php surface: what a test names, and what it has never reached")

    section("the inventory")
    php_files = tracked("php/*.php")
    test_files = tracked("tests/php/*.php", "test_*.py")
    check(len(php_files) >= 8,
          f"{len(php_files)} tracked php file(s) to scan")
    check(len(test_files) >= 4,
          f"{len(test_files)} probe(s) and suite(s) to look for names in")
    if not php_files or not test_files:
        return finish()

    symbols, missing, named, reached = untested(php_files, test_files)
    check(len(symbols) >= 40,
          f"{len(symbols)} function(s) and class(es) declared at column 0")
    print(f"  named by a test: {len(named)}, reachable with them: {len(reached)}, "
          f"never reached: {len(missing)}")

    section("the scan can see both kinds")
    # Two spot checks from opposite ends: a symbol a probe calls directly, and one
    # that only a caller's caller reaches.
    for name in ("merge_write", "export_all", "sync_route"):
        check(name in reached, f"{name} is reached (a probe calls it)")
    check("sync_key" in reached and "sync_key" not in named,
          "sync_key() is covered without being named: the dispatch refuses on it")

    section("the same helper in two entry scripts")
    duplicates = sorted(name for name, files in symbols.items() if len(files) > 1)
    expected = sorted(name for name, _ in KNOWN_DUPLICATES)
    check(duplicates == expected,
          f"the only duplicated declarations are the known ones ({expected}); "
          f"found {duplicates}")

    section("php-untested.txt")
    allowed = read_allowlist(ALLOWLIST)
    check(bool(allowed), f"the allowlist records {len(allowed)} symbol(s)")
    unknown = sorted(name for name in allowed if name not in symbols)
    check(not unknown,
          "every recorded symbol still exists in php/"
          + (f" (gone: {unknown})" if unknown else ""))

    landed = sorted(name for name in missing if name not in allowed)
    check(not landed,
          "no untested symbol is missing from the allowlist"
          + ("".join(f"\n          {name} ({symbols[name][0]}) has no probe or suite "
                     f"naming it" for name in landed) if landed else ""))

    stale = sorted(name for name in allowed if name not in missing)
    check(not stale,
          "no recorded symbol has since been covered"
          + (f" (covered now: {stale}; drop them, then --write-allowlist)"
             if stale else ""))

    lines = [line for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    order = [line.split("#")[0].strip() for line in lines]
    check(order == sorted(order) and len(order) == len(set(order)),
          "the file is sorted and names each symbol once")

    if "--write-allowlist" in argv:
        write_allowlist(ALLOWLIST, missing, allowed)
        print(f"\nallowlist written: {len(missing)} symbol(s) -> {ALLOWLIST.name}")

    section("the writer and the comparison")
    with tempfile.TemporaryDirectory() as folder:
        scratch = Path(folder) / "php-untested.txt"
        write_allowlist(scratch, {"beta", "alpha"}, {"alpha": "a reason"})
        reread = read_allowlist(scratch)
        check(reread == {"alpha": "a reason", "beta": ""},
              f"a written allowlist reads back with its notes ({reread})")
        new_php = Path(folder) / "new.php"
        new_php.write_text("<?php\nfunction freshly_added(): void {}\n", encoding="utf-8")
        more_symbols = surface([new_php])
        check(set(more_symbols) == {"freshly_added"},
              "a new declaration is seen by the same scan that reads php/")
        check("freshly_added" not in reread,
              "and it is not in the allowlist, which is what fails the run")

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
