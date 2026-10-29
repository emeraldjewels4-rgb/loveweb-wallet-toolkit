#!/usr/bin/env python3
"""End-to-end tests for mint_token.py against an in-memory Ethereum chain.

Real contract, real signed transactions, real reverts, real receipts, real
records -- on a local chain, so no funds are at risk and nothing is broadcast.

    python test_mint.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import mint_token as m
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from web3 import EthereumTesterProvider, Web3

ARTIFACT = Path("build/ProjectToken.json")

# eth-tester's first default account is the address of private key 0x...01.
OPERATOR_KEY = "0x" + "00" * 31 + "01"
OPERATOR = Account.from_key(OPERATOR_KEY).address
STRANGER_KEY = "0x" + "00" * 31 + "02"
STRANGER = Account.from_key(STRANGER_KEY).address

TOKEN = 10**18
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
    """Run main(); return (exit code, SystemExit message or "", captured output).

    stderr is captured as well as stdout, because argparse reports its own
    refusals there and a check would otherwise see only the exit code "2".
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rc = m.main(argv)
        return rc, "", buffer.getvalue()
    except SystemExit as exc:
        return -1, str(exc), buffer.getvalue()


def load_abi() -> list[dict]:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))["abi"]


def deploy(w3, deployer: str, *, name="Test Token", symbol="TT", initial=100,
           cap=1000, owner=None, operator=None) -> str:
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    args = encode(
        ["string", "string", "uint256", "uint256", "address", "address"],
        [name, symbol, initial * TOKEN, cap * TOKEN, owner or deployer, operator or deployer],
    ).hex()
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({"from": deployer, "data": artifact["bytecode"] + args,
                                 "gas": 5_000_000})
    )
    return Web3.to_checksum_address(receipt["contractAddress"])


def supply_of(w3, address: str) -> int:
    return w3.eth.contract(address=address, abi=load_abi()).functions.totalSupply().call()


