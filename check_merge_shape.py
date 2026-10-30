#!/usr/bin/env python3
"""Fail if a change landed on main as a merge commit instead of a squash.

WHY THIS EXISTS

PRs #3 and #4 each landed on main as a single commit. PR #5 was merged with
`merge_method: "squash"` and produced a two-parent merge commit anyway, with its
commit message showing that every supplied field had been ignored: the default
"Merge pull request #5 from ..." title and the pull request title as the body.
The repository's settings allow squash, so this was not configuration -- the
merge simply took a different shape.

Nothing notices that on its own. `git log --oneline` reads normally, CI is green
either way, and the only symptom is a history that no longer matches the
`<subject> (#N)` shape the rest of main uses. This script is the notice.

WHAT IT CHECKS

Parent count, and nothing else. A squash or a rebase lands one commit with one
parent; a merge-commit merge lands a commit with two or more. Content is not
compared, because a squash never shares the branch commit's SHA, so there is no
identity to compare.

    python check_merge_shape.py                       # this checkout's HEAD
    python check_merge_shape.py --ref origin/main     # main as the remote has it
    python check_merge_shape.py --base 907633c        # everything landed since
    python check_merge_shape.py --allow <sha>         # accept one more exception

Exit codes: 0 no unexpected merge commits, 1 at least one, 2 could not run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Merge commits that are already on main and cannot be undone without rewriting
# published history. Each one is recorded here WITH ITS REASON, so the list is a
# log of what went wrong rather than a place to silence the check. Nothing new
# belongs here: fix the merge instead, or rewrite the history deliberately.
KNOWN_MERGE_COMMITS: dict[str, str] = {
    "4e56fa3ddfec59db1c7b7c833d901dca09dec43c":
        "PR #5: requested squash, GitHub made a merge commit with default "
        "messages; content verified identical to the branch commit (b7d8a21)",
}

MIN_PREFIX = 7


class GitError(Exception):
    """Git could not answer the question being asked."""


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return its stdout, or raise GitError."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git ({exc})") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(detail[0] if detail else f"git {args[0]} failed")
    return proc.stdout.strip()


def resolve(repo: Path, rev: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    except GitError as exc:
        raise GitError(f"{rev!r} does not resolve to a commit ({exc})") from exc


def short(sha: str) -> str:
    return sha[:MIN_PREFIX]


def is_allowed(sha: str, extra: list[str], known: dict[str, str] | None = None) -> bool:
    """Whether this commit is a recorded exception, matched by SHA prefix.

    The comparison runs both ways on purpose: a 7-character `--allow` prefix has
    to match the full recorded SHA, and a full SHA has to match a shortened
    entry. Only one direction would silently accept nothing, which looks exactly
    like the check working.
    """
    known = KNOWN_MERGE_COMMITS if known is None else known
    lowered = sha.strip().lower()
    if len(lowered) < MIN_PREFIX:
        return False
    for candidate in [*known, *extra]:
        candidate = candidate.strip().lower()
        if len(candidate) < MIN_PREFIX:
            continue
        if lowered.startswith(candidate) or candidate.startswith(lowered):
            return True
    return False


def commits_in(repo: Path, ref: str, base: str | None) -> list[dict]:
    """The commits to inspect, oldest first.

    Without --base this is the one commit at `ref` and nothing else: the question
    a post-merge check asks is "did THIS merge land as a single commit", and
    walking the whole history to answer it would also make an old, already-
    accepted merge fail a run that has nothing to do with it.
    """
    tip = resolve(repo, ref)
    if base:
        bottom = resolve(repo, base)
        rev_range = f"{bottom}..{tip}"
        raw_args = ["rev-list", "--parents", "--reverse", rev_range]
    else:
        rev_range = tip
        raw_args = ["rev-list", "--parents", "-n", "1", rev_range]
    try:
        raw = git(repo, *raw_args)
    except GitError as exc:
        raise GitError(f"could not list commits for {rev_range} ({exc})") from exc

    commits = []
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        sha, parents = parts[0], parts[1:]
        subject = git(repo, "log", "-1", "--format=%s", sha)
        commits.append({"sha": sha, "parents": parents, "subject": subject})
    return commits


def describe(reason_sha: str) -> str:
    """The recorded reason for a known exception, or '' if there is none."""
    for known_sha, reason in KNOWN_MERGE_COMMITS.items():
        if known_sha.startswith(reason_sha) or reason_sha.startswith(known_sha):
            return reason
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a commit that should have landed as a squash is a merge.",
    )
    parser.add_argument("--ref", default="HEAD", help="commit or ref to inspect (default HEAD)")
    parser.add_argument("--base", help="only inspect commits after this ref; "
                                       "without it, only --ref itself is checked")
    parser.add_argument("--allow", action="append", default=[], metavar="SHA",
                        help="accept one more merge commit (repeatable)")
    parser.add_argument("--repo", default=str(ROOT), help="git repository to inspect")
    parser.add_argument("--quiet", action="store_true", help="only print problems")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    try:
        tip = resolve(repo, args.ref)
        commits = commits_in(repo, args.ref, args.base)
    except GitError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    offenders = [c for c in commits if len(c["parents"]) > 1
                 and not is_allowed(c["sha"], args.allow)]
    excused = [c for c in commits if len(c["parents"]) > 1
               and is_allowed(c["sha"], args.allow)]

    if not args.quiet:
        span = f"{short(resolve(repo, args.base))}..{short(tip)}" if args.base else short(tip)
        print(f"repository      : {repo}")
        print(f"checking        : {args.ref} ({span}), {len(commits)} commit(s)")
        print(f"merge commits   : {len(offenders)} unexpected, {len(excused)} recorded")

    for commit in excused:
        reason = describe(commit["sha"])
        if not args.quiet:
            print(f"  known   {short(commit['sha'])}  {commit['subject'][:70]}")
            if reason:
                print(f"          record: {reason}")

    for commit in offenders:
        print(f"  MERGE   {short(commit['sha'])}  {commit['subject'][:70]}")
        print(f"          parents: {' '.join(short(p) for p in commit['parents'])}")
        print("          this should have landed as one commit; the merge took a "
              "different shape than requested")

    if offenders:
        print(f"\nFAILED: {len(offenders)} commit(s) landed as a merge, not a squash.")
        print("Re-land it as one commit (squash locally and push, or merge with a "
              "method that cannot fall back), or record it in KNOWN_MERGE_COMMITS "
              "in this file with the reason it cannot be undone.")
        return 1

    if not args.quiet:
        print("\nOK: no unexpected merge commits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
