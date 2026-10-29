#!/usr/bin/env python3
"""Export every holder of an ERC-20 token to CSV, with balances from chain.

Balances are reconstructed from Transfer logs, not from a local record: every
row is the sum of what the address received minus what it sent, which is the
same arithmetic the contract's own balanceOf() performs. The closing check is
that those balances add up to totalSupply() -- if they do not, the export
missed a transfer (a reorg, a truncated getLogs response, a wrong start block)
and the file would silently understate someone's holding, so the script exits
nonzero rather than writing a plausible-looking lie.

Usage:
    python export_holders.py                       # LOVEWEB on Polygon, default file
    python export_holders.py --out holders.csv
    python export_holders.py --token 0x... --symbol XYZ --from-block 123
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from eth_utils import keccak

TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

# The token this project deployed; see deployment-polygon.json.
DEFAULT_TOKEN = "0xCF2D6c41340Ae035BA76a581AcB9392D11533B70"
DEFAULT_SYMBOL = "LOVEWEB"
DEFAULT_DEPLOY_BLOCK = 95645544
# polygon-rpc.com now answers 403 ("API key disabled"). Of the unauthenticated
# Polygon PoS endpoints, this one serves historical eth_getLogs in hex with a
# 10,000-block cap: publicnode rejects wider ranges (-32701), drpc's free plan
# refuses any fromBlock more than 10,000 blocks behind the tip, and 1rpc caps
# getLogs at 50 blocks. Hence DEFAULT_RPC and the 10,000 default chunk.
DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"


def rpc(url: str, method: str, params: list, retries: int = 5):
    """One JSON-RPC call, retried on transport errors and rate limits."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last = None
    for attempt in range(retries):
        try:
            # Some public endpoints 403 the default "Python-urllib/x.y" agent.
            req = urllib.request.Request(
                url, data=body.encode(),
                headers={"content-type": "application/json",
                         "user-agent": "export_holders/1.0"},
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                doc = json.loads(resp.read().decode())
            if "error" in doc:
                # A rate limit or a range cap is worth retrying; a bad method is not.
                raise RuntimeError(f"{method}: {doc['error'].get('message')}")
            return doc["result"]
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    raise SystemExit(f"Error: {method} failed after {retries} attempts: {last}")


def fetch_logs(url: str, token: str, from_block: int, to_block: int, chunk: int) -> list:
    """All Transfer logs in [from_block, to_block], halving the window on failure."""
    logs: list = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        try:
            batch = rpc(url, "eth_getLogs", [{
                "address": token,
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [TRANSFER_TOPIC],
            }])
        except SystemExit:
            if chunk <= 1_000:
                raise
            chunk //= 2
            print(f"  (window too large, retrying at {chunk:,} blocks)", file=sys.stderr)
            continue
        logs.extend(batch)
        print(f"  blocks {start:,}-{end:,}: {len(batch):,} transfers "
              f"(total {len(logs):,})", file=sys.stderr)
        start = end + 1
    return logs


def call_string(url: str, token: str, selector: str) -> str:
    data = rpc(url, "eth_call", [{"to": token, "data": selector}, "latest"])
    raw = bytes.fromhex(data[2:])
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        length = int.from_bytes(raw[offset:offset + 32], "big")
        return raw[offset + 32:offset + 32 + length].decode()
    return raw.rstrip(b"\x00").decode()


def call_uint(url: str, token: str, selector: str) -> int:
    return int(rpc(url, "eth_call", [{"to": token, "data": selector}, "latest"]), 16)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Export every ERC-20 holder with its on-chain balance.")
    p.add_argument("--token", default=DEFAULT_TOKEN, help=f"token address (default {DEFAULT_TOKEN})")
    p.add_argument("--symbol", default=DEFAULT_SYMBOL, help="symbol for the filename/report")
    p.add_argument("--rpc", default=DEFAULT_RPC, help=f"EVM JSON-RPC URL (default {DEFAULT_RPC})")
    p.add_argument("--from-block", type=int, default=DEFAULT_DEPLOY_BLOCK,
                   help="first block to scan; must be <= the deployment block or the "
                        "supply check will fail (default: this project's deploy block)")
    p.add_argument("--to-block", type=int, help="last block (default: latest)")
    p.add_argument("--chunk", type=int, default=10_000,
                   help="blocks per getLogs request (public endpoints cap this; "
                        "the default is the widest range publicnode accepts)")
    p.add_argument("--out", help="CSV path (default holders-<symbol>.csv)")
    args = p.parse_args(argv)

    token = args.token
    out = Path(args.out or f"holders-{args.symbol.lower()}.csv")
    to_block = args.to_block or int(rpc(args.rpc, "eth_blockNumber", []), 16)

    name = call_string(args.rpc, token, "0x06fdde03")
    symbol = call_string(args.rpc, token, "0x95d89b41")
    decimals = call_uint(args.rpc, token, "0x313ce567")
    total_supply = call_uint(args.rpc, token, "0x18160ddd")

    print(f"{name} ({symbol}), {decimals} decimals, token {token}")
    print(f"blocks {args.from_block:,}..{to_block:,}, supply {total_supply / 10**decimals:,.6f}")

    logs = fetch_logs(args.rpc, token, args.from_block, to_block, args.chunk)

    balances: dict[str, int] = {}
    received: dict[str, int] = {}
    sent: dict[str, int] = {}
    for log in logs:
        topics = log["topics"]
        if len(topics) < 3:
            continue  # ERC-721 style Transfer, not ERC-20
        src = "0x" + topics[1][-40:]
        dst = "0x" + topics[2][-40:]
        value = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
        if src != "0x" + "0" * 40:  # a mint has no sender
            balances[src] = balances.get(src, 0) - value
            sent[src] = sent.get(src, 0) + 1
        if dst != "0x" + "0" * 40:  # a burn has no recipient
            balances[dst] = balances.get(dst, 0) + value
            received[dst] = received.get(dst, 0) + 1

    # A holder is anyone whose net position is positive. Dust from a failed
    # transfer never lands (a revert moves nothing), so this stays exact.
    holders = {a: b for a, b in balances.items() if b > 0}
    imported = sum(holders.values())
    if imported != total_supply:
        print(f"\nFAIL: balances sum to {imported:,} wei but totalSupply is "
              f"{total_supply:,} wei (difference {total_supply - imported:,}). "
              f"The log scan is incomplete -- lower --from-block or the output "
              f"would omit transfers.", file=sys.stderr)
        return 1

    rows = sorted(holders.items(), key=lambda kv: (-kv[1], kv[0]))
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "address", "balance", "balance_wei", "share_pct",
                    "transfers_in", "transfers_out", "token", "symbol"])
        for i, (addr, bal) in enumerate(rows, 1):
            w.writerow([i, addr, f"{bal / 10**decimals:.{decimals}f}", bal,
                        f"{bal / total_supply * 100:.6f}",
                        received.get(addr, 0), sent.get(addr, 0), token, symbol])

    print(f"\n{len(rows):,} holders, {len(logs):,} transfers, "
          f"supply matched exactly ({imported / 10**decimals:,.6f} {symbol})")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
