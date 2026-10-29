#!/usr/bin/env python3
"""Audit the test suites' check() call sites for swapped condition/label arguments.

WHY THIS EXISTS

Each test file defines its own `check()`. Most use `check(condition, label)`;
test_token.py uses the inverted `check(label, condition)`. A call written for the
wrong convention does not crash -- it records a truthy string as the condition
and the boolean as the label, so a genuine failure is reported as `PASS False`
and the suite still exits 0. That is a swallowed failure, not a style problem.

This script infers each file's convention from the parameters the function body
actually uses in its comparison, then flags every call whose arguments do not
fit that convention:

    convention (cond, label):  arg0 must not be a string/f-string, arg1 must not
                               be a comparison or a boolean
    convention (label, cond):  the mirror image

A hard-coded True or False in the CONDITION slot is allowed -- `check(False,
"this should have raised")` is a normal idiom. A string there is always truthy
and therefore always a silent pass, which is the bug being hunted.

Run: python audit_test_checks.py
Exit 0 if every call site fits its file's convention, 1 otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CONDITION_LIKE = (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Call, ast.IfExp)


def literal_text(node: ast.AST) -> str:
    """A short description of the node if it looks like a hard-coded label."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return repr(node.value[:40])
    if isinstance(node, ast.JoinedStr):
        return "an f-string"
    return ""


def parse(path: Path, source: str):
    """A test file that does not parse is a finding, not a reason to crash."""
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        print(f"  PARSE ERROR line {exc.lineno}: {exc.msg}")
        raise SystemExit(1)


def find_check_functions(tree: ast.AST) -> dict:
    """{function_name: (param_names, index_of_condition_parameter)}"""
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "check":
            continue
        params = [a.arg for a in node.args.args]
        if not params:
            continue
        # Which parameter does the body branch on? That is the condition.
        condition_param = None
        for inner in ast.walk(node):
            if not isinstance(inner, ast.IfExp):
                continue
            test = inner.test
            if isinstance(test, ast.Name) and test.id in params and params.index(test.id) <= 1:
                condition_param = params.index(test.id)
                break
        if condition_param is None:
            # No ternary: fall back to the first parameter that the body guards on.
            for inner in ast.walk(node):
                if isinstance(inner, ast.If) and isinstance(inner.test, ast.Name) \
                        and inner.test.id in params:
                    condition_param = params.index(inner.test.id)
                    break
        if condition_param is None and len(params) >= 2:
            condition_param = 1  # test_token.py guards with `if detail and ok`
        found[node.name] = (params, condition_param)
    return found


def audit(path: Path) -> list:
    source = path.read_text(encoding="utf-8")
    tree = parse(path, source)
    info = find_check_functions(tree)
    if "check" not in info:
        return []
    params, cond_index = info["check"]
    label_index = 0 if cond_index == 1 else 1
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name != "check" or len(node.args) < 2:
            continue
        cond_arg = node.args[cond_index] if cond_index < len(node.args) else None
        label_arg = node.args[label_index] if label_index < len(node.args) else None
        if cond_arg is None or label_arg is None:
            continue

        # A hard-coded True/False in the CONDITION slot is legitimate: it is how a
        # test asserts "this must not have raised". A hard-coded string there is
        # not -- it is a label in the wrong slot, and every string is truthy, so
        # the check can never fail.
        text = literal_text(cond_arg)
        if text:
            problems.append((node.lineno, f"condition slot holds {text} (always truthy)"))
            continue

        # The mirror image: the label slot must hold text, never an expression or
        # a boolean.
        if isinstance(label_arg, CONDITION_LIKE):
            problems.append((node.lineno, "label slot holds an expression, probably the condition"))
            continue
        if isinstance(label_arg, ast.Constant) and isinstance(label_arg.value, bool):
            problems.append((node.lineno, f"label slot holds the literal {label_arg.value}"))
            continue
        if literal_text(label_arg):
            continue
        if isinstance(label_arg, ast.Name) and label_arg.id in ("True", "False"):
            problems.append((node.lineno, f"label slot holds {label_arg.id}"))
            continue
        if not isinstance(label_arg, (ast.Constant, ast.JoinedStr, ast.Name,
                                     ast.Attribute, ast.Subscript, ast.BinOp,
                                     ast.IfExp)):
            problems.append((node.lineno, "label slot holds something that is not a label"))
    return problems


