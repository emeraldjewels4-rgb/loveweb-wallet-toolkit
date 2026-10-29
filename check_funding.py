#!/usr/bin/env python3
"""Check which Bitcoin addresses from a wallet export have ever received funds.

    python check_funding.py --wallets wallets.json wallets-btc-900.json

Reads the Bitcoin addresses out of one or more wallet JSON files and asks a
public blockchain API whether each one has ever been paid.

WHAT "IN USE" MEANS HERE -- the important detail:

    an address has been used  <=>  total_received > 0

NOT the current balance. An address can be funded and then swept, leaving a
balance of 0 while still having a full history. And tx_count alone is also not
the test. So the report distinguishes three states:

    never funded      total_received == 0            (untouched)
    funded, emptied   total_received > 0, balance 0  (used, then swept)
    holds funds       balance > 0                    (live)

PRIVACY WARNING: asking a public API about your own addresses tells that
operator that the addresses are related, and links your whole set together.
The usual endpoint is batched, so the provider sees many addresses per request
arriving from your IP. If that matters to you, run a pruning Bitcoin node and
query it locally instead; there is no way to check an address against the
public chain without revealing which address you care about.

Correctness: every address reported as funded is re-checked against a SECOND,
independent provider (Blockstream Esplora, a different operator) before being
reported, so a single provider's bad answer cannot create a false positive.
Three control addresses are injected into every run: a known-funded one that
must come back funded, a funded-then-swept one that must also come back funded
(it has a zero balance), and a known-fresh one that must not. If any control
fails the run is reported as unreliable rather than trusted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Batched: many addresses per request, so ~60 requests cover 3000 addresses
# instead of thousands of single lookups.
BATCH_URL = "https://blockchain.info/multiaddr"

# Independent second opinion, different operator, keyless.
ESPLORA_URL = "https://blockstream.info/api/address/"

# Controls: (label, address, must_be_flagged_as_used). Injected into every run
# so the detector is exercised in both directions.
CONTROLS = [
    # Satoshi's genesis address: funded since 2009 and still holding.
    ("known-funded (genesis)", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", True),
    # BIP173's documented test vector. It has 212 transactions and a final
    # balance of ZERO -- funded, then swept. This is the control that proves
    # the test is not secretly balance-based.
    ("funded-then-swept", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", True),
    # Derived from a throwaway phrase whose keys were discarded: must be clean.
    ("known-fresh", "bc1qzr7zfggvswyqxgmzp3vjcl4al02fwdmxf88juz", False),
]
CONTROL_ADDRESSES = {addr for _label, addr, _expect in CONTROLS}

UA = "wallet-funding-check/1.0"


class ProviderError(RuntimeError):
    pass


def http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def batch_lookup(addresses: list, retries: int = 4) -> dict:
    """One multiaddr request -> {address: {...}}. Raises ProviderError on failure."""
    query = urllib.parse.urlencode({"active": "|".join(addresses)})
    url = f"{BATCH_URL}?{query}"
    delay = 2.0
    last = "unknown"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            payload = http_get_json(url)
            rows = payload.get("addresses")
            if not isinstance(rows, list):
                raise ProviderError(f"unexpected payload: {str(payload)[:200]}")
            return {r["address"]: r for r in rows if "address" in r}
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            # Honour Retry-After when the provider supplies it.
            wait = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = max(delay, float(wait))
            except (TypeError, ValueError):
                pass
            last_exc = exc
            if exc.code in (429, 430, 500, 502, 503, 504):
                if attempt < retries:
                    print(f"    {last}; backing off {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    delay *= 2
                    continue
                break  # out of attempts: the message below reports the count
            # A non-retryable code (404, 401) cannot improve by being asked again.
            raise ProviderError(f"{last} for batch of {len(addresses)}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            last_exc = exc
            if attempt < retries:
                print(f"    {last}; retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
                continue
            break
    # Reached only when every attempt was spent on a retryable failure. This
    # raise used to sit after a loop whose branches each raised first, so it was
    # dead code and the caller saw a bare cause with no attempt count.
    raise ProviderError(f"gave up after {retries} attempt(s): {last}") from last_exc


def esplora_funded(address: str) -> bool:
    """Independent check: has this address ever been funded, per Blockstream?"""
    data = http_get_json(ESPLORA_URL + urllib.parse.quote(address))
    stats = data.get("chain_stats") or {}
    # Confirmed funding only; mempool_stats would mean 'pending', not 'used'.
    return int(stats.get("funded_txo_count", 0)) > 0


def collect(paths: list) -> list:
    """[(source_file, index, address)] across every wallet file, de-duplicated."""
    out, seen = [], set()
    for p in paths:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        if doc.get("encrypted"):
            raise SystemExit(f"Error: {p} is encrypted; decrypt it first.")
        for entry in doc.get("wallets", []):
            btc = entry.get("btc")
            if not btc:
                continue
            addr = btc["address"]
            if addr in seen:
                continue
            seen.add(addr)
            out.append((Path(p).name, entry.get("index"), addr))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report which wallet addresses have ever received Bitcoin."
    )
    parser.add_argument("--wallets", nargs="+", required=True, help="wallet JSON files")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--pause", type=float, default=1.5, help="seconds between requests")
    parser.add_argument("--results", help="resume/progress file (JSON)")
    parser.add_argument("--limit", type=int, help="only check the first N addresses")
    args = parser.parse_args(argv)

    wallets = collect(args.wallets)
    if args.limit:
        wallets = wallets[: args.limit]
    print(f"Checking {len(wallets)} Bitcoin addresses from {len(args.wallets)} file(s)")
    print("Criterion: total_received > 0 (ever funded), not current balance\n")

    # Controls are checked through the same path as everything else.
    control = [("(control)", None, addr) for _l, addr, _e in CONTROLS]

    results = {}
    if args.results and Path(args.results).is_file():
        results = json.loads(Path(args.results).read_text())
        print(f"resuming: {len(results)} addresses already checked")

    todo = [(f, i, a) for f, i, a in wallets if a not in results]
    todo += [(f, i, a) for f, i, a in control if a not in results]
    batches = [todo[k:k + args.batch_size] for k in range(0, len(todo), args.batch_size)]
    print(f"{len(batches)} batch(es) of up to {args.batch_size}\n")

    for n, batch in enumerate(batches, 1):
        addrs = [a for _, _, a in batch]
        try:
            rows = batch_lookup(addrs)
        except ProviderError as exc:
            print(f"  batch {n}/{len(batches)} FAILED: {exc}")
            print("  stopping. Already-collected results are kept; re-run to resume.")
            break
        missing = [a for a in addrs if a not in rows]
        for f, i, a in batch:
            r = rows.get(a)
            if r is None:
                continue
            results[a] = {
                "source": f, "index": i,
                "total_received": r.get("total_received", 0),
                "final_balance": r.get("final_balance", 0),
                "n_tx": r.get("n_tx", 0),
            }
        print(f"  batch {n}/{len(batches)}: {len(rows)}/{len(addrs)} returned"
              + (f"  ({len(missing)} missing, will retry on re-run)" if missing else ""))
        if args.results:
            Path(args.results).write_text(json.dumps(results), encoding="utf-8")
        if n < len(batches):
            time.sleep(args.pause)

    # ---- controls: prove the pipeline can raise and clear a flag -------------
    ok = True
    print("\n--- controls ---")
    for label, addr, expect_used in CONTROLS:
        row = results.get(addr)
        if row is None:
            print(f"  {label}: MISSING -- results are UNRELIABLE")
            ok = False
            continue
        seen = row["total_received"] > 0
        verdict = "ok" if seen == expect_used else "WRONG"
        print(f"  {label:<22} flagged={str(seen):<5} expected={str(expect_used):<5} {verdict}"
              f"  (received {row['total_received']} sat, balance {row['final_balance']} sat)")
        if seen != expect_used:
            ok = False
    if ok:
        print("  controls pass: detection works in both directions, including the")
        print("  funded-but-swept case that a balance-based check would miss")
    else:
        print("  CONTROLS FAILED: do not trust the report below")

    # ---- cross-verify every candidate against a second provider -------------
    users = [a for a, r in results.items()
             if r["total_received"] > 0 and a not in CONTROL_ADDRESSES]
    confirmed, unconfirmed = [], []
    if users:
        print(f"\n--- cross-checking {len(users)} funded candidate(s) with Blockstream ---")
        for a in users:
            try:
                if esplora_funded(a):
                    confirmed.append(a)
                else:
                    unconfirmed.append(a)
                time.sleep(0.5)
            except Exception as exc:
                print(f"  {a}: cross-check failed ({type(exc).__name__})")
                unconfirmed.append(a)

    # ---- report -------------------------------------------------------------
    print("\n" + "=" * 62)
    print("RESULT")
    print("=" * 62)
    # wallets entries are (source_file, index, address) -- unpack in that order.
    known = [addr for _src, _idx, addr in wallets]
    checked = [a for a in known if a in results]
    if results and len(known) and not checked:
        print("INTERNAL ERROR: results collected but none matched the wallet list")
        return 1
    print(f"addresses checked : {len(checked)}/{len(known)}")
    if not ok:
        print("reliability       : CONTROLS FAILED -- numbers below are not trustworthy")

    never = sum(1 for a in checked if results[a]["total_received"] == 0)
    emptied = sum(1 for a in checked if results[a]["total_received"] > 0
                  and results[a]["final_balance"] == 0)
    live = sum(1 for a in checked if results[a]["final_balance"] > 0)
    print(f"never funded      : {never}")
    print(f"funded then swept : {emptied}")
    print(f"currently holds   : {live}")

    if confirmed:
        print(f"\nCONFIRMED funded addresses ({len(confirmed)}) [verified by 2 providers]:")
        for a in sorted(confirmed, key=lambda x: (results[x]["source"], results[x]["index"] or 0)):
            r = results[a]
            print(f"  {a}  {r['source']} index {r['index']}"
                  f"  received {r['total_received']} sat  balance {r['final_balance']} sat")
    else:
        print("\nNo address was confirmed funded by both providers.")

    if unconfirmed:
        print(f"\nReported funded by one provider only, NOT confirmed ({len(unconfirmed)}):")
        for a in unconfirmed:
            print(f"  {a}  (treat as unverified)")

    unfunded_sample = [a for a in known if a in results and results[a]["total_received"] == 0]
    if len(checked) == len(known) and not confirmed and not unconfirmed and unfunded_sample:
        print(f"\nAll {len(checked)} of your addresses are untouched: none has ever received funds.")

    if args.results:
        Path(args.results).write_text(json.dumps(results), encoding="utf-8")
        print(f"\nRaw results written to {args.results}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
