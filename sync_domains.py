#!/usr/bin/env python3
"""Add newly created domains to php/config.local.php, one line each, and upload.

The deployed config names every domain explicitly, so a domain created in cPanel
is attached to nothing until a line exists for it. Re-running the full generator
to add one domain rewrites 2,046 lines; this appends only the missing ones and
leaves every existing line byte-for-byte alone.

    python sync_domains.py                  # domain list from .sandbox/domains.txt
    python sync_domains.py --from-cpanel     # ask cPanel UAPI directly
    python sync_domains.py --dry-run         # say what it would add, change nothing

Sources
    file    .sandbox/domains.txt or .sandbox/domains.b64 -- the account's domain
            list as captured from the cPanel Domains page.
    cpanel  /execute/DomainInfo/list_domains over HTTPS with an API token
            (CPANEL_HOST, CPANEL_USER, CPANEL_TOKEN). Create one under
            cPanel > Security > Manage API Tokens. Fully automatic: no browser,
            so this is the mode to put on a schedule.

Upload
    FTP_HOST / FTP_USER / FTP_PASSWORD, or .sandbox/ftp.json with the same keys.
    --no-upload skips it. Nothing here prints a credential.

Idempotent: a domain already in the config is left alone, so a second run adds
nothing. Exit status is non-zero when the live site does not report the expected
domain count afterwards, so a scheduled run that silently failed is not silent.
"""

import argparse
import base64
import gzip
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "php" / "config.local.php"
LIST_TXT = ROOT / ".sandbox" / "domains.txt"
LIST_B64 = ROOT / ".sandbox" / "domains.b64"
FTP_JSON = ROOT / ".sandbox" / "ftp.json"
LOCAL_PHP = ROOT / ".sandbox" / "php" / "php.exe"
VERIFY_URL = "https://fiatgod.x10.mx/toolkit/preview.php?network=amoy"

# One entry of the `domains` array: eight spaces, a quoted key, a quoted label.
ENTRY = re.compile(r"^\s*'([^']+)'\s*=>\s*'([^']+)',\s*$", re.MULTILINE)
DOMAIN_OK = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def pick(domain):
    """Route an obvious name at its own database; everything else is primary.

    Same rule as .sandbox/gen_config.py, so a domain appended here gets the label
    the generator would have given it.
    """
    if "fiatlord" in domain:
        return "fiatlord"
    if "zipchain" in domain:
        return "zipchain"
    if "loveweb" in domain:
        return "lovewebconnection"
    return "toolkit"


def domains_block(text):
    """Offsets of the `domains` array body: after the opener and before the close."""
    start = text.find("'domains' => array(")
    if start < 0:
        sys.exit("config has no 'domains' => array(...): refusing to guess where to insert")
    end = text.find("\n    ),", start)
    if end < 0:
        sys.exit("could not find where the domains array ends: refusing to write")
    return start, end


def existing_domains(text):
    start, end = domains_block(text)
    return {m.group(1) for m in ENTRY.finditer(text[start:end])}


def names_from_file():
    if LIST_TXT.is_file():
        raw = LIST_TXT.read_text(encoding="utf-8", errors="replace")
        return [line for line in raw.splitlines()], str(LIST_TXT.relative_to(ROOT))
    if LIST_B64.is_file():
        raw = gzip.decompress(base64.b64decode(LIST_B64.read_text())).decode("utf-8")
        return raw.splitlines(), str(LIST_B64.relative_to(ROOT))
    sys.exit("no .sandbox/domains.txt or .sandbox/domains.b64 -- capture the domain list first,"
             " or run with --from-cpanel and CPANEL_* set")


