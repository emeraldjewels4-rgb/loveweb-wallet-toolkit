#!/usr/bin/env python3
"""Tests for install_gateway.py's host list and the cPanel cross-check.

The list used to be a literal in the script, which made "all hosts on" a statement
about the code rather than about the account. It is configuration now, and
configuration can be wrong in ways code cannot be: absent, empty, malformed, or
simply behind the account. Each of those is a case here.

The section before last drives check() itself with DNS, HTTP and SITE_DOMAIN stubbed
out, so the omission report under test is the one that runs, not a description of
it. The last two cover the credentials: the preflight in install_gateway, where a file
in the wrong folder or with a scheme in its host used to reach cPanel as a refused
call instead of a message naming the path, and the same reader reached through
sync_domains.names_from_cpanel(), where a refused call used to be a traceback.

The cross-check is scoped to the group the list names, which is what keeps the
account's ww9999.* siblings out of the report: they share the w*9999. shape with
the six web9999.metakakoon.* targets without being targets, and naming them made
the run fail on twenty-eight hosts it was never meant to install.

No network and no cPanel: gateway_hosts() and omissions() are pure, and check()
gets fake addresses and a fake account.

    python test_install_gateway.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

import install_gateway as g
import sync_domains as s

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def with_env(value, call):
    """Run call() with GATEWAY_HOSTS set to value, or removed when value is None."""
    saved = os.environ.get("GATEWAY_HOSTS")
    if value is None:
        os.environ.pop("GATEWAY_HOSTS", None)
    else:
        os.environ["GATEWAY_HOSTS"] = value
    try:
        return call()
    finally:
        if saved is None:
            os.environ.pop("GATEWAY_HOSTS", None)
        else:
            os.environ["GATEWAY_HOSTS"] = saved


def refusal(call):
    """The message call() refused with, or None when it returned instead."""
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    return None


def write_list(text, newline=""):
    """A throwaway file holding text exactly, so CRLF cases survive the write."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8", newline=newline)
    handle.write(text)
    handle.close()
    return Path(handle.name)


def run_check(targets, account):
    """check() with DNS, HTTP and SITE_DOMAIN stubbed; returns (ok, output)."""
    saved = (g.addresses, g.probe, g.site_domain, g.account_identity)
    g.addresses = lambda host: {"v4": ["192.0.2.10"], "v6": [], "error": None}
    g.probe = lambda host: {"scheme": "https", "status": 200, "on": 1, "detail": "toolkit"}
    g.site_domain = lambda: "example.org"
    g.account_identity = lambda: "acct on s22.infinitysrv.com"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            return g.check(targets, account), out.getvalue()
    finally:
        g.addresses, g.probe, g.site_domain, g.account_identity = saved


def serve_cpanel(payload, seen=None):
    """A urlopen stand-in answering with payload and recording each request."""
    class Reply:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, size=None):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(request, timeout=None):
        if seen is not None:
            seen.append(request)
        return Reply(json.dumps(payload).encode("utf-8"))

    return fake


def refuse_cpanel(status, body, seen=None):
    """A urlopen stand-in that raises the HTTPError a refused call produces."""
    def fake(request, timeout=None):
        if seen is not None:
            seen.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "refused", {},
                                     io.BytesIO(body.encode("utf-8")))

    return fake


