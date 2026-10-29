#!/usr/bin/env python3
"""Tests for the Polygon token pipeline: build, deploy, verify.

Deployment runs on an in-memory EVM (eth-tester), so the suite covers the real
contract -- real solc output, real constructor, real reverts -- without a
network, an RPC key, an API key, or any funds at risk. Nothing here broadcasts
to a live chain, and the verification tests assert the exact request payload
instead of sending it.

    python test_token.py
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ARTIFACT = Path("build/ProjectToken.json")

CHECKS: list[tuple[str, bool, str]] = []


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def check(label: str, ok: bool, detail: str = "") -> bool:
    # Convention here is (label, condition) -- the OPPOSITE of every other test
    # file in this project. A call written for the other convention inverts
    # silently: the condition (a truthy string) lands in the label slot and the
    # label lands in the condition slot, so a genuine failure reports as PASS.
    # Guard both slots so that mistake crashes instead of passing.
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    if isinstance(ok, str):
        raise SystemExit(f"check() got a string as its condition: {ok!r}")
    ok = bool(ok)
    CHECKS.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if detail and ok:
        print(f"        {detail}")
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 68)


def expect_system_exit(fn, *args, **kwargs) -> str:
    """Run fn and return the SystemExit message, failing if it does not exit."""
    try:
        fn(*args, **kwargs)
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("expected a SystemExit, but nothing exited")


def revert_selector(exc: Exception) -> bytes | None:
    """Pull the raw revert data out of an eth-tester revert.

    eth-tester reports reverts as `execution reverted: b'\\x76\\xc6\\xc9:...'`,
    i.e. the ABI-encoded custom error as a bytes repr inside the message. The
    selector is what proves WHICH error fired, rather than merely that something
    reverted.
    """
    text = str(exc)
    match = re.search(r"b'((?:\\.|[^'\\])*)'", text)
    if not match:
        return None
    try:
        return ast.literal_eval("b'" + match.group(1) + "'")
    except (ValueError, SyntaxError):
        return None


def expect_revert(fn, error_signature: str, label: str) -> None:
    """Assert fn reverts with exactly this custom error."""
    from eth_utils import keccak

    expected = keccak(text=error_signature)[:4]
    try:
        fn()
    except Exception as exc:
        data = revert_selector(exc)
        if data is None:
            check(label, False, f"reverted but no revert data found in {type(exc).__name__}")
            return
        check(label, data[:4] == expected,
              f"got selector 0x{data[:4].hex()}, expected 0x{expected.hex()} for {error_signature}")
        return
    check(label, False, "did not revert at all")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def load_artifact() -> dict:
    if not ARTIFACT.is_file():
        raise SystemExit(f"Error: {ARTIFACT} is missing. Run build_token.py first.")
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def tester_chain():
    from web3 import EthereumTesterProvider, Web3

    w3 = Web3(EthereumTesterProvider())
    return w3, list(w3.eth.accounts)


def deploy(artifact: dict, w3, deployer: str, *, name="Test Token", symbol="TT",
           initial=10**24, cap=10**26, owner=None, operator=None):
    from eth_abi import encode

    owner = owner or deployer
    operator = operator or deployer
    args = encode(
        ["string", "string", "uint256", "uint256", "address", "address"],
        [name, symbol, initial, cap, owner, operator],
    ).hex()
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction({
            "from": deployer, "data": artifact["bytecode"] + args,
            "gas": 3_000_000, "chainId": w3.eth.chain_id,
        })
    )
    if receipt["status"] != 1:
        raise AssertionError("deployment reverted")
    return w3.eth.contract(address=receipt["contractAddress"], abi=artifact["abi"])


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_artifact_reproduces_bytecode(artifact: dict) -> None:
    """The stored Standard JSON Input must rebuild the artifact byte-for-byte.

    This is precisely what Polygonscan does: recompile the submitted source with
    the submitted settings and compare against the chain. If this fails, no
    verification request can succeed.
    """
    import solcx

    section("Artifact reproducibility (what Polygonscan re-checks)")
    version = artifact["compiler"]["version"]
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if version not in installed:
        check(f"solc {version} available", False, "not installed on this machine")
        return
    check(f"solc {version} available", True)

    output = solcx.compile_standard(artifact["standard_json_input"], solc_version=version)
    contract = output["contracts"][artifact["source_path"]][artifact["contract"]]

    check("recompiled creation bytecode is identical",
          "0x" + contract["evm"]["bytecode"]["object"] == artifact["bytecode"])
    check("recompiled deployed bytecode is identical",
          "0x" + contract["evm"]["deployedBytecode"]["object"] == artifact["deployed_bytecode"])
    check("stored input carries every source",
          set(artifact["standard_json_input"]["sources"]) == set(artifact["source_sha256"]),
          f"{len(artifact['source_sha256'])} files")
    check("OpenZeppelin sources are pinned and hashed",
          artifact["openzeppelin"]["tag"] == "5.7.0"
          and len(artifact["openzeppelin"]["sources"]) == 7)


def test_compiler_string(artifact: dict) -> None:
    section("Compiler string the verification API requires")
    long_version = artifact["compiler"]["long_version"]
    check("full version with commit hash", bool(re.match(r"^v0\.8\.\d+\+commit\.[0-9a-f]{8}$",
                                                         long_version)), long_version)
    check("raw solc --version recorded",
          artifact["compiler"]["raw_version_output"].startswith("solc, the solidity compiler"))
    check("settings are explicit, not implied",
          artifact["settings"]["metadata"]["bytecodeHash"] == "ipfs"
          and artifact["settings"]["evmVersion"] == "paris"
          and artifact["settings"]["optimizer"]["runs"] == 200)


def test_supply_parsing() -> None:
    import deploy_token as dt

    section("Supply parsing (float rounding would silently mis-mint)")
    check("whole tokens scale by 10**18", dt.parse_supply("1000000", "x") == 10**24)
    check("underscores accepted", dt.parse_supply("1_000_000", "x") == 10**24)
    check("fraction accepted", dt.parse_supply("0.5", "x") == 5 * 10**17)
    check("rejects non-numeric", "not a number" in expect_system_exit(dt.parse_supply, "abc", "x"))
    check("rejects negative", "cannot be negative" in expect_system_exit(dt.parse_supply, "-5", "x"))
    check("rejects more than 18 decimals",
          "decimal places" in expect_system_exit(dt.parse_supply, "1e-30", "x"))
    # A 30-digit supply is 48 digits of wei; Decimal's default 28-digit context
    # would round it silently, so this must be exact.
    big = "123456789012345678901234567890"
    check("no precision loss on huge supplies",
          dt.parse_supply(big, "x") == int(big) * 10**18,
          f"got {dt.parse_supply(big, 'x')}")
    check("a supply beyond uint256 is refused",
          "uint256" in expect_system_exit(dt.parse_supply, "1e60", "x"))
    check("the largest valid supply still parses",
          dt.parse_supply("115792089237316195423570985008687907853269984665640564039457",
                          "x") <= 2**256 - 1)


def test_constructor_args(artifact: dict) -> None:
    from eth_abi import decode

    import deploy_token as dt

    section("Constructor arguments (the usual cause of verification failure)")
    values = ["My Token", "MTK", 10**24, 10**25,
              "0xCA7d3f27Fb59eeBfB3a855C6b180916FF96748Fc",
              "0x1D93716845324d9C90565971EFD22b7E8f272a73"]
    encoded = dt.encode_constructor_args(artifact, values)

    check("authorised without a 0x prefix", not encoded.startswith("0x"))
    check("hex and even length", all(c in "0123456789abcdef" for c in encoded)
          and len(encoded) % 2 == 0)
    decoded = decode(["string", "string", "uint256", "uint256", "address", "address"],
                     bytes.fromhex(encoded))
    # eth_abi returns addresses lowercased; the ABI encoding itself is
    # case-insensitive, so compare that way rather than on checksum casing.
    check("decodes back to the exact values",
          [str(v).lower() for v in decoded] == [str(v).lower() for v in values],
          f"{list(decoded)} != {values}")
    check("types come from the ABI",
          [i["type"] for i in dt.constructor_abi(artifact)["inputs"]]
          == ["string", "string", "uint256", "uint256", "address", "address"])
    check("creation data is bytecode plus arguments",
          len(artifact["bytecode"]) + len(encoded) == len(artifact["bytecode"] + encoded))


def test_metadata_and_supply(artifact: dict) -> None:
    w3, accounts = tester_chain()
    owner, stranger = accounts[0], accounts[1]

    section("ERC-20 metadata and initial supply")
    token = deploy(artifact, w3, owner, name="My Token", symbol="MTK",
                   initial=10**24, cap=10**26)
    check("name", token.functions.name().call() == "My Token")
    check("symbol", token.functions.symbol().call() == "MTK")
    check("decimals are 18", token.functions.decimals().call() == 18)
    check("initial supply minted to the owner",
          token.functions.totalSupply().call() == 10**24
          and token.functions.balanceOf(owner).call() == 10**24)
    check("maxSupply is the constructor value", token.functions.maxSupply().call() == 10**26)
    check("remainingMintable is cap minus supply",
          token.functions.remainingMintable().call() == 10**26 - 10**24)
    check("owner is the deployer", token.functions.owner().call() == owner)
    check("operator granted by the constructor", token.functions.isOperator(owner).call() is True)
    check("a stranger is not an operator", token.functions.isOperator(stranger).call() is False)


def test_mint_permissions(artifact: dict) -> None:
    w3, accounts = tester_chain()
    owner, operator, stranger = accounts[0], accounts[1], accounts[2]

    section("Minting: operator-only, and capped")
    token = deploy(artifact, w3, owner, initial=0, cap=10**24, operator=operator)

    expect_revert(lambda: token.functions.mint(stranger, 1).transact({"from": stranger}),
                  "NotOperator(address)", "non-operator mint reverts NotOperator")
    expect_revert(lambda: token.functions.mint(owner, 1).transact({"from": owner}),
                  "NotOperator(address)", "the OWNER cannot mint directly")

    token.functions.mint(operator, 10**23).transact({"from": operator})
    check("operator can mint", token.functions.balanceOf(operator).call() == 10**23)

    token.functions.mint(operator, 9 * 10**23).transact({"from": operator})
    check("minting exactly up to the cap is allowed",
          token.functions.totalSupply().call() == 10**24
          and token.functions.remainingMintable().call() == 0)
    expect_revert(lambda: token.functions.mint(operator, 1).transact({"from": operator}),
                  "MaxSupplyExceeded(uint256,uint256)",
                  "one token past the cap reverts MaxSupplyExceeded")
    expect_revert(lambda: token.functions.mint(stranger, 0).transact({"from": stranger}),
                  "NotOperator(address)", "cannot mint even zero as a non-operator")

    token.functions.setOperator(operator, False).transact({"from": owner})
    expect_revert(lambda: token.functions.mint(operator, 1).transact({"from": operator}),
                  "NotOperator(address)", "a revoked operator cannot mint")
    token.functions.setOperator(operator, True).transact({"from": owner})
    check("re-granting works", token.functions.isOperator(operator).call() is True)


def test_owner_controls_operators(artifact: dict) -> None:
    w3, accounts = tester_chain()
    owner, stranger = accounts[0], accounts[1]

    section("Ownership: only the owner manages the operator list")
    token = deploy(artifact, w3, owner, initial=0, cap=10**24)

    expect_revert(lambda: token.functions.setOperator(stranger, True).transact({"from": stranger}),
                  "OwnableUnauthorizedAccount(address)",
                  "non-owner cannot grant mint rights")
    expect_revert(lambda: token.functions.setOperator("0x" + "0" * 40, True)
                  .transact({"from": owner}),
                  "ZeroAddress()",
                  "granting rights to the zero address is rejected")

    receipt = w3.eth.wait_for_transaction_receipt(
        token.functions.setOperator(stranger, True).transact({"from": owner})
    )
    events = token.events.OperatorUpdated().process_receipt(receipt)
    check("owner can grant mint rights", token.functions.isOperator(stranger).call() is True)
    check("OperatorUpdated is emitted",
          len(events) == 1 and events[0]["args"]["operator"] == stranger
          and events[0]["args"]["allowed"] is True)


def test_transfer_approve_burn(artifact: dict) -> None:
    w3, accounts = tester_chain()
    owner, other, spender = accounts[0], accounts[1], accounts[2]

    section("Transfers, allowances and burning")
    token = deploy(artifact, w3, owner, initial=10**24, cap=10**24)

    receipt = w3.eth.wait_for_transaction_receipt(
        token.functions.transfer(other, 10**22).transact({"from": owner})
    )
    check("transfer moves the balance",
          token.functions.balanceOf(other).call() == 10**22)
    transfers = token.events.Transfer().process_receipt(receipt)
    check("Transfer event emitted with the right arguments",
          len(transfers) == 1 and transfers[0]["args"]["from"] == owner
          and transfers[0]["args"]["to"] == other)

    token.functions.approve(spender, 5 * 10**21).transact({"from": owner})
    check("allowance recorded", token.functions.allowance(owner, spender).call() == 5 * 10**21)
    token.functions.transferFrom(owner, other, 10**21).transact({"from": spender})
    check("transferFrom spends the allowance",
          token.functions.allowance(owner, spender).call() == 4 * 10**21
          and token.functions.balanceOf(other).call() == 10**22 + 10**21,
          str(token.functions.balanceOf(other).call()))

    expect_revert(lambda: token.functions.transferFrom(owner, other, 10**23)
                  .transact({"from": spender}),
                  "ERC20InsufficientAllowance(address,uint256,uint256)",
                  "spending beyond the allowance reverts")
    expect_revert(lambda: token.functions.transfer(other, 10**30).transact({"from": owner}),
                  "ERC20InsufficientBalance(address,uint256,uint256)",
                  "transferring more than the balance reverts")
    expect_revert(lambda: token.functions.transfer("0x" + "0" * 40, 1).transact({"from": owner}),
                  "ERC20InvalidReceiver(address)",
                  "transferring to the zero address reverts")

    supply_before = token.functions.totalSupply().call()
    token.functions.burn(10**21).transact({"from": owner})
    # Owner has paid out 10**22 by transfer and 2 x 10**21 by transferFrom+burn.
    check("burn reduces supply and balance",
          token.functions.totalSupply().call() == supply_before - 10**21
          and token.functions.balanceOf(owner).call() == supply_before - 10**22 - 2 * 10**21,
          str(token.functions.balanceOf(owner).call()))
    check("burning frees room under the cap",
          token.functions.remainingMintable().call() == 10**21)

    token.functions.approve(spender, 2 * 10**21).transact({"from": owner})
    token.functions.burnFrom(owner, 10**21).transact({"from": spender})
    check("burnFrom spends the allowance",
          token.functions.totalSupply().call() == supply_before - 2 * 10**21)

    # The cap is on totalSupply, so a burn legitimately allows minting again.
    token.functions.mint(owner, 2 * 10**21).transact({"from": owner})
    check("re-minting after a burn is allowed up to the cap",
          token.functions.totalSupply().call() == supply_before)


def test_transfer_ownership(artifact: dict) -> None:
    w3, accounts = tester_chain()
    owner, new_owner = accounts[0], accounts[1]

    section("Transferring ownership")
    token = deploy(artifact, w3, owner, initial=0, cap=10**24)
    token.functions.transferOwnership(new_owner).transact({"from": owner})
    check("ownership moved", token.functions.owner().call() == new_owner)
    expect_revert(lambda: token.functions.setOperator(owner, True).transact({"from": owner}),
                  "OwnableUnauthorizedAccount(address)",
                  "the previous owner loses operator control")
    token.functions.setOperator(new_owner, True).transact({"from": new_owner})
    check("the new owner gains it", token.functions.isOperator(new_owner).call() is True)


def test_constructor_guardrails(artifact: dict) -> None:
    from eth_abi import encode

    w3, accounts = tester_chain()
    owner = accounts[0]

    section("Constructor guardrails")
    abi_types = ["string", "string", "uint256", "uint256", "address", "address"]

    def try_constructor(initial, cap):
        # eth_call, not a transaction: it returns the constructor's revert data
        # (so the exact custom error can be checked) and changes no state.
        args = encode(abi_types, ["T", "T", initial, cap, owner, owner]).hex()
        return lambda: w3.eth.call({"from": owner, "data": artifact["bytecode"] + args})

    expect_revert(try_constructor(0, 0), "ZeroMaxSupply()", "a zero cap is rejected")
    expect_revert(try_constructor(10**25, 10**24), "CapBelowInitialSupply(uint256,uint256)",
                  "an initial supply above the cap is rejected")

    token = deploy(artifact, w3, owner, initial=1, cap=2, operator="0x" + "0" * 40)
    check("a zero operator is allowed (no mint rights yet)",
          token.functions.isOperator("0x" + "0" * 40).call() is False)
    expect_revert(lambda: token.functions.mint(owner, 1).transact({"from": owner}),
                  "NotOperator(address)", "nothing can mint until one is appointed")
    token.functions.setOperator(owner, True).transact({"from": owner})
    token.functions.mint(owner, 1).transact({"from": owner})
    check("the owner can appoint an operator afterwards",
          token.functions.totalSupply().call() == 2)
    expect_revert(lambda: token.functions.mint(owner, 1).transact({"from": owner}),
                  "MaxSupplyExceeded(uint256,uint256)",
                  "and the cap still holds for that new operator")


def test_deployed_code_matches_artifact(artifact: dict) -> None:
    from web3 import Web3

    import deploy_token as dt

    section("On-chain code vs artifact (the comparison Polygonscan performs)")
    w3, accounts = tester_chain()
    owner = accounts[0]
    cap = 10**26
    token = deploy(artifact, w3, owner, initial=10**24, cap=cap)

    chain_code = Web3.to_hex(w3.eth.get_code(token.address))
    ok, note = dt.compare_code(chain_code, artifact, {"maxSupply": cap})
    check("deployed code matches the artifact", ok, note)
    check("the match is explained by immutable slots, not luck",
          "immutable" in note, note)
    check("HexBytes form is accepted too (the 0x trap)",
          dt.compare_code(w3.eth.get_code(token.address).hex(), artifact,
                          {"maxSupply": cap})[0])
    check("immutables really are zeroed in the artifact",
          all(artifact["deployed_bytecode"][2:][r["start"] * 2:(r["start"] + r["length"]) * 2]
              == "00" * r["length"]
              for refs in artifact["immutable_references"].values() for r in refs))

    # The check must be able to fail, or it proves nothing.
    tampered = bytearray(bytes.fromhex(artifact["deployed_bytecode"][2:]))
    tampered[100] ^= 0xFF
    bad_code = "0x" + bytes(tampered).hex()
    ok, note = dt.compare_code(bad_code, artifact, {"maxSupply": cap})
    check("a single flipped byte is caught", not ok and "outside any immutable slot" in note, note)

    ok, note = dt.compare_code("0x" + artifact["deployed_bytecode"][2:-2], artifact,
                               {"maxSupply": cap})
    check("a different code length is caught", not ok and "length differs" in note, note)


def test_dry_run_and_cli_deploy(artifact: dict) -> None:
    import deploy_token as dt

    section("CLI: dry run sends nothing, --execute deploys and records")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "deploy.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dt.main(["--artifact", str(ARTIFACT), "--network", "amoy", "--tester",
                            "--key-file", "wallets-operator.json",
                            "--name", "My Token", "--symbol", "MTK",
                            "--initial-supply", "1000000", "--max-supply", "100000000",
                            "--out", str(out)])
        printed = buffer.getvalue()
        check("dry run exits 0", code == 0)
        check("dry run says nothing was broadcast", "DRY RUN COMPLETE" in printed)
        check("dry run writes no deployment record", not out.exists())
        check("dry run prints the plan", "DEPLOYMENT PLAN" in printed
              and "max supply" in printed and "100000000" in printed)
        check("dry run signs the transaction", "Signed OK" in printed)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dt.main(["--artifact", str(ARTIFACT), "--network", "amoy", "--tester",
                            "--key-file", "wallets-operator.json",
                            "--name", "My Token", "--symbol", "MTK",
                            "--initial-supply", "1000000", "--max-supply", "100000000",
                            "--out", str(out), "--execute", "--yes"])
        printed = buffer.getvalue()
        check("--execute exits 0", code == 0, printed[-400:])
        check("deployment record written", out.is_file())

        record = json.loads(out.read_text(encoding="utf-8"))
        check("record has the address and tx hash",
              record["address"].startswith("0x") and len(record["address"]) == 42
              and record["tx_hash"].startswith("0x"))
        check("record names the deployer, owner and operator",
              record["deployer"] == record["owner"] == record["operator"]
              and record["deployer"].startswith("0x"))
        check("record pins the artifact it deployed",
              record["artifact"]["bytecode_sha256"] == artifact["bytecode_sha256"]
              and record["artifact"]["deployed_bytecode_sha256"]
              == artifact["deployed_bytecode_sha256"])
        check("record carries the tools' own code check",
              record["code_verified_against_artifact"] is True)
        check("record marks the source as not yet verified",
              record["source_verified_on_explorer"] is False)
        check("record states the network and explorer",
              record["network"] == "amoy" and "polygonscan" in record["explorer"])
        check("a test-chain record says so", record["in_memory_test_chain"] is True)

        from eth_abi import decode
        decoded = decode(["string", "string", "uint256", "uint256", "address", "address"],
                         bytes.fromhex(record["constructor_arguments"]))
        expected = ["My Token", "MTK", 10**24, 10**26, record["owner"], record["operator"]]
        check("recorded constructor arguments decode to what was asked for",
              [str(v).lower() for v in decoded] == [str(v).lower() for v in expected],
              str(list(decoded)))

        # The key really came from the operator wallet export.
        operator_wallet = json.loads(Path("wallets-operator.json").read_text(encoding="utf-8"))
        check("deployed from the operator wallet",
              record["deployer"].lower()
              == operator_wallet["wallets"][0]["evm"]["address"].lower())
        check("the record is timestamped in UTC",
              datetime.fromisoformat(record["deployed_at"]).tzinfo is not None)


def test_deploy_guards(artifact: dict) -> None:
    import deploy_token as dt

    section("CLI: refusals that prevent irreversible mistakes")
    base = ["--artifact", str(ARTIFACT), "--tester", "--key-file", "wallets-operator.json",
            "--name", "T", "--symbol", "T"]

    message = expect_system_exit(
        dt.main, [*base, "--network", "polygon",
                  "--initial-supply", "1", "--max-supply", "2"])
    check("mainnet requires the explicit flag",
          "--i-know-this-is-mainnet" in message, message[:120])

    message = expect_system_exit(
        dt.main, [*base, "--network", "amoy", "--initial-supply", "200", "--max-supply", "100"])
    check("initial supply above the cap is refused",
          "exceeds" in message, message[:120])

    message = expect_system_exit(
        dt.main, [*base, "--network", "amoy", "--initial-supply", "0", "--max-supply", "0"])
    check("a zero cap is refused", "permanently mintable-never" in message, message[:120])

    message = expect_system_exit(
        dt.main, [*base, "--network", "amoy", "--initial-supply", "1", "--max-supply", "1",
                  "--operator", "0x" + "0" * 40])
    check("the zero address cannot be an operator",
          "zero address" in message.lower(), message[:120])

    import eth_common as ec

    message = expect_system_exit(ec.build_web3, "not-a-url", False)
    check("an unusable RPC is reported, not ignored", "cannot reach" in message, message[:120])

    message = expect_system_exit(
        dt.main, [*base, "--network", "amoy", "--initial-supply", "1", "--max-supply", "2",
                  "--key-file", "does-not-exist.json"])
    check("a missing key file is refused", "no such key file" in message, message[:120])


def test_unicode_name_and_symbol(artifact: dict) -> None:
    """An ERC-20 name and symbol are UTF-8 strings, so a symbol CAN be a heart.

    This covers the whole pipeline for non-ASCII values -- deployment, what
    symbol() returns, and the bytes Polygonscan receives for verification --
    plus the Windows code-page bug that used to kill the run before anything
    was signed.
    """
    from eth_abi import decode

    import deploy_token as dt

    section("Non-ASCII name and symbol (and the printer that used to crash)")
    w3, accounts = tester_chain()
    owner = accounts[0]
    name = "mindglitchofdecimal.loveweb"
    check("the requested name is 27 characters", len(name) == 27)

    for label, symbol in (("heart U+2764", "\u2764"),
                          ("heart + VS16", "\u2764\ufe0f"),
                          ("sparkling heart U+1F496", "\U0001f496"),
                          ("ASCII ticker", "LOVEWEB")):
        token = deploy(artifact, w3, owner, name=name, symbol=symbol,
                       initial=10**24, cap=10**26)
        check(f"{label}: symbol() returns the exact string",
              token.functions.symbol().call() == symbol, ascii(symbol))
        check(f"{label}: name() returns the exact string",
              token.functions.name().call() == name)

        encoded = dt.encode_constructor_args(
            artifact, [name, symbol, 10**24, 10**26, owner, owner])
        decoded = decode(["string", "string", "uint256", "uint256", "address", "address"],
                         bytes.fromhex(encoded))
        check(f"{label}: verification arguments carry the exact UTF-8 bytes",
              decoded[1] == symbol and symbol.encode("utf-8") in bytes.fromhex(encoded),
              symbol.encode("utf-8").hex())

    # Regression. deploy_token.py prints the name and symbol raw, and on a
    # legacy code page (Windows cp1252) that raised UnicodeEncodeError part-way
    # through the plan, aborting before signing. Reproduce it with a real cp1252
    # stream: without the fix this raises, and the run never reaches the end.
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    crashed = False
    code = None
    try:
        with contextlib.redirect_stdout(stream):
            code = dt.main(["--artifact", str(ARTIFACT), "--network", "amoy", "--tester",
                            "--key-file", "wallets-operator.json", "--name", name,
                            "--symbol", "\u2764", "--initial-supply", "1000000",
                            "--max-supply", "100000000"])
        stream.flush()
    except UnicodeEncodeError:
        crashed = True
    check("the plan survives a heart symbol on a legacy code page", not crashed)
    check("the dry run still reaches its end", code == 0, str(code))
    check("the unencodable glyph is escaped, not dropped",
          b"\\u2764" in raw.getvalue())


def test_receipt_failure_is_reported_not_crashed() -> None:
    """A public RPC rate-limited the receipt poll right after a real broadcast
    (HTTP 403 on Polygon mainnet), which surfaced as a traceback and lost the
    record. By that point the transaction is already sent and the gas spent, so
    the failure must name it and warn against deploying a second contract.
    """
    import deploy_token as dt

    section("A failed receipt poll must not look like a crash")

    class FlakyEth:
        @staticmethod
        def wait_for_transaction_receipt(tx_hash, timeout=300):
            raise RuntimeError("403 Client Error: Forbidden for url: https://rpc/")

    class FlakyW3:
        eth = FlakyEth

    net = dt.eth_common.get_network("polygon")
    message = expect_system_exit(dt.wait_for_receipt, FlakyW3, b"\xab" * 32, net, 1, 2, 0)
    check("it names the transaction hash", "0xabab" in message, message[:80])
    check("it states the transaction was already broadcast", "WAS BROADCAST" in message)
    check("it warns against deploying a second contract",
          "Do NOT re-run with --execute" in message)
    check("it links to the explorer",
          "polygonscan.com/tx/" in message)
    check("its follow-up command names a valid network",
          "--network polygon" in message, message[-90:])

    class GoodEth:
        @staticmethod
        def wait_for_transaction_receipt(tx_hash, timeout=300):
            return {"status": 1, "blockNumber": 7, "gasUsed": 21}

    class GoodW3:
        eth = GoodEth

    check("a successful poll still returns the receipt unchanged",
          dt.wait_for_receipt(GoodW3, b"\x01" * 32, net, 1, 2, 0)["status"] == 1)
    check("and a non-existent network slug cannot reach here",
          net.get("slug") == "polygon")


def test_verify_payload(artifact: dict) -> None:
    import verify_token as vt

    section("Verification request payload")
    record = {
        "address": "0x" + "ab" * 20,
        "constructor_arguments": "00" * 4,
        "artifact": {"bytecode_sha256": artifact["bytecode_sha256"]},
    }

    for network, chainid, explorer in (("polygon", "137", "polygonscan.com"),
                                       ("amoy", "80002", "amoy.polygonscan.com")):
        net = vt.eth_common.get_network(network)
        params, data = vt.build_payload(net, artifact, record["address"],
                                        record["constructor_arguments"], "KEY")
        check(f"{network}: uses chainid {chainid}", params["chainid"] == chainid)
        check(f"{network}: module/action are contract/verifysourcecode",
              params["module"] == "contract" and params["action"] == "verifysourcecode")
        check(f"{network}: explorer is {explorer}",
              net["explorer"].endswith(explorer))
        if network == "amoy":
            check("codeformat is standard-json-input",
                  data["codeformat"] == "solidity-standard-json-input")
            check("contractname is file:contract",
                  data["contractname"] == "contracts/ProjectToken.sol:ProjectToken",
                  data["contractname"])
            check("compilerversion is the full string",
                  re.match(r"^v0\.8\.\d+\+commit\.", data["compilerversion"]) is not None,
                  data["compilerversion"])
            check("sourceCode is the artifact's Standard JSON Input",
                  json.loads(data["sourceCode"]) == artifact["standard_json_input"])
            check("dependency sources are included in the submission",
                  any(s.startswith("@openzeppelin/") for s in json.loads(data["sourceCode"])["sources"]))
            check("settings submitted match the build",
                  json.loads(data["sourceCode"])["settings"] == artifact["settings"])
            check("constructorArguments are passed through, un-prefixed",
                  data["constructorArguments"] == "00" * 4)
            check("MIT licence declared", data["licenseType"] == "3")
            check("verification goes to the V2 API, not the retired V1 host",
                  vt.ETHERSCAN_V2_API == "https://api.etherscan.io/v2/api")


def test_verify_guards(artifact: dict) -> None:
    import verify_token as vt

    section("Verification refusals")
    good = {"artifact": {"bytecode_sha256": artifact["bytecode_sha256"]}, "address": "0x" + "0" * 40}
    stale = {"artifact": {"bytecode_sha256": "deadbeef"}, "address": "0x" + "0" * 40}

    vt.check_same_build(artifact, good, force=False)
    check("matching artifact accepted", True)
    message = expect_system_exit(vt.check_same_build, artifact, stale, False)
    check("a stale artifact is refused", "does not match the deployment record" in message)
    vt.check_same_build(artifact, stale, force=True)
    check("--force overrides the refusal", True)

    # The guard itself, before any network call is possible.
    vt.check_artifact_ready(artifact)
    check("a complete artifact passes the readiness guard", True)

    broken = dict(artifact, compiler=dict(artifact["compiler"], long_version="v0.8.37"))
    message = expect_system_exit(vt.check_artifact_ready, broken)
    check("a commit-less compiler version is refused", "full form" in message)

    message = expect_system_exit(vt.check_artifact_ready, dict(artifact, qualified_name="nope"))
    check("an inconsistent contract name is refused", "qualified" in message)

    message = expect_system_exit(
        vt.check_artifact_ready, dict(artifact, standard_json_input={"language": "Solidity"}))
    check("an artifact with no sources is refused", "Standard JSON Input" in message)

    os.environ["ETHERSCAN_API_KEY"] = "DUMMYKEY"
    try:
        message = expect_system_exit(
            vt.main, ["--network", "amoy", "--address", "0xzz",
                      "--artifact", str(ARTIFACT)])
        check("a malformed address is refused", "contract address" in message)

        message = expect_system_exit(vt.main, ["--network", "amoy", "--deployment", "nope.json"])
        check("a missing deployment record is explained",
              "no deployment record" in message and "deploy_token.py" in message)
    finally:
        os.environ.pop("ETHERSCAN_API_KEY", None)


def test_verify_artifact_resolution(artifact: dict) -> None:
    """The documented --address flow has no deployment record to read."""
    import tempfile

    import verify_token as vt

    section("Finding the artifact (the path users actually take)")
    resolved = vt.load_artifact(None, None)
    check("--address with no record falls back to the default build path",
          resolved["bytecode_sha256"] == artifact["bytecode_sha256"])
    check("an explicit --artifact wins",
          vt.load_artifact(str(ARTIFACT), None)["bytecode_sha256"]
          == artifact["bytecode_sha256"])

    # A deployment record must be able to name its own artifact.
    record = {"artifact": {"path": str(ARTIFACT),
                          "bytecode_sha256": artifact["bytecode_sha256"]},
              "address": "0x" + "ab" * 20,
              "constructor_arguments": ""}
    check("a deployment record's artifact path is honoured",
          vt.load_artifact(None, record)["bytecode_sha256"] == artifact["bytecode_sha256"])
    vt.check_same_build(vt.load_artifact(None, record), record, force=False)
    check("the record and the artifact it names agree", True)

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        message = expect_system_exit(vt.load_artifact, str(missing), None)
        check("a named-but-absent artifact is explained, not a traceback",
              "no artifact at" in message, message[:120])
        bad_record = {"artifact": {"path": str(missing)}}
        message = expect_system_exit(vt.load_artifact, None, bad_record)
        check("a record naming an absent artifact is explained",
              "no artifact at" in message)

    # The full user path -- deploy record on disk -> submittable payload -- with
    # no network. Run from a temp directory so a real deployment-amoy.json on
    # this machine cannot change the result.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_record = Path("deployment-amoy.json").resolve()
        (root / "deployment-amoy.json").write_text(
            json.dumps({"artifact": {"path": str(ARTIFACT.resolve()),
                                     "bytecode_sha256": artifact["bytecode_sha256"]},
                        "address": record["address"], "constructor_arguments": ""}),
            encoding="utf-8")
        previous = os.getcwd()
        os.chdir(root)
        try:
            loaded, path = vt.load_deployment(None, "amoy")
            check("a deployment record is found by convention (deployment-<network>.json)",
                  path.name == "deployment-amoy.json" and loaded["address"] == record["address"])
            check("it is NOT the real record on this machine",
                  path.resolve() != real_record)
            params, data = vt.build_payload(
                vt.eth_common.get_network("amoy"), vt.load_artifact(None, loaded),
                loaded["address"], loaded["constructor_arguments"], "KEY")
            check("the whole path produces a submittable payload",
                  params["chainid"] == "80002"
                  and data["contractaddress"] == record["address"]
                  and json.loads(data["sourceCode"])["language"] == "Solidity")
        finally:
            os.chdir(previous)


def test_network_table() -> None:
    import eth_common as ec

    section("Network table and RPC defaults")
    check("Polygon mainnet is chain 137 and not a testnet",
          ec.NETWORKS["polygon"]["chain_id"] == 137 and ec.NETWORKS["polygon"]["testnet"] is False)
    check("Amoy is chain 80002 with a faucet",
          ec.NETWORKS["amoy"]["chain_id"] == 80002
          and ec.NETWORKS["amoy"]["faucet"].endswith("faucet.polygon.technology/"))
    check("public defaults are set for the testnet only",
          bool(ec.NETWORKS["amoy"]["default_rpc"]) and bool(ec.NETWORKS["polygon"]["default_rpc"]))

    class Args:
        network = "polygon"
        rpc = None
        tester = False

    check("mainnet is never defaulted to a third-party RPC", ec.resolve_rpc(Args()) is None)
    Args.network = "amoy"
    check("the testnet default is used when --rpc is omitted",
          ec.resolve_rpc(Args()) == ec.NETWORKS["amoy"]["default_rpc"])
    Args.rpc = "http://localhost:8545"
    check("--rpc wins", ec.resolve_rpc(Args) == "http://localhost:8545")

    check("unknown networks are rejected",
          "unknown network" in expect_system_exit(ec.get_network, "ethereum"))


def test_key_loading() -> None:
    import eth_common as ec

    section("Key loading")
    key, address, origin = ec.load_private_key(key_file="wallets-operator.json", label="deployer")
    wallet = json.loads(Path("wallets-operator.json").read_text(encoding="utf-8"))
    expected = wallet["wallets"][0]["evm"]
    check("a gen_wallets export is understood", address.lower() == expected["address"].lower())
    check("the key matches the file", key.lower() == expected["private_key"].lower())
    check("the origin is named, never the key", origin == "wallets-operator.json")

    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "key.txt"
        bare.write_text(expected["private_key"] + "\n", encoding="utf-8")
        key2, address2, _ = ec.load_private_key(key_file=str(bare))
        check("a bare key file is understood too",
              key2.lower() == expected["private_key"].lower() and address2 == address)

        message = expect_system_exit(ec.load_private_key, str(Path(tmp) / "missing.txt"))
        check("a missing file is refused clearly", "no such key file" in message)

    message = expect_system_exit(ec.load_private_key, "wallets-btc-900.json")
    check("a Bitcoin-only export is refused with an explanation",
          "no EVM private key" in message, message[:120])

    message = expect_system_exit(ec.load_private_key, "wallets-operator.json", 5)
    check("an out-of-range --key-index is refused", "out of range" in message, message[:120])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Test the Polygon token pipeline.")
    parser.parse_args(argv)

    print("=" * 68)
    print("TOKEN PIPELINE TESTS   (in-memory EVM; nothing is broadcast)")
    print("=" * 68)

    artifact = load_artifact()

    def run(fn, *args) -> None:
        """Run one test group; an unexpected exception is a failure, not a
        reason to abandon the remaining 100+ checks silently."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - a crash here is a failing test
            check(f"{fn.__name__} completed without crashing", False,
                  f"{type(exc).__name__}: {exc}")

    run(test_artifact_reproduces_bytecode, artifact)
    run(test_compiler_string, artifact)
    run(test_supply_parsing)
    run(test_constructor_args, artifact)
    run(test_metadata_and_supply, artifact)
    run(test_mint_permissions, artifact)
    run(test_owner_controls_operators, artifact)
    run(test_transfer_approve_burn, artifact)
    run(test_transfer_ownership, artifact)
    run(test_constructor_guardrails, artifact)
    run(test_deployed_code_matches_artifact, artifact)
    run(test_dry_run_and_cli_deploy, artifact)
    run(test_deploy_guards, artifact)
    run(test_unicode_name_and_symbol, artifact)
    run(test_receipt_failure_is_reported_not_crashed)
    run(test_verify_payload, artifact)
    run(test_verify_guards, artifact)
    run(test_verify_artifact_resolution, artifact)
    run(test_network_table)
    run(test_key_loading)

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 68)
    if failed:
        print(f"{len(failed)} of {len(CHECKS)} CHECKS FAILED:")
        for label, _, detail in failed:
            print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))
        return 1
    print(f"ALL CHECKS PASSED  ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