def balance_of(w3, address: str, holder: str) -> int:
    return w3.eth.contract(address=address, abi=load_abi()
                           ).functions.balanceOf(holder).call()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mint_test_"))
    key_file = tmp / "operator.key"
    key_file.write_text(OPERATOR_KEY + "\n", encoding="utf-8")
    record_path = tmp / "mint.json"
    key_args = ["--key-file", str(key_file)]

    w3 = Web3(EthereumTesterProvider())
    m.build_web3 = lambda rpc, tester=False, tester_keys=None: w3  # one shared chain
    chain_id = w3.eth.chain_id

    # The live token's shape: 100,000,000 minted, 1,000,000,000 immutable cap.
    live = deploy(w3, OPERATOR, name="mindglitchofdecimal.loveweb", symbol="LOVEWEB",
                  initial=100_000_000, cap=1_000_000_000)
    base = ["--token", live, *key_args, "--out", str(record_path), "--tester"]

    # ------------------------------------------------------------------ #
    section("1. The doubling plan, and the exact round the cap stops")
    supply = 100_000_000 * TOKEN
    cap = 1_000_000_000 * TOKEN
    plan = m.plan_doubles(supply, 8, cap)
    check(len(plan) == 8, "eight rounds are planned")
    check([r["round"] for r in plan] == list(range(1, 9)), "rounds are numbered 1..8")
    check(plan[0]["amount_wei"] == supply and plan[0]["new_total_wei"] == 2 * supply,
          "round 1 mints exactly the supply that exists, doubling it")
    check(plan[2]["new_total_wei"] == 8 * supply, "round 3 reaches 8x the start")
    check([r["fits"] for r in plan] == [True, True, True, False, False, False, False, False],
          "only the first three doublings fit under a 1,000,000,000 cap")
    check(plan[3]["new_total_wei"] == 16 * supply,
          "the fourth doubling would need 16x, i.e. 1,600,000,000 tokens")
    check(2**8 * supply == 25_600_000_000 * TOKEN,
          "all eight doublings would require 25,600,000,000 tokens")
    check(m.human(2**8 * supply) == "25,600,000,000", "amounts print with separators")
    check(m.human(TOKEN // 2) == "0.5", "and stay exact below 1 token")

    section("2. Eight doublings is refused before anything is signed")
    block_before = w3.eth.block_number
    rc, err, out = run_cli(base + ["--doubles", "8"])
    check(rc == 1, "exits 1 rather than signing a plan that cannot work")
    check("REFUSING TO SIGN: round 4" in out, "names the round that breaches the cap")
    check("1,600,000,000" in out and "1,000,000,000" in out,
          "shows the supply it would reach and the cap it would pass")
    check("3 of 8 round(s) fit" in out, "says how many rounds do fit")
    check("immutable" in out, "explains that the cap cannot be raised")
    check("--allow-partial" in out, "points at the option that runs what fits")
    check(w3.eth.block_number == block_before, "no transaction was mined")
    check(supply_of(w3, live) == supply, "the supply is unchanged")
    check(not record_path.exists(), "no record file was written")

    section("3. A dry run of three doublings sends nothing")
    rc, err, out = run_cli(base + ["--doubles", "3"])
    check(rc == 0, "exits 0", err or "")
    check("DRY RUN" in out and "Nothing was broadcast" in out, "says it is a dry run")
    check("200,000,000" in out and "800,000,000" in out, "prints the per-round outcome")
    check("mindglitchofdecimal.loveweb (LOVEWEB)" in out, "names the token it will mint")
    check("900,000,000" in out, "prints the headroom under the cap")
    check(w3.eth.block_number == block_before, "mined no blocks")
    check(supply_of(w3, live) == supply, "supply unchanged")
    check(not record_path.exists(), "wrote no record file")

    section("4. Three doublings really move the supply")
    rc, err, out = run_cli(base + ["--doubles", "3", "--execute", "--yes"])
    check(rc == 0, "exits 0", err or out[-200:])
    check(supply_of(w3, live) == 8 * supply, "supply is now 8x, i.e. 800,000,000")
    check(balance_of(w3, live, OPERATOR) == 8 * supply, "and all of it is held by the mint target")
    doc = json.loads(record_path.read_text(encoding="utf-8"))
    run = doc["runs"][-1]
    check(run["status"] == "success", "the run is recorded as success")
    check(len(run["rounds"]) == 3, "three rounds are recorded")
    check(all(r["status"] == "success" for r in run["rounds"]), "every round succeeded")
    check([r["amount_wei"] for r in run["rounds"]] ==
          [str(supply), str(2 * supply), str(4 * supply)],
          "each round minted the supply that existed at the time")
    check(len({r["nonce"] for r in run["rounds"]}) == 3, "three distinct nonces")
    check(all(int(r["gas_used"]) > 21000 for r in run["rounds"]),
          "gas used is recorded per round and exceeds a plain transfer")
    check(all(len(r["tx_hash"]) == 66 for r in run["rounds"]), "every round has a tx hash")
    check(run["end_supply"] == "800000000", "the record's end supply matches the chain")
    check(run["start_supply"] == "100000000", "the record's start supply matches too")
    check(doc["token"] == live and doc["symbol"] == "LOVEWEB", "the record names the token")
    check(doc["max_supply"] == "1000000000", "the record stores the cap it was checked against")

    section("5. Repeating a run appends to the audit trail rather than erasing it")
    rc, err, out = run_cli(base + ["--amount", "100000000", "--execute", "--yes"])
    check(rc == 0, "a flat 100,000,000 mint exits 0", err or out[-160:])
    check(supply_of(w3, live) == 900_000_000 * TOKEN, "800,000,000 -> 900,000,000")
    doc = json.loads(record_path.read_text(encoding="utf-8"))
    check(len(doc["runs"]) == 2, "both runs are recorded")
    check(doc["runs"][0]["end_supply"] == "800000000", "the first run is intact")
    check(doc["runs"][1]["mode"] == "amount", "the second run records its mode")

    rc, err, out = run_cli(base + ["--amount", "100000000", "--execute", "--yes"])
    check(rc == 0, "the third mint exits 0", err or "")
    check(supply_of(w3, live) == 1_000_000_000 * TOKEN,
          "the supply now sits exactly on the 1,000,000,000 cap")
    doc = json.loads(record_path.read_text(encoding="utf-8"))
    check(len(doc["runs"]) == 3, "all three runs are in the one file")

    rc, err, out = run_cli(base + ["--amount", "1"])
    check(rc == 1 and "past the cap" in out, "one token past the cap is refused")
    check(supply_of(w3, live) == 1_000_000_000 * TOKEN, "and nothing was minted")
    check(len(json.loads(record_path.read_text(encoding="utf-8"))["runs"]) == 3,
          "a refused run adds no entry")

    section("6. A fresh token: the cap is hit exactly, then refuses one more")
    exact = deploy(w3, OPERATOR, initial=100, cap=800, symbol="EXACT")
    exact_args = ["--token", exact, *key_args, "--tester"]
    rc, err, out = run_cli(exact_args + ["--out", str(tmp / "exact.json"),
                                          "--doubles", "3", "--execute", "--yes"])
    check(rc == 0, "three doublings 100 -> 800 exit 0", err or "")
    check(supply_of(w3, exact) == 800 * TOKEN, "the supply lands exactly on the cap")

    rc, err, out = run_cli(exact_args + ["--doubles", "4"])
    check(rc == 1, "a further doubling is refused")
    check("REFUSING TO SIGN: round 1" in out,
          "naming round 1, because at the cap even the first round breaches it")
    check("over by" in out or "Over by" in out, "and quantifies the overshoot")

    rc, err, out = run_cli(exact_args + ["--amount", "0.5"])
    check(rc == 1 and "past the cap" in out, "even half a token past the cap is refused")
    rc, err, out = run_cli(exact_args + ["--amount", "0.5", "--allow-partial"])
    check(rc == -1 and "no round fits under the cap" in err,
          "with --allow-partial and no headroom it stops clearly instead of crashing", err)
    check(supply_of(w3, exact) == 800 * TOKEN, "and still minted nothing")

    section("7. --allow-partial mints exactly the rounds that fit")
    part = deploy(w3, OPERATOR, initial=100, cap=1000, symbol="PART")
    part_out = tmp / "part.json"
    rc, err, out = run_cli(["--token", part, *key_args, "--out", str(part_out),
                            "--tester", "--doubles", "8", "--allow-partial",
                            "--execute", "--yes"])
    check(rc == 0, "exits 0", err or out[-200:])
    check(supply_of(w3, part) == 800 * TOKEN, "100 -> 800, three doublings")
    pdoc = json.loads(part_out.read_text(encoding="utf-8"))["runs"][-1]
    check(len(pdoc["rounds"]) == 3, "the three rounds that fit were executed")
    check(pdoc["rounds_skipped_over_cap"] == 5, "the five rounds over the cap were skipped")
    check(pdoc["requested_rounds"] == 8 and pdoc["planned_rounds"] == 3,
          "the record distinguishes what was asked for from what was sent")
    check(pdoc["allow_partial"] is True, "the record says the run was partial")
    check(int(pdoc["end_supply"]) == 800, "the record's end supply is the cap, not past it")

    section("8. A flat amount, in repeated rounds")
    flat = deploy(w3, OPERATOR, initial=100, cap=1000, symbol="FLAT")
    flat_out = tmp / "flat.json"
    rc, err, out = run_cli(["--token", flat, *key_args, "--out", str(flat_out),
                            "--tester", "--amount", "10", "--rounds", "4",
                            "--execute", "--yes"])
    check(rc == 0, "four rounds of 10 exit 0", err or "")
    check(supply_of(w3, flat) == 140 * TOKEN, "100 + 4 x 10 = 140")
    fdoc = json.loads(flat_out.read_text(encoding="utf-8"))["runs"][-1]
    check([r["amount_wei"] for r in fdoc["rounds"]] == [str(10 * TOKEN)] * 4,
          "each round minted the same 10 tokens")
    check(fdoc["mode"] == "amount", "the record says the mode was a flat amount")

    section("9. --to mints to someone else")
    other = deploy(w3, OPERATOR, initial=100, cap=1000, symbol="OTHER")
    other_out = tmp / "other.json"
    rc, err, out = run_cli(["--token", other, *key_args, "--out", str(other_out),
                            "--tester", "--amount", "5", "--to", STRANGER,
                            "--execute", "--yes"])
    check(rc == 0, "exits 0", err or "")
    check(balance_of(w3, other, STRANGER) == 5 * TOKEN, "the stranger received the new tokens")
    check(balance_of(w3, other, OPERATOR) == 100 * TOKEN, "the operator's balance is untouched")
    check(json.loads(other_out.read_text())["runs"][-1]["mint_to"] == STRANGER,
          "the record names the recipient")

    section("10. A non-operator cannot mint, and is told why before signing")
    guarded = deploy(w3, OPERATOR, initial=100, cap=1000, symbol="GUARD",
                     owner=STRANGER, operator=STRANGER)
    g_out = tmp / "guard.json"
    rc, err, out = run_cli(["--token", guarded, *key_args, "--out", str(g_out),
                            "--tester", "--amount", "1", "--execute", "--yes"])
    check(rc == -1 and "is not an operator" in (err or ""), "refused with a clear reason",
          err or "")
    check(STRANGER in (err or ""), "and names the owner who could grant the role")
    check(supply_of(w3, guarded) == 100 * TOKEN, "nothing was minted")
    check(not g_out.exists(), "and no record was written")

    section("11. Guard rails and bad inputs")
    rc, err, out = run_cli(["--network", "polygon", "--token", live, *key_args, "--doubles", "1"])
    check(rc == -1 and "i-know-this-is-mainnet" in (err or ""),
          "mainnet is refused without the explicit flag", err or "")

    for argv, needle in (
        (["--doubles", "0"], "--doubles must be at least 1"),
        (["--doubles", "999999"], "--doubles is capped"),
        (["--amount", "0"], "minting 0 would do nothing"),
        (["--doubles", "2", "--rounds", "3"], "--rounds only applies to --amount"),
        (["--amount", "1", "--rounds", "0"], "--rounds must be at least 1"),
        (["--token", "0x" + "11" * 20, "--amount", "1"], "no contract code"),
        (["--token", "not-an-address", "--amount", "1"], "not a valid address"),
        (["--doubles", "1", "--amount", "1"], "not allowed with"),
    ):
        rc, err, out = run_cli(base + argv)
        check(rc == -1 and needle in err + out, f"rejected: {' '.join(argv)}  [{needle}]",
              (err + out)[-160:])

    section("12. A deployment record is cross-checked against the chain")
    # A fresh token with headroom: `live` is already sitting on its cap by now.
    recorded = deploy(w3, OPERATOR, name="Record Token", symbol="REC",
                      initial=10, cap=100)
    good = tmp / "deployment.json"
    good.write_text(json.dumps({
        "status": "confirmed", "address": recorded, "chain_id": chain_id,
        "name": "Record Token", "symbol": "REC",
    }), encoding="utf-8")
    rc, err, out = run_cli(["--deployment", str(good), *key_args, "--tester", "--doubles", "1"])
    check(rc == 0, "a matching record is accepted", err or out[-160:])
    check(recorded in out and "REC" in out, "and the token is named in the plan")
    check("DRY RUN" in out, "as a dry run")

    for field, wrong, needle in (
        ("name", "Something Else", "Wrong token"),
        ("symbol", "NOPE", "Wrong token"),
        ("chain_id", 1, "chain id"),
        ("address", "0x" + "22" * 20, "no contract code"),
    ):
        bad = tmp / f"bad-{field}.json"
        payload = json.loads(good.read_text(encoding="utf-8"))
        payload[field] = wrong
        bad.write_text(json.dumps(payload), encoding="utf-8")
        rc, err, out = run_cli(["--deployment", str(bad), *key_args, "--tester", "--doubles", "1"])
        check(rc == -1 and needle in err,
              f"a record pointing at a {field} that does not match the chain is refused", err)

    pending = tmp / "pending.json"
    pending.write_text(json.dumps({"status": "broadcast_awaiting_receipt", "address": None}),
                       encoding="utf-8")
    rc, err, out = run_cli(["--deployment", str(pending), *key_args, "--tester", "--doubles", "1"])
    check(rc == -1 and "has no address yet" in (err or ""),
          "a record for a never-confirmed deployment has no token to mint", err or "")

    section("13. Contract reverts are decoded, not dumped")
    abi = load_abi()
    contract = w3.eth.contract(address=exact, abi=abi)  # at its cap already
    try:
        contract.functions.mint(OPERATOR, TOKEN).transact({"from": OPERATOR})
        decoded = None
    except Exception as exc:  # noqa: BLE001 - the revert IS the subject
        decoded = m.decode_revert(abi, exc)
    check(decoded is not None and decoded.startswith("MaxSupplyExceeded"),
          "a live cap breach decodes to MaxSupplyExceeded", str(decoded))
    check(decoded is not None and "801000000000000000000" in decoded
          and "800000000000000000000" in decoded,
          "with the requested total (801e18) and the cap (800e18) in it", str(decoded))

    blocked_contract = w3.eth.contract(address=guarded, abi=abi)
    try:
        blocked_contract.functions.mint(OPERATOR, TOKEN).transact({"from": OPERATOR})
        decoded = None
    except Exception as exc:  # noqa: BLE001
        decoded = m.decode_revert(abi, exc)
    check(decoded is not None and decoded.startswith("NotOperator"),
          "a non-operator call decodes to NotOperator", str(decoded))
    check(OPERATOR.lower() in (decoded or "").lower(), "naming the caller", str(decoded))

    from eth_abi import encode as abi_encode

    class FakeRpcError(Exception):
        """What web3.py raises from a live RPC: the revert data on .data."""

        def __init__(self, data):
            super().__init__("execution reverted")
            self.data = data

    selector = keccak(text="MaxSupplyExceeded(uint256,uint256)")[:4]
    payload = "0x" + (selector + abi_encode(["uint256", "uint256"],
                                            [1_600_000_000 * TOKEN, cap])).hex()
    check(m.decode_revert(abi, FakeRpcError(payload)).startswith("MaxSupplyExceeded"),
          "the same error decodes from the 0x-hex form a live RPC returns")
    check(m.decode_revert(abi, FakeRpcError("0xdeadbeef")) ==
          "selector 0xdeadbeef (not in this contract's ABI)",
          "an unknown selector is reported rather than guessed")
    names = {name for name, _ in m.custom_errors(abi).values()}
    check({"MaxSupplyExceeded", "NotOperator", "ZeroAddress",
           "CapBelowInitialSupply", "ZeroMaxSupply"} <= names,
          "every custom error this contract declares is indexed",
          str(sorted(names)))
    check(m.decode_revert(abi, Exception("no revert data at all")) is None,
          "a non-revert exception yields no decoding")

    section("14. One supply parser, shared with the deployer")
    import deploy_token as d

    check(m.parse_supply is d.parse_supply,
          "mint_token uses deploy_token's parser, so the rounding fix cannot drift")
    check(m.format_supply is d.format_supply and m.TOKEN_SCALE == d.TOKEN_SCALE,
          "and the same scaling and formatting")

    return report()


def report() -> int:
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
