# PHP port

A PHP version of the broadcast layer, for a host that runs PHP and nothing else.

## What is here

| File | What it does |
|---|---|
| `broadcast.php` | `JsonRpc`, `Broadcaster`, `Throttle`, `Journal`, `Outcome` — the same behaviour as `broadcast.py`: retry transient refusals, treat `already known` / `nonce too low` as sent, journal the hash before the send, pace sends, stop a batch at the first fatal error, de-duplicate a batch. |
| `preview.php` | JSON endpoint: chain id, block, gas price, balances, and the cost of a plan. All reads. |
| `index.php` | Server-rendered page over `preview.php`'s logic. No JS build step. |
| `chain.php` | The blockchain interface: `lovewebconnection` wired to Polygon PoS, read live from the RPC, plus the `web9999` gateway bits. HTML, or `?format=json`. |
| `db.php` | Optional MySQL journal (`DbJournal`) over **mysqli**, plus the health probe the two pages report. |

## What it deliberately cannot do

- **No keys, no signing.** `broadcast.php` takes a signed raw transaction as a hex string and never sees a private key. There is no signer and no `eth_sendRawTransaction` route in the web files. A shared host is not where a key belongs.
- **No keccak-256 in core PHP.** A transaction hash cannot be computed here, so pass it in from Python (`Web3.to_hex(signed.hash)`). If you omit it, the journal records `sha256:<digest>` — clearly labelled, so the file never passes a stand-in off as a real tx hash.
- **No token contract work.** `deploy_token.py`, `mint_token.py`, `distribute_token.py` and `seed_pool.py` stay in Python: they build and sign transactions.

Porting the signer would mean implementing secp256k1 and RLP in PHP with no audited library — the kind of thing that loses funds quietly. That is why the split is where it is.

## Requirements

PHP 7.4+ with `ext-curl` and `ext-json`. Both are on x10hosting by default. No composer, no dependencies.

## Database (optional)

The site runs with no database at all. If you want the journal in MySQL instead of a file, create a database and a user in cPanel → MySQL Databases (give that user **ALL PRIVILEGES** on that one database), then write the credentials to `php/config.local.php`:

```php
return array(
    'host' => 'localhost',
    'name' => 'cpaneluser_toolkit',
    'user' => 'cpaneluser_toolkit',
    'password' => '...',
    'port' => 3306,
);
```

That file is gitignored (`php/.gitignore`) and uploaded separately — the deploy job deliberately does not carry it, because credentials do not belong in a repository or in CI logs.

### Which database a domain reads

cPanel has no switch that attaches a database to a domain: a site is bound to one by the config its code reads. That binding is recorded once, in `config.local.php`, and both pages plus the deploy workflow report it:

```php
'domains' => array(
    'fiatgod.x10.mx' => 'toolkit',
    'ww9999.metakakoon.x12.com' => 'toolkit',
),
```

The right-hand side is a **label** from `others` (or `toolkit` for the primary), never a physical database name, so a domain cannot be pointed at something the config never registered. Each entry carries the same `on` bit as the database it points at, and a label that does not exist is rendered as `unbound` with the offending label shown — a typo that silently does nothing is worse than one that says so.

A binding is not a hosting decision, and the difference matters here. Three separate states are worth recording, because all three look like bugs and none of them is one:

- **`ww9999.metakakoon.x12.com` is not on this account.** The cPanel domain search returns no results for it and it is NXDOMAIN publicly, so the entry below binds a database to a host that has not been created yet. It is written down so the binding is already right on the day the domain is added (Create A New Domain) and its DNS points at this server — it does not make the host exist, and nothing is served there today.
- **The other `metakakoon` names do exist in cPanel** with document roots, but their public DNS answers NXDOMAIN, so they cannot be reached from outside either.
- **All of them except `ww9999.metakakoon.x13.network` redirect to `https://fiatgod.x10.mx/`**, so they serve the same site as the toolkit (and that one host keeps its own document root).

