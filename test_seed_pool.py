#!/usr/bin/env python3
"""Tests for seed_pool.py.

Two layers:

  * pure helpers -- the arithmetic that decides how many base units 100,000
    tokens is, and what the deposit prices the token at;
  * a real end-to-end run on an in-memory chain: the token from build/ is
    deployed, a mock QuickSwap router (factory/WETH/addLiquidityETH, compiled
    here) stands in for the DEX, and main() signs and broadcasts approve +
    addLiquidityETH for real. No network, no funds.

The point of the end-to-end half is the failure that costs money: seeding the
same pool twice. The second run must refuse.

    python test_seed_pool.py
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import seed_pool as s
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

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


def section(title: str) -> None:
    print(f"\n{title}")


MOCK_SOL = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transferFrom(address, address, uint256) external returns (bool);
    function balanceOf(address) external view returns (uint256);
}

contract MockPair {
    address public token0;
    address public token1;
    uint112 private r0;
    uint112 private r1;
    uint32 private stamp;

    constructor(address a) { token0 = a; token1 = address(this); }
    receive() external payable {}
    function getReserves() external view returns (uint112, uint112, uint32) {
        return (r0, r1, stamp);
    }
    function balanceOf(address) external pure returns (uint256) { return 1e18; }
    function sync(uint256 a, uint256 b) external { r0 = uint112(a); r1 = uint112(b); stamp = uint32(block.timestamp); }
}

interface IPair {
    function sync(uint256 a, uint256 b) external;
    function balanceOf(address) external view returns (uint256);
}

contract MockFactory {
    address public pair;
    constructor(address p) { pair = p; }
    function getPair(address, address) external view returns (address) { return pair; }
}

contract MockRouter {
    address public factoryAddr;
    address public wmatic;
    address payable public pairAddr;
    address public lastToken;
    uint256 public calls;

    constructor(address f, address w, address payable p) { factoryAddr = f; wmatic = w; pairAddr = p; }
    function factory() external view returns (address) { return factoryAddr; }
    function WETH() external view returns (address) { return wmatic; }
    function addLiquidityETH(address token, uint256 amountTokenDesired, uint256 amountTokenMin,
        uint256 amountETHMin, address, uint256 deadline)
        external payable returns (uint256, uint256, uint256) {
        require(block.timestamp <= deadline, "expired");
        require(msg.value >= amountETHMin, "pol slippage");
        require(IERC20(token).transferFrom(msg.sender, pairAddr, amountTokenDesired), "transferFrom");
        pairAddr.transfer(msg.value);
        IPair(pairAddr).sync(IERC20(token).balanceOf(pairAddr), pairAddr.balance);
        lastToken = token;
        calls += 1;
        return (amountTokenDesired, msg.value, 1e18);
    }
}
"""


def compile_mock():
    """Compile the mock router, installing solc if this machine has none.

    A clean checkout -- and every CI runner -- starts with an empty ~/.solcx, so
    `versions[0]` would raise IndexError and fail this suite for a reason that
    has nothing to do with the code under test. build_token.py already treats
    downloading solc as normal, so the fixture does the same.
    """
    import solcx

    versions = [str(v) for v in solcx.get_installed_solc_versions()]
    if "0.8.24" in versions:
        version = "0.8.24"
    elif versions:
        version = versions[0]
    else:
        print("  (no solc installed; fetching 0.8.24 for the mock router)")
        solcx.install_solc("0.8.24")
        version = "0.8.24"
    compiled = solcx.compile_source(MOCK_SOL, output_values=["abi", "bin"],
                                    solc_version=version)
    return {name.split(":")[-1]: value for name, value in compiled.items()}


def deploy(w3, abi, bytecode, account, *args) -> str:
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = factory.constructor(*args).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 4_000_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw))
    return receipt["contractAddress"]


