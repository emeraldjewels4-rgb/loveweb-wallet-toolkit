#!/usr/bin/env python3
"""Download every table from the databases on the host, merge the rows, upload.

    python sync_database.py                  # download, merge, upload, verify
    python sync_database.py --dry-run         # download and merge locally, send nothing
    python sync_database.py --no-upload       # use the routes already on the host
    python sync_database.py --limit 5000      # rows per table in one export

Download
    php/db_sync.php?action=export reads every table of every database named in
    php/config.local.php -- the primary first, then the SELECT-only neighbours --
    and returns the rows as JSON. That path writes nothing.

Merge
    Rows are merged on the key db_sync.php computed for them, so the same row
    found in two databases collapses into one entry that records both sources.
    Nothing is deleted, so running this twice is the same as running it once.

Upload
    The merged rows go back to php/db_sync.php?action=merge, which recomputes
    every key and refuses a row filed under one that does not describe it, then
    upserts into `merged_rows` in the primary database. The neighbours are never
    written to -- the grants are SELECT-only and that is the arrangement to keep.

Verify
    The remote table's digest (SHA-256 over the sorted row keys) has to equal the
    digest of what was sent, and preview.php has to publish the same numbers.
    Anything else exits non-zero, so a scheduled run cannot fail quietly.

The local copy of the merge is .sandbox/db_merged.json (gitignored): it holds the
rows, so it is as sensitive as the databases themselves.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PHP = ROOT / "php"
KEY_FILE = PHP / "import.key"
SNAPSHOT = ROOT / ".sandbox" / "db_merged.json"
FTP_JSON = ROOT / ".sandbox" / "ftp.json"

BASE = "https://fiatgod.x10.mx/toolkit"
SYNC_URL = BASE + "/db_sync.php"
PREVIEW_URL = BASE + "/preview.php?network=amoy"

# The files this feature is made of. db.php and preview.php changed with it, so
# they go up too: a route that reads a table nothing creates fails on the host
# while looking perfectly correct here.
UPLOADS = ("db_sync.php", "db.php", "preview.php")

# One request carries about this much JSON. PHP's post_max_size is 8 MB by
# default; staying well under it is cheaper than discovering it by 413.
BATCH_BYTES = 2 * 1024 * 1024


def import_key():
    if not KEY_FILE.is_file():
        sys.exit("no %s: the routes on the host disable themselves without it"
                 % KEY_FILE.relative_to(ROOT))
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("%s is empty" % KEY_FILE.relative_to(ROOT))

    return key


def ftp_settings():
    if FTP_JSON.is_file():
        data = json.loads(FTP_JSON.read_text())
        return data.get("host"), data.get("user"), data.get("password")

    return (os.environ.get("FTP_HOST"), os.environ.get("FTP_USER"),
            os.environ.get("FTP_PASSWORD"))


def upload(name, dry_run=False):
    local = PHP / name
    size = local.stat().st_size

    host, user, password = ftp_settings()
    if not (host and user and password):
        sys.exit("no FTP credentials: set FTP_HOST/FTP_USER/FTP_PASSWORD or write"
                 " .sandbox/ftp.json, or re-run with --no-upload")

    if dry_run:
        print("  upload: would send %s (%d bytes)" % (name, size))
        return

    import ftplib

    ftp = ftplib.FTP(timeout=45)
    ftp.connect(host, 21)
    ftp.login(user, password)
    ftp.voidcmd("TYPE I")
    with local.open("rb") as handle:
        ftp.storbinary("STOR %s" % name, handle)
    remote = ftp.size(name)
    ftp.quit()

    print("  upload: %s -> %s (%d bytes local, %s remote)" % (name, name, size, remote))
    if remote is not None and remote != size:
        sys.exit("upload truncated: %d of %d bytes landed" % (remote, size))


def call(url, key, payload=None):
    headers = {"X-Import-Key": key, "Cache-Control": "no-cache"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:400]
        sys.exit("%s refused with HTTP %s: %s" % (url, exc.code, detail))

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit("%s did not answer with JSON: %s" % (url, body[:200]))


def collect(export):
    """Merge the exported tables into one row per distinct key, with provenance."""
    merged = {}
    tables = 0
    seen_rows = 0
    skipped = []

    for table in export.get("tables") or []:
        name = table.get("table")
        if not table.get("on") or not name:
            skipped.append((str(table.get("database")), table.get("error") or "not readable"))
            continue
        if table.get("error"):
            skipped.append(("%s.%s" % (table.get("database"), name), table["error"]))
            continue

        tables += 1
        for row in table.get("rows") or []:
            seen_rows += 1
            key = row["key"]
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "key": key,
                    "table": name,
                    "source_db": table.get("database"),
                    "values": row["values"],
                    "seen": {},
                }
                merged[key] = entry
            source = "%s.%s" % (table.get("database"), name)
            entry["seen"][source] = entry["seen"].get(source, 0) + 1

    return merged, tables, seen_rows, skipped


def digest(keys):
    """The same digest merged_status() computes: SHA-256 over sorted keys."""
    context = hashlib.sha256()
    for key in sorted(keys):
        context.update(key.encode("ascii"))

    return context.hexdigest()


def payload_rows(merged):
    return [
        {
            "table": merged[key]["table"],
            "source_db": merged[key]["source_db"],
            "key": key,
            "values": merged[key]["values"],
            "seen": sum(merged[key]["seen"].values()),
        }
        for key in sorted(merged)
    ]


def batches(rows, limit=BATCH_BYTES):
    batch = []
    size = 0
    for row in rows:
        weight = len(json.dumps(row)) + 1
        if batch and size + weight > limit:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += weight
    if batch:
        yield batch


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20000,
                        help="rows per table in one export (default 20000)")
    parser.add_argument("--no-upload", action="store_true",
                        help="use the routes already on the host")
    parser.add_argument("--dry-run", action="store_true",
                        help="download and merge locally, send nothing")
    parser.add_argument("--no-verify", action="store_true", help="skip the live digest check")
    args = parser.parse_args()

    key = import_key()

    if not args.no_upload:
        for name in UPLOADS:
            if not (PHP / name).is_file():
                sys.exit("missing %s" % (PHP / name).relative_to(ROOT))
            upload(name, dry_run=args.dry_run)

    cap = max(1, min(50000, args.limit))
    export = call("%s?action=export&limit=%d" % (SYNC_URL, cap), key)
    merged, tables, seen_rows, skipped = collect(export)
    print("  export: %d table(s) read, %d row(s)" % (tables, seen_rows))
    for name, reason in skipped:
        print("  export: skipped %s (%s)" % (name, reason))

    print("  merge: %d distinct row(s), %d duplicate(s) collapsed"
          % (len(merged), seen_rows - len(merged)))

    expected = digest(merged)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({
        "generated_at": export.get("generated_at"),
        "cap": cap,
        "databases": sorted({row["source_db"] for row in merged.values()}),
        "rows": len(merged),
        "digest": expected,
        "merged": sorted(merged.values(), key=lambda entry: entry["key"]),
    }, indent=1, sort_keys=True), encoding="utf-8")
    print("  snapshot: %s (%d bytes)" % (SNAPSHOT.relative_to(ROOT), SNAPSHOT.stat().st_size))

    if args.dry_run:
        print("  upload: would send %d row(s), digest %s" % (len(merged), expected[:16]))
        return

    stored = 0
    for batch in batches(payload_rows(merged)):
        reply = call(SYNC_URL + "?action=merge", key, {"rows": batch})
        stored += int(reply.get("accepted") or 0)
        print("  upload: sent %s, accepted %s, distinct %s"
              % (reply.get("received"), reply.get("accepted"), reply.get("distinct")))

    if args.no_verify:
        print("  done: %d row(s) uploaded" % stored)
        return

    status = call(SYNC_URL + "?action=status", key).get("merged") or {}
    remote_rows = status.get("rows")
    remote_digest = status.get("digest")
    print("  verify: remote holds %s row(s), %s bytes, digest %s"
          % (remote_rows, status.get("bytes"), (remote_digest or "")[:16]))

    ok = remote_rows == len(merged) and remote_digest == expected

    with urllib.request.urlopen(PREVIEW_URL, timeout=120) as response:
        live = (json.load(response) or {}).get("merged") or {}
    print("  verify: preview reports %s row(s), digest %s"
          % (live.get("rows"), (live.get("digest") or "")[:16]))
    ok = ok and live.get("rows") == len(merged) and live.get("digest") == expected

    if not ok:
        print("  verify: FAILED, expected %d row(s) and digest %s" % (len(merged), expected[:16]))
        sys.exit(1)

    print("  done: %d row(s) merged from %d table(s), digest verified end to end"
          % (len(merged), tables))


if __name__ == "__main__":
    main()
