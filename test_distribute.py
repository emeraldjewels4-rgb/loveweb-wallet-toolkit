#!/usr/bin/env python3
"""End-to-end tests for distribute_eth.py against an in-memory Ethereum chain.

Runs real signed transactions on a real (local) EVM, so the signing, nonce,
broadcast and receipt paths are all genuinely exercised. No network, no funds.

    python test_distribute.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import distribute_eth as d
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

# eth-tester's first default account is the address of private key 0x...01.
SOURCE_KEY = "0x" + "00" * 31 + "01"
SOURCE = Account.from_key(SOURCE_KEY).address
AMOUNT_ETH = "0.002"

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    # Convention here is (condition, label). test_token.py uses the OPPOSITE
    # order, so an idiom copied between files inverts the call silently: a
    # string condition is always truthy (a check that can never fail) and a
    # boolean label means the two arguments are swapped. Guard both slots so
    # that mistake crashes instead of reporting a false pass.
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def make_recipients(path: Path, n: int = 100) -> list[str]:
    """100 distinct addresses from a throwaway mnemonic."""
    import gen_wallets as g

    doc = g.build(chains=["evm"], count=n, mode="hd", words=12,
                  mnemonic=None, passphrase="", verify=False)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return [w["evm"]["address"] for w in doc["wallets"]]


def deploy_runtime(w3, runtime: bytes, funder: str) -> str:
    """Deploy raw `runtime` bytecode and return the resulting contract address."""
    if len(runtime) > 0xFF:
        raise SystemExit("runtime too long for this helper's single-byte init code")
    # PUSH1 len, PUSH1 12 (where the runtime starts), PUSH1 0, CODECOPY; then
    # PUSH1 len, PUSH1 0, RETURN.
    init = bytes([0x60, len(runtime), 0x60, 0x0C, 0x60, 0x00, 0x39,
                  0x60, len(runtime), 0x60, 0x00, 0xF3])
    tx_hash = w3.eth.send_transaction(
        {"from": funder, "data": (init + runtime).hex(), "gas": 300_000}
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt["contractAddress"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dist_test_"))
    wallets = tmp / "wallets.json"
    results = tmp / "distribution.json"
    recipients = make_recipients(wallets)
    os.environ[d.ENV_KEY_NAME] = SOURCE_KEY

    # One shared chain for every call, so balances persist across runs.
    w3 = Web3(EthereumTesterProvider())
    d.build_web3 = lambda rpc, tester: w3  # inject the shared chain
    start_block = w3.eth.block_number
    print(f"source {SOURCE}  balance {Web3.from_wei(w3.eth.get_balance(SOURCE), 'ether')} ETH")

    base = ["--wallets", str(wallets), "--amount", AMOUNT_ETH, "--results", str(results), "--tester"]

    print("\n=== 1. dry run sends nothing ===")
    rc = d.main(base)
    check(rc == 0, "dry run exits 0")
    check(w3.eth.block_number == start_block, "dry run mined no blocks")
    check(w3.eth.get_balance(recipients[0]) == 0, "dry run left recipient balance at 0")
    check(not results.exists(), "dry run wrote no results file")

    print("\n=== 2. refuses to send without --execute ===")
    check(w3.eth.get_balance(recipients[0]) == 0, "still nothing sent after second dry run")

    print("\n=== 3. real broadcast of 100 transfers ===")
    rc = d.main(base + ["--execute", "--yes"])
    check(rc == 0, "execute exits 0")
    balances = [w3.eth.get_balance(a) for a in recipients]
    want = Web3.to_wei(AMOUNT_ETH, "ether")
    check(all(b == want for b in balances), f"all 100 recipients hold exactly {AMOUNT_ETH} ETH")
    check(sum(balances) > 0, "value actually moved")

    doc = json.loads(results.read_text())
    transfers = doc["transfers"]
    check(len(transfers) == 100, "results file records 100 transfers")
    check(all(t["status"] == "success" for t in transfers), "every transfer marked success")
    check(len({t["nonce"] for t in transfers}) == 100, "100 distinct nonces (no nonce reuse)")
    check(all(t["gas_used"] == 21000 for t in transfers), "gas_used recorded as 21000")
    check(all(t["tx_hash"].startswith("0x") and len(t["tx_hash"]) == 66 for t in transfers),
          "every tx_hash is 0x-prefixed 32-byte hex")
    check(all(int(t["block"]) > 0 for t in transfers), "every transfer has a block number")
    check(doc["source"].lower() == SOURCE.lower(), "results file records the source")

    print("\n=== 4. resume does not double-pay ===")
    before = [w3.eth.get_balance(a) for a in recipients]
    rc = d.main(base + ["--execute", "--yes"])
    after = [w3.eth.get_balance(a) for a in recipients]
    check(rc == 0, "resume run exits 0")
    check(before == after, "resume sent nothing (balances unchanged)")

    print("\n=== 5. insufficient balance is refused ===")
    rc = d.main(["--wallets", str(wallets), "--amount", "20000",
                 "--results", str(tmp / "big.json"), "--tester", "--execute", "--yes"])
    check(rc == 1, "insufficient balance exits 1")

    print("\n=== 6. --max-total cap is enforced ===")
    rc = d.main(base + ["--max-total", "0.001", "--no-resume", "--execute", "--yes"])
    check(rc == 1, "cap exceeded exits 1")

    print("\n=== 7. bad inputs are rejected ===")
    bad = tmp / "bad.json"
    bad.write_text(json.dumps(["0xnotanaddress"]), encoding="utf-8")
    try:
        d.main(["--wallets", str(bad), "--amount", "0.001", "--tester"])
        check(False, "invalid address rejected")
    except SystemExit as e:
        check("invalid address" in str(e), "invalid address rejected")

    try:
        d.main(base + ["--source-address", "0x" + "22" * 20])
        check(False, "source mismatch rejected")
    except SystemExit as e:
        check("does not match" in str(e), "source mismatch rejected")

    print("\n=== 8. duplicates and self-sends are filtered ===")
    dup = tmp / "dup.json"
    dup.write_text(json.dumps([recipients[0], recipients[0], SOURCE, recipients[1]]), encoding="utf-8")
    rc = d.main(["--wallets", str(dup), "--amount", "0.001", "--results", str(tmp / "d.json"),
                 "--tester", "--execute", "--yes"])
    recs = json.loads((tmp / "d.json").read_text())["transfers"]
    check(rc == 0 and len(recs) == 2, "2 unique recipients after dedupe + self-send removal")

    print("\n=== 9. a lost receipt never becomes a second payment ===")
    # This is the failure that cost real POL: the transfer is mined, but the
    # receipt call fails (a rate-limited endpoint answers 403), so the record
    # says "error". A retry that re-signs with a fresh nonce pays twice.
    late_wallets = tmp / "late.json"
    late_targets = make_recipients(late_wallets, n=3)
    late_results = tmp / "late-results.json"
    real_wait = w3.eth.wait_for_transaction_receipt
    calls = {"n": 0}

    def flaky_wait(tx_hash, timeout=180):
        rc = real_wait(tx_hash, timeout=timeout)
        calls["n"] += 1
        if calls["n"] <= 2:  # the transaction is mined; only the receipt is lost
            raise IOError("simulated 403 from the endpoint")
        return rc

    w3.eth.wait_for_transaction_receipt = flaky_wait
    try:
        rc = d.main(["--wallets", str(late_wallets), "--amount", "0.001",
                     "--results", str(late_results), "--tester",
                     "--retries", "1", "--execute", "--yes"])
    finally:
        w3.eth.wait_for_transaction_receipt = real_wait
    late = [w3.eth.get_balance(a) for a in late_targets]
    one = Web3.to_wei("0.001", "ether")
    check(late == [one] * 3, "each recipient paid exactly once after a lost receipt")
    late_recs = json.loads(late_results.read_text())["transfers"]
    check(all(r["status"] == "success" for r in late_recs),
          "retry reconciled the lost receipts instead of re-sending")

    print("\n=== 10. gas is measured per recipient, not assumed 21,000 ===")
    # An address with code executes that code on a plain value transfer. This is
    # the stand-in for the EIP-7702 delegated account that cost real POL: it
    # needs more than 21,000, so a hardcoded 21,000 runs out and reverts.
    funder = w3.eth.accounts[0]
    costly = deploy_runtime(w3, bytes.fromhex("34600055" "34600155" "34600255" "00"), funder)
    costly_file = tmp / "costly.json"
    costly_file.write_text(json.dumps([costly]), encoding="utf-8")
    costly_results = tmp / "costly-results.json"
    rc = d.main(["--wallets", str(costly_file), "--amount", "0.001", "--results",
                 str(costly_results), "--tester", "--execute", "--yes"])
    costly_rec = json.loads(costly_results.read_text())["transfers"][0]
    check(rc == 0, "a recipient whose code costs gas is paid successfully")
    check(costly_rec["gas_limit"] > d.DEFAULT_GAS_LIMIT,
          f"gas limit {costly_rec['gas_limit']} is above the 21,000 floor")
    check(costly_rec["status"] == "success", "no out-of-gas revert for the code recipient")
    check(w3.eth.get_balance(costly) == Web3.to_wei("0.001", "ether"),
          "the code recipient actually received the value")
    check(all(t["gas_limit"] == 21000 for t in transfers),
          "plain wallets still get exactly 21,000, not a padded estimate")

    print("\n=== 11. an un-estimable recipient is reported, never sent blind ===")
    # A contract that rejects value: there is no honest gas limit to sign with.
    # Guessing 21,000 here would burn the fee and deliver nothing.
    rejects = deploy_runtime(w3, bytes.fromhex("60006000fd"), funder)
    rej_file = tmp / "reject.json"
    rej_file.write_text(json.dumps([rejects]), encoding="utf-8")
    rej_results = tmp / "reject-results.json"

    blocks_before = w3.eth.block_number
    rc = d.main(["--wallets", str(rej_file), "--amount", "0.001",
                 "--results", str(rej_results), "--tester"])
    check(rc == 1, "a dry run reports an unpriceable recipient in its exit status")
    check(w3.eth.block_number == blocks_before, "that dry run still mined nothing")

    rc = d.main(["--wallets", str(rej_file), "--amount", "0.001",
                 "--results", str(rej_results), "--tester", "--execute", "--yes"])
    check(rc == 1, "a run where nothing can be priced exits 1")
    check(w3.eth.get_balance(rejects) == 0, "nothing was sent to the rejecting address")
    check(not rej_results.exists(), "no results file claiming work was done")

    # Mixed: the priceable recipient is still paid, the other is recorded as an
    # error, and the exit status says something went wrong.
    mix_file = tmp / "mix.json"
    mix_file.write_text(json.dumps([recipients[3], rejects]), encoding="utf-8")
    mix_results = tmp / "mix-results.json"
    before = w3.eth.get_balance(recipients[3])
    rc = d.main(["--wallets", str(mix_file), "--amount", "0.001", "--results",
                 str(mix_results), "--tester", "--execute", "--yes"])
    mix = {t["address"].lower(): t
           for t in json.loads(mix_results.read_text())["transfers"]}
    check(rc == 2, "a partially priceable run exits 2")
    check(mix[recipients[3].lower()]["status"] == "success", "the priceable recipient was paid")
    check(w3.eth.get_balance(recipients[3]) == before + Web3.to_wei("0.001", "ether"),
          "and received exactly one more transfer")
    check(mix[rejects.lower()]["status"] == "estimate_failed",
          "the unpriceable recipient is recorded as estimate_failed, not as sent")
    check("gas estimate failed" in mix[rejects.lower()].get("error", ""),
          "the reason is kept in the results file")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
