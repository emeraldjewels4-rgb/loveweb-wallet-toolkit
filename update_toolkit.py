#!/usr/bin/env python3
"""Check for and apply toolkit updates from the git remote.

The safe direction to move a checkout is forwards, and only when nothing local
would be lost. This refuses rather than being helpful:

  * a dirty working tree is refused -- an update that silently stashes local
    edits is an update that can lose them;
  * a branch other than `main` is refused -- the release line is `main`;
  * a diverged branch is refused -- `--ff-only` means no merge commits and no
    conflicts arriving by surprise;
  * `--apply` without `--check` still prints the decision first, so the reason
    for a refusal is always on screen.

    python update_toolkit.py --check
    python update_toolkit.py --apply

The runner is injectable so the decisions above can be tested without a network,
a remote, or a real repository.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

VERSION = "2.0.0"
RELEASE_BRANCH = "main"
REMOTE = "origin"

Runner = Callable[[Sequence[str], Path], "tuple[int, str]"]


class UpdateRefused(RuntimeError):
    """The update was not attempted, and this says why."""


def git_runner(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run one git command. Returns (exit code, combined output)."""
    try:
        proc = subprocess.run(
            list(command), cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _git(runner: Runner | None, command: Sequence[str], cwd: Path) -> tuple[int, str]:
    return (runner or git_runner)(command, cwd)


def check(root: str | Path = ".", *, runner: Runner | None = None,
          remote: str = REMOTE) -> dict:
    """What an update would do right now. Reads only; changes nothing."""
    cwd = Path(root)
    code, branch = _git(runner, ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if code != 0:
        return {"status": "not-a-repo", "detail": branch[:200], "dirty": None,
                "branch": None, "behind": 0, "ahead": 0, "can_apply": False}

    code, porcelain = _git(runner, ["git", "status", "--porcelain"], cwd)
    dirty = code == 0 and bool(porcelain.strip())

    code, counts = _git(runner, ["git", "rev-list", "--left-right", "--count",
                                 f"HEAD...{remote}/{RELEASE_BRANCH}"], cwd)
    ahead = behind = 0
    if code == 0 and counts.strip():
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])

    if dirty:
        status, detail = "dirty", "the working tree has uncommitted changes"
    elif branch != RELEASE_BRANCH:
        status, detail = "wrong-branch", f"on {branch!r}, releases live on {RELEASE_BRANCH!r}"
    elif ahead and behind:
        status, detail = "diverged", f"{ahead} local and {behind} remote commit(s)"
    elif ahead:
        status, detail = "ahead", f"{ahead} local commit(s) not on {remote}"
    elif behind:
        status, detail = "behind", f"{behind} commit(s) available from {remote}"
    else:
        status, detail = "up-to-date", f"level with {remote}/{RELEASE_BRANCH}"

    return {
        "status": status,
        "detail": detail,
        "dirty": dirty,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "can_apply": status == "behind",
        "version": VERSION,
    }


def apply(root: str | Path = ".", *, runner: Runner | None = None,
          remote: str = REMOTE) -> dict:
    """Fast-forward to the remote, or raise UpdateRefused explaining why not."""
    cwd = Path(root)
    verdict = check(cwd, runner=runner, remote=remote)
    if not verdict["can_apply"]:
        raise UpdateRefused(f"{verdict['status']}: {verdict['detail']}")
    code, out = _git(runner, ["git", "pull", "--ff-only", remote, RELEASE_BRANCH], cwd)
    if code != 0:
        raise UpdateRefused(f"git pull --ff-only failed: {out[:300]}")
    return {"status": "updated", "detail": out[:300], "from": verdict["behind"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check for or apply toolkit updates.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="report whether an update is available")
    group.add_argument("--apply", action="store_true", help="fast-forward to the remote release")
    parser.add_argument("--root", default=".", help="checkout to inspect (default: .)")
    parser.add_argument("--remote", default=REMOTE)
    args = parser.parse_args(argv)

    if not args.apply:
        verdict = check(args.root, remote=args.remote)
        print(f"version {verdict['version']}  status {verdict['status']}")
        print(f"  {verdict['detail']}")
        print(f"  branch {verdict['branch']}  behind {verdict['behind']}  ahead {verdict['ahead']}")
        if verdict["can_apply"]:
            print("  run with --apply to fast-forward")
        return 0

    try:
        result = apply(args.root, remote=args.remote)
    except UpdateRefused as exc:
        print(f"refused: {exc}")
        print("nothing was changed.")
        return 1
    print(f"updated: {result['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