The map binds them anyway: a checked config beats four applications each pasting their own credentials, and a wrong binding is visible on the page instead of buried in a deploy script.

The deployed config lists **every domain explicitly, one line each** — 2,046 entries on this account. `'default' => '<label>'` is supported as the alternative: it binds every domain not named above, including ones added later, which is far less to maintain but says nothing about any individual host. Explicit entries win where both exist, and `domain_synergy()` counts the default rule as one more bit, so the aggregate can never report "all connected" while thousands of hosts are attached to nothing.

### Adding a domain after the fact

The explicit list does not grow by itself, so `sync_domains.py` (repository root) closes that gap: it takes the account's domain list, appends one line for each domain the config does not already name, lints the result, uploads it, and then checks that the live site reports the new count.

    python sync_domains.py                  # list from .sandbox/domains.txt
    python sync_domains.py --from-cpanel     # ask cPanel UAPI directly
    python sync_domains.py --dry-run         # report, write nothing

`--from-cpanel` needs `CPANEL_HOST`, `CPANEL_USER` and `CPANEL_TOKEN` (cPanel → Security → Manage API Tokens) and no browser, which makes it the mode to put on a schedule. Without a token, the file source uses the list captured from the cPanel Domains page. Existing lines are never rewritten, so a second run adds nothing; upload credentials (`FTP_HOST`/`FTP_USER`/`FTP_PASSWORD`, or `.sandbox/ftp.json`) are never printed, and a live count that does not match makes the command exit non-zero instead of reporting success.

### The stored text file

`cjd.php` in this directory is not PHP: it is a 65 KB text file of bracketed hostnames, served as-is. `cjd_store.php` reads it and writes it into `cjd_source` in the primary database — one row holding the whole file, its byte count and its sha256. That is a write endpoint, so it refuses to do anything unless `import.key` (gitignored) sits next to it and the request presents that value, and the file name must match a plain-filename allowlist and resolve inside this directory. The reply reports the row read back out of the database, so "it landed" means the bytes are in MySQL rather than that PHP answered 200. `preview.php` publishes the same row's size and hash — never the body — as `cjd`, which is what makes the claim checkable from outside.

The column is `LONGTEXT`, not `TEXT`: a `TEXT` column stops at 65,535 bytes and this file is 65,154, close enough that the next edit would truncate silently.

### More than one database

The account has four databases, so `config.local.php` names them all: `name` is the primary one (the only one anything writes to), and `others` maps a display label to another database reached with the same credentials:

```php
'others' => array(
    'fiatlord' => 'cpaneluser_fiatlord',
    'zipchain' => 'cpaneluser_zipchain',
),
```

`db_overview()` then reports each one — `on`, connected, table count, row estimate — and both pages show it. `on` is the connection state as an explicit bit: **`01` = reachable, `00` = not**, present on every entry and on the primary status, so a caller never has to infer "working" from a mixture of prose and booleans. Three rules keep that safe:

- **Only configured labels resolve.** `db_named()` takes a label from `config.local.php`, never from a request, so no query string can reach a database that was not registered.
- **`on` is stated, not deduced.** `db_status()` and every `db_overview()` entry carry `on`, and `preview.php` publishes it, so a monitor can alert on `on != 1` without parsing sentences.
- **Neighbours are read-only.** The user's grants on them are `SELECT` only; the `broadcast_journal` table is created in the primary database and nowhere else. Row counts come from `information_schema.TABLES` (an estimate for InnoDB) rather than `COUNT(*)`, because scanning another application's data on every page view is not a status page's job. `db.php` creates its `broadcast_journal` table on first use (`CREATE TABLE IF NOT EXISTS`), so there is no migration step. `preview.php` reports `configured` / `connected` / `table` and never the host or the password; a database that is down is *reported*, not thrown, and the pages render either way.

**Why mysqli and not PDO.** The x10hosting PHP 8.1 build ships `mysqlnd` + `mysqli` and no PDO whatsoever (`Class "PDO" not found`). The first version of `db.php` used PDO, and so it reported a correctly created, correctly granted database as "configured but not reachable" — it never opened a socket. A probe that printed the real exception found it, and the rewrite targets the driver that is actually installed.