def main() -> int:
    print("=== 1. the list comes from a file, one gateway hostname per line ===")
    listing = write_list("# the gateway group, and its ww9999 siblings\n\nweb9999.a.example\n"
                         "Web9999.B.example  # second\n"
                         "web9999.a.example\n")
    hosts = with_env(None, lambda: g.gateway_hosts(listing))
    check(hosts == ["web9999.a.example", "web9999.b.example"],
          "comments, blanks, case and duplicates reduce to the two names, in order")
    check(hosts[1] == "web9999.b.example", "an inline comment is not part of the name")

    crlf = write_list("web9999.a.example\r\nweb9999.b.example\r\n")
    hosts = with_env(None, lambda: g.gateway_hosts(crlf))
    check(hosts == ["web9999.a.example", "web9999.b.example"],
          "a file written with CRLF parses the same as one with LF")

    empty = write_list("# nothing but a comment\n\n")
    message = with_env(None, lambda: refusal(lambda: g.gateway_hosts(empty)))
    check(message is not None and "names no hosts" in message,
          "an empty list is a refusal, not a run over nothing")

    missing = Path(listing).with_suffix(".absent")
    message = with_env(None, lambda: refusal(lambda: g.gateway_hosts(missing)))
    check(message is not None and str(missing) in message and "GATEWAY_HOSTS" in message,
          "a missing list names the path it looked at and the environment override")
    check(g.HOSTS_TXT.parts[-2:] == (".sandbox", "gateway_hosts.txt"),
          "and the default path is the gitignored .sandbox one")

    print("\n=== 2. GATEWAY_HOSTS is the override, and takes commas or spaces ===")
    hosts = with_env("web9999.from-env.example", lambda: g.gateway_hosts(listing))
    check(hosts == ["web9999.from-env.example"], "the environment wins over the file")
    hosts = with_env("web9999.a.example,web9999.b.example", lambda: g.gateway_hosts(listing))
    check(hosts == ["web9999.a.example", "web9999.b.example"], "a comma-separated list parses")
    hosts = with_env("web9999.a.example\nweb9999.b.example", lambda: g.gateway_hosts(listing))
    check(hosts == ["web9999.a.example", "web9999.b.example"], "a newline-separated list parses")
    hosts = with_env("web9999.a.example, web9999.a.example", lambda: g.gateway_hosts(listing))
    check(hosts == ["web9999.a.example"], "a repeated name appears once")

    print("\n=== 3. a malformed entry is refused by name, not guessed at ===")
    message = with_env("https://web9999.a.example", lambda: refusal(g.gateway_hosts))
    check(message is not None and "not a bare hostname" in message and "https://" in message,
          "a scheme is refused and the offending line is quoted")
    message = with_env("web9999.a.example/toolkit", lambda: refusal(g.gateway_hosts))
    check(message is not None and "web9999.a.example/toolkit" in message, "a path is refused")
    message = with_env("web9999", lambda: refusal(g.gateway_hosts))
    check(message is not None and "'web9999'" in message, "a single label is refused")
    message = with_env("web8888.a.example", lambda: refusal(g.gateway_hosts))
    check(message is not None and "w*9999" in message,
          "a hostname outside the gateway group is refused, not checked anyway")

    print("\n=== 4. the cross-check names both directions, inside the list's group ===")
    targets = ["web9999.a.example", "web9999.b.example"]
    account = {"web9999.a.example", "web9999.new.example",
               "ww9999.metamask.x12.mx", "x12.mx"}
    extras, absent = g.omissions(targets, account)
    check(extras == ["web9999.new.example"],
          "a host in the list's group the account serves is reported as omitted")
    check("ww9999.metamask.x12.mx" not in extras,
          "a ww9999.* sibling the account serves is not reported as an omission")
    check("x12.mx" not in extras, "a name outside the group is not an omission either")
    extras, absent = g.omissions(targets, {"web9999.a.example"})
    check(absent == ["web9999.b.example"], "a target the account lacks comes back in the other list")
    agenda = {"web9999.a.example", "web9999.new.example", "ww9999.metamask.x12.mx"}
    check(g.omissions(sorted(agenda - {"ww9999.metamask.x12.mx"}), agenda) == ([], []),
          "an account that matches the list produces two empty lists")

    print("\n=== 5. the group comes from the list, not from a wider shape ===")
    check(g.gateway_group(["web9999.metakakoon.x0.network",
                           "web9999.metakakoon.x12.mx"]) == "web9999.metakakoon.",
          "names in one group reduce to that group")
    check(g.gateway_group(["web9999.a.example"]) == "web9999.a.",
          "a single name derives its group without taking its zone with it")
    check(g.GATEWAY.match("ww9999.metamask.mind.exchange"),
          "the shape still matches a ww9999 sibling -- it validates, it does not compare")
    message = refusal(lambda: g.gateway_group(["web9999.a.example", "other.example"]))
    check(message is not None and "more than one group" in message,
          "names sharing no group are refused, so the pattern cannot fall back to everything")
    message = with_env("web9999.a.example,w1.9999.b.example", lambda: refusal(g.gateway_hosts))
    check(message is not None and "more than one group" in message,
          "and a list spanning two groups is refused before any check runs")

    print("\n=== 6. check() fails on an omission and says the comparison did not run ===")
    targets = ["web9999.a.example", "web9999.b.example"]
    ok, output = run_check(targets, set(targets))
    check(ok, "a list the account agrees with, all serving, passes")
    check("agree on every gateway host" in output, "and the agreement is stated, not assumed")

    ok, output = run_check(targets, set(targets) | {"web9999.dark.example"})
    check(not ok, "a gateway host on the account but not in the list fails the run")
    check("web9999.dark.example" in output and "not in the list" in output,
          "and it is named with what to do about it")

    ok, output = run_check(targets, None)
    check(ok, "no credentials still allows the DNS and HTTP check")
    check("did not run" in output, "but the omission check is reported as not run, not as passed")

    ok, output = run_check(targets + ["web9999.unbound.example"], set(targets))
    check(not ok, "a listed host the account does not have fails too")
    check("not on the account" in output, "and points at --plan as the way to create it")

    print("\n=== 7. the credentials file is checked before a request is built ===")
    with tempfile.TemporaryDirectory() as folder:
        where = Path(folder) / "cpanel.json"
        message = g.missing_credentials(where)
        check(str(where) in message and "CPANEL_HOST" in message,
              "an absent file is reported with its absolute path, and with the variables")

        where.write_text("{host: s22.infinitysrv.com", encoding="utf-8")
        message = refusal(lambda: g.cpanel_settings(where))
        check(message is not None and "not valid JSON" in message and str(where) in message,
              "a file that is not JSON is refused, naming the file")

        where.write_text('["s22.infinitysrv.com"]', encoding="utf-8")
        message = refusal(lambda: g.cpanel_settings(where))
        check(message is not None and "one JSON object" in message,
              "an array is refused, because the three keys cannot come from one")

        where.write_text('{"host": "s22.infinitysrv.com", "user": "acct"}',
                         encoding="utf-8")
        message = refusal(lambda: g.cpanel_settings(where))
        check(message is not None and "no token" in message,
              "a missing token is named rather than sent as an empty credential")

        where.write_text('{"host": "https://s22.infinitysrv.com", "user": "acct",'
                         ' "token": "t"}', encoding="utf-8")
        message = refusal(lambda: g.cpanel_settings(where))
        check(message is not None and "bare hostname" in message
              and "'s22.infinitysrv.com'" in message,
              "a scheme is refused, and the message shows the bare host to write")

        where.write_text('{"host": "s22.infinitysrv.com:2083", "user": "acct",'
                         ' "token": "t"}', encoding="utf-8")
        message = refusal(lambda: g.cpanel_settings(where))
        check(message is not None and ":2083" in message and "bare hostname" in message,
              "a port is refused, because the script adds :2083 itself")

        where.write_text('{"host": " s22.infinitysrv.com ", "user": "acct",'
                         ' "token": "tok"}', encoding="utf-8")
        check(g.cpanel_settings(where) == ("s22.infinitysrv.com", "acct", "tok"),
              "a well-formed file gives the three values back, stripped")

        saved = {name: os.environ.pop(name, None) for name in g.CPANEL_ENV}
        os.environ.update({"CPANEL_HOST": "s22.infinitysrv.com", "CPANEL_USER": "acct",
                           "CPANEL_TOKEN": "tok"})
        try:
            check(g.cpanel_settings(Path(folder) / "absent.json")
                  == ("s22.infinitysrv.com", "acct", "tok"),
                  "with no file, the three CPANEL_* variables are used instead")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    print("\n=== 8. sync_domains reads the same file, and refuses the same way ===")
    rows = {"data": [
        {"domain": "a.example", "servername": "a.example", "rootdomain": "example"},
        {"domain": "b.example"},
        {"rootdomain": "example"},
        "junk",
    ]}
    seen = []
    with tempfile.TemporaryDirectory() as folder:
        where = Path(folder) / "cpanel.json"
        where.write_text('{"host": "s22.infinitysrv.com", "user": "acct", "token": "tok"}',
                         encoding="utf-8")
        missing = Path(folder) / "absent.json"
        bad = Path(folder) / "bad.json"
        bad.write_text("{host: s22", encoding="utf-8")
        scheme = Path(folder) / "scheme.json"
        scheme.write_text('{"host": "https://s22.infinitysrv.com", "user": "a",'
                          ' "token": "t"}', encoding="utf-8")

        saved_env = {name: os.environ.pop(name, None) for name in g.CPANEL_ENV}
        saved = (g.CPANEL_JSON, g.urllib.request.urlopen)
        try:
            g.CPANEL_JSON = where
            g.urllib.request.urlopen = serve_cpanel(rows, seen)
            names, source = s.names_from_cpanel()
            check(seen[0].full_url == "https://s22.infinitysrv.com:2083/execute/"
                  "DomainInfo/list_domains?api.version=1",
                  "the file is read, and the request is the shared client's")
            check(seen[0].headers.get("Authorization") == "cpanel acct:tok",
                  "with the token in the header and never in the URL")
            check("next_step" not in seen[0].full_url,
                  "and the refusal's next step is not sent as a query parameter")
            check(names == ["a.example", "b.example"],
                  "only each row's domain is taken, so a parent zone is not a binding")
            check(source == "cPanel UAPI",
                  "and the source label still says where the list came from")

            g.CPANEL_JSON = missing
            message = refusal(lambda: s.names_from_cpanel())
            check(message is not None and str(missing) in message,
                  "no credentials names the absolute file it looked for")
            check(message is not None and message.endswith(
                      "Run without --from-cpanel to use the saved domain list instead.")
                  and "--plan" not in message,
                  "and ends with this script's next step, not a flag it does not have")

            g.CPANEL_JSON = bad
            message = refusal(lambda: s.names_from_cpanel())
            check(message is not None and "not valid JSON" in message and str(bad) in message,
                  "a malformed file is the same refusal here as in install_gateway")

            g.CPANEL_JSON = scheme
            message = refusal(lambda: s.names_from_cpanel())
            check(message is not None and "bare hostname" in message
                  and "'s22.infinitysrv.com'" in message,
                  "and so is a host written with a scheme")

            g.CPANEL_JSON = where
            g.urllib.request.urlopen = refuse_cpanel(
                403, '{"errors":["The API token is invalid."]}')
            message = refusal(lambda: s.names_from_cpanel())
            check(message is not None and "refused with HTTP 403" in message
                  and "The API token is invalid." in message,
                  "a refused call exits with cPanel's status and text, not a traceback")
        finally:
            g.CPANEL_JSON, g.urllib.request.urlopen = saved
            for name, value in saved_env.items():
                if value is not None:
                    os.environ[name] = value

    print("\n=== 9. the account being asked is named, and the token is not ===")
    targets = ["web9999.a.example", "web9999.b.example"]
    ok, output = run_check(targets, set(targets) | {"web9999.dark.example"})
    check("account: asking cPanel as acct on s22.infinitysrv.com" in output,
          "check() names the account it asked before comparing itself with it")
    check(output.index("asking cPanel") < output.index("not in the list"),
          "and names it before the omission report, not after it")

    ok, output = run_check(targets, None)
    check("asking cPanel" not in output,
          "with no credentials it makes no claim about which account was asked")

    saved_json = g.CPANEL_JSON
    saved_env = {name: os.environ.pop(name, None) for name in g.CPANEL_ENV}
    with tempfile.TemporaryDirectory() as folder:
        where = Path(folder) / "cpanel.json"
        where.write_text('{"host": "s22.infinitysrv.com", "user": "acct",'
                         ' "token": "tok-secret"}', encoding="utf-8")
        g.CPANEL_JSON = where
        try:
            asking = g.account_identity()
        finally:
            g.CPANEL_JSON = saved_json
        check(asking == "acct on s22.infinitysrv.com",
              "the identity is the user and the bare host")
        check("tok-secret" not in (asking or ""), "and the token is not part of it")

        g.CPANEL_JSON = Path(folder) / "absent.json"
        try:
            check(g.account_identity() is None,
                  "no file and no variables means there is no account to name")
        finally:
            g.CPANEL_JSON = saved_json
    for name, value in saved_env.items():
        if value is not None:
            os.environ[name] = value

    calls = []
    saved_plan = (g.cpanel_call, g.listed_domains, g.account_identity)
    g.cpanel_call = lambda path, **params: calls.append(path) or {"status": 1}
    g.listed_domains = lambda: set(targets)
    g.account_identity = lambda: "acct on s22.infinitysrv.com"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            g.plan(targets, set(targets), apply_changes=False)
    finally:
        g.cpanel_call, g.listed_domains, g.account_identity = saved_plan
    output = out.getvalue()
    check("account: asking cPanel as acct on s22.infinitysrv.com" in output,
          "a --plan run names it too, so a wrong token is visible before anything is created")
    check(output.index("asking cPanel") < output.index("cPanel lists"),
          "and does so before the list it is about to compare")
    check(calls == [], "that plan run still sends no request")

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
