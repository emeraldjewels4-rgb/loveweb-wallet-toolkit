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
    Every run names itself with a token and the host stamps the rows it writes
    with it, so the digest over just this run (SHA-256 of the sorted keys) has
    to equal the digest of what was sent -- exact equality, not a sample.

    Nothing is ever deleted from the table, so a row removed from its source
    stays behind carrying an older token: the total is reported, not required to
    match. What is required is that the total never drops below what earlier
    runs already stored, and that preview.php publishes that same total.
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
import uuid
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


def previous_keys():
    """The keys the last run wrote, read before this one overwrites the snapshot.

    Losing a row from a source is not a fault, so the accumulated table is
    allowed to be bigger than this export. Being SMALLER than what a previous
    run stored is a different thing: that is the table having lost rows, and the
    snapshot is the only local record of what it once held.
    """
    try:
        data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()

    return {row.get("key") for row in data.get("merged") or [] if row.get("key")}


def verification(scoped, accumulated, local_keys, expected, prev_keys):
    """Decide whether this run's rows are stored, tolerating older ones.

    Returns (ok, lines). The gate is exact equality against the digest of the
    rows this run wrote. The accumulated total is checked only for the two
    mistakes that matter -- fewer rows than were just sent, or fewer than an
    earlier run had already stored -- because the table is a union over time and
    demanding it equal one export would fail every run after any source delete.
    """
    lines = []
    rows = scoped.get("rows")
    got = scoped.get("digest")
    lines.append("  verify: this run stored %s row(s), digest %s (sent %d, %s)"
                 % (rows, (got or "")[:16], len(local_keys), expected[:16]))
    ok = rows == len(local_keys) and got == expected

    if not ok and local_keys and rows == 0:
        lines.append("  verify: nothing was recorded under this run token, which is what a"
                     " host still running the older db.php or db_sync.php looks like")

    total = (accumulated or {}).get("rows")
    if not isinstance(total, int):
        lines.append("  verify: the table's total was not readable, so accumulation is unchecked")
        return ok, lines

    lines.append("  verify: table holds %d row(s) in total (%+d against this export; rows"
                 " removed from a source stay here by design)" % (total, total - len(local_keys)))

    floor = len(set(local_keys) | set(prev_keys))
    if total < len(local_keys):
        lines.append("  verify: the table holds fewer rows than were just sent")
        ok = False
    elif total < floor:
        lines.append("  verify: the table lost rows an earlier run had stored (%d expected at least)"
                     % floor)
        ok = False

    return ok, lines


PRIMARY_LABEL = "toolkit"  # DB_PRIMARY in php/db.php


def schema_preflight(report):
    """Say in advance whether the merge can store rows, before any are sent.

    The first merge after a deploy has to add `run_token`, which needs ALTER on
    merged_rows. Without that grant the merge fails with a generic 503 that says
    nothing about privileges, and it fails only after every row has been built
    and posted. ?action=report states what the account actually holds, so the
    answer is available before the first request that writes.

    Returns (ok, lines)."""
    lines = []
    primary = ((report or {}).get("databases") or {}).get(PRIMARY_LABEL) or {}
    table = primary.get("merged_table") or {}
    if not table:
        lines.append("  preflight: the host did not report merged_rows, so ALTER access is "
                     "unknown and the merge will be the one to find out")
        return True, lines

    database = table.get("database") or primary.get("database") or PRIMARY_LABEL
    exists = table.get("exists")
    has_run = table.get("has_run_token")
    privs = table.get("privileges") or {}
    lines.append("  preflight: %s.merged_rows exists=%s run_token=%s | ALTER=%s DROP=%s CREATE=%s"
                 % (database, exists, has_run, privs.get("alter"), privs.get("drop"),
                    privs.get("create")))

    if exists is False and not privs.get("create"):
        lines.append("  preflight: merged_rows does not exist and CREATE is not granted, so it "
                     "cannot be created. Grant CREATE and INSERT on %s." % database)
        return False, lines

    if table.get("needs_alter") and not privs.get("alter"):
        lines.append("  preflight: merged_rows has no run_token column and ALTER is not granted, "
                     "so the column cannot be added; the merge would fail with a 503 after "
                     "sending rows")
        lines.append("  preflight: grant ALTER on %s.merged_rows (cPanel -> MySQL Databases -> "
                     "privileges), or drop the table so it can be recreated with the column"
                     % database)
        return False, lines

    if not privs.get("drop"):
        lines.append("  preflight: DROP is not granted; that is only needed if the table has to "
                     "be rebuilt, so the merge can proceed")
    return True, lines


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
    parser.add_argument("--skip-preflight", action="store_true",
                        help="do not ask the host whether ALTER is granted before merging")
    args = parser.parse_args()

    key = import_key()

    # Before anything is uploaded or sent: the host is the only party that knows
    # what the account may do, and a missing ALTER has a name worth printing.
    if not args.skip_preflight:
        ok, lines = schema_preflight(call(SYNC_URL + "?action=report", key))
        for line in lines:
            print(line)
        if not ok:
            print("  preflight: refusing to merge; nothing was uploaded and nothing was sent")
            sys.exit(1)

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
    # Read before the snapshot is overwritten: it is the only local record of
    # what earlier runs had already stored.
    prev_keys = previous_keys()
    run = uuid.uuid4().hex
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({
        "generated_at": export.get("generated_at"),
        "run": run,
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
        reply = call(SYNC_URL + "?action=merge", key, {"rows": batch, "run": run})
        stored += int(reply.get("accepted") or 0)
        print("  upload: sent %s, accepted %s, distinct %s"
              % (reply.get("received"), reply.get("accepted"), reply.get("distinct")))

    if args.no_verify:
        print("  done: %d row(s) uploaded" % stored)
        return

    scoped = call("%s?action=status&run=%s" % (SYNC_URL, run), key).get("merged") or {}
    accumulated = call(SYNC_URL + "?action=status", key).get("merged") or {}
    ok, lines = verification(scoped, accumulated, set(merged), expected, prev_keys)
    for line in lines:
        print(line)

    with urllib.request.urlopen(PREVIEW_URL, timeout=120) as response:
        live = (json.load(response) or {}).get("merged") or {}
    print("  verify: preview reports %s row(s), digest %s"
          % (live.get("rows"), (live.get("digest") or "")[:16]))
    if live.get("rows") != accumulated.get("rows") or live.get("digest") != accumulated.get("digest"):
        print("  verify: preview disagrees with ?action=status about the same table")
        ok = False

    if not ok:
        print("  verify: FAILED, expected %d row(s) with digest %s under run %s"
              % (len(merged), expected[:16], run[:8]))
        sys.exit(1)

    print("  done: %d row(s) merged from %d table(s), this run verified end to end"
          % (len(merged), tables))


if __name__ == "__main__":
    main()