def names_from_cpanel():
    host = os.environ.get("CPANEL_HOST", "").strip()
    user = os.environ.get("CPANEL_USER", "").strip()
    token = os.environ.get("CPANEL_TOKEN", "").strip()
    if not (host and user and token):
        sys.exit("--from-cpanel needs CPANEL_HOST, CPANEL_USER and CPANEL_TOKEN"
                 " (cPanel > Security > Manage API Tokens)")

    url = "https://%s:2083/execute/DomainInfo/list_domains?api.version=1" % host
    request = urllib.request.Request(url, headers={
        "Authorization": "cpanel %s:%s" % (user, token),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.load(response)

    rows = payload.get("data")
    if not isinstance(rows, list):
        detail = (payload.get("errors") or ["no data in the UAPI reply"])[0]
        sys.exit("cPanel refused the domain list: %s" % detail)

    names = [row.get("domain") for row in rows if isinstance(row, dict) and row.get("domain")]
    return names, "cPanel UAPI"


def clean(names):
    """Lowercase, trim, drop the impossible. A hostname is never a guess."""
    out = []
    for name in names:
        name = (name or "").strip().lower()
        if not name or "." not in name or " " in name:
            continue
        if not DOMAIN_OK.match(name):
            continue
        out.append(name)
    return sorted(set(out))


def lint(path):
    """php -l with the local harness when it is present; no PHP, no lint."""
    if not LOCAL_PHP.is_file():
        print("  lint: skipped (no local php at %s)" % LOCAL_PHP.relative_to(ROOT))
        return True
    result = subprocess.run(
        [str(LOCAL_PHP), "-c", str(LOCAL_PHP.parent / "php.ini"), "-l", str(path)],
        capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip().splitlines()
    print("  lint: %s" % (output[-1] if output else "exit %d" % result.returncode))
    return result.returncode == 0


def ftp_settings():
    if FTP_JSON.is_file():
        data = json.loads(FTP_JSON.read_text())
        return data.get("host"), data.get("user"), data.get("password"), data.get("remote", "config.local.php")

    return (os.environ.get("FTP_HOST"), os.environ.get("FTP_USER"),
            os.environ.get("FTP_PASSWORD"), os.environ.get("FTP_REMOTE", "config.local.php"))


def upload(path, dry_run=False):
    host, user, password, remote = ftp_settings()
    if not (host and user and password):
        sys.exit("no FTP credentials: set FTP_HOST/FTP_USER/FTP_PASSWORD or write"
                 " .sandbox/ftp.json, or re-run with --no-upload")

    if dry_run:
        print("  upload: would send %s -> %s" % (path.name, remote))
        return

    import ftplib

    ftp = ftplib.FTP(timeout=45)
    ftp.connect(host, 21)
    ftp.login(user, password)
    ftp.voidcmd("TYPE I")
    with path.open("rb") as handle:
        ftp.storbinary("STOR %s" % remote, handle)
    size = ftp.size(remote)
    ftp.quit()

    print("  upload: %s -> %s (%d bytes local, %d remote)" % (path.name, remote, path.stat().st_size, size))
    if size != path.stat().st_size:
        sys.exit("upload truncated: %d of %d bytes landed" % (size, path.stat().st_size))


def verify(expected, dry_run=False, url=VERIFY_URL):
    """The live site must report the count we just wrote, with everything on."""
    if dry_run:
        print("  verify: would expect %d domains live" % expected)
        return True

    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)

    synergy = payload.get("synergy") or {}
    live = synergy.get("domains")
    bits = len(synergy.get("vector") or "")
    print("  verify: live reports %s domains (%d bits), all_on=%s"
          % (live, bits, synergy.get("all_on")))

    if live != expected or bits != expected:
        print("  verify: FAILED, expected %d" % expected)
        return False
    if synergy.get("all_on") != 1:
        print("  verify: FAILED, not every domain is on")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-cpanel", action="store_true",
                        help="ask cPanel UAPI for the domain list instead of reading a saved one")
    parser.add_argument("--config", default=str(CONFIG), help="config to update (default: php/config.local.php)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--no-upload", action="store_true", help="update the local file only")
    parser.add_argument("--no-verify", action="store_true", help="skip the live check")
    args = parser.parse_args()

    path = Path(args.config)
    if not path.is_file():
        sys.exit("no config at %s" % path)

    names, source = names_from_cpanel() if args.from_cpanel else names_from_file()
    names = clean(names)
    if not names:
        sys.exit("the %s domain list was empty: refusing to write" % source)

    text = path.read_text(encoding="utf-8")
    known = existing_domains(text)
    missing = [name for name in names if name not in known]

    print("  source: %s | %d domains listed, %d already bound, %d new"
          % (source, len(names), len(names) - len(missing), len(missing)))

    if not missing:
        print("  nothing to add: every listed domain already has a line")
    else:
        counts = {}
        lines = []
        for name in missing:
            label = pick(name)
            counts[label] = counts.get(label, 0) + 1
            lines.append("        '%s' => '%s',\n" % (name, label))

        # Insert as a block just before the array's closing "    ),", keeping the
        # file's own indentation so a diff shows only the new lines.
        start, end = domains_block(text)
        updated = text[:end] + "\n" + "".join(lines) + text[end + 1:]

        for label in sorted(counts):
            print("    + %-20s %d" % (label, counts[label]))
        for name in missing[:5]:
            print("    + %s" % name)
        if len(missing) > 5:
            print("    + ... and %d more" % (len(missing) - 5))

        if args.dry_run:
            print("  dry run: %s not written" % path.name)
        else:
            path.write_text(updated, encoding="utf-8")
            print("  wrote %s (%d bytes)" % (path.name, path.stat().st_size))

    if not args.dry_run and not lint(path):
        sys.exit("refusing to upload a config that does not lint")

    if not args.no_upload and not args.dry_run:
        upload(path)

    expected = len(known) + len(missing)
    if not args.no_verify:
        if not verify(expected, dry_run=args.dry_run):
            sys.exit(1)

    print("  done: %d domains bound, one line each" % expected)


if __name__ == "__main__":
    main()