def run(argv: list[str]) -> tuple[int, str]:
    """Call main() and capture everything it prints."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = s.main(argv)
    except SystemExit as exc:
        # SystemExit.code is an int for sys.exit(n) but a STRING when main()
        # raises with a message -- the error paths here all do the latter, so
        # int(exc.code) would crash instead of reporting the refusal.
        code = exc.code
        rc = code if isinstance(code, int) else (0 if not code else 1)
        return rc, buf.getvalue() + str(exc)
    return int(rc), buf.getvalue()


def pure_checks() -> None:
    section("amounts: 100,000 tokens is not 100,000 base units")

    units = s.parse_amount("100000", 18, "amount")
    check(units == 100_000 * 10**18, f"100000 tokens -> {units} base units (1e23)")
    check(units != 100_000, "100000 tokens is NOT 100000 base units (1e-13 LOVEWEB)")
    check(s.parse_amount("100,000", 18, "amount") == units, "thousands separator accepted")
    check(s.parse_amount("100_000", 18, "amount") == units, "underscore separator accepted")
    check(s.parse_amount("1", 6, "amount") == 10**6, "decimals come from the token, not 18")
    check(s.parse_amount("0.0000015", 18, "amount") == 1_500_000_000_000,
          "fractional amounts scale exactly")

    section("slippage floors, never rounds up")
    check(s.with_slippage(10**18, 100) == 99 * 10**16, "1% tolerance floors to 0.99")
    check(s.with_slippage(10**18, 0) == 10**18, "0 bps keeps the full amount")
    try:
        s.with_slippage(10**18, 10_000)
        check(False, "100% slippage is rejected")
    except ValueError:
        check(True, "100% slippage is rejected")

    section("price the deposit sets")
    pricing = s.price_and_fdv(5 * 10**18, 100_000 * 10**18, 10**9 * 10**18)
    check(abs(pricing["pol_per_token"] - 0.00005) < 1e-12,
          f"5 POL / 100,000 tokens = {pricing['pol_per_token']:.8f} POL each")
    check(abs(pricing["fdv_pol"] - 50_000) < 1e-6,
          f"implied FDV at 1B supply = {pricing['fdv_pol']:,.0f} POL")

    section("resume guard keys on the broadcast, not on success")
    pool, signer = "0xAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaa", "0xBBbbBBbbBBbbBBbbBBbbBBbbBBbbBBbbBBbbBBbb"
    check(s.already_seeded({"pool": pool.lower(), "signer": signer.lower(),
                            "status": "success"}, pool, signer),
          "success blocks a reseed")
    check(s.already_seeded({"pool": pool.upper().replace("0X", "0x"), "signer": signer,
                            "status": "broadcast"}, pool, signer),
          "a lost receipt (still 'broadcast') blocks a reseed")
    check(not s.already_seeded({"pool": pool, "signer": signer, "status": "reverted"},
                               pool, signer), "a reverted attempt does not block")
    check(not s.already_seeded({"pool": pool, "signer": signer, "status": "success"},
                               pool, "0xCCccCCccCCccCCccCCccCCccCCccCCccCCccCCcc"),
          "another signer is not blocked by this one")


def guard_checks(tmp: Path) -> None:
    section("guards that must fire before anything is signed")

    rc, out = run(["--network", "polygon", "--token", "0x" + "11" * 20,
                   "--amount", "1", "--pol", "1"])
    check(rc != 0 and "--i-know-this-is-mainnet" in out,
          "mainnet refuses without --i-know-this-is-mainnet")

    rc, out = run(["--network", "amoy", "--token", "0x" + "11" * 20,
                   "--amount", "1", "--pol", "1"])
    check(rc != 0 and "QuickSwap" in out, f"unknown DEX refuses cleanly ({out.strip()[:60]})")

    deployment = tmp / "dep.json"
    deployment.write_text(json.dumps({"address": "0x" + "22" * 20}), encoding="utf-8")
    check(s.resolve_token(type("A", (), {"token": None, "deployment": str(deployment)}))
          == "0x" + "22" * 20, "token address can come from the deployment record")

    try:
        s.resolve_token(type("A", (), {"token": None, "deployment": None}))
        check(False, "--token or --deployment is required")
    except SystemExit:
        check(True, "--token or --deployment is required")


def end_to_end(tmp: Path) -> None:
    section("end-to-end: approve + addLiquidityETH on an in-memory chain")

    w3 = Web3(EthereumTesterProvider())
    key = "0x" + "00" * 31 + "0f"
    account = Account.from_key(key)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": account.address,
                             "value": 500 * 10**18})

    mock = compile_mock()
    artifact = json.loads(Path("build/ProjectToken.json").read_text(encoding="utf-8"))
    bytecode = artifact.get("bytecode") or artifact.get("bin")
    if isinstance(bytecode, dict):
        bytecode = bytecode.get("object")
    supply = 200_000 * 10**18

    # The token first: the pair records token0 at construction, and read_pair()
    # decides which reserve is the token by comparing token0 with the address.
    # Deploying the pair against a placeholder makes that comparison lie.
    token = deploy(w3, artifact["abi"], bytecode, account,
                   "mindglitchofdecimal.loveweb", "LOVEWEB", supply, 10**9 * 10**18,
                   account.address, account.address)
    pair = deploy(w3, mock["MockPair"]["abi"], mock["MockPair"]["bin"], account, token)
    factory = deploy(w3, mock["MockFactory"]["abi"], mock["MockFactory"]["bin"], account, pair)
    router = deploy(w3, mock["MockRouter"]["abi"], mock["MockRouter"]["bin"], account,
                    factory, "0x" + "0d" + "50" * 19, pair)
    key_file = tmp / "signer.key"
    key_file.write_text(key, encoding="utf-8")
    results = tmp / "seed.json"

    s.DEXES["polygon"] = {"name": "Mock QuickSwap", "router": router,
                          "factory": factory, "wmatic": "0x" + "0d" + "50" * 19}
    # main() asks eth_common for a chain; hand it THIS one. A second
    # EthereumTesterProvider() would be a different, empty chain -- the mocks
    # and the token deployed above would simply not exist there.
    s.eth_common.build_web3 = lambda *a, **k: w3

    base = ["--network", "polygon", "--i-know-this-is-mainnet", "--tester",
            "--token", token, "--amount", "100000", "--pol", "5",
            "--key-file", str(key_file), "--results", str(results),
            "--deadline-minutes", "60"]

    rc, out = run(base)
    check(rc == 0, "dry run exits 0")
    check("nothing was signed" in out, "dry run broadcasts nothing")
    check(not results.exists(), "dry run writes no record")
    token_contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=s.TOKEN_ABI)
    check(token_contract.functions.balanceOf(account.address).call() == supply,
          "dry run moved no tokens")

    rc, out = run(base + ["--execute", "--yes"])
    check(rc == 0, f"execute exits 0 ({out.strip().splitlines()[-1][:60]})")
    record = json.loads(results.read_text(encoding="utf-8"))
    check(record.get("status") == "success", "record says success")
    check(record.get("token_units") == 100_000 * 10**18,
          f"deposited 100000 tokens = {record.get('token_units')} base units, not 100000")
    check(record.get("pol_units") == 5 * 10**18, "deposited 5 POL")
    # Approve is for the exact deposit, and the router spends exactly that, so
    # nothing is left behind. An unlimited approval would leave ~2**256-1e23.
    check(token_contract.functions.allowance(account.address, router).call() == 0,
          "approval was exactly the deposit and was fully spent, not unlimited")
    check(token_contract.functions.balanceOf(account.address).call()
          == supply - 100_000 * 10**18, "tokens left the signer exactly once")
    check(token_contract.functions.balanceOf(pair).call() == 100_000 * 10**18,
          "the pair holds the tokens")
    check(w3.eth.get_balance(Web3.to_checksum_address(pair)) == 5 * 10**18,
          "the pair holds the 5 POL")
    router_contract = w3.eth.contract(
        address=Web3.to_checksum_address(router),
        abi=mock["MockRouter"]["abi"])
    check(record.get("reserve_token") == 100_000 * 10**18
          and record.get("reserve_pol") == 5 * 10**18,
          "record reserves match what the pair reports")
    check("add_liquidity_tx" in record and record["add_liquidity_tx"].startswith("0x"),
          "record carries the add-liquidity hash")
    check(abs(record.get("price_pol_per_token", 0) - 0.00005) < 1e-12,
          "record prices the token at 0.00005 POL")
    check(router_contract.functions.calls().call() == 1, "router was called once")

    section("the guard that matters: a second run must not reseed")
    rc, out = run(base + ["--execute", "--yes"])
    check(rc != 0 and "Refusing to seed again" in out, "second run is refused")
    check(router_contract.functions.calls().call() == 1, "router still called exactly once")
    check(token_contract.functions.balanceOf(account.address).call()
          == supply - 100_000 * 10**18, "no second 100,000 left the signer")
    check(w3.eth.get_balance(Web3.to_checksum_address(pair)) == 5 * 10**18,
          "the pair still holds 5 POL, not 10")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="seed_pool_test_"))
    pure_checks()
    guard_checks(tmp)
    end_to_end(tmp)

    print()
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("All seed_pool checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
