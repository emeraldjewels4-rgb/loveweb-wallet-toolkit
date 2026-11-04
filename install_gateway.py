#!/usr/bin/env python3
"""Install the web9999 gateway hosts: cPanel documents roots, then prove they answer.

    python install_gateway.py            # check only: DNS and HTTP, needs no credentials
    python install_gateway.py --plan     # ask cPanel what exists, change nothing
    python install_gateway.py --apply    # create the missing domains, then check

Why the document root matters: a new domain gets an empty document root, and an
empty document root answers 404 while the interface sits one directory away. Every
host here is pointed at `public_html/toolkit` -- the directory that already serves
`https://fiatgod.x10.mx/toolkit/` -- so the interface answers at
`https://<host>/chain.php` with no second deploy.

Credentials: `.sandbox/cpanel.json` ({"host", "user", "token"}) or CPANEL_HOST /
CPANEL_USER / CPANEL_TOKEN in the environment. Create the token in cPanel ->
Security -> Manage API Tokens. The file is gitignored like `.sandbox/ftp.json`,
and the token is never printed, logged or echoed, even when a call fails.

What this deliberately does not do: create DNS records. The zones these hosts
belong to are not served by this cPanel server -- `x12.mx`, `x13.mx`, `x0.network`
and `x13.network` answer from dyna-ns.net, `mind.exchange` from helloserver.win --
so the A/AAAA record belongs at that provider, not in a zone edit here. The check
reports the missing record per host instead of pretending cPanel owns the zone.

Exit status is 0 only when every host resolves and answers `chain.php` with
`on = 01`; anything else exits non-zero, so a deploy cannot report success on a
host that is still dark.
"""

import argparse
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CPANEL_JSON = ROOT / ".sandbox" / "cpanel.json"

# The five hosts, and the document root that serves the toolkit. The directory is
# relative to the account's home, which is how cPanel's API wants it.
HOSTS = (
    "web9999.metakakoon.x0.network",
    "web9999.metakakoon.x12.mx",
    "web9999.metakakoon.x13.mx",
    "web9999.metakakoon.x13.network",
    "web9999.metakakoon.mind.exchange",
    "web9999.metakakoon.x12.network",
)
DOCUMENT_ROOT = "public_html/toolkit"
PROBE = "/chain.php?format=json"

REFERENCE = "fiatgod.x10.mx"


def cpanel_settings():
    if CPANEL_JSON.is_file():
        data = json.loads(CPANEL_JSON.read_text())

        return data.get("host"), data.get("user"), data.get("token")

    import os

    return (os.environ.get("CPANEL_HOST"), os.environ.get("CPANEL_USER"),
            os.environ.get("CPANEL_TOKEN"))


def cpanel_call(path, **params):
    """One UAPI call. Errors carry cPanel's own message and never the token."""
    host, user, token = cpanel_settings()
    if not (host and user and token):
        sys.exit("no cPanel credentials: write .sandbox/cpanel.json with host, user and"
                 " token, or set CPANEL_HOST/CPANEL_USER/CPANEL_TOKEN."
                 " Run without --plan/--apply for the DNS and HTTP check only.")

    query = urllib.parse.urlencode({"api.version": "1", **params})
    url = "https://%s:2083/execute/%s?%s" % (host, path, query)
    request = urllib.request.Request(url, headers={
        "Authorization": "cpanel %s:%s" % (user, token),
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        sys.exit("%s refused with HTTP %s: %s" % (path, exc.code, detail))
    except urllib.error.URLError as exc:
        sys.exit("could not reach cPanel at %s: %s" % (host, exc.reason))


def listed_domains():
    payload = cpanel_call("DomainInfo/list_domains")
    rows = payload.get("data")
    if not isinstance(rows, list):
        errors = payload.get("errors") or ["no data in the reply"]
        sys.exit("cPanel refused the domain list: %s" % errors[0])

    names = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("domain", "servername", "rootdomain"):
            value = row.get(key)
            if isinstance(value, str) and value:
                names.add(value.lower())

    return names


def addresses(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return {"v4": [], "v6": [], "error": str(exc)}

    v4 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET})
    v6 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET6})

    return {"v4": v4, "v6": v6, "error": None}


