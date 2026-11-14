#!/usr/bin/env python3
"""The route's dispatch, executed: every refusal it can answer with.

merge.php's decisions and db_sync.php's merge half both run offline now. What did
not was the dispatch itself -- the guard and the argument checks that stand
between a request and any of it. Those are the refusals that matter to a stranger
with a URL: a GET where a merge would write, a wrong key, a body that is not what
it claims, more rows than one request may carry.

sync_route() takes the request as data, so tests/php/sync_route_probe.php runs
each of those in-process with its own stub connection and prints the status, the
message and what the connection ended up holding. Under SYNC_LIBRARY, refuse()
throws its status instead of exiting, so a refusal is a value.

"Nothing was written" is asserted per case rather than once: each case gets a
fresh connection, the successful merge is included so the refusals are refusals
and not a route that cannot write at all, and the messages are asserted so a case
refused for the wrong reason cannot pass.

    python test_sync_route.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROBE = ROOT / "tests" / "php" / "sync_route_probe.php"
FAILED: list[str] = []


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


def finish() -> int:
    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


def main() -> int:
    print("sync_route(): the guard, the body checks and what it answers with")

    section("a PHP interpreter and the probe")
    php = find_php()
    check(php is not None, "php_cli_is_available (PHP_BIN, PATH or .sandbox/php/php.exe)")
    if not php:
        return finish()
    print(f"  using {php}")

    proc = subprocess.run([php, "-n", str(PROBE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    check(proc.returncode == 0, f"the probe dispatches every case (exit {proc.returncode})")
    if proc.returncode != 0:
        print("  stderr:", (proc.stderr or proc.stdout).strip()[:500])
        return finish()
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        check(False, f"the probe answers with JSON (got {proc.stdout.strip()[:80]!r})")
        return finish()

    check("unavailable" not in report,
          "the stub stands in: this interpreter has no mysqli to shadow")
    cases = report.get("cases", {})

    section("the key")
    check(cases.get("no key installed", {}).get("status") == 503
          and cases["no key installed"].get("message")
          == "sync disabled: no import.key on the host",
          "no import.key on the host: 503, and it says so")
    check(cases.get("wrong key", {}).get("status") == 403
          and cases["wrong key"].get("message") == "forbidden",
          "a wrong key: 403, with no hint about the right one")
    header = cases.get("key in the header", {})
    check(header.get("status") == 200 and header.get("payload", {}).get("action") == "merge",
          "the key is accepted from the X-Import-Key header")
    query = cases.get("key in the query", {})
    check(query.get("status") == 200 and query.get("payload", {}).get("action") == "status",
          "and from the query string, for a caller that cannot set a header")

    section("a GET that would write")
    get_merge = cases.get("GET merge", {})
    check(get_merge.get("status") == 405,
          "a GET to ?action=merge is 405 (%s)" % get_merge.get("status"))
    check("a GET that could write is a link that could write"
          in str(get_merge.get("message", "")),
          "and the message says why, rather than only refusing")

    section("the body")
    check(cases.get("empty body", {}).get("status") == 400
          and cases["empty body"].get("message") == "an empty body has nothing to merge",
          "an empty body: 400")
    for name in ("malformed body", "rows not a list"):
        case = cases.get(name, {})
        check(case.get("status") == 400
              and case.get("message") == 'expected {"rows": [...]}',
              f"{name}: 400 naming the shape it wanted")
    check(cases.get("no rows", {}).get("status") == 400
          and cases["no rows"].get("message") == "no rows to merge",
          "an empty rows list is refused rather than run as a no-op")
    check(cases.get("too many rows", {}).get("status") == 413
          and cases["too many rows"].get("message")
          == "too many rows for one request: split the batch",
          "50,001 rows: 413, and it says to split the batch")

    section("the run token")
    bad = cases.get("bad run token", {})
    check(bad.get("status") == 400 and bad.get("message") == "run token is not a plain token",
          "a token with spaces in it is refused: 400")
    bad_status = cases.get("bad run token in status", {})
    check(bad_status.get("status") == 400
          and bad_status.get("message") == "run token is not a plain token",
          "and ?action=status refuses the same token rather than echoing it")

    section("anything else")
    unknown = cases.get("unknown action", {})
    check(unknown.get("status") == 400
          and unknown.get("message") == "unknown action; use export, merge, report or status",
          "an unknown action: 400 listing the four that exist")

    section("what each case left behind")
    refusals = {name: case for name, case in cases.items() if case.get("status") != 200}
    merged = [name for name, case in cases.items() if case.get("status") == 200]
    check(len(refusals) == 11, f"11 refusals were executed ({len(refusals)})")
    check(all(case.get("written") == 0 and case.get("committed") is False
              for case in refusals.values()),
          "no refusal wrote a row or committed a transaction")
    check(all(case.get("written", 0) > 0 and case.get("committed") is True
              for name in merged for case in [cases[name]] if case.get("payload", {})
              .get("action") == "merge"),
          f"the merges that were allowed did write ({len(merged)} accepted)")
    merge = cases.get("merge", {})
    token = merge.get("payload", {}).get("run_token")
    check(merge.get("payload", {}).get("accepted") == 1
          and token == "r" * 32,
          "the merge it accepts stamps the run token it was given")
    no_run = cases.get("merge without a run token", {})
    check(no_run.get("payload", {}).get("run_token") is None,
          "a merge with no run token is accepted, and reports the token as absent")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