### Downloading and merging every table

`db_sync.php` takes everything out of the databases and puts the merged result back into one table. Two of its three actions can read or write data, so it is guarded exactly like `cjd_store.php`: no `import.key` on the host and it answers `503`, a wrong key is `403`, and the key is never echoed back.

| Request | Does |
|---|---|
| `GET ?action=export&limit=N` | Every table of every configured database, rows as JSON. Caps at N rows per table (default 20,000) and marks the table `truncated` when the cap bit. |
| `POST ?action=merge` | `{"rows": [...]}` into `merged_rows` in the primary database. Every row's key is recomputed here and a row filed under a key that does not describe it is refused. |
| `GET ?action=status` | Row count, bytes, digest and per-source-table counts of `merged_rows`. |
| `GET ?action=report` | Read-only survey of every database: server version and defaults, each table's engine and collation, the privileges `SHOW GRANTS` reports, TLS, and the limits that bite (packet size, `sql_mode`, `wait_timeout`). |

`sync_database.py` (repository root) drives all three:

    python sync_database.py                  # download, merge, upload, verify
    python sync_database.py --dry-run         # download and merge locally, send nothing
    python sync_database.py --no-upload       # use the routes already on the host
    python sync_database.py --limit 5000      # rows per table in one export

A row's identity is SHA-256 over its table name and its values, computed on the host, never by the caller. The database name is deliberately *not* in that key, so the same row found in two databases merges to one entry whose `seen_in` count records that it appeared twice -- "merged" and "it only ever existed once" are different claims, and this keeps them apart. Keys are sorted before hashing because two schemas can return the same columns in a different order, which would otherwise look like two different rows.

Nothing is deleted and no neighbour is written to: the rows are copied into `merged_rows` in the primary database, and only ever there. That makes the merge repeatable -- the second run upserts the same keys and updates `last_seen` instead of inserting copies.

`merged_status()` publishes the table's digest (SHA-256 over the sorted row keys) in `preview.php` as `merged`. That is what the sync checks: the digest of what it sent has to equal the digest the host reports, and the digest `preview.php` publishes. A row count alone cannot distinguish "the same three rows" from "three other rows"; a digest can, without dragging 65 KB rows back to diff them.

The local copy of the merge lands in `.sandbox/db_merged.json` (gitignored) -- it holds the rows themselves, so treat it like the databases it came from.

### The blockchain interface

`chain.php` is that interface: `lovewebconnection` wired to Polygon PoS over `https://polygon-bor-rpc.publicnode.com`.

    GET chain.php                 HTML
    GET chain.php?format=json     the same facts as JSON

Its settings are a row in `chain_connections`, not constants: network, chain id, RPC URL, currency, explorer, gateway, and two separate bits — `enabled` (configured) and `on` (live). Stored, because changing the endpoint should be an `UPDATE` rather than a deploy, and because `on` should be a fact somebody set rather than something inferred from "the page rendered". A first run seeds the row with both bits at `1`, which is what *full on* means here; switching it off afterwards is an `UPDATE`, not a missing feature.

The row lives in the primary database, not in `lovewebconnection`, because that database is `SELECT`-only for this user — `?action=report` proves it from `SHOW GRANTS`. `label` is what links the two, and the JSON says `storage` outright so the difference is never guessed at.

`gateway` is the `web9999` host group: every bound host matching `w*9999.`, which is 28 domains (`ww9999.metamask.x12.mx` and friends — no domain is literally named `web9999`). Each reports its `on` bit and the group reports `all_on` with the same convention `synergy` uses: `1` only when every member is `1`, so a gap inside the group is not hidden by its two ends.

The page reads the chain with `eth_chainId`, `eth_blockNumber` and `eth_gasPrice` only. There is no send path and no key on the host, so "connected" here can never mean "can move funds".

