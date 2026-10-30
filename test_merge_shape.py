#!/usr/bin/env python3
"""Tests for check_merge_shape.py.

Runs against real throwaway git repositories, not mocks: the whole value of this
tool is that it asks git a question and reads the answer correctly, so the only
honest way to test it is to give git histories with a known shape.

    python test_merge_shape.py
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import check_merge_shape as c

FAILED: list[str] = []

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Shape Test",
    "GIT_AUTHOR_EMAIL": "shape@example.com",
    "GIT_COMMITTER_NAME": "Shape Test",
    "GIT_COMMITTER_EMAIL": "shape@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def check(cond: bool, label: str) -> None:
    # Convention here is (condition, label). test_token.py uses the OPPOSITE
    # order, so an idiom copied between files inverts the call silently: a
    # string condition is always truthy (a check that can never fail) and a
    # boolean label means the two arguments are swapped. Guard both slots so
    # that mistake crashes instead of reporting a false pass.
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Shape Test",
         "-c", "user.email=shape@example.com", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout.strip()


def make_repo(root: Path, name: str) -> Path:
    """A repository with one commit on `main`."""
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "first commit")
    return repo


def add_commit(repo: Path, text: str, message: str) -> str:
    (repo / "a.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def merge_branch(repo: Path, branch: str) -> str:
    """Land a branch with --no-ff, which is what a merge-commit merge looks like."""
    git(repo, "switch", "-c", branch)
    add_commit(repo, f"{branch}\n", f"work on {branch}")
    git(repo, "switch", "main")
    git(repo, "merge", "--no-ff", "-m", f"Merge branch {branch}", branch)
    return git(repo, "rev-parse", "HEAD")


def run(*args: str) -> tuple[int, str]:
    """Run the tool, returning (exit code, everything it printed)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = c.main(list(args))
    return rc, buf.getvalue()


def main() -> int:
    if shutil.which("git") is None:
        raise SystemExit("git is not installed; this suite needs it")

    root = Path(tempfile.mkdtemp(prefix="shape_test_"))
    try:
        print("=== 1. a squash-shaped history passes ===")
        linear = make_repo(root, "linear")
        add_commit(linear, "two\n", "second commit")
        rc, _ = run("--repo", str(linear), "--quiet")
        check(rc == 0, "a single-parent tip exits 0")
        # A merge commit far below the tip must not fail a tip-only check.
        merge_branch(linear, "already-landed")
        add_commit(linear, "four\n", "after the merge")
        rc, _ = run("--repo", str(linear), "--quiet")
        check(rc == 0, "an older merge below the tip is not what a tip check asks about")

        print("\n=== 2. a merge commit at the tip fails ===")
        merged = make_repo(root, "merged")
        merge_sha = merge_branch(merged, "feature")
        parents = git(merged, "rev-list", "--parents", "-n", "1", merge_sha).split()[1:]
        check(len(parents) == 2, "the fixture really is a two-parent commit")
        rc, _ = run("--repo", str(merged), "--quiet")
        check(rc == 1, "a two-parent tip exits 1")
        commits = c.commits_in(merged, "HEAD", None)
        check(len(commits) == 1, "tip mode inspects exactly one commit")
        check(commits[0]["sha"] == merge_sha, "and it is the tip")
        check(len(commits[0]["parents"]) == 2, "the parser reads both parents")

        print("\n=== 3. the failure names the commit and says what to do ===")
        rc, text = run("--repo", str(merged))
        check(rc == 1, "it still exits 1")
        check(c.short(merge_sha) in text, "the offending commit is named")
        check("parents:" in text, "its parents are shown")
        check("landed as a merge, not a squash" in text, "the verdict is stated")
        check("KNOWN_MERGE_COMMITS" in text, "and it points at the escape hatch")
        check("OK: no unexpected merge commits" not in text,
              "and it does not also claim success")

        print("\n=== 4. --allow accepts one recorded exception ===")
        rc, _ = run("--repo", str(merged), "--allow", merge_sha, "--quiet")
        check(rc == 0, "--allow with the full sha passes")
        rc, _ = run("--repo", str(merged), "--allow", merge_sha[:8], "--quiet")
        check(rc == 0, "a sha prefix is enough")
        rc, _ = run("--repo", str(merged), "--allow", "deadbeef", "--quiet")
        check(rc == 1, "an unrelated sha does not")
        rc, _ = run("--repo", str(merged), "--allow", "abc", "--quiet")
        check(rc == 1, "a 3-character prefix is too short to be an identity")

        print("\n=== 5. --base restricts the range ===")
        after = add_commit(merged, "after\n", "commit after the merge")
        rc, _ = run("--repo", str(merged), "--base", merge_sha, "--quiet")
        check(rc == 0, "a range that starts after the merge is clean")
        rc, _ = run("--repo", str(merged), "--base", f"{merge_sha}~1", "--quiet")
        check(rc == 1, "a range that includes it is not")
        commits = c.commits_in(merged, "HEAD", merge_sha)
        check([x["sha"] for x in commits] == [after], "the range lists only newer commits")
        check(c.commits_in(merged, "HEAD", f"{merge_sha}~1")[-1]["sha"] == after,
              "and is ordered oldest first")

        print("\n=== 6. the recorded exception list is a real record ===")
        check(len(c.KNOWN_MERGE_COMMITS) >= 1, "at least one exception is recorded")
        for known_sha, reason in c.KNOWN_MERGE_COMMITS.items():
            check(len(known_sha) == 40 and all(ch in "0123456789abcdef" for ch in known_sha),
                  f"{known_sha[:7]} is a full lowercase sha")
            check(len(reason) > 40, f"{known_sha[:7]} carries a reason, not just a placeholder")
        recorded = next(iter(c.KNOWN_MERGE_COMMITS))
        check(c.is_allowed(recorded[:7], []), "the recorded exception matches by prefix")
        check(c.is_allowed(recorded[:7], [], known={}) is False,
              "and only because it is in the list, not by accident")

        print("\n=== 7. bad input cannot be mistaken for a pass ===")
        rc, _ = run("--repo", str(merged), "--ref", "no-such-ref")
        check(rc == 2, "an unresolvable ref exits 2, not 0")
        empty = root / "empty"
        empty.mkdir()
        rc, _ = run("--repo", str(empty))
        check(rc == 2, "a directory that is not a repository exits 2")
        rc, _ = run("--repo", str(merged), "--base", "no-such-base")
        check(rc == 2, "an unresolvable --base exits 2")

        print("\n=== 8. this repository's own main is clean ===")
        repo_root = Path(__file__).resolve().parent
        rc, _ = run("--repo", str(repo_root), "--ref", "HEAD", "--quiet")
        check(rc == 0, "checking this checkout's tip finds no unexpected merge commit")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
