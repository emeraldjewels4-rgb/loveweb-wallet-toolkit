# PHP port

A PHP version of the broadcast layer, for a host that runs PHP and nothing else.

## What is here

| File | What it does |
|---|---|
| `broadcast.php` | `JsonRpc`, `Broadcaster`, `Throttle`, `Journal`, `Outcome` — the same behaviour as `broadcast.py`: retry transient refusals, treat `already known` / `nonce too low` as sent, journal the hash before the send, pace sends, stop a batch at the first fatal error, de-duplicate a batch. |
| `preview.php` | JSON endpoint: chain id, block, gas price, balances, and the cost of a plan. All reads. |
| `index.php` | Server-rendered page over `preview.php`'s logic. No JS build step. |
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
    'ww9999.metakakoon.x13.network' => 'toolkit',
),
```

The right-hand side is a **label** from `others` (or `toolkit` for the primary), never a physical database name, so a domain cannot be pointed at something the config never registered. Each entry carries the same `on` bit as the database it points at, and a label that does not exist is rendered as `unbound` with the offending label shown — a typo that silently does nothing is worse than one that says so.

Two facts about the deployed hosts are worth recording, because both look like a bug and neither is one: the `metakakoon` names resolve to NXDOMAIN from the public internet (the domains exist in cPanel with document roots, but their DNS is not live), and all of them except `ww9999.metakakoon.x13.network` redirect to `https://fiatgod.x10.mx/`, so they serve the same site. The map binds them anyway, so the database is already correct on the day DNS starts answering.

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

## Deployed

Live on x10hosting's Infinity plan (server `s22.infinitysrv.com`), account `fiatgodx`, home `/home/fiatgodx`:

- `https://fiatgod.x10.mx/toolkit/` — status page, renders block, gas price, balances and plan.
- `https://fiatgod.x10.mx/toolkit/preview.php?network=amoy` — JSON, `chain_id: 80002`, `signs: false`, plus a `database` health block.
- MySQL attached: `fiatgodx_toolkit` (user of the same name, `ALL PRIVILEGES`). Credentials live only in `php/config.local.php` on the host.

Uploaded over FTP through a dedicated account `toolkit@fiatgod.x10.mx` scoped to `/home/fiatgodx/public_html/toolkit`. Delete it in cPanel → FTP Accounts when it is no longer wanted (leave "Delete the User's Home Directory" unchecked, or the files go with it).

The toolkit sits in a subdirectory on purpose: `public_html` serves the FiatLord SPA from `index.html`, and an `index.php` in the root can take over as `DirectoryIndex`.

## Notes for a local run

Windows PHP ships no CA bundle, so a local run needs `curl.cainfo` pointing at a `cacert.pem` or every HTTPS RPC fails with `SSL certificate problem` (error 60). A Linux host uses the system store and needs nothing — the deployed copy works without any such setting.
