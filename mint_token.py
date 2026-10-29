#!/usr/bin/env python3
"""Mint more ProjectToken: a flat amount, or N successive doublings.

SAFE BY DEFAULT. Without --execute this reads the chain, prints the exact
round-by-round plan (amount, resulting supply, gas, cost) and broadcasts
nothing.

The private key is never taken from the command line: argv is written to shell
history and is visible in the process list. Use --key-file, the
DEPLOYER_PRIVATE_KEY / SOURCE_PRIVATE_KEY environment variable, or the hidden
prompt.

    # plan only: what would three doublings cost? (the mainnet flag is required
    # to PLAN against mainnet too, so a real network can never be reached by a
    # typo in a testnet command -- the same rule deploy_token.py applies)
    python mint_token.py --network polygon --i-know-this-is-mainnet \\
        --deployment deployment-polygon.json --key-file wallets-operator.json --doubles 3

    # broadcast
    python mint_token.py --network polygon --i-know-this-is-mainnet \\
        --deployment deployment-polygon.json --key-file wallets-operator.json \\
        --doubles 3 --execute --yes

    # a flat amount, in as many rounds as you like
    python mint_token.py --network polygon --i-know-this-is-mainnet \\
        --token 0x... --amount 1000000 --rounds 8 --key-file wallets-operator.json

Two things this tool will not do, and both are properties of the contract
rather than cautiousness here:

* **The cap is immutable.** `maxSupply` is fixed at deployment and baked into
  the bytecode, so no key can raise it. Every round is checked against it
  BEFORE anything is signed, and a plan that would breach it is refused with the
  round number and the exact shortfall -- rather than signing a transaction that
  is certain to revert and burning gas on it.
* **Minting is not a split.** An ERC-20 cannot be re-denominated: `decimals` is
  fixed and there is no split function. Doubling the supply halves every
  existing holder's share of the total, so N doublings multiply the supply by
  2**N while each holder's fraction divides by it. If you want holders to hold
  more units at the same fraction, that needs a new contract, not a mint.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import eth_common
# DECIMALS, TOKEN_SCALE, the supply parser and the console guard are imported
# from deploy_token rather than copied: the 28-significant-digit rounding bug in
# the parser was fixed once already, and a second copy would drift. mint_token
# passes its own --amount label, so the error messages still name the right flag.
from deploy_token import (
    DECIMALS,
    MAINNET_FLAG,
    TOKEN_SCALE,
    format_supply,
    make_output_unicode_safe,
    parse_supply,
    redact_rpc,
)

MAX_ROUNDS = 512


# --------------------------------------------------------------------------- #
# Web3 construction (module-level so tests can inject a shared in-memory chain)
# --------------------------------------------------------------------------- #
def build_web3(rpc: str | None, tester: bool = False, tester_keys: list[str] | None = None):
    return eth_common.build_web3(rpc, tester=tester, tester_keys=tester_keys)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def human(wei: int) -> str:
    """Whole-token amount with thousands separators, exactly (no float)."""
    with localcontext() as ctx:
        ctx.prec = 200
        return f"{Decimal(wei) / TOKEN_SCALE:,f}"


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def plan_doubles(supply_wei: int, rounds: int, cap_wei: int) -> list[dict]:
    """Successive doublings: each round mints exactly the supply already there.

    `new_total` is tracked as an exact integer rather than recomputed from
    `2**n * supply`, so the numbers the plan prints are the numbers the contract
    will actually hold after each round.
    """
    plan = []
    current = supply_wei
    for i in range(1, rounds + 1):
        new_total = current + current
        plan.append({"round": i, "amount_wei": current, "new_total_wei": new_total,
                     "fits": new_total <= cap_wei})
        current = new_total
    return plan


def plan_flat(supply_wei: int, amount_wei: int, rounds: int, cap_wei: int) -> list[dict]:
    plan = []
    current = supply_wei
    for i in range(1, rounds + 1):
        new_total = current + amount_wei
        plan.append({"round": i, "amount_wei": amount_wei, "new_total_wei": new_total,
                     "fits": new_total <= cap_wei})
        current = new_total
    return plan


def print_plan(plan: list[dict], cap_wei: int) -> None:
    rows = plan
    if len(rows) > 14:
        rows = plan[:8] + [None] + plan[-3:]
    print(f"  {'round':>5}  {'mint':>26}  {'resulting supply':>26}  fits")
    for row in rows:
        if row is None:
            print(f"  {'...':>5}  {'...':>26}  {'...':>26}")
            continue
        mark = "yes" if row["fits"] else "NO - over cap"
        print(f"  {row['round']:>5}  {human(row['amount_wei']):>26}  "
              f"{human(row['new_total_wei']):>26}  {mark}")
    print(f"  cap: {human(cap_wei)} (immutable)")


# --------------------------------------------------------------------------- #
# Revert decoding
# --------------------------------------------------------------------------- #
def custom_errors(abi: list[dict]) -> dict[bytes, tuple[str, list[str]]]:
    from eth_utils import keccak

    found: dict[bytes, tuple[str, list[str]]] = {}
    for item in abi:
        if item.get("type") == "error":
            types = [i["type"] for i in item.get("inputs", [])]
            signature = f"{item['name']}({','.join(types)})"
            found[keccak(text=signature)[:4]] = (item["name"], types)
    return found


def revert_data(exc: Exception) -> bytes | None:
    """Pull raw revert data out of whichever way the provider reported it.

    A live RPC surfaces a custom error as `exc.data` (a 0x hex string), while
    eth-tester embeds the bytes repr in the message. Both are handled so the
    same message is produced on a local chain and on Polygon.
    """
    data = getattr(exc, "data", None)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    if isinstance(data, str) and data.startswith("0x") and len(data) > 2:
        try:
            return bytes.fromhex(data[2:])
        except ValueError:
            pass
    match = re.search(r"b'((?:\\.|[^'\\])*)'", str(exc))
    if not match:
        return None
    try:
        return ast.literal_eval("b'" + match.group(1) + "'")
    except (ValueError, SyntaxError):
        return None


def decode_revert(abi: list[dict], exc: Exception) -> str | None:
    """'MaxSupplyExceeded(requested=1600000000000000000000000000, cap=...)'."""
    from eth_abi import decode as abi_decode

    data = revert_data(exc)
    if not data or len(data) < 4:
        return None
    entry = custom_errors(abi).get(bytes(data[:4]))
    if entry is None:
        return f"selector 0x{bytes(data[:4]).hex()} (not in this contract's ABI)"
    name, types = entry
    args = ""
    if types:
        try:
            values = abi_decode(types, data[4:])
            args = ", ".join(f"{t}={v}" for t, v in zip(types, values))
        except Exception:
            args = "(arguments could not be decoded)"
    return f"{name}({args})"


# --------------------------------------------------------------------------- #
# Chain reads
# --------------------------------------------------------------------------- #
def read_token(w3, address: str, abi: list[dict], signer: str) -> dict:
    token = w3.eth.contract(address=w3.to_checksum_address(address), abi=abi)
    return {
        "contract": token,
        "name": token.functions.name().call(),
        "symbol": token.functions.symbol().call(),
        "decimals": token.functions.decimals().call(),
        "total_supply": token.functions.totalSupply().call(),
        "max_supply": token.functions.maxSupply().call(),
        "owner": token.functions.owner().call(),
        "is_operator": token.functions.isOperator(signer).call(),
    }


def mint_calldata(to: str, amount_wei: int) -> str:
    """mint(address,uint256) calldata, encoded directly.

    Encoded with eth_abi rather than a web3 helper because the helper's name
    changed between web3.py 6 and 7 (`encodeABI` -> `encode_abi`), and this
    pipeline pins no exact version.
    """
    from eth_abi import encode
    from eth_utils import keccak

    selector = keccak(text="mint(address,uint256)")[:4]
    return "0x" + (selector + encode(["address", "uint256"], [to, amount_wei])).hex()


# --------------------------------------------------------------------------- #
# Record (append-only, so repeated runs form an audit trail)
# --------------------------------------------------------------------------- #
def load_runs(path: Path, header: dict) -> dict:
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("runs"), list):
                return doc
        except json.JSONDecodeError:
            pass
    return {**header, "runs": []}


def save_runs(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint more ProjectToken. Dry run unless --execute.",
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
    source.add_argument("--token", help="address of the deployed token")
    source.add_argument("--deployment", help="a deployment record from deploy_token.py")

    amount = parser.add_mutually_exclusive_group(required=True)
    amount.add_argument("--doubles", type=int,
                        help="mint this many successive doublings of the whole supply")
    amount.add_argument("--amount", help="flat amount per round, in whole tokens")

    parser.add_argument("--rounds", type=int, default=1,
                        help="rounds for --amount (default 1); ignored with --doubles")
    parser.add_argument("--to", help="recipient (default: the signing account)")
    parser.add_argument("--allow-partial", action="store_true",
                        help="with --doubles: execute only the rounds that fit under "
                             "the cap instead of refusing the whole plan")
    parser.add_argument("--gas-limit", type=int, help="override the per-round gas limit")
    parser.add_argument("--out", help="mint record path (default mint-<network>.json)")
    parser.add_argument("--execute", action="store_true", help="broadcast (default: dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    parser.add_argument(MAINNET_FLAG, dest="mainnet_ok", action="store_true",
                        help="required to mint on mainnet")
    parser.add_argument("--tester", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    make_output_unicode_safe()

    artifact_path = Path(args.artifact)
    if not artifact_path.is_file():
        raise SystemExit(f"Error: no artifact at {artifact_path}. Run build_token.py first.")
    abi = json.loads(artifact_path.read_text(encoding="utf-8"))["abi"]

    net = eth_common.get_network(args.network)
    if not net["testnet"] and not args.mainnet_ok:
        raise SystemExit(
            f"Error: refusing to mint real tokens on {net['name']} without {MAINNET_FLAG}.\n"
            "Minting changes the supply for every holder and costs real money. If that\n"
            f"is what you want, add {MAINNET_FLAG}."
        )

    if args.doubles is not None:
        if args.doubles < 1:
            raise SystemExit("Error: --doubles must be at least 1.")
        if args.doubles > MAX_ROUNDS:
            raise SystemExit(f"Error: --doubles is capped at {MAX_ROUNDS}.")
        if args.rounds != 1:
            raise SystemExit("Error: --rounds only applies to --amount, not --doubles.")
    else:
        if args.rounds < 1:
            raise SystemExit("Error: --rounds must be at least 1.")
        if args.rounds > MAX_ROUNDS:
            raise SystemExit(f"Error: --rounds is capped at {MAX_ROUNDS}.")

    fixed_wei = parse_supply(args.amount, "amount") if args.amount is not None else None
    if fixed_wei is not None and fixed_wei == 0:
        raise SystemExit("Error: minting 0 would do nothing. Give a positive --amount.")

    key, signer, key_origin = eth_common.load_private_key(
        key_file=args.key_file,
        key_index=args.key_index,
        password_file=args.password_file,
        label="operator",
    )

    # Work out which token, and cross-check a deployment record against the
    # chain rather than trusting the file.
    record = None
    if args.deployment:
        path = Path(args.deployment)
        if not path.is_file():
            raise SystemExit(f"Error: no deployment record at {path}.")
        record = json.loads(path.read_text(encoding="utf-8"))
        address = record.get("address")
        if not address:
            raise SystemExit(
                f"Error: {path} has no address yet (status '{record.get('status')}').\n"
                "It describes a deployment that never confirmed, so there is no token to mint."
            )
    else:
        address = args.token

    from web3 import Web3

    try:
        address = Web3.to_checksum_address(address)
    except Exception:
        raise SystemExit(f"Error: not a valid address: {address}")

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

    code = w3.eth.get_code(address)
    if len(code) == 0:
        raise SystemExit(
            f"Error: there is no contract code at {address} on chain {chain_id}.\n"
            "Minting would send value-less calldata to an address that cannot answer."
        )

    facts = read_token(w3, address, abi, signer)

    if record is not None:
        for field, on_chain in (("name", facts["name"]), ("symbol", facts["symbol"])):
            if record.get(field) not in (None, on_chain):
                raise SystemExit(
                    f"Error: {args.deployment} says {field} is {record[field]!r}, but the "
                    f"contract at {address} reports {on_chain!r}. Wrong token."
                )
        # A record whose address holds no contract is caught by the code check
        # above, which is the stronger test: it asks the chain, not the file.
        if record.get("chain_id") and record["chain_id"] != chain_id:
            raise SystemExit(
                f"Error: record says chain id {record['chain_id']}, this chain is {chain_id}."
            )

    to = signer
    if args.to:
        try:
            to = Web3.to_checksum_address(args.to)
        except Exception:
            raise SystemExit(f"Error: --to is not a valid address: {args.to}")

    if not facts["is_operator"]:
        raise SystemExit(
            f"Error: {signer} is not an operator of {facts['symbol']}, so the contract\n"
            "would revert with NotOperator(address) -- no gas would be spent, but nothing\n"
            "would be minted either.\n"
            f"  owner    : {facts['owner']}\n"
            f"  signer   : {signer}   (key from {key_origin})\n"
            "Only the owner can grant mint rights (setOperator), so either use the operator\n"
            "key or have the owner add this address first."
        )

    plan = (plan_doubles(facts["total_supply"], args.doubles, facts["max_supply"])
            if args.doubles is not None
            else plan_flat(facts["total_supply"], fixed_wei, args.rounds, facts["max_supply"]))

    fits = [r for r in plan if r["fits"]]
    blocked = [r for r in plan if not r["fits"]]

    print("=" * 68)
    print("MINT PLAN" + ("" if args.execute else "   (DRY RUN - nothing will be sent)"))
    print("=" * 68)
    print(f"network         : {net['name']} (chain id {net['chain_id']})")
    print(f"rpc             : {redact_rpc(rpc)}")
    print(f"token           : {facts['name']} ({facts['symbol']}), {facts['decimals']} decimals")
    print(f"address         : {address}")
    print(f"signer          : {signer}   (key from {key_origin})")
    print(f"mint to         : {to}")
    print(f"current supply  : {human(facts['total_supply'])}")
    print(f"cap             : {human(facts['max_supply'])} (immutable)")
    print(f"headroom        : {human(facts['max_supply'] - facts['total_supply'])}")
    print("-" * 68)
    print_plan(plan, facts["max_supply"])
    print("-" * 68)

    if blocked and not args.allow_partial:
        first = blocked[0]
        print(f"\nREFUSING TO SIGN: round {first['round']} would need "
              f"{human(first['amount_wei'])} and take the supply to "
              f"{human(first['new_total_wei'])}, past the cap of "
              f"{human(facts['max_supply'])}.")
        print(f"Over by {human(first['new_total_wei'] - facts['max_supply'])}.")
        print(f"\n{len(fits)} of {len(plan)} round(s) fit under the cap"
              + (f"; the most they can reach is {human(fits[-1]['new_total_wei'])}." if fits else "."))
        print("\nThe cap is `immutable`: it is baked into the deployed bytecode, so not even\n"
              "the owner can raise it. A contract that can grow past it needs to be\n"
              "deployed with a higher cap; --allow-partial will run just the rounds that fit.")
        return 1

    if blocked:
        print(f"\n--allow-partial would execute the {len(fits)} round(s) that fit and stop "
              f"at {human(fits[-1]['new_total_wei'])}." if fits else
              "\n--allow-partial: none of these rounds fit, so there is nothing to send.")

    if not fits:
        raise SystemExit(
            f"Error: no round fits under the cap. The supply is already "
            f"{human(facts['total_supply'])} of {human(facts['max_supply'])}, so every "
            "round in this plan would revert with MaxSupplyExceeded(uint256,uint256).\n"
            "Nothing was signed and no gas was spent."
        )

    # Cost, against the state that exists now.
    fees = eth_common.fee_params(w3)
    probe = fits[0]
    try:
        estimated = w3.eth.estimate_gas({
            "from": signer, "to": address, "value": 0,
            "data": mint_calldata(to, probe["amount_wei"]),
        })
    except Exception as exc:
        decoded = decode_revert(abi, exc)
        raise SystemExit(
            "Error: the contract refused the first round of this plan during gas\n"
            f"estimation: {decoded or exc}\n"
            "Nothing was signed. Re-check the amount, the recipient and the cap."
        )

    # Every later round is cheaper, not dearer: the first mint to a fresh
    # recipient writes only zero-valued slots (20k each) and the first mint
    # writes totalSupply's slot, while later rounds write slots that already
    # hold a value (5k). The arithmetic itself is constant-time on uint256, so
    # round 1's estimate is the ceiling for the whole run.
    gas_limit = args.gas_limit or int(estimated * 1.25) + 1000
    per_gas = fees.get("maxFeePerGas") or fees.get("gasPrice")
    balance = w3.eth.get_balance(signer)
    total_cost = gas_limit * per_gas * len(fits)

    print(f"fees per round  : {eth_common.describe_fees(fees)}")
    print(f"gas per round   : estimated {estimated}, limit {gas_limit} (round 1 is the ceiling)")
    print(f"rounds to send  : {len(fits)}")
    print(f"balance         : {Web3.from_wei(balance, 'ether')} {net['currency']}")
    print(f"max total cost  : {Web3.from_wei(total_cost, 'ether')} {net['currency']}")
    print(f"supply after    : {human(fits[-1]['new_total_wei'])}")
    print(f"minted in total : {human(fits[-1]['new_total_wei'] - facts['total_supply'])}")

    if balance < total_cost:
        print(f"\nINSUFFICIENT BALANCE: need up to {Web3.from_wei(total_cost, 'ether')} "
              f"{net['currency']}, have {Web3.from_wei(balance, 'ether')} {net['currency']}.")
        return 1

    if not args.execute:
        print("\nDRY RUN COMPLETE. Nothing was broadcast.")
        print("Re-run with --execute to mint.")
        return 0

    if not args.yes:
        print(f"\nAbout to mint {human(fits[-1]['new_total_wei'] - facts['total_supply'])} "
              f"{facts['symbol']} to {to} on {net['name']}.")
        if input("Type MINT (uppercase) to confirm: ").strip() != "MINT":
            print("Aborted. Nothing was minted.")
            return 1

    out = Path(args.out or f"mint-{args.network}.json")
    doc = load_runs(out, {
        "network": args.network,
        "network_name": net["name"],
        "chain_id": chain_id,
        "token": address,
        "name": facts["name"],
        "symbol": facts["symbol"],
        "decimals": facts["decimals"],
        "max_supply": format_supply(facts["max_supply"]),
        "max_supply_wei": str(facts["max_supply"]),
        "in_memory_test_chain": bool(args.tester),
    })
    run = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "completed_at": None,
        "status": "broadcasting",
        "mode": "doubles" if args.doubles is not None else "amount",
        "requested_rounds": len(plan),
        "planned_rounds": len(fits),
        "rounds_skipped_over_cap": len(blocked),
        "allow_partial": bool(args.allow_partial),
        "signer": signer,
        "mint_to": to,
        "start_supply": format_supply(facts["total_supply"]),
        "start_supply_wei": str(facts["total_supply"]),
        "end_supply": None,
        "rounds": [],
    }
    doc["runs"].append(run)

    from eth_account import Account

    account = Account.from_key(key)
    spent = 0

    for index, row in enumerate(fits, start=1):
        data = mint_calldata(to, row["amount_wei"])
        # Re-read the nonce and the fees each round: another transaction may have
        # landed in between, and the base fee moves block by block.
        nonce = w3.eth.get_transaction_count(signer, block_identifier="pending")
        tx = {
            "to": address, "value": 0, "gas": gas_limit, "nonce": nonce,
            "chainId": chain_id, "from": signer, "data": data,
            **eth_common.fee_params(w3),
        }
        signed = account.sign_transaction(tx)
        tx_hash = Web3.to_hex(signed.hash)
        entry = {
            "round": row["round"], "amount": format_supply(row["amount_wei"]),
            "amount_wei": str(row["amount_wei"]),
            "expected_total": format_supply(row["new_total_wei"]),
            "expected_total_wei": str(row["new_total_wei"]),
            "nonce": nonce, "tx_hash": tx_hash, "status": "broadcast",
            "block": None, "gas_used": None,
        }
        run["rounds"].append(entry)
        # Written BEFORE the receipt is fetched: if the poll is rate-limited the
        # hash is already on disk, and this tool never re-sends a round that is
        # already recorded.
        save_runs(out, doc)
        print(f"\n[{index}/{len(fits)}] broadcasting {human(row['amount_wei'])} "
              f"-> {human(row['new_total_wei'])}\n  tx {tx_hash}")
        try:
            w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            entry.update({"status": "rejected", "error": str(exc)[:200]})
            run.update({"status": "failed", "error": f"broadcast rejected: {exc}"})
            save_runs(out, doc)
            raise SystemExit(
                f"Error: the network rejected round {row['round']} ({exc}).\n"
                f"  record: {out}\n"
                "Nothing further was sent. Fix the cause and re-run; already-confirmed\n"
                "rounds are in the record and can be skipped with a smaller --doubles."
            )

        receipt = eth_common.wait_for_receipt(
            w3, signed.hash, net, action="mint these tokens a second time"
        )
        if receipt["status"] != 1:
            entry.update({"status": "reverted", "block": receipt["blockNumber"],
                          "gas_used": receipt["gasUsed"]})
            run.update({"status": "failed",
                        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            save_runs(out, doc)
            raise SystemExit(
                f"Error: round {row['round']} reverted (block {receipt['blockNumber']}).\n"
                f"  {eth_common.explorer_tx(net, tx_hash)}\n"
                f"  record: {out}\n"
                "The rounds before it were mined and are recorded, so the supply is\n"
                "larger than it was when you started. Do not re-run the whole plan."
            )
        entry.update({"status": "success", "block": receipt["blockNumber"],
                      "gas_used": receipt["gasUsed"]})
        spent += receipt["gasUsed"]
        save_runs(out, doc)

    supply_now = facts["contract"].functions.totalSupply().call()
    run.update({
        "status": "success",
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "end_supply": format_supply(supply_now),
        "end_supply_wei": str(supply_now),
        "gas_used_total": spent,
    })
    save_runs(out, doc)

    print("\n" + "=" * 68)
    print(f"MINTED. supply {human(facts['total_supply'])} -> {human(supply_now)}")
    print(f"  minted this run : {human(supply_now - facts['total_supply'])} {facts['symbol']}")
    print(f"  headroom left   : {human(facts['max_supply'] - supply_now)}")
    print(f"  gas used        : {spent} across {len(run['rounds'])} round(s)")
    print(f"  record          : {out}")
    print(f"  token           : {eth_common.explorer_address(net, address)}")

    if supply_now != fits[-1]["new_total_wei"]:
        print(f"\nWARNING: the supply on chain is {human(supply_now)}, but the plan expected\n"
              f"         {human(fits[-1]['new_total_wei'])}. Someone else minted or burned\n"
              "         during this run. Trust the on-chain number and the record.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
