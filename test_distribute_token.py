#!/usr/bin/env python3
"""End-to-end tests for distribute_token.py against an in-memory Ethereum chain.

Real contract, real signed ERC-20 transfers, real receipts, real records, and
real resumption -- on a local chain, so no funds are at risk.

    python test_distribute_token.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import distribute_token as d
import gen_wallets as g
import wallet_fixtures
from eth_abi import encode
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

ARTIFACT = Path("build/ProjectToken.json")

SOURCE_KEY = "0x" + "00" * 31 + "01"
SOURCE = Account.from_key(SOURCE_KEY).address
ACCOUNT_2 = Account.from_key("0x" + "00" * 31 + "02").address
TOKEN = 10**18

# Mirrors the live token: 1,000,000,000 supply, 1,000,000,000 immutable cap.
SUPPLY = 1_000_000_000
FAILED: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
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
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 68)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rc = d.main(argv)
        return rc, "", buffer.getvalue()
    except SystemExit as exc:
        return -1, str(exc), buffer.getvalue()


def load_abi() -> list[dict]:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))["abi"]


def deploy(w3, deployer: str, *, name="Test Token", symbol="TT", initial=SUPPLY,
           cap=SUPPLY, decimals=None) -> str:
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    args = encode(
        ["string", "string", "uint256", "uint256", "address", "address"],
        [name, symbol, initial * TOKEN, cap * TOKEN, deployer, deployer],
    ).hex()
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": deployer, "data": artifact["bytecode"] + args,
                                 "gas": 5_000_000})
    )
    return Web3.to_checksum_address(receipt["contractAddress"])


def balance_of(w3, token: str, holder: str) -> int:
    return w3.eth.contract(address=token, abi=load_abi()).functions.balanceOf(holder).call()


def make_wallets(path: Path, n: int) -> list[str]:
    doc = g.build(chains=["evm"], count=n, mode="hd", words=12,
                  mnemonic=None, passphrase="", verify=False)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return [w["evm"]["address"] for w in doc["wallets"]]


def main() -> int:
    # Section 10 reads the real 500-wallet export; it is gitignored (private
    # keys), so a fresh clone gets a throwaway one of the same shape instead.
    wallet_fixtures.ensure(["wallets-500.json"])

    tmp = Path(tempfile.mkdtemp(prefix="dist_token_"))
    results = tmp / "distribution.json"
    wallets_20 = tmp / "wallets-20.json"
    recipients = make_wallets(wallets_20, 20)

    key_file = tmp / "source.key"
    key_file.write_text(SOURCE_KEY + "\n", encoding="utf-8")

    w3 = Web3(EthereumTesterProvider())
    d.build_web3 = lambda rpc, tester=False, tester_keys=None: w3  # one shared chain
    chain_id = w3.eth.chain_id

    token = deploy(w3, SOURCE)
    base = ["--token", token, "--key-file", str(key_file), "--tester",
            "--wallets", str(wallets_20), "--results", str(results)]

    section("1. The plan, and a dry run that sends nothing")
    start_block = w3.eth.block_number
    rc, err, out = run_cli(base + ["--amount", "2000000"])
    check(rc == 0, "exits 0", err or "")
    check("DRY RUN COMPLETE" in out and "Nothing was broadcast" in out, "says it is a dry run")
    check("recipients      : 20" in out, "counts the 20 recipients", out[-400:])
    check("2,000,000" in out, "prints the per-wallet amount")
    check("40,000,000" in out, "prints the total that would move")
    check("960,000,000" in out, "prints what is left in the wallet (1e9 - 40e6)")
    check(w3.eth.block_number == start_block, "mined no blocks")
    check(all(balance_of(w3, token, a) == 0 for a in recipients), "no recipient was funded")
    check(not results.exists(), "wrote no record")

    section("2. --total splits evenly, and the remainder stays with the sender")
    rc, err, out = run_cli(base + ["--total", "1000"])
    check(rc == 0, "--total exits 0", err or "")
    check("each receives   : 50" in out, "1,000 split across 20 is 50 each", out[-600:])
    check("total to send   : 1,000" in out, "total is exactly 1,000")

    rc, err, out = run_cli(base + ["--total", "1005"])
    check(rc == 0, "an indivisible total is accepted", err or "")
    check("each receives   : 50.25" in out,
          "a fractional remainder is represented exactly, not rounded", out[-600:])

    section("3. A real broadcast funds every recipient")
    rc, err, out = run_cli(base + ["--amount", "2000000", "--execute", "--yes"])
    check(rc == 0, "exits 0", err or out[-300:])
    want = 2_000_000 * TOKEN
    balances = [balance_of(w3, token, a) for a in recipients]
    check(all(b == want for b in balances), "all 20 hold exactly 2,000,000")
    check(balance_of(w3, token, SOURCE) == SUPPLY * TOKEN - want * 20,
          "the sender is down exactly 40,000,000")

    doc = json.loads(results.read_text(encoding="utf-8"))
    transfers = doc["transfers"]
    check(len(transfers) == 20, "20 transfers recorded")
    check(all(t["status"] == "success" for t in transfers), "every transfer succeeded")
    check(len({t["nonce"] for t in transfers}) == 20, "20 distinct nonces")
    check(all(len(t["tx_hash"]) == 66 for t in transfers), "every hash is 32-byte hex")
    check(all(int(t["gas_used"]) > 21000 for t in transfers),
          "each transfer cost more than a plain send")
    check(doc["runs"][-1]["sent"] == 20 and doc["runs"][-1]["symbol"] == "TT",
          "the run summary records what was sent")

    section("4. Resuming never pays twice")
    before = [balance_of(w3, token, a) for a in recipients]
    rc, err, out = run_cli(base + ["--amount", "2000000", "--execute", "--yes"])
    after = [balance_of(w3, token, a) for a in recipients]
    check(rc == 0, "the resumed run exits 0", err or "")
    check(before == after, "no one was paid twice")
    check("already recorded as paid" in out and "Nothing to do" in out,
          "it reports that all 20 were already paid and stops", out[-300:])
    check(len(json.loads(results.read_text(encoding="utf-8"))["transfers"]) == 20,
          "the record did not grow")

    section("5. --limit funds a prefix, and a second run continues")
    part = tmp / "part.json"
    fifth_before = balance_of(w3, token, recipients[4])
    rc, err, out = run_cli(base + ["--amount", "1000", "--limit", "5",
                                    "--results", str(part), "--execute", "--yes"])
    check(rc == 0, "--limit 5 exits 0", err or "")
    check("recipients      : 5" in out, "only 5 recipients are planned", out[-500:])
    check(balance_of(w3, token, recipients[4]) - fifth_before == 1_000 * TOKEN,
          "the fifth gained exactly its 1,000 share")
    check(balance_of(w3, token, recipients[5]) == 2_000_000 * TOKEN,
          "the sixth was untouched by the limited run, holding only its section-3 share")

    section("6. Guards: balance, caps, duplicates, bad input")
    rc, err, out = run_cli(base + ["--amount", "9999999999"])
    check(rc == -1 and "holds" in err and "but this plan sends" in err,
          "a plan bigger than the balance is refused", err[:200])

    rc, err, out = run_cli(base + ["--amount", "2000000", "--max-total", "1000"])
    check(rc == -1 and "over the" in err, "a --max-total cap is enforced", err[:200])

    # A gas limit that the entire balance could pay for ONCE, so 20 of them are
    # 20x what is available. Derived from the live balance rather than hardcoded,
    # so it stays true whatever the in-memory chain's defaults are. A FRESH
    # --results file, because a resumed record would leave nothing pending and
    # the cost check is (correctly) skipped when there is nothing to send.
    price = w3.eth.gas_price
    too_much_gas = w3.eth.get_balance(SOURCE) // price
    gas_results = tmp / "gas.json"
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "2000000",
                            "--gas-limit", str(too_much_gas), "--results", str(gas_results)])
    check(rc == 1 and "INSUFFICIENT" in out,
          "a gas cost the wallet cannot pay is refused before broadcasting", out[-400:])
    check("cannot afford even one transfer" in out or "affords about" in out,
          "and says what the balance does afford", out[-300:])
    check(not gas_results.exists(), "and wrote no record, because nothing was sent")

    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1",
                            "--gas-limit", "1", "--results", str(tmp / "g.json")])
    check(rc == -1 and "below the" in err and "fail out of gas" in err,
          "an explicit gas limit below the measured cost is refused", err[:200])

    dup = tmp / "dup.json"
    dup.write_text(json.dumps([recipients[0], recipients[0], SOURCE, recipients[1]]),
                   encoding="utf-8")
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(dup), "--amount", "1000",
                            "--results", str(tmp / "d.json")])
    check(rc == 0 and "recipients      : 2" in out,
          "duplicates and self-sends are dropped", out[-500:])
    check("1 duplicate(s)" in out and "1 self-send(s)" in out,
          "and the plan says how many were dropped", out[-500:])

    bad = tmp / "bad.json"
    bad.write_text(json.dumps(["0xnotanaddress"]), encoding="utf-8")
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(bad), "--amount", "1"])
    check(rc == -1 and "invalid address" in err, "an invalid address is refused", err[:160])

    rc, err, out = run_cli(["--network", "polygon", "--token", token, "--key-file", str(key_file),
                            "--wallets", str(wallets_20), "--amount", "1"])
    check(rc == -1 and "i-know-this-is-mainnet" in err,
          "mainnet is refused without the explicit flag", err[:160])

    rc, err, out = run_cli(base + ["--amount", "1", "--wallets", str(tmp / "missing.json")])
    check(rc == -1 and "no such recipient file" in err,
          "a missing recipient file is refused with a message, not a traceback", err[:160])

    enc = tmp / "enc.json"
    enc.write_text(json.dumps({"encrypted": True, "wallets": []}), encoding="utf-8")
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(enc), "--amount", "1"])
    check(rc == -1 and "encrypted" in err, "an encrypted wallet export is refused clearly",
          err[:160])

    section("7. --spend-all prices at base+tip and trims to what the balance affords")
    # What one transfer costs under each pricing rule, straight from the chain.
    default_fees = d.eth_common.fee_params(w3)
    exact_fees = d.exact_fee_params(w3)
    check(exact_fees["maxFeePerGas"] < default_fees["maxFeePerGas"],
          "exact pricing is cheaper than the 2x-base ceiling",
          f"{exact_fees['maxFeePerGas']} vs {default_fees['maxFeePerGas']}")
    check(exact_fees["maxFeePerGas"] == w3.eth.get_block('latest')['baseFeePerGas']
          + w3.eth.max_priority_fee,
          "and is exactly base + tip, the price a transfer actually pays")

    # Force a real trim using only valid parameters: reserve the BLOCK gas limit
    # (30,000,000 -- the most any chain will accept in one transaction) per
    # transfer, then leave the account able to pay for exactly three of them.
    # Note the reservation is what trims; each transfer still only consumes ~58k
    # gas. The leftover is well above the ~0.03 ETH this in-memory chain needs
    # for the tool's own eth_calls.
    price = exact_fees["maxFeePerGas"]
    big_gas = 30_000_000
    per_tx = big_gas * price
    # 3.5 transfers' worth: the midpoint of the band that affords exactly 3, so a
    # little fee drift between blocks cannot flip the outcome.
    target = per_tx * 7 // 2
    balance = w3.eth.get_balance(SOURCE)
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": SOURCE, "to": ACCOUNT_2,
                                 "value": balance - target - 21_000 * price}))
    balance = w3.eth.get_balance(SOURCE)
    check(balance // per_tx == 3, "the balance affords exactly 3 of these transfers",
          f"{balance} / {per_tx} = {balance // per_tx}")

    # The live case exactly: an --amount that 20 recipients could NOT afford
    # (20 x 60,000,000 = 1.2bn against ~960m held), which is only valid once the
    # POL trim cuts the list. The token check must not refuse it before the trim.
    each_check = 60_000_000
    check(balance_of(w3, token, SOURCE) < each_check * 20 * TOKEN,
          "20 recipients of 60,000,000 would exceed the token balance")

    before = [balance_of(w3, token, a) for a in recipients]
    spend_all = tmp / "spend-all.json"
    argv = ["--token", token, "--key-file", str(key_file), "--tester",
            "--wallets", str(wallets_20), "--amount", str(each_check),
            "--gas-limit", str(big_gas), "--spend-all", "--results", str(spend_all)]

    rc, err, out = run_cli(argv)
    check(rc == 0, "--spend-all exits 0", err or out[-300:])
    check("recipients      : 3" in out,
          "it trimmed the plan to the 3 transfers the balance affords", out[-800:])
    check("TRIMMED by --spend-all from 20 to 3" in out, "and says so in the plan")
    check("limited by POL for gas" in out, "naming POL as the binding limit", out[-900:])
    check("total to send   : 180,000,000 TT" in out,
          "the token total is recomputed from the trimmed count, not the original",
          out[-800:])

    # And when TOKENS are the tighter limit rather than POL, that must be reported
    # instead -- it is the limit a resumed run usually hits.
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "60000000",
                            "--spend-all", "--results", str(tmp / "by-tokens.json")])
    held_now = balance_of(w3, token, SOURCE)
    expect_n = held_now // (60_000_000 * TOKEN)
    check(rc == 0, "the token-limited plan still exits 0", err or out[-300:])
    check(f"recipients      : {expect_n}" in out,
          f"it trimmed to the {expect_n} the token balance covers", out[-900:])
    check("limited by the TT balance" in out, "naming the token balance as the limit",
          out[-900:])
    check(f"total to send   : {expect_n * 60_000_000:,} TT" in out,
          "and the total matches that count", out[-900:])

    rc, err, out = run_cli(argv + ["--execute", "--yes"])
    check(rc == 0, "the trimmed run broadcasts", err or out[-300:])
    gained = [a for a, b in zip(recipients, before)
              if balance_of(w3, token, a) - b == each_check * TOKEN]
    check(len(gained) == 3, "exactly 3 recipients gained their 60,000,000", str(len(gained)))
    doc3 = json.loads(spend_all.read_text(encoding="utf-8"))
    check(len(doc3["transfers"]) == 3, "the record holds exactly 3 transfers")
    check(all(t["status"] == "success" for t in doc3["transfers"]), "all succeeded")
    check(int(doc3.get("runs", [{}])[-1].get("gas_used_total", 0)) > 3 * 21000,
          "the run summary totals the gas", json.dumps(doc3.get("runs", [{}])[-1])[:160])

    # Now leave less than ONE transfer's reservation, so nothing at all is
    # affordable and it must refuse rather than send a doomed transaction.
    # A balance too small for even one transfer is impractical to reach for real
    # here: this in-memory chain requires ~0.03 ETH of headroom for an eth_call,
    # which is a large fraction of one 30M-gas reservation, and evaluating it
    # would test the harness rather than the tool. So the balance READ is stubbed
    # for one call -- the tool then sees a wallet with 1 wei and must refuse.
    real_get_balance = w3.eth.get_balance
    w3.eth.get_balance = lambda *a, **k: 1
    try:
        rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                                "--wallets", str(wallets_20), "--amount", "1000",
                                "--gas-limit", str(big_gas), "--spend-all",
                                "--results", str(tmp / "none.json")])
    finally:
        w3.eth.get_balance = real_get_balance
    check(rc == -1 and "nothing can be sent from this wallet" in err,
          "with nothing affordable it refuses clearly instead of looping", err[:220])
    check(not (tmp / "none.json").exists(), "and wrote no record")

    # Hand the reserved POL back: later sections deploy a token, which needs gas.
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": ACCOUNT_2, "to": SOURCE,
                                 "value": w3.eth.get_balance(ACCOUNT_2) - per_tx}))
    check(w3.eth.get_balance(SOURCE) > per_tx,
          "the drained balance was returned", str(w3.eth.get_balance(SOURCE)))

    section("7b. A run interrupted between broadcast and receipt never pays twice")
    # Reproduce the live failure: a transfer that WAS mined but whose receipt was
    # lost to a rate-limited RPC, so the record still says "broadcast".
    interrupted = tmp / "interrupted.json"
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1000",
                            "--no-resume", "--results", str(interrupted),
                            "--execute", "--yes"])
    check(rc == 0, "a control run broadcasts against a fresh record", err or out[-200:])
    doc_i = json.loads(interrupted.read_text(encoding="utf-8"))
    mined_hash = doc_i["transfers"][0]["tx_hash"]
    doc_i["transfers"][0].update({"status": "broadcast", "block": None, "gas_used": None})
    interrupted.write_text(json.dumps(doc_i), encoding="utf-8")

    recipient_0_before = balance_of(w3, token, recipients[0])
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1000",
                            "--results", str(interrupted)])
    check(rc == 0, "the resumed run exits 0", err or out[-300:])
    check("reconciled #" in out and "confirmed on chain" in out,
          "it resolved the interrupted entry against the chain", out[-900:])
    doc_i = json.loads(interrupted.read_text(encoding="utf-8"))
    check(doc_i["transfers"][0]["status"] == "success",
          "and marked it successful", doc_i["transfers"][0]["status"])
    check(doc_i["transfers"][0]["reconciled"] is True, "recording that it was reconciled")
    check(balance_of(w3, token, recipients[0]) == recipient_0_before,
          "that wallet was NOT paid a second time")
    check(len(json.loads(interrupted.read_text(encoding="utf-8"))["transfers"]) == 20,
          "and no extra transfer was recorded")

    # An entry whose transaction is unknown and whose nonce has moved past it: it
    # must be treated as paid, not resent.
    ghost = tmp / "ghost.json"
    ghost.write_text(json.dumps({"transfers": [{
        "index": 1, "to": recipients[1], "amount": "1000", "amount_wei": str(1000 * TOKEN),
        "nonce": 0, "tx_hash": "0x" + "ab" * 32, "status": "broadcast",
        "block": None, "gas_used": None}]}), encoding="utf-8")
    recipient_1_before = balance_of(w3, token, recipients[1])
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1000",
                            "--results", str(ghost)])
    check(rc == 0, "an unresolvable broadcast entry does not crash the run", err or out[-200:])
    check("nonce has moved past it" in out, "and is reported as mined rather than resent",
          out[-900:])
    check(balance_of(w3, token, recipients[1]) == recipient_1_before,
          "so that wallet was not sent to again")

    # A REVERTED transfer is known not to have paid, so it must be retried.
    revert_path = tmp / "revert.json"
    revert_path.write_text(json.dumps({"transfers": [{
        "index": 1, "to": recipients[2], "amount": "1000", "amount_wei": str(1000 * TOKEN),
        "nonce": 0, "tx_hash": "0x" + "cd" * 32, "status": "reverted",
        "block": 1, "gas_used": 21000}]}), encoding="utf-8")
    recipient_2_before = balance_of(w3, token, recipients[2])
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1000",
                            "--results", str(revert_path), "--execute", "--yes"])
    check(rc == 0, "a reverted entry does not block the run", err or out[-200:])
    check(balance_of(w3, token, recipients[2]) - recipient_2_before == 1000 * TOKEN,
          "and its recipient IS paid, because a revert paid nobody")

    section("7c. The record's own observed gas raises the limit above a bad estimate")
    # The live failure: an RPC estimated 39,921 where 58,303 was needed, and the
    # transfer reverted out of gas. A record holding a real, higher gas_used must
    # lift the limit, so the same mistake cannot repeat on a resumed run.
    learned = tmp / "learned.json"
    learned.write_text(json.dumps({"transfers": [{
        "index": 1, "to": recipients[3], "amount": "1000", "amount_wei": str(1000 * TOKEN),
        "nonce": 0, "tx_hash": "0x" + "ee" * 32, "status": "success",
        "block": 1, "gas_used": 250_000}]}), encoding="utf-8")
    probe = w3.eth.estimate_gas({"from": SOURCE, "to": token, "value": 0,
                                 "data": d.transfer_calldata(recipients[4], 1000 * TOKEN)})
    check(probe < 250_000, "the node's own estimate is below the recorded cost",
          str(probe))
    rc, err, out = run_cli(["--token", token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1000",
                            "--results", str(learned)])
    check(rc == 0, "the plan still exits 0", err or out[-200:])
    check("this record's earlier transfers used up to 250,000 gas" in out,
          "it says the record raised the estimate", out[-900:])
    check("gas per transfer: estimated 250000" in out,
          "and the limit is built from the recorded cost, not the node's", out[-900:])

    section("8. A deployment record is cross-checked against the chain")
    good = tmp / "deployment.json"
    good.write_text(json.dumps({"status": "confirmed", "address": token, "chain_id": chain_id,
                                "name": "Test Token", "symbol": "TT"}), encoding="utf-8")
    rc, err, out = run_cli(["--deployment", str(good), "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1",
                            "--results", str(tmp / "rec.json")])
    check(rc == 0, "a matching record is accepted", err or out[-200:])
    wrong = tmp / "wrong.json"
    wrong.write_text(json.dumps({"address": token, "name": "Something Else", "symbol": "XX"}),
                     encoding="utf-8")
    rc, err, out = run_cli(["--deployment", str(wrong), "--key-file", str(key_file), "--tester",
                            "--wallets", str(wallets_20), "--amount", "1"])
    check(rc == -1 and "Wrong token" in err, "a record naming another token is refused", err[:160])

    section("9. The amount arithmetic is exact, and agrees with the deployer's")
    import deploy_token

    for text in ("1", "1000000", "1000000000", "123456789", "50.25", "0.000000000000000001"):
        check(d.parse_amount(text, 18, "amount") == deploy_token.parse_supply(text, "amount"),
              f"parse_amount agrees with deploy_token for {text}")
    check(d.parse_amount("1", 6, "amount") == 10**6, "6-decimal tokens scale by 10**6")
    check(d.parse_amount("1", 0, "amount") == 1, "a 0-decimal token scales by 1")
    check(d.human(2_000_000 * TOKEN, 18) == "2,000,000", "human() formats whole tokens")
    check(d.human(5 * TOKEN // 10, 18) == "0.5", "and fractions")
    for text in ("-5", "abc", ""):
        try:
            d.parse_amount(text, 18, "amount")
            check(False, f"{text!r} is rejected")
        except SystemExit:
            check(True, f"{text!r} is rejected as not an amount")
    try:
        d.parse_amount("0.0000000000000000001", 18, "amount")
        check(False, "more decimals than the token has is rejected")
    except SystemExit as exc:
        check("decimal places" in str(exc), "more decimals than the token has is rejected")

    check(d.transfer_calldata(SOURCE, 1).startswith("0xa9059cbb"),
          "the calldata carries the ERC-20 transfer selector")

    section("10. The real 500-wallet file, read but not funded")
    real = Path("wallets-500.json")
    if not real.is_file():
        raise SystemExit(f"fixture missing: {real} was not provisioned")
    loaded = d.load_recipients(real)
    cleaned, dups, selfs = d.clean_recipients(loaded, SOURCE, str(real))
    check(len(loaded) == 500, "500 addresses load from the file", str(len(loaded)))
    check(len(cleaned) == 500 and not dups and not selfs,
          "all 500 are unique and none is the sender")
    # A fresh token shaped like the live one: the shared `token` has been spent
    # down by the sections above, so sending 1,000,000,000 from it is correctly
    # refused and would prove nothing about the 500-wallet file.
    real_token = deploy(w3, SOURCE, name="mindglitchofdecimal.loveweb",
                        symbol="LOVEWEB", initial=1_000_000_000, cap=1_000_000_000)
    rc, err, out = run_cli(["--token", real_token, "--key-file", str(key_file), "--tester",
                            "--wallets", str(real), "--total", "1000000000",
                            "--results", str(tmp / "real.json")])
    check(rc == 0, "a dry run over all 500 exits 0", err or out[-300:])
    check("recipients      : 500" in out, "the plan covers all 500")
    check("2,000,000 LOVEWEB" in out or "2,000,000" in out,
          "splitting 1,000,000,000 across 500 is 2,000,000 each", out[-700:])
    check(not (tmp / "real.json").exists(), "and nothing was written")

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
