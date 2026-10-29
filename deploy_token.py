#!/usr/bin/env python3
"""Deploy the compiled ProjectToken to Polygon (Amoy testnet or PoS mainnet).

SAFE BY DEFAULT. Without --execute it reads the chain, builds and signs the
deployment transaction, prints the exact cost, and broadcasts nothing.

The private key is never taken from the command line: argv is written to shell
history and is visible in the process list. Use --key-file, the
DEPLOYER_PRIVATE_KEY environment variable, or the hidden prompt.

    # 1. plan only, against Amoy
    python deploy_token.py --network amoy --key-file wallets-operator.json \\
        --name "My Token" --symbol MTK --initial-supply 1000000 --max-supply 100000000

    # 2. broadcast
    python deploy_token.py --network amoy --key-file wallets-operator.json \\
        --name "My Token" --symbol MTK --initial-supply 1000000 --max-supply 100000000 \\
        --execute

    # 3. verify the source on Polygonscan
    python verify_token.py --network amoy

By default the deployer is BOTH the owner and the operator. Use --owner and
--operator to split those roles: deploying from a hot "operator wallet" while a
cold wallet holds ownership is the safer arrangement, and the only one where
revoking a leaked deployer key is possible without touching the token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import eth_common

DECIMALS = 18
TOKEN_SCALE = 10**DECIMALS
ZERO_ADDRESS = "0x" + "0" * 40
UINT256_MAX = 2**256 - 1

MAINNET_FLAG = "--i-know-this-is-mainnet"


def wait_for_receipt(w3, tx_hash, net, timeout: int = 300, attempts: int = 3,
                    retry_delay: int = 5):
    """Fetch a receipt without letting one flaky endpoint end the run.

    A public RPC can rate-limit mid-poll (HTTP 403 was observed from a public
    Polygon endpoint, immediately after a successful broadcast). By that point
    the transaction is already sent and the gas is already committed, so this
    must never surface as a traceback: it retries, and if it still cannot get an
    answer it says what to check -- because re-running with --execute would
    deploy a SECOND contract rather than recover the first.
    """
    from web3 import Web3

    last = None
    for attempt in range(1, attempts + 1):
        try:
            return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        except Exception as exc:
            last = exc
            print(f"  receipt poll {attempt}/{attempts} failed: "
                  f"{type(exc).__name__}: {str(exc)[:90]}")
            if attempt < attempts:
                time.sleep(retry_delay)

    shown = Web3.to_hex(tx_hash)
    raise SystemExit(
        f"Error: the transaction WAS BROADCAST, but its receipt could not be read "
        f"({type(last).__name__}).\n"
        f"  tx hash: {shown}\n"
        f"  check  : {eth_common.explorer_tx(net, shown)}\n"
        "Do NOT re-run with --execute: that would deploy a second contract.\n"
        "A record was already written naming this transaction, so once it shows as\n"
        "confirmed you can recover the details from it and verify with:\n"
        f"  python verify_token.py --network {net.get('slug', 'polygon')} "
        "--deployment <record>"
    )


# --------------------------------------------------------------------------- #
# Supplies
# --------------------------------------------------------------------------- #
def make_output_unicode_safe() -> None:
    """Stop a non-ASCII token name or symbol from crashing the run.

    Windows defaults to a legacy code page (cp1252 here). Printing a symbol
    like a heart to a redirected or piped stream raises UnicodeEncodeError
    part-way through the plan, which aborts the deployment before anything is
    signed. backslashreplace keeps the run alive and prints \\u2764
    unambiguously rather than a question mark, and cannot change what is
    signed -- the name and symbol are taken from the arguments, not from this
    output.
    """
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:  # pragma: no cover - stdout is not reconfigurable
        pass


def parse_supply(text: str, label: str) -> int:
    """'1000000' or '1_000_000' -> 1000000 * 10**18 wei, exactly.

    Decimal, not float: float would round a large supply silently.
    """
    cleaned = text.replace("_", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise SystemExit(f"Error: --{label} is not a number: {text!r}")
    if value < 0:
        raise SystemExit(f"Error: --{label} cannot be negative.")

    # Decimal's default context carries only 28 significant digits, so this
    # multiply would silently ROUND a large supply (30 digits of tokens is 48
    # digits of wei). A uint256 is at most 78 digits, so 200 is far more than
    # any value the contract could ever hold, which makes the scaling exact.
    with localcontext() as ctx:
        ctx.prec = 200
        wei = value * TOKEN_SCALE
        if wei != wei.to_integral_value():
            raise SystemExit(
                f"Error: --{label} {text} has more than {DECIMALS} decimal places, "
                "which a token with 18 decimals cannot represent."
            )
        wei_int = int(wei)

    if wei_int > UINT256_MAX:
        raise SystemExit(
            f"Error: --{label} {text} is larger than a uint256 can hold. The largest "
            "representable supply is about 1.15e59 tokens."
        )
    return wei_int


def format_supply(wei: int) -> str:
    whole = Decimal(wei) / TOKEN_SCALE
    return f"{whole:f}"


# --------------------------------------------------------------------------- #
# Constructor
# --------------------------------------------------------------------------- #
def constructor_abi(artifact: dict) -> dict:
    for item in artifact["abi"]:
        if item.get("type") == "constructor":
            return item
    raise SystemExit("Error: the artifact has no constructor in its ABI.")


def encode_constructor_args(artifact: dict, values: list) -> str:
    """ABI-encode the constructor arguments. This exact hex is what Polygonscan
    needs, and getting it wrong is the most common verification failure."""
    from eth_abi import encode

    types = [i["type"] for i in constructor_abi(artifact).get("inputs", [])]
    if len(types) != len(values):
        raise SystemExit(
            f"Error: constructor takes {len(types)} argument(s), {len(values)} provided."
        )
    return encode(types, values).hex()


def redact_rpc(url: str | None) -> str:
    """RPC URLs often carry an API key in the path; never print the whole thing."""
    if not url:
        return "(in-memory chain)"
    match = re.match(r"^(https?://[^/]+)(/.*)?$", url)
    return match.group(1) + ("/..." if match and match.group(2) else "") if match else "(set)"


# --------------------------------------------------------------------------- #
# Runtime code check
# --------------------------------------------------------------------------- #
def compare_code(onchain_hex: str, artifact: dict, immutables: dict[str, int]) -> tuple[bool, str]:
    """Compare the chain's runtime code against the artifact.

    solc leaves immutable slots as ZEROS in `deployedBytecode` -- the real values
    are patched into the runtime code by the constructor at deployment time. So
    the chain's code is expected to differ from the artifact exactly where those
    slots are, and nowhere else. Any other difference means the code on chain is
    not the code that was built, and Polygonscan would reject it.
    """
    # Accept either form: HexBytes.hex() drops the 0x prefix on hexbytes>=2,
    # and stripping two characters there would delete a real byte and shift
    # every offset by one (the existing distribute_eth.py warns about the same
    # trap). Web3.to_hex is the reliable accessor.
    chain = bytes.fromhex(onchain_hex[2:] if onchain_hex.startswith("0x") else onchain_hex)
    built = bytes.fromhex(artifact["deployed_bytecode"][2:])

    if len(chain) != len(built):
        return False, f"length differs: chain {len(chain)} bytes, artifact {len(built)} bytes"

    diff = [i for i in range(len(chain)) if chain[i] != built[i]]
    if not diff:
        return True, f"identical to the artifact ({len(built)} bytes)"

    covered: set[int] = set()
    for value in immutables.values():
        word = value.to_bytes(32, "big")
        start = chain.find(word)
        while start != -1:
            if built[start:start + 32] == b"\x00" * 32:
                covered.update(range(start, start + 32))
            start = chain.find(word, start + 1)

    unexplained = [i for i in diff if i not in covered]
    if unexplained:
        return False, (
            f"{len(unexplained)} byte(s) differ outside any immutable slot "
            f"(first at offset {unexplained[0]:#x}); the deployed code is NOT "
            "the artifact"
        )
    return True, f"matches the artifact except {len(diff)} byte(s) in immutable slot(s)"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy ProjectToken to Polygon. Dry run unless --execute.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--artifact", default="build/ProjectToken.json",
                        help="compiled artifact from build_token.py")
    eth_common.add_network_arg(parser, default="amoy")
    parser.add_argument("--rpc", help="EVM JSON-RPC URL (has a public default on Amoy)")
    parser.add_argument("--key-file", help="bare key file, or a gen_wallets.py JSON export")
    parser.add_argument("--key-index", type=int, default=0,
                        help="which wallet to use when --key-file is an export (default 0)")
    parser.add_argument("--password-file", help="password for an encrypted key export")

    parser.add_argument("--name", required=True, help='token name, e.g. "My Token"')
    parser.add_argument("--symbol", required=True, help="token symbol, e.g. MTK")
    parser.add_argument("--initial-supply", required=True,
                        help="minted to the owner at deployment, in whole tokens")
    parser.add_argument("--max-supply", required=True,
                        help="PERMANENT ceiling on totalSupply, in whole tokens")
    parser.add_argument("--owner", help="address that will own the contract (default: deployer)")
    parser.add_argument("--operator", help="address allowed to mint (default: deployer)")

    parser.add_argument("--gas-limit", type=int, help="override the gas limit")
    parser.add_argument("--out", help="deployment record path (default deployment-<network>.json)")
    parser.add_argument("--execute", action="store_true", help="broadcast (default: dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    parser.add_argument(MAINNET_FLAG, dest="mainnet_ok", action="store_true",
                        help="required to deploy to mainnet")
    parser.add_argument("--tester", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    make_output_unicode_safe()

    artifact_path = Path(args.artifact)
    if not artifact_path.is_file():
        raise SystemExit(
            f"Error: no artifact at {artifact_path}. Run build_token.py first."
        )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    net = eth_common.get_network(args.network)
    if not net["testnet"] and not args.mainnet_ok:
        raise SystemExit(
            f"Error: refusing to deploy to {net['name']} without {MAINNET_FLAG}.\n"
            "Deployment is irreversible and costs real money. If that is what you\n"
            f"want, add {MAINNET_FLAG}."
        )

    from web3 import Web3

    initial_wei = parse_supply(args.initial_supply, "initial-supply")
    max_wei = parse_supply(args.max_supply, "max-supply")
    if max_wei == 0:
        raise SystemExit(
            "Error: --max-supply 0 would make the token permanently mintable-never "
            "(the contract rejects a zero cap)."
        )
    if initial_wei > max_wei:
        raise SystemExit(
            f"Error: --initial-supply {format_supply(initial_wei)} exceeds "
            f"--max-supply {format_supply(max_wei)}. The contract would reject it."
        )

    key, deployer, key_origin = eth_common.load_private_key(
        key_file=args.key_file,
        key_index=args.key_index,
        password_file=args.password_file,
        label="deployer",
    )

    def resolve_role(value: str | None, fallback: str, label: str) -> str:
        if not value:
            return fallback
        try:
            return Web3.to_checksum_address(value)
        except Exception:
            raise SystemExit(f"Error: --{label} is not a valid address: {value}")

    owner = resolve_role(args.owner, deployer, "owner")
    operator = resolve_role(args.operator, deployer, "operator")
    if owner == ZERO_ADDRESS or operator == ZERO_ADDRESS:
        raise SystemExit(
            "Error: the zero address cannot own or operate anything -- nobody holds "
            "its key, so rights given to it are destroyed."
        )

    values = [args.name, args.symbol, initial_wei, max_wei, owner, operator]
    arg_types = [i["type"] for i in constructor_abi(artifact).get("inputs", [])]
    args_hex = encode_constructor_args(artifact, values)
    creation_data = artifact["bytecode"] + args_hex

    rpc = eth_common.resolve_rpc(args)
    if rpc is None and not args.tester:
        raise SystemExit(
            f"Error: no RPC for {net['name']}. Pass --rpc <URL> "
            "(Alchemy, Infura, QuickNode, or any JSON-RPC provider)."
        )

    # On the in-memory chain there is no faucet, so the signing account is made
    # the funded one. That keeps --tester usable with a real key.
    w3 = eth_common.build_web3(rpc, tester=args.tester,
                               tester_keys=[key] if args.tester else None)
    chain_id = w3.eth.chain_id
    if not args.tester and chain_id != net["chain_id"]:
        raise SystemExit(
            f"Error: that RPC reports chain id {chain_id}, but --network {args.network} "
            f"expects {net['chain_id']}. Wrong endpoint."
        )

    fees = eth_common.fee_params(w3)
    balance = w3.eth.get_balance(deployer)
    nonce = w3.eth.get_transaction_count(deployer, block_identifier="pending")

    try:
        estimated = w3.eth.estimate_gas({"from": deployer, "data": creation_data, "value": 0})
        gas_limit = args.gas_limit or int(estimated * 1.25) + 1000
    except Exception as exc:
        print(f"Warning: gas estimation failed ({exc}).")
        gas_limit = args.gas_limit
        if gas_limit is None:
            print(f"\nThe deployment cannot be costed. The usual causes are a deployer\n"
                  f"without {net['currency']} for gas (balance shown below) or an RPC that\n"
                  "refuses eth_estimateGas. Pass --gas-limit to continue anyway.")
            print(f"deployer balance : {Web3.from_wei(balance, 'ether')} {net['currency']}")
            return 1

    fee_per_gas = fees.get("maxFeePerGas") or fees.get("gasPrice")
    max_cost = gas_limit * fee_per_gas

    print("=" * 68)
    print("DEPLOYMENT PLAN" + ("" if args.execute else "   (DRY RUN - nothing will be sent)"))
    print("=" * 68)
    print(f"network         : {net['name']} (chain id {net['chain_id']})")
    print(f"rpc             : {redact_rpc(rpc)}")
    print(f"contract        : {artifact['qualified_name']}")
    print(f"compiler        : {artifact['compiler']['long_version']}")
    print(f"deployer        : {deployer}   (key from {key_origin})")
    print(f"balance         : {Web3.from_wei(balance, 'ether')} {net['currency']}")
    print(f"fees            : {eth_common.describe_fees(fees)}")
    print(f"gas             : estimated {estimated}, limit {gas_limit}")
    print(f"max deploy cost : {Web3.from_wei(max_cost, 'ether')} {net['currency']}")
    print(f"start nonce     : {nonce}")
    print("-" * 68)
    print(f"name            : {args.name}")
    print(f"symbol          : {args.symbol}")
    print(f"decimals        : {DECIMALS}")
    print(f"initial supply  : {format_supply(initial_wei)}  -> minted to the OWNER")
    print(f"max supply      : {format_supply(max_wei)}  (immutable; nobody can raise it)")
    print(f"owner           : {owner}")
    print(f"operator        : {operator}")
    print("-" * 68)
    print(f"constructor args: {args_hex[:64]}...{args_hex[-16:]} ({len(arg_types)} values)")
    print(f"creation data   : {len(creation_data) // 2 - 1} bytes")

    if owner.lower() != deployer.lower():
        print(f"\nNOTE: ownership goes to {owner}, not the deployer. Only that key\n"
              "      can add or remove operators from then on. If it is wrong or\n"
              "      unreachable, that power is gone permanently.")
    if operator.lower() != deployer.lower():
        print(f"\nNOTE: mint rights go to {operator}. The deployer cannot mint.")

    if balance < max_cost:
        print(f"\nINSUFFICIENT BALANCE: need up to "
              f"{Web3.from_wei(max_cost, 'ether')} {net['currency']}, have "
              f"{Web3.from_wei(balance, 'ether')} {net['currency']}.")
        if net["testnet"]:
            print(f"Get test POL from {net['faucet']}")
        return 1

    tx = {
        "to": None,
        "value": 0,
        "gas": gas_limit,
        "nonce": nonce,
        "chainId": chain_id,
        "from": deployer,
        "data": creation_data,
        **fees,
    }

    from eth_account import Account

    account = Account.from_key(key)
    try:
        signed = account.sign_transaction(tx)
    except Exception as exc:
        raise SystemExit(f"Error: could not sign the deployment transaction ({exc}).")
    tx_hash = Web3.to_hex(signed.hash)
    print(f"\nSigned OK. Transaction hash: {tx_hash}")

    if not args.execute:
        print("\nDRY RUN COMPLETE. Nothing was broadcast.")
        print("Re-run with --execute to deploy.")
        return 0

    if not args.yes:
        print(f"\nAbout to deploy {args.name} ({args.symbol}) to {net['name']} "
              f"from {deployer}.")
        if input('Type DEPLOY (uppercase) to confirm: ').strip() != "DEPLOY":
            print("Aborted. Nothing was deployed.")
            return 1

    print("\nBroadcasting...")
    try:
        w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        raise SystemExit(f"Error: the network rejected the transaction ({exc}).")

    out = Path(args.out or f"deployment-{args.network}.json")
    # Written BEFORE the receipt is fetched. A broadcast transaction and its gas
    # are already committed at this point, so losing the hash to a failing RPC
    # is the one unrecoverable mistake here; the receipt details are filled in
    # and the file rewritten once they arrive.
    record = {
        "status": "broadcast_awaiting_receipt",
        "broadcast_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deployed_at": None,
        "network": args.network,
        "network_name": net["name"],
        "chain_id": chain_id,
        # True only for the hidden --tester flag: the record then describes an
        # in-memory chain, so its chain_id and explorer are not live.
        "in_memory_test_chain": bool(args.tester),
        "explorer": net["explorer"],
        "address": None,
        "tx_hash": tx_hash,
        "block": None,
        "gas_used": None,
        "deployer": deployer,
        "owner": owner,
        "operator": operator,
        "name": args.name,
        "symbol": args.symbol,
        "decimals": DECIMALS,
        "initial_supply": format_supply(initial_wei),
        "initial_supply_wei": str(initial_wei),
        "max_supply": format_supply(max_wei),
        "max_supply_wei": str(max_wei),
        "constructor_arguments": args_hex,
        "artifact": {
            "path": str(artifact_path),
            "qualified_name": artifact["qualified_name"],
            "compiler_long_version": artifact["compiler"]["long_version"],
            "bytecode_sha256": artifact["bytecode_sha256"],
            "deployed_bytecode_sha256": artifact["deployed_bytecode_sha256"],
            "source_sha256": artifact["source_sha256"],
        },
        "code_verified_against_artifact": None,
        "source_verified_on_explorer": False,
    }

    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Broadcast accepted. {out} written now, so the transaction hash cannot be lost.")

    print("\nWaiting for the receipt...")
    receipt = wait_for_receipt(w3, signed.hash, net)
    if receipt["status"] != 1:
        record.update({"status": "reverted", "block": receipt["blockNumber"],
                       "gas_used": receipt["gasUsed"]})
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(
            f"Error: the deployment transaction reverted (block {receipt['blockNumber']}).\n"
            f"  {eth_common.explorer_tx(net, tx_hash)}\n"
            f"  record updated: {out}"
        )

    address = receipt.get("contractAddress")
    record.update({
        "status": "confirmed",
        "deployed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "address": address,
        "block": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
    })
    print(f"\nDeployed at {address}")
    print(eth_common.explorer_address(net, address))

    code_ok, code_note = compare_code(
        Web3.to_hex(w3.eth.get_code(address)), artifact, {"maxSupply": max_wei}
    )
    record["code_verified_against_artifact"] = code_ok
    print(f"code check      : {'OK' if code_ok else 'FAILED'} - {code_note}")

    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Deployment record written to {out}")

    print("\nNext step -- publish the source so Polygonscan shows it as verified:")
    print(f"  python verify_token.py --network {args.network}")

    if not code_ok:
        print("\nWARNING: the runtime code on chain does not match the artifact, so\n"
              "         source verification will fail. Do not rely on this deployment.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