## Upload

Do **not** upload these into `public_html/` itself: that directory already serves the FiatLord SPA from `index.html`, and an `index.php` there can take over as `DirectoryIndex` and change your homepage. Upload as a subdirectory instead.

```
toolkit/
  broadcast.php
  preview.php
  index.php
```

FTP, once you have the account username (the short cPanel name, not the email):

```
ftp ftp.fiatgod.x10.mx
  user: <cpanel-user>@fiatgod.x10.mx
  pass: <password>

ftp> cd public_html
ftp> mkdir toolkit
ftp> cd toolkit
ftp> binary
ftp> put broadcast.php
ftp> put preview.php
ftp> put index.php
ftp> bye
```

Then check it:

```
curl -s "https://fiatgod.x10.mx/toolkit/preview.php?network=amoy" | head -20
curl -s -o /dev/null -w '%{http_code}\n' "https://fiatgod.x10.mx/toolkit/"
```

`preview.php` should return JSON with `chain_id: 80002`; the directory index should return `200`. If you get `500`, the server is on PHP 7.4 or older and the most likely culprit is a syntax feature it does not have — tell me the version from cPanel and I will adjust.

## Numbers are exact, on purpose

`Wei` (in `broadcast.php`) does decimal arithmetic on strings. It exists because the obvious code is silently wrong: PHP's int is 64-bit, so `hexdec()` on a balance above about 9.2 POL returns a float and drops low digits. A live check of the zero address on Amoy returns `670591364568878992754` wei — 670.59 POL — which is exactly the case that overflows. Balances, fees and plan totals all go through `Wei::fromHex` / `Wei::add` / `Wei::mulSmall` / `Wei::scale` instead.

## Verified

Run with a portable PHP 8.1.34 (NTS, x64) and no framework:

- `php -l` clean on all three files.
- 33 behaviour checks pass: `classify` (6 cases), `Wei` (18 cases including 2^256-1 to the last digit, plus proof that `hexdec()` really does go float on the same input), `Journal` (3), `Throttle` (3), live Amoy reads, a malformed raw transaction failing without throwing, batch skip-after-fatal, and the labelled `sha256:` hash stand-in.
- Served over HTTP against live networks: `preview.php` returns `chain_id: 80002` on Amoy and `137` (matching) on Polygon; `index.php` renders block, gas price, balances and every plan row with zero PHP notices in the log.
- Error paths answer `400` with a JSON message: unknown network, malformed address, non-numeric amount.
- Database, live: database `fiatgodx_toolkit` created in cPanel with `ALL PRIVILEGES` for user `fiatgodx_toolkit`; `preview.php` returns `{"configured": true, "connected": true, "table": true, "driver": "mysqli"}` on both Amoy and Polygon.
- `DbJournal` round-trip against the live table: one row inserted, read back with the same hash and status, then deleted; the probe file itself was removed from the host afterwards.
- All four databases on the account connect live: `toolkit` (1 table), and `fiatlord`, `lovewebconnection`, `zipchain` reachable with `SELECT` only and **0 tables each** — they are empty shells, so there was no data in them to merge anywhere.
- The pages degrade correctly when no MySQL is reachable: four entries, `on=0` / `connected=false`, no PHP notice, no credential in the output.
- Live connection check after the binary change: primary `on=1` with its journal table ready, and all four databases `on=01`, `connected=true`; `index.php` renders `01` beside each one. The deploy workflow now fails on any `on != 1`.

- Every table in every database downloaded and merged: the export read both tables in `toolkit` (`broadcast_journal` empty, `cjd_source` 1 row) and reported the three empty neighbours as connected with 0 tables; `sync_database.py` merged 1 row (a single 66,510-byte JSON row for a 65,154-byte file), uploaded it, and the merge reply, `?action=status` and `preview.php` all reported the same digest `9e64549755dfadd0...`.
- The merged row still hashes to its own stored sha256 (`2927b2e5...`, 65,154 bytes and 65,154 characters), and `preview.php`'s `cjd` block reports that same hash — the file survived download → merge → upload instead of being re-encoded on the way.
- A second full run changed nothing: 1 row, the same digest, `seen_in` still 1.
- The route's guard answers `403` with no key and with a wrong one, `405` for a merge over GET, `400` for an unknown action, and `503` when no `import.key` is installed.

