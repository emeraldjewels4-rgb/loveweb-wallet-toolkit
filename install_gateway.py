#!/usr/bin/env python3
"""Install the web9999 gateway hosts: cPanel documents roots, then prove they answer.

    python install_gateway.py            # check only: DNS and HTTP, needs no credentials
    python install_gateway.py --plan     # ask cPanel what exists, change nothing
    python install_gateway.py --apply    # create the missing domains, then check
    GATEWAY_HOSTS=web9999.metakakoon.x12.mx python install_gateway.py   # list inline

Why the document root matters: a new domain gets an empty document root, and an
empty document root answers 404 while the interface sits one directory away. Every
host here is pointed at `public_html/toolkit` -- the directory that already serves
the toolkit -- so the interface answers at `https://<host>/chain.php` with no
second deploy.

The host every other host is measured against is SITE_DOMAIN, the bare domain the
toolkit is served from (no scheme, no path). Which account that is, is deployment
data, so it is not in this file: a run without it refuses rather than guessing.

Which hosts to install is configuration for the same reason: the list names the
account. The group is the `web9999.metakakoon.*` set -- those six names, one per
zone -- and nothing wider: the account also serves twenty-eight `ww9999.*` names,
the same `w*9999.` shape with one more `w`, and those siblings are not install
targets. The list comes from `.sandbox/gateway_hosts.txt` (one hostname per line,
`#` starts a comment) or `GATEWAY_HOSTS` (comma- or space-separated, which wins
when both are present), and a run with neither refuses instead of checking nothing.
Every name is validated against the `w*9999.` shape on the way in, so a typo'd zone
or a host outside it is refused rather than quietly checked.

A list that lives in configuration can fall behind the account, so with cPanel
credentials the run also asks DomainInfo/list_domains and names every host in the
list's own group that the account serves and the list does not. That is the
direction worth reporting: an omitted host is a host that is dark and nobody is
checking, so the run fails rather than treating it as somebody else's problem. The
other direction -- a target the account does not have -- is named too, and
`--plan` creates it.

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
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CPANEL_JSON = ROOT / ".sandbox" / "cpanel.json"

# The document root that serves the toolkit, and the route the check asks for. The
# directory is relative to the account's home, which is how cPanel's API wants it.
DOCUMENT_ROOT = "public_html/toolkit"
PROBE = "/chain.php?format=json"

# Where the target list is read from when GATEWAY_HOSTS is not set. Gitignored with
# the rest of .sandbox: which hosts this account serves is deployment data.
HOSTS_TXT = ROOT / ".sandbox" / "gateway_hosts.txt"

# The shape every listed name must have: a `w` label carrying `9999.`. It validates
# what goes into the list and nothing more. It is deliberately not what the
# cross-check matches on -- see gateway_group() -- because the account's twenty-eight
# `ww9999.*` siblings share this shape without being install targets, and matching
# them reported all twenty-eight as omissions on every run.
GATEWAY = re.compile(r"^w.*9999\.")

# A bare hostname and nothing else: no scheme, no path, no port, no wildcard.
HOSTNAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def gateway_hosts(path=None):
    """The hosts to install and check, or a refusal.

    GATEWAY_HOSTS wins over the file when both are present, so a one-off run can
    name its targets without writing one. Neither present is a refusal rather than
    an empty list: a run with no targets would report success over nothing, and an
    empty file is the same mistake spelled differently. Each name has to be a bare
    hostname in the gateway group, so a stray `https://`, an extra `/toolkit`, or a
    host from outside `w*9999.` is refused instead of checked as if it were one.
    """
    raw = os.environ.get("GATEWAY_HOSTS")
    source = "GATEWAY_HOSTS"
    if raw is None:
        path = HOSTS_TXT if path is None else Path(path)
        if not path.is_file():
            sys.exit("no gateway host list: write one w*9999. hostname per line to %s,"
                     " or set GATEWAY_HOSTS to a comma-separated list. Which hosts this"
                     " account serves is deployment data, so the code holds no copy."
                     % path)
        raw = path.read_text(encoding="utf-8", errors="replace")
        source = str(path)

    hosts = []
    # Commas separate names in GATEWAY_HOSTS and nowhere else: in a file the comma
    # rule would turn a comment such as "the ww9999 siblings, and their zone" into a
    # second line that is no longer a comment, and then into a hostname.
    lines = raw.splitlines()
    if source == "GATEWAY_HOSTS":
        lines = raw.replace(",", "\n").splitlines()

    for line in lines:
        name = line.split("#", 1)[0].strip().lower()
        if not name:
            continue
        if not HOSTNAME.match(name):
            sys.exit("%s: not a bare hostname: %r -- one name per line, no scheme,"
                     " no path, no port" % (source, line.strip()))
        if not GATEWAY.match(name):
            sys.exit("%s: not a gateway host: %r -- every name must match w*9999.,"
                     " the group the check reports on" % (source, name))
        if name not in hosts:
            hosts.append(name)

    if not hosts:
        sys.exit("%s names no hosts, so there would be nothing to check" % source)

    gateway_group(hosts)  # a list spanning two groups has nothing to compare against

    return hosts


def gateway_group(targets):
    """The namespace the targets share, e.g. `web9999.metakakoon.`.

    The cross-check matches on this rather than on the `w*9999.` shape, so its
    pattern can never be wider than the list it compares. On this account the shape
    also matches twenty-eight `ww9999.*` siblings that are not install targets, and
    naming those as omissions made every credentialed run fail on hosts nobody
    meant to install: the check could never pass.

    Read from the list, so a list that moves to another group takes the pattern with
    it and this file holds no group name. Names sharing no namespace are a refusal --
    with no common group the pattern would match the whole account, which is the
    behaviour being removed.
    """
    prefix = os.path.commonprefix(list(targets))
    cut = prefix.rfind(".")
    if cut < 1:
        sys.exit("the list spans more than one group: %r share no namespace, so"
                 " there is nothing to compare the account against" % sorted(targets))

    return prefix[:cut + 1]


def omissions(targets, account):
    """Hosts in the list's group the account serves that it does not, and the reverse.

    A configured list can fall behind the account, and a host missing from it is a
    host nothing checks. Naming both directions is what makes the list trustworthy
    without freezing it back into code. Only the group the list names is compared,
    so a sibling group such as `ww9999.*` -- same shape, different targets -- is not
    reported as an omission it was never meant to install.
    """
    wanted = set(targets)
    group = gateway_group(targets)
    gateway = {name for name in account if name.startswith(group)}

    return sorted(gateway - wanted), sorted(wanted - account)


def account_domains():
    """The account's names, or None when cPanel cannot be asked.

    The plain check is documented as needing no credentials, so this is how that
    stays true: no credentials means the list goes uncompared, and the run says so
    instead of implying a comparison it did not make.
    """
    host, user, token = cpanel_settings()
    if not (host and user and token):
        return None

    return listed_domains()


def site_domain():
    """The host every other host is measured by, or a refusal.

    SITE_DOMAIN is the bare domain -- no scheme, no path. The account behind it
    is deployment data, so the repository holds no copy of it, and a run that has
    not been told where the site is refuses instead of guessing.
    """
    domain = os.environ.get("SITE_DOMAIN", "").strip()
    if not domain:
        sys.exit("no SITE_DOMAIN: the domain the toolkit is served from is"
                 " deployment data, so nothing here guesses it. Set SITE_DOMAIN to"
                 " the bare domain, e.g. example.org -- no scheme, no path.")
    if "://" in domain or "/" in domain:
        sys.exit("SITE_DOMAIN must be a bare domain, e.g. example.org: got %r"
                 % domain)

    return domain


CPANEL_ENV = ("CPANEL_HOST", "CPANEL_USER", "CPANEL_TOKEN")
CPANEL_HELP = '{"host": "s22.infinitysrv.com", "user": "ACCOUNT", "token": "..."}'
CPANEL_HINT = " Run without --plan/--apply for the DNS and HTTP check only."


def missing_credentials(path=None, next_step=None):
    """The one message for nothing usable being configured, naming both places.

    `next_step` is the caller's advice for what still works without credentials:
    this script can check DNS and HTTP, sync_domains can read a saved list, and
    the message should not name a flag the caller does not have.
    """
    path = Path(path) if path is not None else CPANEL_JSON
    next_step = CPANEL_HINT if next_step is None else " " + next_step.strip()

    return ("no cPanel credentials: write %s as one JSON object with \"host\","
            " \"user\" and \"token\", or set CPANEL_HOST/CPANEL_USER/CPANEL_TOKEN.%s"
            % (path, next_step))


def cpanel_settings(path=None):
    """host, user and token, checked before any request is built.

    Where the file is and how it is shaped used to surface only as a failed call,
    so a file saved in the wrong folder looked like cPanel refusing: the run had
    already got as far as asking. Every refusal here happens before a socket is
    opened, and each one names the absolute path the script reads.

    Nothing configured is not a refusal here: (None, None, None) is how the
    plain check learns to skip the cross-check. require_settings() is this same
    read for a caller that cannot do without the account.
    """
    path = Path(path) if path is not None else CPANEL_JSON
    where = str(path)

    if path.is_file():
        try:
            with open(path, encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except ValueError as exc:
            sys.exit("%s is not valid JSON (%s). It must be one object like %s."
                     % (where, exc, CPANEL_HELP))
        if not isinstance(data, dict):
            sys.exit("%s must hold one JSON object like %s, not a %s."
                     % (where, CPANEL_HELP, type(data).__name__))
        absent = [key for key in ("host", "user", "token")
                  if not str(data.get(key) or "").strip()]
        if absent:
            sys.exit("%s has no %s. All three of \"host\", \"user\" and \"token\""
                     " are required, e.g. %s."
                     % (where, " or ".join(absent), CPANEL_HELP))
        host, user, token = [str(data[key]).strip()
                             for key in ("host", "user", "token")]
    else:
        host, user, token = (os.environ.get(name) for name in CPANEL_ENV)
        if not (host and user and token):
            return None, None, None
        host, user, token = host.strip(), user.strip(), token.strip()

    if any(part in host for part in ("://", "/", ":")):
        bare = host.split("://")[-1].split("/")[0].split(":")[0]
        sys.exit("%s: \"host\" is a bare hostname, because this script adds"
                 " https:// and :2083 itself. Write %r, not %r."
                 % (where, bare, host))

    return host, user, token


def require_settings(next_step=None):
    """host, user and token for a run that needs the account, or the refusal.

    The one place a missing account becomes a message. Every request goes through
    cpanel_call(), so a run that needs cPanel is told once, with the absolute path
    -- not read twice by main() and again by the first call. `next_step` is the
    caller's own advice, passed to missing_credentials().
    """
    host, user, token = cpanel_settings()
    if not (host and user and token):
        sys.exit(missing_credentials(next_step=next_step))

    return host, user, token


def cpanel_call(path, next_step=None, **params):
    """One UAPI call. Errors carry cPanel's own message and never the token.

    `next_step` is not a query parameter: it is the sentence the no-credentials
    refusal ends with, for a caller that can still do something without an account.
    """
    host, user, token = require_settings(next_step)

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


def check(targets, account):
    reference_host = site_domain()
    reference = set(addresses(reference_host)["v4"])
    if reference:
        print("  reference: %s resolves to %s"
              % (reference_host, ", ".join(sorted(reference))))
    else:
        print("  reference: %s did not resolve from here" % reference_host)

    problems = 0
    for host in targets:
        found = addresses(host)
        answer = probe(host)
        same = bool(set(found["v4"]) & reference)

        print("\n  %s" % host)
        print("    dns    : A %s | AAAA %d record(s)%s" % (
            ", ".join(found["v4"]) or "none (%s)" % found["error"],
            len(found["v6"]), " (same server as %s)" % reference_host if same else ""))
        print("    chain  : %s" % (
            "%s:// %s-> on=%02d" % (answer["scheme"], answer["status"], answer["on"])
            if answer["on"] is not None
            else "%s: %s" % (answer["scheme"], answer["detail"])))

        if not (found["v4"] or found["v6"]):
            print("    needs  : an A/AAAA record at the zone's provider, then the domain in cPanel")
            problems += 1
        elif not same:
            print("    needs  : the record points somewhere else; it must reach %s"
                  % reference_host)
            problems += 1
        elif answer["on"] != 1:
            print("    needs  : the domain added in cPanel with document root %s" % DOCUMENT_ROOT)
            problems += 1
        else:
            print("    state  : serving the interface")

    if account is None:
        print("\n  account: no cPanel credentials, so the list was not compared with"
              " what cPanel serves -- the omission check did not run")
    else:
        extras, absent = omissions(targets, account)
        for host in extras:
            print("\n  %s\n    list   : on the account as a gateway host, not in the list"
                  "\n    needs  : add it, or it is dark and unchecked" % host)
            problems += 1
        for host in absent:
            print("\n  %s\n    state  : in the list, not on the account"
                  "\n    needs  : --plan to create it, or drop it from the list" % host)
            problems += 1
        if not (extras or absent):
            print("\n  account: cPanel and the list agree on every gateway host")

    print("\n  %d of %d hosts still need work" % (problems, len(targets)))

    return problems == 0


def plan(targets, existing, apply_changes):
    missing = [host for host in targets if host not in existing]
    extras, _ = omissions(targets, existing)
    print("  cPanel lists %d name(s); %d of %d target(s) present, %d missing"
          % (len(existing), len(targets) - len(missing), len(targets), len(missing)))

    for host in extras:
        print("    ! %-38s on the account, not in the list -- add it before it goes dark"
              % host)

    for host in missing:
        labels = host.split(".")
        subdomain, root = ".".join(labels[:-2]), ".".join(labels[-2:])
        print("    + %-38s as %s.%s -> %s" % (host, subdomain, root, DOCUMENT_ROOT))

    if not missing:
        print("  nothing to create")
        return existing

    if not apply_changes:
        print("  plan only: re-run with --apply to create them")
        return existing

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
    still = [host for host in targets if host not in after]
    print("  after apply: %d of %d present%s"
          % (len(targets) - len(still), len(targets), "" if not still else ", missing " + ", ".join(still)))

    return after


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true",
                        help="ask cPanel which of the targets exist; change nothing")
    parser.add_argument("--apply", action="store_true",
                        help="create the missing domains with the toolkit as document root")
    args = parser.parse_args()

    targets = gateway_hosts()
    if args.plan or args.apply:
        # The first request reads the credentials file and refuses there, naming
        # its absolute path, so a missing or malformed file is that one message
        # rather than a refused call -- and the file is read once, not twice.
        account = plan(targets, listed_domains(), apply_changes=args.apply)
        print("\nChecking what the hosts actually answer\n")
    else:
        account = account_domains()
        print("Checking DNS and HTTP for %d host(s)\n" % len(targets))

    if not check(targets, account):
        sys.exit(1)


if __name__ == "__main__":
    main()
