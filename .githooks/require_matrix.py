#!/usr/bin/env python3
"""Refuse to push to main unless all three matrix jobs passed on that commit.

WHY A HOOK AND NOT BRANCH PROTECTION

The obvious answer is server-side: require the three status checks on `main`.
This repository cannot have that. It is private and the account is on the Free
plan, so both APIs answer the same way:

    POST /repos/{owner}/{repo}/branches/main/protection   403
    POST /repos/{owner}/{repo}/rulesets                   403
    "Upgrade to GitHub Pro or make this repository public to enable this
     feature."

So the rule is enforced here, at push time, instead of by GitHub. Be clear about
what that buys and what it does not: it stops THIS clone from putting a commit
on main that CI has not passed, and it does nothing at all about a push from
another machine, the web UI, or `git push --no-verify`.

ENABLE IT

    git config core.hooksPath .githooks

It reads the commit's check runs from the GitHub API, so it needs a token:
GITHUB_TOKEN or GH_TOKEN in the environment, or the credential git already
stores for github.com.

HOW IT BEHAVES

    all three suites succeeded              -> push allowed
    any still queued / running / failed     -> push refused, statuses printed
    no checks exist yet for that commit     -> push refused (open a PR: that is
                                               what gives the commit a run)
    API unreachable or unauthorized         -> push refused, with the reason
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

REQUIRED = ("suites (py 3.11)", "suites (py 3.12)", "suites (py 3.13)")
PROTECTED_REF = "refs/heads/main"


def fail(message: str) -> None:
    print(f"\npre-push: {message}\n", file=sys.stderr)
    print("pre-push: to push anyway (and accept that main is unverified):", file=sys.stderr)
    print("pre-push:     git push --no-verify", file=sys.stderr)
    raise SystemExit(1)


def owner_and_repo(url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return (match.group(1), match.group(2)) if match else None


def token_for(host: str) -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    # Fall back to whatever git already uses for github.com.
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


class NotOnRemote(Exception):
    """The commit exists locally but GitHub has never seen it, so it has no runs."""


def check_runs(owner: str, repo: str, sha: str, token: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "pre-push-matrix-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        # 422 "No commit found for SHA" means the commit has not been pushed
        # anywhere yet -- a normal state for a fresh local commit, and the exact
        # case this hook must refuse rather than report as a transport failure.
        if exc.code == 422 and "no commit found" in body.lower():
            raise NotOnRemote(sha) from exc
        fail(f"could not read check runs for {sha[:7]} (HTTP {exc.code}: {body})")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"could not reach the GitHub API for {sha[:7]} ({exc})")
    raise SystemExit(1)  # unreachable; keeps type checkers honest


def main() -> int:
    # git feeds: <local ref> <local sha> <remote ref> <remote sha>
    lines = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]
    targets = [(local_sha, remote_ref)
               for local_ref, local_sha, remote_ref, remote_sha in lines
               if remote_ref == PROTECTED_REF]
    if not targets:
        return 0

    url = sys.argv[2] if len(sys.argv) > 2 else ""
    parsed = owner_and_repo(url)
    if not parsed:
        fail(f"cannot tell which repository {url!r} is")
    owner, repo = parsed

    token = token_for("github.com")
    if not token:
        fail("no GitHub token: set GITHUB_TOKEN or let git store a credential for github.com")

    for sha, _ in targets:
        if set(sha) == {"0"}:
            continue  # deleting the branch is not this hook's business
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            fail(f"git handed over {sha!r} where a commit sha belongs")
        try:
            payload = check_runs(owner, repo, sha, token)
        except NotOnRemote:
            fail(f"{sha[:7]} has never been pushed, so CI has not run it.\n"
                 "         Push it to a branch and open a pull request -- that is what\n"
                 "         gives a commit a run; direct pushes to main have none yet.")
        seen = {run["name"]: run for run in payload.get("check_runs", [])}

        missing = [name for name in REQUIRED if name not in seen]
        bad = []
        for name in REQUIRED:
            run = seen.get(name)
            if run is None:
                continue
            state = run["conclusion"] or run["status"]
            if state != "success":
                bad.append(f"{name}: {state}")

        if bad:
            fail(f"{owner}/{repo}@{sha[:7]} has not passed the matrix:\n"
                 "         " + "\n         ".join(bad))
        if missing:
            fail(f"{owner}/{repo}@{sha[:7]} has no run for: {', '.join(missing)}\n"
                 "         Push this commit to a branch and open a pull request -- "
                 "that is what gives it a run.")

        print(f"pre-push: {sha[:7]} passed all three suites on "
              f"{owner}/{repo}; allowing push to main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