- `db_sync.php?action=report` surveyed every database without writing a thing: MariaDB 11.4.13-cll-lve, protocol 10, `utf8mb4` available, InnoDB the default engine, no TLS (connections ride the local socket), `sql_mode` empty, `max_allowed_packet` 256 MB, round trip 0.1-0.2 ms.
- `SHOW GRANTS` is parsed with the escaped underscores MariaDB prints (`fiatgodx\_lovewebconnection`): `ALL PRIVILEGES` on `fiatgodx_toolkit`, `SELECT` only on `fiatlord`, `lovewebconnection` and `zipchain`. The first version of the parse missed the escapes and reported the primary database as read-only while it was busy writing rows, so the route also returns the normalised target map and nothing is dropped silently.
- Every database's schema default is `latin1/latin1_swedish_ci`, even though the toolkit's own tables are `utf8mb4_general_ci`: a table created here without an explicit charset inherits latin1.
- `lovewebconnection` connects and is empty: 0 tables, `SELECT` only, schema default latin1. `zipchain` gained a `zipchain_state(id, payload)` table in `latin1_swedish_ci` between the merge run and the report.

- `chain.php` is live and switched on: `lovewebconnection` -> `polygon` / chain `137` / `https://polygon-bor-rpc.publicnode.com`, seeded into `chain_connections` in the primary database (`source: database`, `enabled = 1`, `on = 01`), reading block `96,715,559` and `272.350` gwei from the RPC with `chain_id_matches: true`.
- The `web9999` gateway reports 28 hosts, 28 on, `all_on = 01` -- matched as `w*9999.`, because no domain on the account is literally named `web9999`; all 2046 bindings still report `all_on = 01` with the interface in place.
- `preview.php` publishes the `chain` block on every network and `index.php` links the page. The stored row is in `fiatgodx_toolkit.chain_connections` and the reply says so, because `lovewebconnection` itself is `SELECT`-only.
- A column named `on` is a reserved word in MariaDB, so the first version's `CREATE TABLE` was rejected while the caught exception served defaults and reported `on = 00`; quoting it fixed the seed, and a failed storage read now says `chain table not ready` in `storage.error` instead of passing for "switched off".
- The sync ran again after the upgrade and picked up the new table: 3 merged row(s), digest `abdea4c48ad3daec...`, verified end to end.

## Deployed

Live on x10hosting's Infinity plan (server `s22.infinitysrv.com`), account `fiatgodx`, home `/home/fiatgodx`:

- `https://fiatgod.x10.mx/toolkit/` — status page, renders block, gas price, balances and plan.
- `https://fiatgod.x10.mx/toolkit/preview.php?network=amoy` — JSON, `chain_id: 80002`, `signs: false`, plus a `database` health block.
- MySQL attached: `fiatgodx_toolkit` (user of the same name, `ALL PRIVILEGES`). Credentials live only in `php/config.local.php` on the host.

Uploaded over FTP through a dedicated account `toolkit@fiatgod.x10.mx` scoped to `/home/fiatgodx/public_html/toolkit`. Delete it in cPanel → FTP Accounts when it is no longer wanted (leave "Delete the User's Home Directory" unchecked, or the files go with it).

The toolkit sits in a subdirectory on purpose: `public_html` serves the FiatLord SPA from `index.html`, and an `index.php` in the root can take over as `DirectoryIndex`.

## Notes for a local run

Windows PHP ships no CA bundle, so a local run needs `curl.cainfo` pointing at a `cacert.pem` or every HTTPS RPC fails with `SSL certificate problem` (error 60). A Linux host uses the system store and needs nothing — the deployed copy works without any such setting.