def probe(host):
    """What the host answers for the interface, over https then http."""
    for scheme in ("https", "http"):
        url = "%s://%s%s" % (scheme, host, PROBE)
        try:
            with urllib.request.urlopen(url, timeout=12) as response:
                body = response.read(20000).decode("utf-8", "replace")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    return {"scheme": scheme, "status": response.status, "on": None,
                            "detail": "answered, but not the interface"}
                return {"scheme": scheme, "status": response.status,
                        "on": payload.get("on"), "detail": payload.get("label")}
        except urllib.error.HTTPError as exc:
            last = {"scheme": scheme, "status": exc.code, "on": None, "detail": "HTTP %s" % exc.code}
        except Exception as exc:  # TLS, DNS, timeout
            last = {"scheme": scheme, "status": None, "on": None,
                    "detail": type(exc).__name__}

    return last


def check():
    reference = set(addresses(REFERENCE)["v4"])
    if reference:
        print("  reference: %s resolves to %s" % (REFERENCE, ", ".join(sorted(reference))))
    else:
        print("  reference: %s did not resolve from here" % REFERENCE)

    problems = 0
    for host in HOSTS:
        found = addresses(host)
        answer = probe(host)
        same = bool(set(found["v4"]) & reference)

        print("\n  %s" % host)
        print("    dns    : A %s | AAAA %d record(s)%s" % (
            ", ".join(found["v4"]) or "none (%s)" % found["error"],
            len(found["v6"]), " (same server as %s)" % REFERENCE if same else ""))
        print("    chain  : %s" % (
            "%s:// %s-> on=%02d" % (answer["scheme"], answer["status"], answer["on"])
            if answer["on"] is not None
            else "%s: %s" % (answer["scheme"], answer["detail"])))

        if not (found["v4"] or found["v6"]):
            print("    needs  : an A/AAAA record at the zone's provider, then the domain in cPanel")
            problems += 1
        elif not same:
            print("    needs  : the record points somewhere else; it must reach %s" % REFERENCE)
            problems += 1
        elif answer["on"] != 1:
            print("    needs  : the domain added in cPanel with document root %s" % DOCUMENT_ROOT)
            problems += 1
        else:
            print("    state  : serving the interface")

    print("\n  %d of %d hosts still need work" % (problems, len(HOSTS)))

    return problems == 0


def plan(apply_changes):
    existing = listed_domains()
    missing = [host for host in HOSTS if host not in existing]
    print("  cPanel lists %d name(s); %d of the five present, %d missing"
          % (len(existing), len(HOSTS) - len(missing), len(missing)))

    for host in missing:
        labels = host.split(".")
        subdomain, root = ".".join(labels[:-2]), ".".join(labels[-2:])
        print("    + %-38s as %s.%s -> %s" % (host, subdomain, root, DOCUMENT_ROOT))

    if not missing:
        print("  nothing to create")
        return

    if not apply_changes:
        print("  plan only: re-run with --apply to create them")
        return

    for host in missing:
        labels = host.split(".")
        reply = cpanel_call(
            "SubDomain/addsubdomain",
            domain=".".join(labels[:-2]),
            rootdomain=".".join(labels[-2:]),
            dir=DOCUMENT_ROOT,
        )
        status = reply.get("status")
        print("    %-38s %s" % (host, "created" if status == 1 else "refused"))
        if status != 1:
            for error in reply.get("errors") or ["no error text in the reply"]:
                print("        %s" % error)

    after = listed_domains()
    still = [host for host in HOSTS if host not in after]
    print("  after apply: %d of %d present%s"
          % (len(HOSTS) - len(still), len(HOSTS), "" if not still else ", missing " + ", ".join(still)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true",
                        help="ask cPanel which of the five exist; change nothing")
    parser.add_argument("--apply", action="store_true",
                        help="create the missing domains with the toolkit as document root")
    args = parser.parse_args()

    if args.apply:
        plan(apply_changes=True)
    elif args.plan:
        plan(apply_changes=False)
    else:
        print("Checking DNS and HTTP for %d host(s)\n" % len(HOSTS))

    if args.plan or args.apply:
        print("\nChecking what the hosts actually answer\n")

    if not check():
        sys.exit(1)


if __name__ == "__main__":
    main()
