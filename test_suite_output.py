#!/usr/bin/env python3
"""Every suite's PASS/FAIL lines come from check(), and it ends with one summary.

The runner counts the lines a suite prints that begin with PASS or FAIL, and
test_docs_counts compares the totals it gets that way against the README. Both are
only meaningful while one shape holds: check() is the only thing that prints such
a line, and the run ends with exactly one "ALL CHECKS PASSED".

A suite that prints "PASS" in prose -- a progress note, a tutorial line, a
sentence starting "Fails when..." -- inflates its own count by one and nothing
notices. That exact bug (counting the closing summary as a check) was found by
hand once already.

This is static: it reads the source, not the output, so a suite that cannot even
run here is still held to the shape. Reads `run_tests.SUITES` rather than globbing
test_*.py, so a suite added to the list but shaped wrongly is caught, and a file
the runner does not run is not held to rules that do not apply to it.

    python test_suite_output.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import run_tests

ROOT = Path(__file__).resolve().parent
SUMMARY = "ALL CHECKS PASSED"
# The audit prints a report rather than checks; it is the runner's other half, not a
# suite, and its own output is not counted.
NOT_A_SUITE = "audit_test_checks.py"
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


def literal_prefix(node: ast.AST) -> str | None:
    """The text a print() call starts with, when it can be read off the source.

    f-strings count by their leading constant part, which is where check() puts
    the two spaces and where a stray "PASS ..." would sit.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant) \
            and isinstance(node.left.value, str):
        return node.left.value
    return None


def prints_of(source: str) -> list[tuple[str | None, str]]:
    """(literal prefix, enclosing function name) for every print call in a file."""
    tree = ast.parse(source)
    found: list[tuple[str | None, str]] = []

    def walk(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                    and child.func.id == "print" and child.args:
                found.append((literal_prefix(child.args[0]), function))
            walk(child, name)

    walk(tree, "")
    return found


def exits_through_main(source: str) -> bool:
    """Whether the module hands main()'s return value to the interpreter."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        body = node.body
        for inner in body:
            if isinstance(inner, ast.Raise) and isinstance(inner.exc, ast.Call) \
                    and isinstance(inner.exc.func, ast.Name) \
                    and inner.exc.func.id == "SystemExit" and inner.exc.args:
                argument = inner.exc.args[0]
                if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name) \
                        and argument.func.id == "main":
                    return True
            if isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Call):
                call = inner.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "exit" \
                        and call.args and isinstance(call.args[0], ast.Call) \
                        and isinstance(call.args[0].func, ast.Name) \
                        and call.args[0].func.id == "main":
                    return True
    return False


def main() -> int:
    print(f"suite output: one {SUMMARY}, and no PASS/FAIL line from outside check()")

    suites = [name for name, _ in run_tests.SUITES if name != NOT_A_SUITE]
    section("what is being held to the shape")
    check(len(suites) > 1, f"the runner names {len(suites)} suite(s)")

    stray: list[str] = []
    summaries: list[str] = []
    no_check: list[str] = []
    no_exit: list[str] = []
    paragraphs = 0

    for name in suites:
        source = (ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        defines_check = any(isinstance(node, ast.FunctionDef) and node.name == "check"
                            for node in ast.walk(tree))
        if defines_check:
            paragraphs += 1
        else:
            no_check.append(name)

        printed = prints_of(source)
        summary_count = 0
        for prefix, function in printed:
            if prefix is None:
                continue
            text = prefix.lstrip()
            if text.startswith(SUMMARY):
                summary_count += 1
            elif text.startswith(("PASS", "FAIL")) and function != "check":
                stray.append(f"{name}:{function or 'module'} prints {text[:30]!r}")
        if defines_check and summary_count != 1:
            summaries.append(f"{name} ({summary_count})")
        if defines_check and not exits_through_main(source):
            no_exit.append(name)

    section("the summary line")
    check(not summaries,
          f"each of the {paragraphs} suite(s) with a check() ends with one {SUMMARY}"
          + (f": {', '.join(summaries)}" if summaries else ""))

    section("the PASS/FAIL lines")
    check(not stray,
          "no print outside check() starts a line with PASS or FAIL"
          + (f": {'; '.join(stray[:4])}" if stray else ""))

    section("the exit code")
    check(not no_exit,
          "each suite hands main()'s return value to the interpreter, so a failure "
          "cannot exit 0"
          + (f": {', '.join(no_exit)}" if no_exit else ""))

    section("and the rule itself is not vacuous")
    # The parse above would pass if check() were not the only emitter. Prove the
    # detector separates the two shapes on source it is given.
    shaped = 'def check(c, label):\n    print(f"  {\'PASS\' if c else \'FAIL\'}  {label}")\n'
    check(prints_of(shaped)[0][1] == "check",
          "a PASS line printed inside check() is not counted as a stray")
    leaky = 'def report(c):\n    print("  PASS  looks fine")\n'
    prefix, function = prints_of(leaky)[0]
    check(prefix is not None and prefix.lstrip().startswith("PASS")
          and function != "check",
          "and the same line printed elsewhere is a violation")
    check(not exits_through_main("def main():\n    return 0\n\nmain()\n"),
          "a module that calls main() without exiting is caught")

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
