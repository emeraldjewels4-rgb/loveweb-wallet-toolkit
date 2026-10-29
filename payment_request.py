#!/usr/bin/env python3
"""Build a Bitcoin payment request sheet: addresses + amounts, ready to hand a payer.

    python payment_request.py --wallets wallets-btc-1000-a.json \\
        wallets-btc-1000-b.json wallets-btc-1000-c.json wallets-btc-1000-d.json \\
        --total 1 --out payment-request

Writes two files:

    payment-request.csv   one row per address: n, batch, address, amount_btc,
                          amount_sat, bip21
    payment-request.md    the cover note: totals, batch plan, fee arithmetic

PUBLIC DATA ONLY. This tool never reads a private key or a phrase -- it needs
the addresses, and every address in these exports is public. Nothing it writes
is a secret. But the sheet does collect every address you own in one file, which
links the set together, so treat it the way you would a bank statement: fine to
send the payer, not something to publish.

WHY THE BATCH COLUMN EXISTS

A standard Bitcoin transaction may weigh at most 400,000 weight units
(MAX_STANDARD_TX_WEIGHT), i.e. 100,000 vbytes. Paying a native SegWit address
costs 31 vbytes per output (8 value + 1 script-length + 22 script). So a single
relayable transaction tops out near 3,225 P2WPKH outputs, not 4,000. Ask for
4,000 in one transaction and the payer either gets a non-standard transaction
most nodes will not relay, or silently drops rows. Splitting into batches is not
a nicety here; it is the difference between a request that can be paid and one
that cannot.

Default batch size is 2,000: about 248,000 WU of outputs, which leaves room for
the payer's inputs and a change output inside the standardness limit.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import textwrap
import urllib.parse
from decimal import Decimal, InvalidOperation
from pathlib import Path

SAT = Decimal(1) / Decimal(100_000_000)

# Conservative relay floor. Native SegWit dust is lower (~294 sat), but 546 sat
# is the widely-used limit that is safe for every script type, so a request that
# clears it cannot be rejected as dust by any common node policy.
DUST_FLOOR_SAT = 546

# Recipient addresses here are BIP84 native SegWit on mainnet only.
HRP = "bc"

# Standardness: 400,000 WU = 100,000 vB per transaction; one P2WPKH output is
# 31 vB = 124 WU. Anything above this cannot be relayed by default.
MAX_STANDARD_VBYTES = 100_000
P2WPKH_OUTPUT_VBYTES = 31
MAX_P2WPKH_OUTPUTS = MAX_STANDARD_VBYTES // P2WPKH_OUTPUT_VBYTES  # ~3225

# All bitcoin that will ever exist: 21,000,000 BTC. A request above this is not
# merely ambitious, it is arithmetically unpayable, so it is refused outright.
# Between this and SUPPLY_WARN_SAT it is payable in principle but nobody holds
# it, so the sheet says so on its face rather than looking like an ordinary bill.
TOTAL_SUPPLY_SAT = 21_000_000 * 100_000_000
SUPPLY_WARN_SAT = TOTAL_SUPPLY_SAT // 100  # 1% of supply = 210,000 BTC

CSV_HEADER = ["n", "batch", "address", "amount_btc", "amount_sat", "bip21"]


class RequestError(SystemExit):
    """A request that cannot be paid as specified. Exits 1, never writes."""


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #
def validate_address(address: str) -> None:
    """Raise unless this is a mainnet BIP84 (bc1q) address with a valid checksum."""
    from bip_utils import P2WPKHAddrDecoder

    if not isinstance(address, str) or not address.startswith(HRP + "1"):
        raise RequestError(
            f"Error: {address!r} is not a mainnet native SegWit address "
            f"(expected bc1...). Testnet/legacy addresses do not belong in this request."
        )
    try:
        P2WPKHAddrDecoder().DecodeAddr(address, hrp=HRP)
    except Exception:
        raise RequestError(f"Error: {address!r} failed address validation (bad checksum?).")


def collect(paths: list) -> list:
    """[(source_file, index, address)] in file order. Refuses duplicates.

    A duplicate would mean paying one address twice in a single request, which
    is never intentional and is worth stopping for by name.
    """
    entries, seen = [], {}
    for p in paths:
        path = Path(p)
        if not path.is_file():
            raise RequestError(f"Error: no such wallet file: {path}")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RequestError(f"Error: {path} is not valid JSON ({exc}).")
        if doc.get("encrypted"):
            raise RequestError(
                f"Error: {path} is encrypted; decrypt it first "
                f"(gen_wallets.py --verify-file handles this, but not this tool)."
            )
        for entry in doc.get("wallets", []):
            btc = entry.get("btc")
            if not btc or not btc.get("address"):
                continue
            address = btc["address"]
            validate_address(address)
            if address in seen:
                where = seen[address]
                raise RequestError(
                    f"Error: duplicate address {address} in {path.name} and {where}; "
                    f"refusing to request one address twice."
                )
            seen[address] = path.name
            entries.append((path.name, entry.get("index"), address))
    if not entries:
        raise RequestError("Error: no Bitcoin addresses found in the given files.")
    return entries


# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #
def to_sat(btc) -> int:
    """BTC (str/Decimal/float-free) -> integer satoshis. Refuses sub-satoshi."""
    try:
        value = Decimal(str(btc))
    except InvalidOperation:
        raise RequestError(f"Error: {btc!r} is not a number.")
    if value <= 0:
        raise RequestError(f"Error: amount must be greater than zero (got {btc}).")
    sats = value / SAT
    if sats != sats.to_integral_value():
        raise RequestError(
            f"Error: {btc} BTC is not a whole number of satoshis; the payer's "
            f"wallet cannot express it (1 sat = 0.00000001 BTC)."
        )
    return int(sats)


def split_even(total_sat: int, count: int) -> list:
    """Divide total_sat into count parts; the remainder goes to the first rows.

    The sum is exactly total_sat -- a payment request that does not add up is
    worse than no request.
    """
    if count < 1:
        raise RequestError("Error: nothing to split.")
    base, remainder = divmod(total_sat, count)
    return [base + 1 if i < remainder else base for i in range(count)]


def per_address_sat(amount_btc, count: int) -> list:
    each = to_sat(amount_btc)
    return [each] * count


def check_dust(amounts: list) -> None:
    thin = [a for a in amounts if a < DUST_FLOOR_SAT]
    if not thin:
        return
    raise RequestError(
        f"Error: {len(thin)} of {len(amounts)} rows fall below the dust floor of "
        f"{DUST_FLOOR_SAT} sat ({DUST_FLOOR_SAT * SAT:.8f} BTC). Such an output "
        f"cannot be spent later for less than it holds. Raise the total to at "
        f"least {DUST_FLOOR_SAT * len(amounts)} sat "
        f"({DUST_FLOOR_SAT * len(amounts) * SAT:.8f} BTC) for {len(amounts)} rows."
    )


def check_supply(total_sat: int) -> None:
    """Refuse an ask larger than all bitcoin that will ever exist.

    Asking for more than 21,000,000 BTC is not a large request, it is a
    contradiction: the coins do not exist and no payment can satisfy it. The
    sheet would be worthless to any payer, so it never gets written.
    """
    if total_sat > TOTAL_SUPPLY_SAT:
        raise RequestError(
            f"Error: {total_sat * SAT:.8f} BTC requested, but only "
            f"{TOTAL_SUPPLY_SAT * SAT:.0f} BTC will ever exist. "
            f"Reduce the total or the per-address amount."
        )


def supply_note(total_sat: int) -> list:
    """Payer-facing reality check for asks no wallet could actually fund."""
    if total_sat < SUPPLY_WARN_SAT:
        return []
    share = Decimal(total_sat) / Decimal(TOTAL_SUPPLY_SAT) * 100
    body = (
        f"**Reality check.** {total_sat * SAT:,.8f} BTC is {share:.2f}% of the "
        f"{TOTAL_SUPPLY_SAT * SAT:,.0f} BTC that will ever exist. No wallet has "
        f"ever held anything close to this, so read the figures above as a "
        f"statement of what is being asked for, not as funds that exist. Every "
        f"address in this sheet starts empty: a request is not a deposit."
    )
    # Wrap rather than hand-splitting: the numbers change width with the ask.
    return ["> " + line for line in textwrap.wrap(body, width=76)] + [""]


def check_batches(batch_size: int) -> None:
    if batch_size < 1:
        raise RequestError("Error: --batch-size must be at least 1.")
    if batch_size > MAX_P2WPKH_OUTPUTS:
        raise RequestError(
            f"Error: --batch-size {batch_size} exceeds what one standard "
            f"transaction can carry (~{MAX_P2WPKH_OUTPUTS} P2WPKH outputs at the "
            f"{MAX_STANDARD_VBYTES} vbyte standardness limit). Most nodes would "
            f"refuse to relay it."
        )


# --------------------------------------------------------------------------- #
# Rows and output
# --------------------------------------------------------------------------- #
def bip21(address: str, sats: int, label: str = "") -> str:
    """bitcoin:<addr>?amount=<btc>&label=<text>, per BIP21."""
    amount = f"{sats * SAT:.8f}".rstrip("0").rstrip(".")
    query = {"amount": amount}
    if label:
        query["label"] = label
    return "bitcoin:" + address + "?" + urllib.parse.urlencode(query)


def build_rows(entries: list, amounts: list, batch_size: int, label: str = "",
               with_bip21: bool = True) -> list:
    rows = []
    for i, ((source, index, address), sats) in enumerate(zip(entries, amounts)):
        rows.append({
            "n": i + 1,
            "batch": i // batch_size + 1,
            "address": address,
            "amount_btc": f"{sats * SAT:.8f}",
            "amount_sat": sats,
            "bip21": bip21(address, sats, label) if with_bip21 else "",
            "source": source,
            "index": index,
        })
    return rows


def write_csv(path: Path, rows: list, force: bool) -> None:
    if path.exists() and not force:
        raise RequestError(f"Error: {path} already exists. Use --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" is required by the csv module or Windows adds blank lines.
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def batch_summary(rows: list) -> list:
    out = []
    for row in rows:
        if not out or out[-1]["batch"] != row["batch"]:
            out.append({"batch": row["batch"], "count": 0, "sats": 0})
        out[-1]["count"] += 1
        out[-1]["sats"] += row["amount_sat"]
    return out


def write_cover(path: Path, rows: list, csv_name: str, label: str, force: bool) -> None:
    if path.exists() and not force:
        raise RequestError(f"Error: {path} already exists. Use --force to overwrite.")
    totals = sum(r["amount_sat"] for r in rows)
    batches = batch_summary(rows)
    per = sorted({r["amount_sat"] for r in rows})
    sizes = [b["count"] for b in batches]
    outputs_vb = max(sizes) * P2WPKH_OUTPUT_VBYTES

    lines = [
        "# Bitcoin payment request",
        "",
        f"**Payer note:** {label}" if label else "**Payer note:** (none)",
        "",
        "| | |",
        "|---|---|",
        f"| Addresses | {len(rows):,} |",
        f"| Total requested | **{totals * SAT:,.8f} BTC** ({totals:,} sat) |",
        (
            f"| Amount per address | {per[0] * SAT:,.8f} BTC ({per[0]:,} sat) |"
            if len(per) == 1 else
            f"| Amount per address | {per[0] * SAT:,.8f}–{per[-1] * SAT:,.8f} BTC "
            f"({per[0]:,}–{per[-1]:,} sat, remainder spread over the first rows) |"
        ),
        f"| Transactions needed | {len(batches)} |",
        "",
    ]
    lines += supply_note(totals)
    lines += [
        f"Rows are in `{csv_name}`: `n, batch, address, amount_btc, amount_sat, bip21`.",
        "`bip21` is a `bitcoin:` URI for that exact amount, if your wallet can take one.",
        "",
        "## Send in these batches",
        "",
        "> A standard transaction can weigh at most 100,000 vbytes. A native SegWit",
        f"> output is {P2WPKH_OUTPUT_VBYTES} vbytes, so ~{MAX_P2WPKH_OUTPUTS:,} outputs is the hard ceiling",
        "> per transaction — 4,000 in one transaction would be non-standard and most",
        "> nodes would refuse to relay it. That is why this sheet is split.",
        "",
        "| Batch | Addresses | Amount |",
        "|---|---|---|",
    ]
    for b in batches:
        lines.append(
            f"| {b['batch']} | {b['count']:,} | {b['sats'] * SAT:.8f} BTC |"
        )
    lines += [
        f"| **Total** | **{len(rows):,}** | **{totals * SAT:.8f} BTC** |",
        "",
        "## Fees are on top of the total",
        "",
        f"Each batch is roughly {outputs_vb:,} vbytes of outputs plus the payer's inputs.",
        "For one input and one change output, the fee is about:",
        "",
        "| Fee rate | Per batch | All batches |",
        "|---|---|---|",
    ]
    for rate in (1, 5, 20):
        per_batch = (outputs_vb + 120) * rate
        lines.append(
            f"| {rate} sat/vB | {per_batch:,} sat | {per_batch * len(batches):,} sat "
            f"({per_batch * len(batches) * SAT:.8f} BTC) |"
        )
    lines += [
        "",
        "Sending the total without fee headroom leaves the last batch short; fund the",
        "totals above *plus* the fee, in the sending wallet.",
        "",
        "## Raising or lowering this request",
        "",
        "**An address has no maximum.** A balance is whatever is sent to it; only the",
        "payer's funds and the transaction limits above are real ceilings. So the ask is",
        "a choice, not a computed figure -- this one is "
        f"{totals * SAT:,.8f} BTC total:",
        "",
        "| Total requested | Per address |",
        "|---|---|",
    ]
    for multiple in (Decimal(1), Decimal(10), Decimal(100)):
        target = totals * multiple
        # Never print a multiple the cap would refuse; say why instead of
        # handing the reader a command that exits 1.
        if target > TOTAL_SUPPLY_SAT:
            lines.append(
                f"| {target * SAT:,.8f} BTC | not possible — more than the "
                f"{TOTAL_SUPPLY_SAT * SAT:,.0f} BTC that will ever exist |"
            )
            continue
        each = target / len(rows)
        star = " (this sheet)" if multiple == 1 else ""
        lines.append(f"| {target * SAT:,.8f} BTC{star} | {each * SAT:,.8f} BTC |")
    wallets = " ".join(sorted({r["source"] for r in rows}))
    # Reproduce THIS sheet exactly: a fixed per-address amount when the rows are
    # uniform, otherwise the exact total. A hard-coded example would regenerate
    # a different request than the one the reader is holding.
    if len(per) == 1:
        regen = f"    --amount {per[0] * SAT:.8f} --out payment-request --force"
    else:
        regen = f"    --total {totals * SAT:.8f} --out payment-request --force"
    lines += [
        "",
        "Regenerate exactly this sheet:",
        "",
        "```bash",
        "python payment_request.py \\",
        f"    --wallets {wallets} \\",
        regen,
        "```",
        "",
        "## Before you send",
        "",
        f"- Every row is at least {DUST_FLOOR_SAT:,} sat, so no output is unspendable dust.",
        "- Pay each address once. If a batch is interrupted, resume it rather than",
        "  re-sending the whole batch.",
        "- This sheet contains **no private keys** — only public addresses and amounts.",
        "  The receiving side verifies a payment by address and amount, nothing else.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a CSV + cover note requesting Bitcoin to a set of addresses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--wallets", nargs="+", required=True, help="wallet JSON files")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--total", help="total BTC to request, split evenly")
    group.add_argument("--amount", help="BTC per address")
    parser.add_argument("--out", default="payment-request",
                        help="output stem; writes <stem>.csv and <stem>.md")
    parser.add_argument("--memo", default="", help="note shown to the payer")
    parser.add_argument("--batch-size", type=int, default=2000,
                        help=f"outputs per transaction (default 2000, max ~{MAX_P2WPKH_OUTPUTS})")
    parser.add_argument("--no-bip21", action="store_true",
                        help="omit the bitcoin: URI column")
    parser.add_argument("--force", action="store_true", help="overwrite existing outputs")
    args = parser.parse_args(argv)

    check_batches(args.batch_size)
    entries = collect(args.wallets)

    if args.total is not None:
        amounts = split_even(to_sat(args.total), len(entries))
    else:
        amounts = per_address_sat(args.amount, len(entries))
    check_dust(amounts)
    check_supply(sum(amounts))

    rows = build_rows(entries, amounts, args.batch_size, args.memo,
                      with_bip21=not args.no_bip21)

    stem = Path(args.out)
    csv_path = stem.with_suffix(".csv")
    md_path = stem.with_suffix(".md")
    # Check both up front: writing the CSV and then refusing the cover note would
    # leave a half-written request behind.
    for path in (csv_path, md_path):
        if path.exists() and not args.force:
            raise RequestError(f"Error: {path} already exists. Use --force to overwrite.")
    write_csv(csv_path, rows, args.force)
    write_cover(md_path, rows, csv_path.name, args.memo, args.force)

    total = sum(amounts)
    batches = batch_summary(rows)
    print(f"{len(rows):,} addresses -> {total * SAT:.8f} BTC "
          f"({total:,} sat) in {len(batches)} transaction(s)")
    print(f"  {csv_path}")
    print(f"  {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
