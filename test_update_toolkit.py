#!/usr/bin/env python3
"""Tests for update_toolkit.py, driven by a fake git runner.

No repository, no remote and no network: every decision is a function of what
`git` would have printed, so the interesting cases (dirty tree, wrong branch,
diverged history) are one dictionary away instead of a fixture to build.

    python test_update_toolkit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import update_toolkit as u

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    # Convention here is (condition, label), matching test_broadcast.py.
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def fake_git(*, branch: str = "main", status: str = "", counts: str = "0\t0",
             pull_code: int = 0, pull_out: str = "Already up to date.",
             rev_parse_code: int = 0, seen: list | None = None):
    """A runner that answers the four commands update_toolkit.py issues."""

    def runner(command, cwd):
        if seen is not None:
            seen.append(list(command))
        joined = " ".join(command)
        if "rev-parse --abbrev-ref HEAD" in joined:
            return rev_parse_code, branch
        if "status --porcelain" in joined:
            return 0, status
        if "rev-list --left-right --count" in joined:
            return 0, counts
        if "pull --ff-only" in joined:
            return pull_code, pull_out
        return 0, ""

    return runner


def main() -> int:
    root = Path(".")

    print("\n=== 1. the verdict is decided by git's own answer ===")
    level = u.check(root, runner=fake_git())
    check(level["status"] == "up-to-date", f"level history is up to date ({level['status']})")
    check(level["can_apply"] is False, "and there is nothing to apply")

    behind = u.check(root, runner=fake_git(counts="0\t3"))
    check(behind["status"] == "behind", f"remote commits ahead is 'behind' ({behind['status']})")
    check(behind["behind"] == 3, f"the count is carried through ({behind['behind']})")
    check(behind["can_apply"] is True, "and that is the one case worth applying")

    print("\n=== 2. anything that could lose local work is refused ===")
    dirty = u.check(root, runner=fake_git(status=" M broadcast.py", counts="0\t3"))
    check(dirty["status"] == "dirty", "a dirty tree is its own status")
    check(dirty["can_apply"] is False, "and never counts as applicable")

    branch = u.check(root, runner=fake_git(branch="feature/x", counts="0\t3"))
    check(branch["status"] == "wrong-branch", f"off-main is refused ({branch['status']})")

    diverged = u.check(root, runner=fake_git(counts="2\t3"))
    check(diverged["status"] == "diverged", f"diverged history is refused ({diverged['status']})")
    check(diverged["ahead"] == 2 and diverged["behind"] == 3,
          f"both counts are reported ({diverged['ahead']}/{diverged['behind']})")

    local = u.check(root, runner=fake_git(counts="4\t0"))
    check(local["status"] == "ahead", f"unpushed local commits are not overwritten ({local['status']})")

    missing = u.check(root, runner=fake_git(rev_parse_code=128))
    check(missing["status"] == "not-a-repo", "a directory that is not a repo says so")
    check(missing["can_apply"] is False, "and is not treated as updatable")

    print("\n=== 3. apply runs the command only when every guard passes ===")
    seen: list = []
    applied = u.apply(root, runner=fake_git(counts="0\t1", pull_out="Fast-forward", seen=seen))
    check(applied["status"] == "updated", f"a fast-forward reports updated ({applied['status']})")
    check(applied["from"] == 1, "and says how many commits it moved")
    check(any("pull --ff-only origin main" in " ".join(cmd) for cmd in seen),
          f"it pulled with --ff-only from origin main ({seen})")
    check(not any("push" in " ".join(cmd) for cmd in seen), "nothing is ever pushed")

    for kwargs, why in ((dict(status=" M x"), "a dirty tree"),
                        (dict(branch="dev", counts="0\t1"), "another branch"),
                        (dict(counts="1\t1"), "diverged history"),
                        (dict(counts="0\t0"), "nothing to pull")):
        calls: list = []
        try:
            u.apply(root, runner=fake_git(seen=calls, **kwargs))
        except u.UpdateRefused as exc:
            check(len(str(exc).strip()) > 0, f"apply refuses {why} and explains: {str(exc)[:40]}")
            check(not any("pull" in " ".join(cmd) for cmd in calls),
                  f"and runs no pull for {why}")
        else:
            check(False, f"apply refuses {why}")

    failed_pull: list = []
    try:
        u.apply(root, runner=fake_git(counts="0\t2", pull_code=1,
                                     pull_out="error: cannot fast-forward", seen=failed_pull))
    except u.UpdateRefused as exc:
        check("failed" in str(exc), f"a failing pull is surfaced, not swallowed ({str(exc)[:40]})")
    else:
        check(False, "a failing pull is surfaced, not swallowed")

    print("\n=== 4. the CLI reports and exits nonzero only on refusal ===")
    check(u.main(["--check", "--root", "."]) in (0, 1), "check mode exits cleanly either way")
    check(bool(u.VERSION), f"a version is declared ({u.VERSION})")
    check(u.RELEASE_BRANCH == "main", "releases are main")

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
