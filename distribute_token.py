#!/usr/bin/env python3
"""Send an ERC-20 token from one wallet to many, one transaction per recipient.

SAFE BY DEFAULT. Without --execute this reads the chain, prints the exact plan
(recipients, tokens each, gas, cost) and broadcasts nothing.

The private key is never taken from the command line: argv is written to shell
history and is visible in the process list. Use --key-file, the
DEPLOYER_PRIVATE_KEY / SOURCE_PRIVATE_KEY environment variable, or the hidden
prompt.

    # split the whole balance across a fresh set of wallets, paying for gas
    python distribute_token.py --network polygon --i-know-this-is-mainnet \\
        --token 0xCF2D6c41340Ae035BA76a581AcB9392D11533B70 \\
        --wallets wallets-500.json --total 1000000000 \\
        --key-file wallets-operator.json --execute --yes

    # or a fixed amount to each, and only the first 50 recipients
    python distribute_token.py ... --amount 2000000 --limit 50

`--spend-all` is the mode for "fund as many wallets as this balance pays for": it
prices each transfer at base+tip rather than the 2x-base ceiling, funds only the
recipients the balance then affords, and reports how many were dropped.

Three things this tool refuses to do, because all three cost real money:

* **Sign a transfer the sender cannot pay gas for.** A token transfer to a
  wallet that has never held the token costs ~58,000 gas, not 21,000, so 500 of
  them is ~29,100,000 gas. That is checked against the balance BEFORE the first
  broadcast, and the tool will say how many recipients the balance does afford.
* **Send twice to the same wallet.** The record is written before each receipt
  is fetched, so an interrupted run resumes without paying again.
* **Send to the sender itself,** or to a duplicate address in the recipient file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import eth_common
from deploy_token import MAINNET_FLAG, make_output_unicode_safe, redact_rpc

UINT256_MAX = 2**256 - 1


# --------------------------------------------------------------------------- #
# Web3 construction (module-level so tests can inject a shared in-memory chain)
# --------------------------------------------------------------------------- #
def build_web3(rpc: str | None, tester: bool = False, tester_keys: list[str] | None = None):
    return eth_common.build_web3(rpc, tester=tester, tester_keys=tester_keys)


# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #
def parse_amount(text: str, decimals: int, label: str) -> int:
    """'2000000' or '2_000_000' -> 2_000_000 * 10**decimals, exactly.

    Decimal, not float, and with a widened context: the default precision is 28
    significant digits, which silently ROUNDS a large whole-token amount once it
    is scaled by 10**18. deploy_token.parse_supply fixed exactly this bug for the
    deploy path; this is the same arithmetic generalised to any decimals, and a
    test asserts the two agree at 18.
    """
    cleaned = text.replace("_", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise SystemExit(f"Error: --{label} is not a number: {text!r}")
    if value < 0:
        raise SystemExit(f"Error: --{label} cannot be negative.")

    with localcontext() as ctx:
        ctx.prec = 200
        scaled = value * (10**decimals)
        if scaled != scaled.to_integral_value():
            raise SystemExit(
                f"Error: --{label} {text} has more than {decimals} decimal places, "
                f"which this token cannot represent."
            )
        units = int(scaled)

    if units > UINT256_MAX:
        raise SystemExit(f"Error: --{label} {text} is larger than a uint256 can hold.")
    return units


def exact_fee_params(w3) -> dict:
    """Price transactions at what they actually pay now: base fee + tip.

    eth_common.fee_params doubles the base fee to ride out a surge, which is the
    right default -- but it reserves roughly twice what a transfer ends up
    costing, and on a 114-transfer run that difference decides how many wallets
    can be funded at all. Capping maxFeePerGas at base + tip means a transaction
    can never cost more than the plan assumed.

    The price of that choice is honest and cheap: if the base fee rises above
    the cap, later transfers are REJECTED rather than silently overpaying. A
    rejection is free -- no gas is spent -- and the record makes the run
    resumable, so the worst case is "run it again later", not "spend more".
    """
    base = w3.eth.get_block("latest").get("baseFeePerGas")
    tip = w3.eth.max_priority_fee
    if base is None or tip is None:
        return {"gasPrice": w3.eth.gas_price}
    return {"maxFeePerGas": base + tip, "maxPriorityFeePerGas": tip}


def transfer_calldata(to: str, value: int) -> str:
    """transfer(address,uint256) calldata, encoded directly.

    eth_abi rather than a web3 helper because the helper's name changed between
    web3.py 6 and 7 (`encodeABI` -> `encode_abi`) and this pipeline pins no
    exact version.
    """
    from eth_abi import encode
    from eth_utils import keccak

    selector = keccak(text="transfer(address,uint256)")[:4]
    return "0x" + (selector + encode(["address", "uint256"], [to, value])).hex()


def human(units: int, decimals: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 200
        return f"{Decimal(units) / (10**decimals):,f}"


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #
def load_recipients(path: Path) -> list[str]:
    """Accept a gen_wallets.py export, a JSON array, or one address per line."""
    if not path.is_file():
        # Without this, a typo in --wallets surfaces as a FileNotFoundError
        # traceback rather than as a refusal anyone can act on.
        raise SystemExit(f"Error: no such recipient file: {path}")
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()

    if not stripped:
        raise SystemExit(f"Error: {path} is empty.")

    addresses: list[str] = []
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Error: {path} is not valid JSON ({exc}).")
        if isinstance(doc, dict) and "wallets" in doc:
            if doc.get("encrypted"):
                raise SystemExit(
                    f"Error: {path} is encrypted. Decrypt it first, or pass a plain "
                    "address list instead -- this tool never needs the keys."
                )
            for entry in doc["wallets"]:
                node = entry.get("evm") if isinstance(entry.get("evm"), dict) else entry
                if node.get("address"):
                    addresses.append(str(node["address"]))
        elif isinstance(doc, list):
            addresses = [str(a) for a in doc]
        else:
            raise SystemExit(f"Error: {path} holds no addresses this tool understands.")
    else:
        addresses = [line.strip() for line in stripped.splitlines()
                     if line.strip() and not line.strip().startswith("#")]

    if not addresses:
        raise SystemExit(f"Error: {path} holds no addresses.")
    return addresses


def clean_recipients(raw: list[str], sender: str, source_label: str):
    """Checksum, drop duplicates, drop the sender. Returns (list, dropped_counts)."""
    from web3 import Web3

    recipients: list[str] = []
    seen: set[str] = set()
    duplicates = self_sends = 0
    for address in raw:
        try:
            checksummed = Web3.to_checksum_address(address)
        except Exception:
            raise SystemExit(f"Error: invalid address in {source_label}: {address}")
        if checksummed.lower() == sender.lower():
            self_sends += 1
            continue
        if checksummed.lower() in seen:
            duplicates += 1
            continue
        seen.add(checksummed.lower())
        recipients.append(checksummed)
    return recipients, duplicates, self_sends


# --------------------------------------------------------------------------- #
# Record (resumable)
# --------------------------------------------------------------------------- #
def load_record(path: Path) -> dict:
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("transfers"), list):
                return doc
        except json.JSONDecodeError:
            pass
    return {"transfers": []}


def save_record(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


NOT_PAID = {"rejected", "reverted"}


def completed_addresses(doc: dict) -> set[str]:
    """Addresses a transaction was broadcast to, so a resume never pays twice.

    Deliberately broader than "status == success". A run interrupted between the
    broadcast and the receipt leaves an entry that may well have been mined -- a
    public RPC answering 403 mid-poll is exactly how that happens -- and treating
    it as unpaid would send a second transfer to that wallet. Only a REJECTED or
    REVERTED transaction is known not to have paid.
    """
    return {str(entry.get("to", "")).lower()
            for entry in doc.get("transfers", [])
            if entry.get("status") not in NOT_PAID}


def reconcile_broadcast(w3, doc: dict, sender: str, results_path: Path) -> list[dict]:
    """Resolve entries that were broadcast but never confirmed.

    Each is settled against the chain: a receipt decides it outright, and when
    there is none, the sender's nonce does -- a nonce that has moved past the
    entry means that transaction was mined or replaced, so it must not be sent
    again. Anything still genuinely pending is left alone for the same reason,
    and reported rather than resent.

    Returns a short report so the caller can say what happened instead of
    silently changing the plan.
    """
    stale = [e for e in doc.get("transfers", []) if e.get("status") == "broadcast"]
    if not stale:
        return []

    mined_count = w3.eth.get_transaction_count(sender)
    report: list[dict] = []
    for entry in stale:
        try:
            receipt = w3.eth.get_transaction_receipt(entry["tx_hash"])
        except Exception:
            receipt = None

        if receipt is not None:
            if receipt["status"] == 1:
                entry.update({"status": "success", "block": receipt["blockNumber"],
                              "gas_used": receipt["gasUsed"], "reconciled": True})
                outcome = "confirmed on chain"
            else:
                entry.update({"status": "reverted", "block": receipt["blockNumber"],
                              "gas_used": receipt["gasUsed"], "reconciled": True})
                outcome = "reverted, so it can be sent again"
        elif int(entry.get("nonce", 0)) < mined_count:
            entry.update({"status": "mined_no_receipt", "reconciled": True})
            outcome = "nonce has moved past it, so it was mined; not resent"
        else:
            entry.update({"status": "unresolved", "reconciled": True})
            outcome = "still pending; not resent, to avoid paying twice"

        report.append({"index": entry["index"], "to": entry["to"], "outcome": outcome})

    save_record(results_path, doc)
    return report


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Send an ERC-20 token from one wallet to many. Dry run unless --execute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--artifact", default="build/ProjectToken.json",
                        help="compiled artifact from build_token.py (for its ABI)")
    eth_common.add_network_arg(parser, default="amoy")
    parser.add_argument("--rpc", help="EVM JSON-RPC URL (has a public default on Amoy)")
    parser.add_argument("--key-file", help="bare key file, or a gen_wallets.py JSON export")
    parser.add_argument("--key-index", type=int, default=0,
                        help="which wallet to use when --key-file is an export (default 0)")
    parser.add_argument("--password-file", help="password for an encrypted key export")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token", help="address of the token to send")
    source.add_argument("--deployment", help="a deployment record from deploy_token.py")

    parser.add_argument("--wallets", required=True,
                        help="recipients: gen_wallets.py export, JSON array, or one per line")
    amount = parser.add_mutually_exclusive_group(required=True)
    amount.add_argument("--amount", help="tokens to send to EACH recipient")
    amount.add_argument("--total", help="tokens to split evenly across all recipients")

    parser.add_argument("--limit", type=int,
                        help="only the first N recipients (after cleaning)")
    parser.add_argument("--max-total", help="hard cap on the total tokens sent")
    parser.add_argument("--gas-limit", type=int,
                        help="gas limit per transfer (default: estimated, plus headroom)")
    parser.add_argument("--results", help="audit record (default distribution-token-<network>.json)")
    parser.add_argument("--execute", action="store_true", help="broadcast (default: dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    parser.add_argument("--no-resume", action="store_true",
                        help="ignore an existing --results file and send to everyone again")
    parser.add_argument("--spend-all", action="store_true",
                        help="price fees at base+tip instead of the 2x-base ceiling, and "
                             "fund only as many recipients as the balance then affords")
    parser.add_argument(MAINNET_FLAG, dest="mainnet_ok", action="store_true",
                        help="required to send on mainnet")
    parser.add_argument("--tester", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    make_output_unicode_safe()

    net = eth_common.get_network(args.network)
    if not net["testnet"] and not args.mainnet_ok:
        raise SystemExit(
            f"Error: refusing to send real tokens on {net['name']} without {MAINNET_FLAG}.\n"
            "Every transfer costs real money and cannot be undone. If that is what you\n"
            f"want, add {MAINNET_FLAG}."
        )

    artifact_path = Path(args.artifact)
    if not artifact_path.is_file():
        raise SystemExit(f"Error: no artifact at {artifact_path}. Run build_token.py first.")
    abi = json.loads(artifact_path.read_text(encoding="utf-8"))["abi"]

    key, sender, key_origin = eth_common.load_private_key(
        key_file=args.key_file,
        key_index=args.key_index,
        password_file=args.password_file,
        label="source",
    )

    record = None
    if args.deployment:
        path = Path(args.deployment)
        if not path.is_file():
            raise SystemExit(f"Error: no deployment record at {path}.")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not record.get("address"):
            raise SystemExit(f"Error: {path} has no address yet, so there is no token to send.")
        address = record["address"]
    else:
        address = args.token

    from web3 import Web3

    try:
        address = Web3.to_checksum_address(address)
    except Exception:
        raise SystemExit(f"Error: not a valid address: {address}")

    raw = load_recipients(Path(args.wallets))
    recipients, duplicates, self_sends = clean_recipients(raw, sender, args.wallets)
    if not recipients:
        raise SystemExit("Error: no valid recipients after removing duplicates and the sender.")
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("Error: --limit must be at least 1.")
        recipients = recipients[: args.limit]

    rpc = eth_common.resolve_rpc(args)
    if rpc is None and not args.tester:
        raise SystemExit(
            f"Error: no RPC for {net['name']}. Pass --rpc <URL> "
            "(Alchemy, Infura, QuickNode, or any JSON-RPC provider)."
        )

    w3 = build_web3(rpc, tester=args.tester, tester_keys=[key] if args.tester else None)
    chain_id = w3.eth.chain_id
    if not args.tester and chain_id != net["chain_id"]:
        raise SystemExit(
            f"Error: that RPC reports chain id {chain_id}, but --network {args.network} "
            f"expects {net['chain_id']}. Wrong endpoint."
        )

    if len(w3.eth.get_code(address)) == 0:
        raise SystemExit(f"Error: there is no contract code at {address} on chain {chain_id}.")

    token = w3.eth.contract(address=address, abi=abi)
    name = token.functions.name().call()
    symbol = token.functions.symbol().call()
    decimals = token.functions.decimals().call()

    if record is not None:
        for field, on_chain in (("name", name), ("symbol", symbol)):
            if record.get(field) not in (None, on_chain):
                raise SystemExit(
                    f"Error: {args.deployment} says {field} is {record[field]!r}, but the "
                    f"contract at {address} reports {on_chain!r}. Wrong token."
                )
        if record.get("chain_id") and record["chain_id"] != chain_id:
            raise SystemExit(
                f"Error: record says chain id {record['chain_id']}, this chain is {chain_id}."
            )

    if args.amount is not None:
        each = parse_amount(args.amount, decimals, "amount")
    else:
        total_wanted = parse_amount(args.total, decimals, "total")
        each = total_wanted // len(recipients)
        if each == 0:
            raise SystemExit(
                f"Error: --total {args.total} split across {len(recipients)} recipients is less "
                "than one base unit each."
            )
    fee_fn = exact_fee_params if args.spend_all else eth_common.fee_params
    fees = fee_fn(w3)
    per_gas = fees.get("maxFeePerGas") or fees.get("gasPrice")
    pol = w3.eth.get_balance(sender)

    # In default mode the token balance is checked against the full plan right
    # away: that gives a far better message than the contract's own
    # ERC20InsufficientBalance revert would, coming out of the gas probe below.
    # In --spend-all mode the recipient count is not decided yet -- the trim does
    # that -- so it is checked once the count is final.
    held = token.functions.balanceOf(sender).call()
    if not args.spend_all:
        total = each * len(recipients)
        if args.max_total:
            cap = parse_amount(args.max_total, decimals, "max-total")
            if total > cap:
                raise SystemExit(
                    f"Error: this would send {human(total, decimals)} {symbol}, over the "
                    f"--max-total cap of {human(cap, decimals)}.\n"
                    "Lower --amount/--total, or raise --max-total deliberately."
                )
        if held < total:
            raise SystemExit(
                f"Error: {sender} holds {human(held, decimals)} {symbol} but this plan "
                f"sends {human(total, decimals)}.\n"
                f"The most that can go to {len(recipients)} recipients is "
                f"{human(held // len(recipients), decimals)} each."
            )

    # The record is loaded (and reconciled) BEFORE the gas estimate, because the
    # estimate is checked against what earlier transfers of this exact shape
    # really cost -- see the floor applied below.
    results_path = Path(args.results or f"distribution-token-{args.network}.json")
    doc = {"transfers": []} if args.no_resume else load_record(results_path)
    reconciled = [] if args.no_resume else reconcile_broadcast(w3, doc, sender, results_path)
    for item in reconciled:
        print(f"reconciled #{item['index']:<3} {item['to']}  {item['outcome']}")
    done = set() if args.no_resume else completed_addresses(doc)
    pending = [a for a in recipients if a.lower() not in done]
    if not pending:
        print(f"Nothing to do: all {len(recipients)} recipient(s) are already recorded as "
              f"paid in {results_path}.")
        return 0

    try:
        estimated = w3.eth.estimate_gas({"from": sender, "to": address, "value": 0,
                                         "data": transfer_calldata(pending[0], each)})
    except Exception as exc:
        raise SystemExit(
            f"Error: the token refused a transfer to {pending[0]} during gas "
            f"estimation: {exc}\nNothing was signed. Check the balance and the amount."
        )
    # A single probe is NOT a trustworthy gas limit for a batch. A public RPC
    # estimated 39,921 for a transfer that actually needs 58,303 -- it priced the
    # recipient's balance slot as though it were already funded -- and the
    # transaction then reverted OUT OF GAS on the real chain, burning the whole
    # limit. The record is a better witness: it holds the gas_used of transfers to
    # wallets of exactly this shape, so the highest one seen becomes a floor.
    observed = [int(e["gas_used"]) for e in doc.get("transfers", []) if e.get("gas_used")]
    if observed and max(observed) > estimated:
        print(f"note: this record's earlier transfers used up to {max(observed):,} gas, "
              f"above the {estimated:,} the node estimated -- using {max(observed):,}")
        estimated = max(observed)

    gas_limit = args.gas_limit or int(estimated * 1.15) + 1000
    if args.gas_limit is not None and gas_limit < estimated:
        # An explicit limit below the measured cost produces a transaction that
        # cannot possibly succeed: it burns the intrinsic gas and mints nothing.
        # Better to refuse here than to sign it.
        raise SystemExit(
            f"Error: --gas-limit {args.gas_limit} is below the {estimated} gas this "
            "transfer actually needs.\n"
            "It would fail out of gas. Omit --gas-limit to use the measured cost plus "
            "headroom."
        )
    worst_cost = gas_limit * per_gas * len(recipients)

    # --spend-all funds as many recipients as the balance pays for, priced at what
    # NOTE the trim runs BEFORE the totals below are computed and checked, because
    # trimming is what makes an otherwise-unaffordable plan valid. Checking the
    # token balance first would refuse a plan that this mode can legitimately
    # carry out in part.
    # a transfer actually costs. The trim happens before the plan is printed, so
    # the numbers shown are the numbers that will be sent.
    trimmed_from = None
    trimmed_by = None
    if args.spend_all and pending:
        # Two separate limits, and the smaller one wins: POL pays for the gas, and
        # the token balance is what actually funds the transfers. A resumed run
        # usually hits the token limit -- earlier transfers already spent tokens.
        by_pol = int(pol // (gas_limit * per_gas))
        by_tokens = held // each if each else 0
        affordable = min(by_pol, by_tokens, len(pending))
        if affordable < 1:
            raise SystemExit(
                f"Error: nothing can be sent from this wallet at the current "
                f"price.\n  one transfer costs up to "
                f"{Web3.from_wei(gas_limit * per_gas, 'ether')} {net['currency']} "
                f"(holds {Web3.from_wei(pol, 'ether')})\n  and one transfer of "
                f"{human(each, decimals)} {symbol} needs that much {symbol} "
                f"(holds {human(held, decimals)}).\n"
                f"Send {net['currency']} to {sender}, or top the wallet up with {symbol}."
            )
        if affordable < len(pending):
            trimmed_from = len(pending)
            trimmed_by = ("POL for gas" if by_pol <= by_tokens else f"the {symbol} balance")
            pending = pending[:affordable]
            keep = {a.lower() for a in pending}
            recipients = [a for a in recipients if a.lower() in keep]
            total = each * len(recipients)
            worst_cost = gas_limit * per_gas * len(recipients)

    total = each * len(recipients)
    if total == 0:
        raise SystemExit("Error: that would send nothing.")

    if args.max_total:
        cap = parse_amount(args.max_total, decimals, "max-total")
        if total > cap:
            raise SystemExit(
                f"Error: this would send {human(total, decimals)} {symbol}, over the "
                f"--max-total cap of {human(cap, decimals)}.\n"
                "Lower --amount/--total, or raise --max-total deliberately."
            )

    if held < total:
        raise SystemExit(
            f"Error: {sender} holds {human(held, decimals)} {symbol} but this plan sends "
            f"{human(total, decimals)}.\n"
            f"The most that can go to {len(recipients)} recipients is "
            f"{human(held // len(recipients), decimals)} each."
        )

    print("=" * 68)
    print("DISTRIBUTION PLAN" + ("" if args.execute else "   (DRY RUN - nothing will be sent)"))
    print("=" * 68)
    print(f"network         : {net['name']} (chain id {net['chain_id']})")
    print(f"rpc             : {redact_rpc(rpc)}")
    print(f"token           : {name} ({symbol}), {decimals} decimals")
    print(f"address         : {address}")
    print(f"from            : {sender}   (key from {key_origin})")
    print(f"source balance  : {human(held, decimals)} {symbol}, "
          f"{Web3.from_wei(pol, 'ether')} {net['currency']}")
    print("-" * 68)
    print(f"recipients      : {len(recipients)}" +
          (f" (from {len(raw)} entries; {duplicates} duplicate(s), "
           f"{self_sends} self-send(s) dropped)" if duplicates or self_sends else ""))
    if args.limit is not None and len(raw) > len(recipients):
        print(f"                  limited to the first {len(recipients)} by --limit")
    if trimmed_from:
        print(f"                  TRIMMED by --spend-all from {trimmed_from} to "
              f"{len(pending)}, limited by {trimmed_by}: "
              f"{trimmed_from - len(pending)} wallet(s) get nothing this run")
    if done:
        print(f"already paid    : {len(done)} (resumed from {results_path}); "
              f"{len(pending)} still to send")

    print(f"each receives   : {human(each, decimals)} {symbol}")
    print(f"total to send   : {human(total, decimals)} {symbol}")
    print(f"left in wallet  : {human(held - total, decimals)} {symbol}")
    print("-" * 68)
    print(f"fees            : {eth_common.describe_fees(fees)}")
    print(f"gas per transfer: estimated {estimated}, limit {gas_limit} "
          "(a first-time recipient is the expensive case)")
    print(f"gas for {len(pending):>4}      : {gas_limit * len(pending):,}"
          f"  = {Web3.from_wei(worst_cost, 'ether')} {net['currency']} "
          f"at {per_gas / 1e9:.3f} gwei")
    print(f"available {net['currency']:<7} : {Web3.from_wei(pol, 'ether')}")

    if pending and gas_limit * per_gas * len(pending) > pol:
        affordable = int(pol // (gas_limit * per_gas))
        needed = gas_limit * per_gas * len(pending)
        print(f"\nINSUFFICIENT {net['currency']} FOR GAS: {len(pending)} transfer(s) need up to "
              f"{Web3.from_wei(needed, 'ether')}, "
              f"the wallet holds {Web3.from_wei(pol, 'ether')} "
              f"-- short by {Web3.from_wei(needed - pol, 'ether')} {net['currency']}.")
        if affordable >= 1:
            print(f"The balance affords about {affordable} transfer(s). "
                  f"Send {net['currency']} to {sender}, or use --limit {affordable}.")
        else:
            print(f"It cannot afford even one transfer at this gas price. Send "
                  f"{net['currency']} to {sender} first.")
        return 1

    if not args.execute:
        print(f"\nDRY RUN COMPLETE. Nothing was broadcast. "
              f"{human(total, decimals)} {symbol} would move to {len(recipients)} wallet(s).")
        print("Re-run with --execute to send.")
        return 0

    if not args.yes:
        print(f"\nAbout to send {human(each, decimals)} {symbol} to {len(pending)} "
              f"wallet(s) on {net['name']}.")
        if input("Type SEND (uppercase) to confirm: ").strip() != "SEND":
            print("Aborted. Nothing was sent.")
            return 1

    from eth_account import Account

    account = Account.from_key(key)
    sent = 0

    for index, to in enumerate(pending, start=1):
        # Re-read the balance, nonce and fees each round: gas prices move block to
        # block, and stopping cleanly here is free -- a failed send is not.
        nonce = w3.eth.get_transaction_count(sender, block_identifier="pending")
        this_fees = fee_fn(w3)  # same pricing rule the plan used, re-read per round
        need = gas_limit * (this_fees.get("maxFeePerGas") or this_fees.get("gasPrice"))
        balance = w3.eth.get_balance(sender)
        if balance < need:
            save_record(results_path, doc)
            raise SystemExit(
                f"Error: ran out of {net['currency']} for gas after {sent} transfer(s):\n"
                f"  need up to {Web3.from_wei(need, 'ether')} for the next one, "
                f"hold {Web3.from_wei(balance, 'ether')}.\n"
                f"  {results_path} records what was already sent, so a later run resumes\n"
                "  from here rather than paying anyone twice. Top up and re-run."
            )
        tx = {
            "to": address, "value": 0, "gas": gas_limit, "nonce": nonce,
            "chainId": chain_id, "from": sender, "data": transfer_calldata(to, each),
            **this_fees,
        }
        signed = account.sign_transaction(tx)
        tx_hash = Web3.to_hex(signed.hash)
        entry = {"index": index, "to": to, "amount": human(each, decimals),
                 "amount_wei": str(each), "nonce": nonce, "tx_hash": tx_hash,
                 "status": "broadcast", "block": None, "gas_used": None}
        doc["transfers"].append(entry)
        # Written before the receipt is fetched, so an interrupted or
        # rate-limited run never loses a hash that already cost gas.
        save_record(results_path, doc)

        try:
            w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            entry.update({"status": "rejected", "error": str(exc)[:200]})
            save_record(results_path, doc)
            raise SystemExit(
                f"Error: the network rejected the transfer to {to} ({exc}).\n"
                f"  record: {results_path}\n"
                "Nothing further was sent. Fix the cause and re-run to resume."
            )

        receipt = eth_common.wait_for_receipt(
            w3, signed.hash, net, action="send these tokens a second time"
        )
        if receipt["status"] != 1:
            entry.update({"status": "reverted", "block": receipt["blockNumber"],
                          "gas_used": receipt["gasUsed"]})
            save_record(results_path, doc)
            out_of_gas = receipt["gasUsed"] >= gas_limit
            raise SystemExit(
                f"Error: the transfer to {to} reverted (block {receipt['blockNumber']}).\n"
                + (f"  it used its entire {gas_limit:,} gas limit, so it ran OUT OF GAS:\n"
                   f"  the {estimated:,} estimate was too low for this recipient.\n"
                   if out_of_gas else
                   "  it had gas left, so the contract refused the transfer itself.\n")
                + f"  {eth_common.explorer_tx(net, tx_hash)}\n"
                f"  record: {results_path}\n"
                "Earlier transfers were mined and are recorded. Do not restart from zero."
            )
        entry.update({"status": "success", "block": receipt["blockNumber"],
                      "gas_used": receipt["gasUsed"]})
        save_record(results_path, doc)
        sent += 1
        if index % 25 == 0 or index == len(pending):
            print(f"  [{index}/{len(pending)}] sent to {to} "
                  f"(block {receipt['blockNumber']})")

    doc.setdefault("runs", []).append({
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": args.network,
        "chain_id": chain_id,
        "token": address,
        "symbol": symbol,
        "from": sender,
        "sent": sent,
        "each": human(each, decimals),
        "total": human(each * sent, decimals),
        "gas_used_total": sum(int(t["gas_used"] or 0) for t in doc["transfers"]),
    })
    save_record(results_path, doc)

    print("\n" + "=" * 68)
    print(f"SENT. {human(each * sent, decimals)} {symbol} to {sent} wallet(s).")
    print(f"  left in wallet : {human(token.functions.balanceOf(sender).call(), decimals)}")
    print(f"  POL left       : {Web3.from_wei(w3.eth.get_balance(sender), 'ether')}")
    print(f"  record         : {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
