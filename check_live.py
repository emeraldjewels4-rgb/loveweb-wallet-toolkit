#!/usr/bin/env python3
"""Warn when the deployed toolkit stops answering, not only when it 404s.

    python check_live.py              # check the pages the site serves
    python check_live.py --self-test  # prove the checks catch what they claim

Why status codes are not enough
    The domain's parent .htaccess rewrites every unknown path to the FiatLord SPA.
    When the whole toolkit directory disappeared, https://fiatgod.x10.mx/toolkit/
    still answered 200 -- with 1,877 bytes of the parent site -- and so did
    /toolkit/zzz-not-there. A monitor that only reports "is it 200?" would have
    called that healthy for as long as nobody clicked. Every check here therefore
    reads the body: the expected content has to be there, and the parent site's
    page has to be absent.

What counts as a failure
    * the request fails, times out, or returns anything but 200
    * the body is the parent SPA (it contains "FiatLord") on a page that is not it
    * chain.php's JSON arrives without bound.connected true, or with no tables --
      that is the database binding broken, which reads as "up" by status code
    * preview.php's JSON arrives without a chain_id
    * a page is served with the wrong content type (PHP disabled often shows up as
      text/plain rather than a 404)

Exit status
    0 when every check passes, 1 otherwise, so cron, Task Scheduler or a GitHub
    Actions job cannot fail quietly.

Alerting
    Set SLACK_WEBHOOK_URL to post a one-line message on failure. With the bundled
    workflow you do not need it: a failed scheduled run emails the repository
    owner, and the webhook is used only when that secret exists.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://fiatgod.x10.mx/toolkit"

# Present in every page the parent site serves; never in this toolkit's own pages.
SPA_MARKERS = ("FiatLord",)

TIMEOUT = 45
ATTEMPTS = 2
RETRY_GAP = 8


def fetch(url):
    """Return (status, content_type, body); status is None when the request failed."""
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "loveweb-toolkit-watch/1.0", "Cache-Control": "no-cache"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read().decode("utf-8", "replace")
                return response.status, response.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return exc.code, exc.headers.get("Content-Type", ""), body
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:80])
            if attempt < ATTEMPTS:
                time.sleep(RETRY_GAP)

    return None, "", last or "request failed"


def looks_like_parent_site(body):
    return any(marker in body for marker in SPA_MARKERS)


def check_json_page(page, extra):
    """Shared shape for the two JSON routes: JSON content type, real JSON, no SPA."""
    def check(status, content_type, body):
        if status != 200:
            return False, "http %s" % status
        if looks_like_parent_site(body):
            return False, "parent site answered instead of %s" % page
        if "json" not in content_type.lower():
            return False, "content-type %r is not JSON (PHP may be off)" % content_type[:40]
        try:
            payload = json.loads(body)
        except ValueError:
            return False, "body is not JSON: %s" % " ".join(body.split())[:70]
        return extra(payload)

    return check


def chain_facts(payload):
    bound = payload.get("bound") if isinstance(payload, dict) else None
    if not isinstance(bound, dict):
        return False, "JSON has no bound section"
    if bound.get("connected") is not True:
        return False, "database binding not connected"
    tables = bound.get("tables")
    if not isinstance(tables, int) or tables < 1:
        return False, "binding reports %r tables" % (tables,)

    return True, "%s: connected, %d table(s), %d grant line(s)" % (
        bound.get("label") or "bound database", tables, len(bound.get("grants") or []))


def preview_facts(payload):
    if not isinstance(payload, dict):
        return False, "JSON is not an object"
    chain_id = payload.get("chain_id")
    if not isinstance(chain_id, int):
        return False, "JSON has no chain_id"

    return True, "chain_id %d, block %s" % (chain_id, payload.get("block"))


def status_page(status, content_type, body):
    if status != 200:
        return False, "http %s" % status
    if looks_like_parent_site(body):
        return False, "parent site answered instead of index.php"
    if "html" not in content_type.lower():
        return False, "content-type %r is not HTML" % content_type[:40]
    if "Polygon" not in body:
        return False, "page has no network section (%d bytes)" % len(body)

    return True, "%d bytes, network section present" % len(body)


CHECKS = (
    ("index.php", BASE + "/index.php", status_page),
    ("chain.php", BASE + "/chain.php?format=json", check_json_page("chain.php", chain_facts)),
    ("preview.php", BASE + "/preview.php?network=amoy", check_json_page("preview.php", preview_facts)),
)


def alert(failures, results):
    """Post one line to Slack when configured; never let alerting mask the result."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook or not failures:
        return
    text = ":rotating_light: toolkit unreachable: " + "; ".join(
        "%s (%s)" % (name, detail) for name, _, _, detail in results if _)
    body = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(webhook, data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            print("  alert: slack answered %s" % response.status)
    except Exception as exc:
        print("  alert: slack failed (%s)" % type(exc).__name__)


def self_test():
    """Run every check against samples, so both verdicts are proven."""
    spa = "<!DOCTYPE html><html><head><title>FiatLord - live crypto dashboard</title></head></html>"
    good_chain = json.dumps({"bound": {"label": "fiatgodx_toolkit", "connected": True,
                                       "tables": 4, "grants": ["SELECT", "INSERT"]}})
    bad_chain = json.dumps({"bound": {"label": "fiatgodx_toolkit", "connected": False,
                                      "tables": 0}})
    good_preview = json.dumps({"chain_id": 137, "block": 12345})
    good_page = "<!DOCTYPE html><html><body><h1>Polygon Amoy testnet</h1></body></html>"

    chain = CHECKS[1][2]
    preview = CHECKS[2][2]
    page = CHECKS[0][2]

    cases = (
        ("index.php serves the toolkit page", page(200, "text/html; charset=UTF-8", good_page), True),
        ("index.php replaced by the parent SPA", page(200, "text/html", spa), False),
        ("index.php missing", page(404, "text/html", "Not Found"), False),
        ("PHP served as plain text", page(200, "text/plain", good_page), False),
        ("chain.php with a connected binding", chain(200, "application/json", good_chain), True),
        ("chain.php with the binding down", chain(200, "application/json", bad_chain), False),
        ("chain.php returning the parent SPA", chain(200, "text/html", spa), False),
        ("preview.php with a chain id", preview(200, "application/json", good_preview), True),
        ("preview.php without JSON", preview(200, "text/html", spa), False),
    )

    failed = 0
    for label, (ok, detail), expected in cases:
        verdict = "pass" if ok == expected else "WRONG"
        if ok != expected:
            failed += 1
        print("  %-8s %-42s %s" % (verdict, label, detail))
    print("  self-test: %d case(s) wrong out of %d" % (failed, len(cases)))

    return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true",
                        help="prove the checks against samples instead of the site")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(self_test())

    results = []
    for name, url, check in CHECKS:
        status, content_type, body = fetch(url)
        ok, detail = check(status, content_type, body)
        results.append((name, url, ok, detail))
        print("  %-12s %-6s %s" % (name, "ok" if ok else "FAIL", detail))

    failures = [row for row in results if not row[2]]
    alert(failures, results)

    if failures:
        print("  RESULT: %d of %d check(s) failed -- the toolkit is not serving"
              % (len(failures), len(results)))
        sys.exit(1)

    print("  RESULT: all %d check(s) passed" % len(results))
    sys.exit(0)


if __name__ == "__main__":
    main()