def probe_runtime(modules: list) -> list:
    """Import each suite and confirm its guard rejects a swapped call.

    Static analysis says the call sites are fine; this says the guard that is
    supposed to catch a future one actually fires.
    """
    import contextlib
    import importlib
    import io

    problems = []
    for name in modules:
        slug = Path(name).stem
        try:
            module = importlib.import_module(slug)
        except Exception as exc:  # noqa: BLE001 - an unimportable suite is a finding
            problems.append((slug, f"could not import: {type(exc).__name__}: {exc}"))
            continue
        check = getattr(module, "check", None)
        if check is None:
            problems.append((slug, "no check() to probe"))
            continue
        params = list(check.__code__.co_varnames[:2])
        label_first = params == ["label", "ok"]
        convention = "(label, condition)" if label_first else "(condition, label)"
        correct = ("probe", True) if label_first else (True, "probe")
        swapped = (True, "probe") if label_first else ("probe", True)

        # The probe calls print through the suite's own check(), so their output
        # is captured -- this audit's report is the only thing worth showing.
        quiet = io.StringIO()
        try:
            with contextlib.redirect_stdout(quiet):
                check(*correct)
            if not label_first:
                # A passing check records nothing; make sure it did not fail.
                holder = getattr(module, "FAILED", [])
                if "probe" in holder:
                    holder.remove("probe")
                    problems.append((slug, "a passing probe was recorded as a failure"))
        except SystemExit as exc:
            problems.append((slug, f"a correctly-ordered call was rejected: {exc}"))

        # The call written for the OTHER convention must be refused, not counted
        # as a pass. This is the regression the guards exist for.
        verdict = "guard rejects a swapped call"
        try:
            with contextlib.redirect_stdout(quiet):
                check(*swapped)
        except SystemExit:
            pass
        else:
            problems.append((slug, "a swapped call was accepted silently"))
            verdict = "SWAPPED CALL ACCEPTED"
        print(f"  {slug:<26} {convention:<21} {verdict}")
    return problems


SENTINEL = "SENTINEL_seeded_failure"


def sabotage_suite(slug: str) -> int:
    """Seed a failure into one suite and require the suite to report it.

    Runs in a fresh interpreter (see --run-sabotage in main) so the seeded
    failure cannot leak into another suite's accumulator. Exits 0 only when the
    suite's own summary names the sentinel AND main() returns 1: "ALL CHECKS
    PASSED" in the presence of a seeded failure means the harness lies.
    """
    import contextlib
    import importlib
    import inspect
    import io
    import tempfile

    sys.path.insert(0, str(Path.cwd()))
    module = importlib.import_module(Path(slug).stem)
    check = module.check
    label_first = list(check.__code__.co_varnames[:2]) == ["label", "ok"]
    if label_first:
        check(SENTINEL, False)
    else:
        check(False, SENTINEL)

    if Path(slug).stem == "test_payment_request":
        # This one reads its options from sys.argv.
        scratch = Path(tempfile.mkdtemp(prefix="audit_sabotage_"))
        sys.argv = ["audit", "--wallets", "wallets-btc-1000-a.json",
                    "--amount", "0.001", "--out", str(scratch / "out")]

    takes_argv = len(inspect.signature(module.main).parameters) >= 1
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            code = module.main([]) if takes_argv else module.main()
    except SystemExit as exc:
        print(f"  {slug:<26} main() exited {exc.code} before reporting anything")
        return 1
    except BaseException as exc:  # noqa: BLE001 - a crash is not a reported failure
        print(f"  {slug:<26} main() CRASHED {type(exc).__name__}: {exc}")
        return 1

    text = captured.getvalue()
    named = SENTINEL in text
    ok = code == 1 and named and "FAIL" in text
    print(f"  {slug:<26} main()={code}  names the seeded failure={named}  "
          f"-> {'reports failures' if ok else 'DOES NOT REPORT FAILURES'}")
    return 0 if ok else 1


def run_sabotage_section(files: list) -> int:
    """One subprocess per suite, so accumulators stay isolated."""
    import subprocess

    me = str(Path(__file__).resolve())
    failures = 0
    for path in files:
        proc = subprocess.run([sys.executable, me, "--run-sabotage", path.name],
                              capture_output=True, text=True)
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        print(lines[-1] if lines else f"  {path.name:<26} (no output)")
        if proc.returncode != 0:
            failures += 1
            for line in proc.stderr.strip().splitlines()[-3:]:
                print(f"        {line}")
    return failures


def main() -> int:
    if "--run-sabotage" in sys.argv:
        return sabotage_suite(sys.argv[sys.argv.index("--run-sabotage") + 1])

    with_sabotage = "--sabotage" in sys.argv
    files = sorted(Path(".").glob("test_*.py"))
    total = 0
    print("=" * 70)
    print("CHECK() ARGUMENT-ORDER AUDIT")
    print("=" * 70)
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = parse(path, source)
        info = find_check_functions(tree)
        if "check" not in info:
            print(f"\n{path.name}: no check() harness")
            continue
        params, cond_index = info["check"]
        calls = sum(1 for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "check"
                    and len(n.args) >= 2)
        convention = "cond, label" if cond_index == 0 else "label, cond"
        print(f"\n{path.name}")
        print(f"  signature : check({', '.join(params)})")
        print(f"  convention: ({convention})   [{calls} two-arg-or-more calls]")
        problems = audit(path)
        if problems:
            total += len(problems)
            for lineno, why in problems:
                print(f"  SUSPECT line {lineno}: {why}")
        else:
            print("  all call sites fit")
    print("\n" + "=" * 70)
    print("RUNTIME GUARD PROBE")
    print("=" * 70)
    probes = probe_runtime([p.name for p in files])
    for slug, why in probes:
        print(f"  {slug}: {why}")
    total += len(probes)

    if with_sabotage:
        print("\n" + "=" * 70)
        print("SABOTAGE: does each suite actually report a failure?")
        print("=" * 70)
        total += run_sabotage_section(files)

    print("\n" + "=" * 70)
    if total:
        print(f"{total} PROBLEM(S) FOUND")
        return 1
    print("No swapped call sites found, and every guard rejects a swapped call.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
