#!/usr/bin/env python3
"""Distribute native ETH from one wallet you own to many destination wallets.

Only works with funds the source key actually holds: native ETH cannot be moved
by an approved spender, and this script holds no approvals.

SAFE BY DEFAULT. Without --execute it is a dry run: it reads balances, builds and
signs every transaction, prints the full cost, and sends nothing.

Typical use:
    # 1. see the plan and cost, send nothing
    python distribute_eth.py --wallets wallets.json --rpc <URL> --amount 0.002

    # 2. actually send
    python distribute_eth.py --wallets wallets.json --rpc <URL> --amount 0.002 --execute

The source private key is never taken from the command line (argv is visible in
process lists and shell history). Use the SOURCE_PRIVATE_KEY env var, a file, or
the interactive prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GAS_LIMIT = 21_000
ENV_KEY_NAME = "SOURCE_PRIVATE_KEY"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def load_recipients(path: Path) -> list[str]:
    """Accept a gen_wallets.py export, a JSON array, or a plain one-per-line list."""
    text = path.read_text(encoding="utf-8").strip()
    addresses: list[str] = []

    if text.startswith("{"):
        doc = json.loads(text)
        for entry in doc.get("wallets", []):
            # gen_wallets.py nests chains; prefer the EVM address.
            if isinstance(entry.get("evm"), dict):
                addresses.append(entry["evm"]["address"])
            elif entry.get("address"):
                addresses.append(entry["address"])
    elif text.startswith("["):
        addresses = [str(a) for a in json.loads(text)]
    else:
        addresses = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if not addresses:
        raise SystemExit(f"Error: no addresses found in {path}")
    return addresses


def load_private_key(args) -> str:
    if args.private_key_file:
        key = Path(args.private_key_file).read_text(encoding="utf-8").strip()
    elif os.environ.get(ENV_KEY_NAME):
        key = os.environ[ENV_KEY_NAME].strip()
    else:
        import getpass

        print(f"No {ENV_KEY_NAME} in the environment; reading the source key now.")
        print("(Input is hidden and is never written to disk.)")
        key = getpass.getpass("Source private key: ").strip()

    if not key:
        raise SystemExit("Error: no source private key provided.")
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        from eth_account import Account

        Account.from_key(key)
    except Exception as exc:
        raise SystemExit(f"Error: that is not a valid private key ({exc}).")
    return key


def build_web3(rpc: str | None, tester: bool):
    import eth_common
    from web3 import Web3

    if tester:
        from web3 import EthereumTesterProvider

        return Web3(EthereumTesterProvider())
    if not rpc:
        raise SystemExit("Error: --rpc is required (or use --tester for a local dry chain).")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    # Polygon and Amoy are proof-of-authority chains. Without this, reading a
    # block raises ExtraDataLengthError, fee_params() swallows it, and the run
    # silently sends LEGACY transactions on a chain that prices EIP-1559.
    eth_common.inject_poa_middleware(w3)
    return w3


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #
def fee_params(w3) -> dict:
    """EIP-1559 where the chain supports it, otherwise legacy gasPrice."""
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas")
        tip = w3.eth.max_priority_fee
        if base is not None and tip is not None:
            # 2x base covers a surge for the next several blocks.
            return {"maxFeePerGas": base * 2 + tip, "maxPriorityFeePerGas": tip}
    except Exception:
        pass
    return {"gasPrice": w3.eth.gas_price}


def describe_fees(fees: dict) -> str:
    if "maxFeePerGas" in fees:
        return (f"EIP-1559  maxFee={fees['maxFeePerGas'] / 1e9:.3f} gwei  "
                f"tip={fees['maxPriorityFeePerGas'] / 1e9:.3f} gwei")
    return f"legacy  gasPrice={fees['gasPrice'] / 1e9:.3f} gwei"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Send native ETH from one wallet to many. Dry run unless --execute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--wallets", required=True, help="recipients: gen_wallets.json / JSON array / list")
    parser.add_argument("--rpc", help="EVM JSON-RPC URL")
    parser.add_argument("--amount", required=True, help="ETH to send to each wallet, e.g. 0.002")
    parser.add_argument("--gas-limit", type=int, default=DEFAULT_GAS_LIMIT)
    parser.add_argument("--private-key-file", help=f"file holding the source key (or set {ENV_KEY_NAME})")
    parser.add_argument("--source-address", help="expected source address; checked against the key")
    parser.add_argument("--results", default="distribution.json", help="audit trail of every tx")
    parser.add_argument("--max-total", help="hard cap on total ETH sent, e.g. 0.5")
    parser.add_argument("--execute", action="store_true", help="actually broadcast (default: dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    parser.add_argument("--retries", type=int, default=1, help="retry failed sends (default 1)")
    parser.add_argument("--no-resume", action="store_true", help="ignore an existing --results file")
    parser.add_argument("--tester", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    from eth_account import Account
    from web3 import Web3

    key = load_private_key(args)
    account = Account.from_key(key)
    source = account.address

    if args.source_address and args.source_address.lower() != source.lower():
        raise SystemExit(
            f"Error: --source-address {args.source_address} does not match the key ({source})."
        )

    try:
        amount_wei = Web3.to_wei(args.amount, "ether")
    except Exception as exc:
        raise SystemExit(f"Error: bad --amount ({exc}).")
    if amount_wei <= 0:
        raise SystemExit("Error: --amount must be greater than zero.")

    raw = load_recipients(Path(args.wallets))
    recipients, seen, self_count = [], set(), 0
    for addr in raw:
        try:
            checksummed = Web3.to_checksum_address(addr)
        except Exception:
            raise SystemExit(f"Error: invalid address in {args.wallets}: {addr}")
        if checksummed.lower() == source.lower():
            self_count += 1
            continue
        if checksummed.lower() in seen:
            continue
        seen.add(checksummed.lower())
        recipients.append(checksummed)

    if not recipients:
        raise SystemExit("Error: no valid recipients after removing duplicates and the source.")

    w3 = build_web3(args.rpc, args.tester)
    chain_id = w3.eth.chain_id
    fees = fee_params(w3)
    per_tx_fee = args.gas_limit * (fees.get("maxFeePerGas") or fees.get("gasPrice"))
    total_value = amount_wei * len(recipients)
    total_needed = total_value + per_tx_fee * len(recipients)
    balance = w3.eth.get_balance(source)
    nonce_start = w3.eth.get_transaction_count(source, block_identifier="pending")

    # ---- resume: never pay twice for a wallet already funded -----------------
    results_path = Path(args.results)
    done: dict[str, dict] = {}
    if results_path.exists() and not args.no_resume:
        try:
            for rec in json.loads(results_path.read_text(encoding="utf-8")).get("transfers", []):
                if rec.get("status") == "success":
                    done[rec["address"].lower()] = rec
        except Exception as exc:
            print(f"Warning: could not read {results_path} ({exc}); starting fresh.\n")

    pending = [a for a in recipients if a.lower() not in done]
    skipped = len(recipients) - len(pending)

    # ---- plan ----------------------------------------------------------------
    print("=" * 68)
    print("PLAN" + ("" if args.execute else "   (DRY RUN - nothing will be sent)"))
    print("=" * 68)
    print(f"source          : {source}")
    print(f"chain id        : {chain_id}")
    print(f"fees            : {describe_fees(fees)}")
    print(f"recipients      : {len(recipients)} unique"
          f"{f'  (+{self_count} source, skipped)' if self_count else ''}")
    if skipped:
        print(f"already funded  : {skipped} (from {results_path}) -> {len(pending)} left")
    print(f"amount each     : {Web3.from_wei(amount_wei, 'ether')} ETH")
    print(f"total to send   : {Web3.from_wei(total_value, 'ether')} ETH")
    print(f"network fees    : {Web3.from_wei(per_tx_fee * len(pending), 'ether')} ETH "
          f"({per_tx_fee * len(pending) / 1e18:.8f})")
    print(f"TOTAL REQUIRED  : {Web3.from_wei(total_value + per_tx_fee * len(pending), 'ether')} ETH")
    print(f"source balance  : {Web3.from_wei(balance, 'ether')} ETH")
    print(f"start nonce     : {nonce_start}")

    if not pending:
        print("\nNothing to do: every recipient is already funded per the results file.")
        return 0

    per_tx_fee_pending = per_tx_fee * len(pending)
    needed = amount_wei * len(pending) + per_tx_fee_pending
    if balance < needed:
        print(f"\nINSUFFICIENT BALANCE: need {Web3.from_wei(needed, 'ether')} ETH, "
              f"have {Web3.from_wei(balance, 'ether')} ETH.")
        print(f"Reduce --amount (current plan sends {Web3.from_wei(total_value, 'ether')} ETH)"
              f" or top up the source wallet.")
        return 1

    if args.max_total:
        cap = Web3.to_wei(args.max_total, "ether")
        if amount_wei * len(pending) > cap:
            print(f"\nABORT: total {Web3.from_wei(amount_wei * len(pending), 'ether')} ETH "
                  f"exceeds --max-total {args.max_total} ETH.")
            return 1

    # ---- sign everything first, so a bad key fails before anything is sent ---
    print(f"\nSigning {len(pending)} transaction(s)...")
    signed = []
    for i, dest in enumerate(pending):
        tx = {
            "to": dest,
            "value": amount_wei,
            "gas": args.gas_limit,
            "nonce": nonce_start + i,
            "chainId": chain_id,
            "from": source,
            **fees,
        }
        signed.append(account.sign_transaction(tx))
    # Web3.to_hex, not HexBytes.hex(): the latter drops the 0x prefix on hexbytes>=2.
    print(f"Signed OK. First tx hash: {Web3.to_hex(signed[0].hash)}")

    if not args.execute:
        print("\nDRY RUN COMPLETE. Nothing was broadcast.")
        print("Re-run with --execute to send.")
        return 0

    # ---- confirm -------------------------------------------------------------
    if not args.yes:
        print(f"\nAbout to send {Web3.from_wei(amount_wei * len(pending), 'ether')} ETH "
              f"to {len(pending)} wallets from {source}.")
        if input('Type SEND (uppercase) to confirm: ').strip() != "SEND":
            print("Aborted. Nothing was sent.")
            return 1

    # ---- broadcast -----------------------------------------------------------
    print("\nBroadcasting...")
    records = list(done.values())
    sent: list[tuple[dict, object]] = []
    for i, (dest, stx) in enumerate(zip(pending, signed)):
        rec = {
            "address": dest,
            "amount_eth": str(Web3.from_wei(amount_wei, "ether")),
            "amount_wei": amount_wei,
            "nonce": nonce_start + i,
            "tx_hash": Web3.to_hex(stx.hash),
            "status": "pending",
        }
        try:
            w3.eth.send_raw_transaction(stx.raw_transaction)
            sent.append((rec, stx.hash))
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = str(exc)
            # A nonce collision means our view was stale; stop rather than pile up.
            print(f"  [{i + 1}/{len(pending)}] FAILED {dest}: {exc}")
            records.append(rec)
            break
        records.append(rec)
        if (i + 1) % 10 == 0 or i + 1 == len(pending):
            print(f"  broadcast {i + 1}/{len(pending)}")

    # ---- receipts ------------------------------------------------------------
    print("\nWaiting for receipts...")
    confirmed = 0
    for rec, tx_hash in sent:
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            rec["block"] = receipt["blockNumber"]
            rec["gas_used"] = receipt["gasUsed"]
            rec["status"] = "success" if receipt["status"] == 1 else "reverted"
            if receipt["status"] == 1:
                confirmed += 1
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = f"receipt: {exc}"

    # ---- retries -------------------------------------------------------------
    failed = [r for r in records if r.get("status") in ("error", "reverted", "pending")]
    for attempt in range(args.retries):
        if not failed:
            break
        print(f"\nRetry {attempt + 1}: {len(failed)} wallet(s)")
        still = []
        for rec in failed:
            # A receipt call can fail AFTER the transaction is mined -- one HTTP
            # 403 from the endpoint is enough. Treating that as "never sent" and
            # re-signing with a fresh nonce pays the recipient a second time, and
            # both transactions succeed because they carry different nonces.
            #
            # So: settle the original first by looking its hash up, and reuse the
            # nonce it was signed with for any resend. Two transactions sharing a
            # nonce cannot both land, so a double payment is impossible here even
            # if this logic is wrong about what happened.
            if rec.get("tx_hash"):
                try:
                    prior = w3.eth.get_transaction_receipt(rec["tx_hash"])
                except Exception:
                    prior = None
                if prior is not None:
                    if prior["status"] == 1:
                        rec.update({"status": "success", "block": prior["blockNumber"],
                                    "gas_used": prior["gasUsed"]})
                        rec.pop("error", None)
                        confirmed += 1
                    else:
                        rec["status"] = "reverted"
                        still.append(rec)
                    continue
            nonce = rec.get("nonce")
            if nonce is None:  # never signed, so any free nonce is correct
                nonce = w3.eth.get_transaction_count(source, block_identifier="pending")
            tx = {"to": rec["address"], "value": amount_wei, "gas": args.gas_limit,
                  "nonce": nonce, "chainId": chain_id, "from": source, **fees}
            try:
                stx = account.sign_transaction(tx)
                w3.eth.send_raw_transaction(stx.raw_transaction)
                rc = w3.eth.wait_for_transaction_receipt(stx.hash, timeout=180)
                if rc["status"] == 1:
                    rec.update({"status": "success", "tx_hash": Web3.to_hex(stx.hash),
                                "block": rc["blockNumber"], "gas_used": rc["gasUsed"]})
                    rec.pop("error", None)
                    confirmed += 1
                else:
                    rec["status"] = "reverted"
                    still.append(rec)
            except Exception as exc:
                # "nonce too low" / "already known" here means the original
                # transfer did land. The receipt lookup settles it on the next
                # attempt, and the shared nonce blocks any second payment.
                rec["status"] = "error"
                rec["error"] = str(exc)
                still.append(rec)
        failed = still

    # ---- report --------------------------------------------------------------
    results_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "chain_id": chain_id,
        "amount_eth_each": str(Web3.from_wei(amount_wei, "ether")),
        "transfers": records,
    }, indent=2) + "\n", encoding="utf-8")

    ok = sum(1 for r in records if r["status"] == "success")
    bad = [r for r in records if r["status"] != "success"]
    print("\n" + "=" * 68)
    print(f"confirmed {ok}/{len(records)} transfers")
    print(f"sent      {Web3.from_wei(amount_wei * ok, 'ether')} ETH")
    if bad:
        print(f"FAILED {len(bad)}:")
        for r in bad[:10]:
            print(f"  {r['address']}  {r['status']}  {r.get('error', '')}")
    print(f"results written to {results_path}")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
