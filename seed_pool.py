#!/usr/bin/env python3
"""Create and seed a QuickSwap V2 token/POL liquidity pool.

SAFE BY DEFAULT. Without --execute it reads the chain, prices the position,
prints the exact cost, and broadcasts nothing.

The private key is never taken from the command line: argv is written to shell
history and is visible in the process list. Use --key-file, the
DEPLOYER_PRIVATE_KEY environment variable, or the hidden --key-file-less prompt.

    # 1. plan only: what would 100,000 tokens + 5 POL create?
    python seed_pool.py --network polygon --token 0xCF2D... --amount 100000 --pol 5 \\
        --key-file key-7f7c.txt

    # 2. broadcast (approve + addLiquidityETH)
    python seed_pool.py ... --execute

Three things this deliberately refuses to do:

  * guess a pool that is not on QuickSwap V2 (the one factory/router pair is
    verified against each other before a single wei moves);
  * seed twice. The add-liquidity hash is written to the record file BEFORE the
    receipt is awaited, so a lost receipt becomes "check record, read the
    outcome", never "run it again" -- a second addLiquidity on an empty pair
    would double the token deposit and set a different price;
  * treat "100000" as base units. The amount is human tokens, scaled by the
    token's own decimals() (18 here), because 100000 wei is 1e-13 tokens and
    that mistake is invisible in a confirmation screen.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import eth_common
from deploy_token import MAINNET_FLAG
from distribute_token import human, parse_amount

# --------------------------------------------------------------------------- #
# QuickSwap V2 on Polygon PoS.
#
# Not taken on trust: main() reads factory() and WETH() off the router and
# compares them with the values below, so a wrong or replaced router address is
# caught before it is approved to spend tokens.
# --------------------------------------------------------------------------- #
DEXES = {
    "polygon": {
        "name": "QuickSwap V2",
        "router": "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",
        "factory": "0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
        "wmatic": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    },
}

ROUTER_ABI = [
    {"name": "factory", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "WETH", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "addLiquidityETH", "type": "function", "stateMutability": "payable",
     "inputs": [
         {"name": "token", "type": "address"},
         {"name": "amountTokenDesired", "type": "uint256"},
         {"name": "amountTokenMin", "type": "uint256"},
         {"name": "amountETHMin", "type": "uint256"},
         {"name": "to", "type": "address"},
         {"name": "deadline", "type": "uint256"},
     ],
     "outputs": [{"name": "amountToken", "type": "uint256"},
                 {"name": "amountETH", "type": "uint256"},
                 {"name": "liquidity", "type": "uint256"}]},
]

FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}],
     "outputs": [{"type": "address"}]},
]

PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "_reserve0", "type": "uint112"},
                 {"name": "_reserve1", "type": "uint112"},
                 {"name": "_blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]

TOKEN_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]

ZERO_ADDRESS = "0x" + "0" * 40


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def load_record(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_record(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def already_seeded(doc: dict, pool: str, signer: str) -> bool:
    """True once an addLiquidity for this (signer, pool) has been broadcast.

    Keyed on the broadcast, not on success: a transaction whose receipt could
    not be read has still almost certainly been mined, and reseeding would
    deposit a second 100,000 tokens at a different price.
    """
    return (doc.get("pool", "").lower() == pool.lower()
            and doc.get("signer", "").lower() == signer.lower()
            and doc.get("status") in {"broadcast", "success"})


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def with_slippage(units: int, bps: int) -> int:
    """Minimum acceptable amount: floor, never above the desired amount."""
    if not 0 <= bps < 10_000:
        raise ValueError(f"slippage {bps} bps is out of range (0..9999)")
    return units * (10_000 - bps) // 10_000


def price_and_fdv(pol_units: int, token_units: int, supply_units: int,
                  pol_decimals: int = 18, token_decimals: int = 18) -> dict:
    """POL per whole token, and the implied fully-diluted value in POL.

    Computed in floats on purpose: these are planning estimates for a human to
    read, and every figure the transaction itself needs is an exact integer.
    """
    if token_units <= 0:
        raise ValueError("token amount must be positive")
    pol_per_token = (pol_units / 10**pol_decimals) / (token_units / 10**token_decimals)
    supply = supply_units / 10**token_decimals
    return {
        "pol_per_token": pol_per_token,
        "tokens_per_pol": 1 / pol_per_token if pol_per_token else None,
        "fdv_pol": pol_per_token * supply,
    }


def resolve_token(args) -> str:
    if args.token:
        return args.token
    if not args.deployment:
        raise SystemExit("Error: pass --token or --deployment (with the token address).")
    path = Path(args.deployment)
    if not path.is_file():
        raise SystemExit(f"Error: no such deployment record: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    address = doc.get("address")
    if not address:
        raise SystemExit(f"Error: {path} has no 'address' field.")
    return str(address)


def check_router(w3, dex: dict) -> None:
    """The pool is only as real as the router: confirm it is the one we think."""
    from web3 import Web3

    C = Web3.to_checksum_address
    router = w3.eth.contract(address=C(dex["router"]), abi=ROUTER_ABI)
    if not w3.eth.get_code(C(dex["router"])):
        raise SystemExit(f"Error: no contract code at the router {dex['router']}.")
    factory = router.functions.factory().call()
    wmatic = router.functions.WETH().call()
    if factory.lower() != dex["factory"].lower():
        raise SystemExit(
            f"Error: router {dex['router']} points at factory {factory}, "
            f"not the expected {dex['factory']}. Refusing to seed an unknown DEX."
        )
    if wmatic.lower() != dex["wmatic"].lower():
        raise SystemExit(
            f"Error: router {dex['router']} wraps {wmatic}, not the expected "
            f"WMATIC {dex['wmatic']}."
        )


def read_pair(w3, dex: dict, token: str) -> dict:
    """Existing pair (if any) and its reserves, so the plan can say 'new' or not."""
    from web3 import Web3

    C = Web3.to_checksum_address
    factory = w3.eth.contract(address=C(dex["factory"]), abi=FACTORY_ABI)
    pair_address = factory.functions.getPair(C(token), C(dex["wmatic"])).call()
    exists = int(pair_address, 16) != 0
    out = {"address": pair_address, "exists": exists,
           "reserve_token": 0, "reserve_pol": 0, "token0": None}
    if exists:
        pair = w3.eth.contract(address=C(pair_address), abi=PAIR_ABI)
        r0, r1, _ = pair.functions.getReserves().call()
        token0 = pair.functions.token0().call()
        out["token0"] = token0
        if token0.lower() == C(token).lower():
            out["reserve_token"], out["reserve_pol"] = r0, r1
        else:
            out["reserve_token"], out["reserve_pol"] = r1, r0
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create and seed a QuickSwap V2 token/POL pool (dry run by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    eth_common.add_network_arg(parser, default="polygon")
    parser.add_argument("--rpc", help="Polygon RPC URL (mainnet has no default: pass one)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--token", help="token contract address")
    source.add_argument("--deployment", help="deployment record from deploy_token.py")
    parser.add_argument("--amount", required=True,
                        help="tokens to place in the pool, e.g. 100000")
    parser.add_argument("--pol", required=True,
                        help="POL to pair with them (the ratio sets the price)")
    parser.add_argument("--slippage-bps", type=int, default=100,
                        help="minimum-out tolerance in basis points (default 100 = 1%%)")
    parser.add_argument("--key-file", help="bare key file, or a gen_wallets.py JSON export")
    parser.add_argument("--key-index", type=int, default=0,
                        help="which wallet from a gen_wallets.py export (default 0)")
    parser.add_argument("--password-file", help="password for an encrypted key export")
    parser.add_argument("--deadline-minutes", type=int, default=20,
                        help="transaction deadline, in minutes from now (default 20)")
    parser.add_argument("--results", help="record path (default seed-pool-<network>.json)")
    parser.add_argument("--execute", action="store_true",
                        help="broadcast (default: dry run)")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    parser.add_argument(MAINNET_FLAG, dest="mainnet_ok", action="store_true",
                        help="required for mainnet: this spends real funds")
    parser.add_argument("--tester", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    net = eth_common.get_network(args.network)
    if not net["testnet"] and not args.mainnet_ok:
        raise SystemExit(
            f"Error: {net['name']} spends real funds. Re-run with {MAINNET_FLAG} if "
            "that is what you mean."
        )
    dex = DEXES.get(net["slug"])
    if dex is None:
        raise SystemExit(
            f"Error: no known QuickSwap V2 deployment on {net['name']}. "
            f"Known: {', '.join(DEXES)}."
        )

    token = resolve_token(args)
    key, signer, key_origin = eth_common.load_private_key(
        args.key_file, args.key_index, args.password_file, label="signer"
    )

    rpc = eth_common.resolve_rpc(args)
    w3 = eth_common.build_web3(rpc, tester=args.tester, tester_keys=[key])

    from web3 import Web3

    C = Web3.to_checksum_address
    token = C(token)
    signer = C(signer)

    if not w3.eth.get_code(token):
        raise SystemExit(
            f"Error: no contract code at {token}. That address is an EOA or wrong "
            "chain; a pool cannot be seeded against it."
        )

    tok = w3.eth.contract(address=token, abi=TOKEN_ABI)
    decimals = tok.functions.decimals().call()
    symbol = tok.functions.symbol().call()
    supply = tok.functions.totalSupply().call()

    amount = parse_amount(args.amount, decimals, "amount")
    pol_units = parse_amount(args.pol, 18, "pol")
    if amount <= 0:
        raise SystemExit("Error: --amount must be greater than zero.")
    if pol_units <= 0:
        raise SystemExit(
            "Error: --pol must be greater than zero. QuickSwap V2 needs both sides: "
            "a token-only pool is not a thing, and POL is what the token trades against."
        )

    token_balance = tok.functions.balanceOf(signer).call()
    pol_balance = w3.eth.get_balance(signer)

    check_router(w3, dex)
    pair = read_pair(w3, dex, token)
    allowance = tok.functions.allowance(signer, C(dex["router"])).call()

    amount_min = with_slippage(amount, args.slippage_bps)
    pol_min = with_slippage(pol_units, args.slippage_bps)
    pricing = price_and_fdv(pol_units, amount, supply, 18, decimals)

    results_path = Path(args.results or f"seed-pool-{args.network}.json")
    record = load_record(results_path)

    print("=" * 68)
    print("POOL SEED PLAN")
    print("=" * 68)
    print(f"network        : {net['name']} (chain {w3.eth.chain_id})")
    print(f"dex            : {dex['name']}, router {dex['router']} (factory verified)")
    print(f"token          : {token}  {symbol}  ({decimals} decimals)")
    print(f"signer         : {signer}  (key from {key_origin})")
    print(f"pool           : {pair['address']}  "
          f"({'EXISTS' if pair['exists'] else 'will be created by this transaction'})")
    print(f"deposit        : {human(amount, decimals)} {symbol} + {human(pol_units, 18)} POL")
    print(f"min out        : {human(amount_min, decimals)} {symbol} + {human(pol_min, 18)} POL "
          f"({args.slippage_bps} bps slippage)")
    print(f"price          : {pricing['pol_per_token']:.10f} POL per {symbol}  "
          f"(implied FDV {pricing['fdv_pol']:,.2f} POL)")

    print("\nbalances")
    print(f"  signer {symbol:<8}: {human(token_balance, decimals)} "
          f"({'OK' if token_balance >= amount else 'SHORT'})")
    print(f"  signer POL      : {human(pol_balance, 18)} "
          f"({'OK' if pol_balance >= pol_units else 'SHORT'})")
    print(f"  allowance       : {human(allowance, decimals)} "
          f"({'sufficient' if allowance >= amount else 'needs approve()'})")

    if pair["exists"]:
        print(f"\nexisting reserves: {human(pair['reserve_token'], decimals)} {symbol} / "
              f"{human(pair['reserve_pol'], 18)} POL")
        print("  note: adding to a live pool at a different ratio deposits only part of")
        print("  each side and refunds the rest, so the price does not move as planned.")

    if w3.eth.get_code(signer):
        raise SystemExit(f"Error: {signer} has contract code; it cannot sign.")
    if token_balance < amount:
        short = amount - token_balance
        raise SystemExit(
            f"\nError: {signer} holds {human(token_balance, decimals)} {symbol} but "
            f"{human(amount, decimals)} is needed ({human(short, decimals)} short).\n"
            "Move the tokens into the signer first, e.g.:\n"
            f"  python distribute_token.py --network {args.network} --token {token} "
            f"--key-file <holder-key-export> --key-index 1 --wallets <file-with-{signer}> "
            f"--amount {args.amount}"
        )

    gas_reserve = 400_000 * (w3.eth.gas_price or 0)
    if pol_balance < pol_units + gas_reserve:
        raise SystemExit(
            f"\nError: {signer} holds {human(pol_balance, 18)} POL; this needs "
            f"{human(pol_units, 18)} to the pool plus ~{human(gas_reserve, 18)} for gas."
        )

    if already_seeded(record, pair["address"], signer):
        print(f"\nAlready broadcast for this signer and pool -- see {results_path}.")
        print(f"  status : {record.get('status')}")
        print(f"  tx     : {record.get('add_liquidity_tx')}")
        print("Refusing to seed again: a second deposit would change the price and")
        print("double the tokens at risk. Check the hash above before doing anything.")
        return 1

    print("\nplan looks coherent; nothing was signed.")
    if not args.execute:
        print("Re-run with --execute to approve and add liquidity.")
        return 0

    if not args.yes:
        print(f"\nAbout to deposit {human(amount, decimals)} {symbol} and "
              f"{human(pol_units, 18)} POL into a pool on {net['name']}.")
        print("This is irreversible: the price it sets is the price buyers see.")
        if input("Type SEED (uppercase) to confirm: ").strip() != "SEED":
            print("Aborted. Nothing was sent.")
            return 1

    from eth_account import Account

    account = Account.from_key(key)
    router = w3.eth.contract(address=C(dex["router"]), abi=ROUTER_ABI)

    def send(fn, value: int, gas: int, node: str) -> str:
        """Sign, broadcast, and record the hash before the receipt is awaited."""
        tx = fn.build_transaction({
            "from": signer,
            "value": value,
            "gas": gas,
            "nonce": w3.eth.get_transaction_count(signer, "pending"),
            "chainId": w3.eth.chain_id,
            **eth_common.fee_params(w3),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw)
        hex_hash = Web3.to_hex(tx_hash)
        print(f"\n{node} broadcast: {hex_hash}")
        record.update({
            "network": net["slug"],
            "chain_id": w3.eth.chain_id,
            "dex": dex["name"],
            "router": dex["router"],
            "token": token,
            "symbol": symbol,
            "signer": signer,
            "pool": pair["address"],
            "token_amount": str(args.amount),
            "pol_amount": str(args.pol),
            "token_units": amount,
            "pol_units": pol_units,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if node == "approve":
            record["approve_tx"] = hex_hash
        else:
            record["add_liquidity_tx"] = hex_hash
            record["status"] = "broadcast"
        save_record(results_path, record)
        return hex_hash

    if allowance < amount:
        gas = 80_000
        try:
            gas = int(tok.functions.approve(C(dex["router"]), amount)
                      .estimate_gas({"from": signer}) * 1.3)
        except Exception:
            pass
        approve_hash = send(tok.functions.approve(C(dex["router"]), amount), 0, gas, "approve")
        receipt = eth_common.wait_for_receipt(
            w3, approve_hash, net, action="re-send an approve worth nothing extra")
        if receipt.get("status") != 1:
            record["status"] = "approve_failed"
            save_record(results_path, record)
            print("Approval reverted. Nothing was deposited.")
            return 1
        print(f"approve confirmed in block {receipt['blockNumber']}, "
              f"gas {receipt['gasUsed']}")

    deadline = int(datetime.now(timezone.utc).timestamp()) + args.deadline_minutes * 60
    call = router.functions.addLiquidityETH(
        token, amount, amount_min, pol_min, signer, deadline
    )
    gas = 350_000
    try:
        gas = int(call.estimate_gas({"from": signer, "value": pol_units}) * 1.3)
    except Exception:
        pass

    seed_hash = send(call, pol_units, gas, "addLiquidityETH")
    receipt = eth_common.wait_for_receipt(
        w3, seed_hash, net, action="deposit a SECOND 100,000 tokens at a new price")
    if receipt.get("status") != 1:
        record["status"] = "reverted"
        save_record(results_path, record)
        print("The addLiquidityETH transaction reverted. Gas was spent, nothing was")
        print("deposited, and the record above names the hash.")
        return 1

    pair_now = read_pair(w3, dex, token)
    seeded_pair = w3.eth.contract(address=C(pair_now["address"]), abi=PAIR_ABI)
    lp_balance = seeded_pair.functions.balanceOf(signer).call()

    record.update({
        "status": "success",
        "block": receipt["blockNumber"],
        "gas_used": receipt["gasUsed"],
        "pool": pair_now["address"],
        "reserve_token": pair_now["reserve_token"],
        "reserve_pol": pair_now["reserve_pol"],
        "lp_tokens": lp_balance,
        "price_pol_per_token": pricing["pol_per_token"],
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    save_record(results_path, record)

    print("\n" + "=" * 68)
    print("SEEDED")
    print("=" * 68)
    print(f"block       : {receipt['blockNumber']}  gas {receipt['gasUsed']}")
    print(f"pool        : {pair_now['address']}")
    print(f"reserves    : {human(pair_now['reserve_token'], decimals)} {symbol} / "
          f"{human(pair_now['reserve_pol'], 18)} POL")
    print(f"LP tokens   : {human(lp_balance, 18)} (held by {signer})")
    print(f"record      : {results_path}")
    print(eth_common.explorer_address(net, pair_now["address"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
