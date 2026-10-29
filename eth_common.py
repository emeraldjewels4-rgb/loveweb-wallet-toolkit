#!/usr/bin/env python3
"""Shared helpers for the Polygon ERC-20 pipeline.

Used by build_token.py, deploy_token.py, verify_token.py and test_token.py.

distribute_eth.py predates this module and keeps its own copy of the key-loading
and fee logic; it is deliberately left alone rather than refactored under a
passing test suite.

Nothing here prints a private key, and nothing here takes one from argv: argv is
stored in shell history and is visible to every process on the machine.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Networks
# --------------------------------------------------------------------------- #
# `api_chainid` is the value the unified Etherscan V2 API expects alongside
# `chainid`; it happens to equal the EVM chain id for these two, but it is kept
# as its own field because they are not the same kind of identifier.
#
# `default_rpc` is a public endpoint that needs no key, verified live rather
# than assumed. polygon-rpc.com currently answers "tenant disabled" and Ankr's
# public Polygon endpoints now demand a key, so neither is used.
NETWORKS: dict[str, dict] = {
    "amoy": {
        "slug": "amoy",
        "name": "Polygon Amoy testnet",
        "chain_id": 80002,
        "api_chainid": 80002,
        "explorer": "https://amoy.polygonscan.com",
        "default_rpc": "https://polygon-amoy-bor-rpc.publicnode.com",
        "currency": "POL",
        "testnet": True,
        "faucet": "https://faucet.polygon.technology/",
    },
    "polygon": {
        "slug": "polygon",
        "name": "Polygon PoS mainnet",
        "chain_id": 137,
        "api_chainid": 137,
        "explorer": "https://polygonscan.com",
        "default_rpc": "https://polygon-bor-rpc.publicnode.com",
        "currency": "POL",
        "testnet": False,
        "faucet": None,
    },
}

# The verification API is Etherscan's unified V2 endpoint for every chain.
# The old per-explorer host (api.polygonscan.com/api) was retired with API V1
# and now answers HTTP 301; sending a key there no longer verifies anything.
ETHERSCAN_V2_API = "https://api.etherscan.io/v2/api"

PRIVATE_KEY_ENV_VARS = ("DEPLOYER_PRIVATE_KEY", "SOURCE_PRIVATE_KEY")


def get_network(name: str) -> dict:
    try:
        return NETWORKS[name.lower()]
    except KeyError:
        raise SystemExit(
            f"Error: unknown network '{name}'. Choose one of: {', '.join(NETWORKS)}"
        )


def add_network_arg(parser, default: str = "amoy") -> None:
    parser.add_argument(
        "--network",
        choices=sorted(NETWORKS),
        default=default,
        help=f"target network (default {default}); mainnet also requires --i-know-this-is-mainnet",
    )


# --------------------------------------------------------------------------- #
# Private keys
# --------------------------------------------------------------------------- #
def private_key_from_export(path: Path, index: int = 0, password: str | None = None) -> str:
    """Pull one EVM private key out of a gen_wallets.py export."""
    from gen_wallets import decrypt_doc

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Error: {path} is not valid JSON ({exc}).")

    if not isinstance(doc, dict) or "wallets" not in doc:
        raise SystemExit(
            f"Error: {path} is not a key file and not a gen_wallets.py export "
            "(no 'wallets' array)."
        )

    if doc.get("encrypted"):
        if password is None:
            raise SystemExit(
                f"Error: {path} is encrypted. Re-run the token tool with "
                "--password-file pointing at the password, or re-export the "
                "wallets in plaintext."
            )
        doc = decrypt_doc(doc, password)

    entries = doc.get("wallets", [])
    if not entries:
        raise SystemExit(f"Error: no wallets in {path}.")
    if not 0 <= index < len(entries):
        raise SystemExit(
            f"Error: --key-index {index} is out of range; {path} holds "
            f"{len(entries)} wallet(s) (0..{len(entries) - 1})."
        )

    entry = entries[index]
    node = entry.get("evm") if isinstance(entry.get("evm"), dict) else entry
    key = node.get("private_key")
    if not key:
        raise SystemExit(
            f"Error: wallet #{index} in {path} has no EVM private key "
            "(was it generated with --chains btc only?)."
        )
    return str(key).strip()


def load_private_key(
    key_file: str | None = None,
    key_index: int = 0,
    password_file: str | None = None,
    label: str = "deployer",
) -> str:
    """Resolve a private key from a file, the environment, or a hidden prompt.

    A file may be either a bare 64-hex-char key or a gen_wallets.py JSON export
    (use --key-index to pick which wallet).
    """
    password = None
    if password_file:
        password = Path(password_file).read_text(encoding="utf-8").rstrip("\n")
        if not password:
            raise SystemExit("Error: the password file is empty.")

    key = None
    origin = None

    if key_file:
        path = Path(key_file)
        if not path.is_file():
            raise SystemExit(f"Error: no such key file: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if text.startswith("{"):
            key = private_key_from_export(path, key_index, password)
        else:
            key = text
        origin = str(path)
    else:
        for env_name in PRIVATE_KEY_ENV_VARS:
            if os.environ.get(env_name):
                key = os.environ[env_name].strip()
                origin = f"${env_name}"
                break

    if not key:
        import getpass

        print(f"No {' or '.join(PRIVATE_KEY_ENV_VARS)} in the environment; reading the {label} key now.")
        print("(Input is hidden and is never written to disk.)")
        key = getpass.getpass(f"{label.capitalize()} private key: ").strip()
        origin = "prompt"

    if not key:
        raise SystemExit("Error: no private key provided.")
    if not key.startswith("0x"):
        key = "0x" + key

    from eth_account import Account

    try:
        account = Account.from_key(key)
    except Exception as exc:
        raise SystemExit(f"Error: {origin} does not hold a valid private key ({exc}).")
    # Return both so callers can print where a key came from without printing it.
    return key, account.address, origin


# --------------------------------------------------------------------------- #
# Chain access
# --------------------------------------------------------------------------- #
def inject_poa_middleware(w3) -> bool:
    """Polygon and Amoy are proof-of-authority chains.

    Their blocks carry far more than 32 bytes of extraData, which web3 rejects
    by default: eth_getBlock raises ExtraDataLengthError. Without this, every
    block read on Polygon fails, and because fee_params() treats a failed block
    read as "this chain has no EIP-1559", the silent result is a legacy
    gas-price transaction instead of the intended EIP-1559 one. Returns False if
    the middleware is unavailable, so the caller can say so rather than pretend.
    """
    try:
        # web3 >= 7 exports the class; web3 6 called it geth_poa_middleware.
        from web3.middleware import ExtraDataToPOAMiddleware as middleware
    except ImportError:  # pragma: no cover - only on much older web3
        try:
            from web3.middleware import geth_poa_middleware as middleware
        except ImportError:
            return False
    try:
        w3.middleware_onion.inject(middleware, layer=0)
        return True
    except Exception:  # pragma: no cover - already injected / incompatible
        return False


def build_web3(rpc: str | None, tester: bool = False, tester_keys: list[str] | None = None):
    from web3 import Web3

    if tester:
        from web3 import EthereumTesterProvider

        w3 = Web3(EthereumTesterProvider())
        # An in-memory chain has no faucet, so fund any key passed here from a
        # default account. Without this, --tester only works with eth-tester's
        # own accounts and fails on a real wallet with "cannot afford txn gas".
        # Transferring is used rather than custom genesis because eth-tester
        # moved account_keys/genesis_params between releases.
        if tester_keys:
            from eth_account import Account

            funder = w3.eth.accounts[0]
            for key in tester_keys:
                address = Account.from_key(key).address
                if address not in w3.eth.accounts:
                    w3.eth.send_transaction({"from": funder, "to": address,
                                             "value": 1_000 * 10**18})
        return w3
    if not rpc:
        raise SystemExit("Error: --rpc is required (or use --tester for a local dry chain).")
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    inject_poa_middleware(w3)
    try:
        # A malformed URL raises here rather than returning False, and a
        # traceback is a poor way to learn that the endpoint is wrong.
        connected = w3.is_connected()
    except Exception as exc:
        raise SystemExit(f"Error: cannot reach the RPC endpoint {rpc} ({exc}).")
    if not connected:
        raise SystemExit(f"Error: cannot reach the RPC endpoint {rpc}")
    return w3


def resolve_rpc(args) -> str | None:
    """--rpc wins; otherwise the network's public endpoint, for testnets only."""
    if args.rpc:
        return args.rpc
    if getattr(args, "tester", False):
        return None
    net = get_network(args.network)
    if not net["testnet"]:
        # Never silently default to mainnet through a third party.
        return None
    return net["default_rpc"]


def fee_params(w3) -> dict:
    """EIP-1559 where the chain supports it, otherwise legacy gasPrice.

    Requires the POA middleware (see inject_poa_middleware): on Polygon the
    block read below fails without it, and the fallback would quietly produce a
    legacy transaction on a chain that does price EIP-1559.
    """
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
        return (
            f"EIP-1559  maxFee={fees['maxFeePerGas'] / 1e9:.3f} gwei  "
            f"tip={fees['maxPriorityFeePerGas'] / 1e9:.3f} gwei"
        )
    return f"legacy  gasPrice={fees['gasPrice'] / 1e9:.3f} gwei"


def explorer_tx(net: dict, tx_hash: str) -> str:
    return f"{net['explorer']}/tx/{tx_hash}"


def explorer_address(net: dict, address: str) -> str:
    return f"{net['explorer']}/address/{address}"


def wait_for_receipt(w3, tx_hash, net, action: str, timeout: int = 300,
                     attempts: int = 3, retry_delay: int = 5):
    """Fetch a receipt without letting one flaky endpoint end the run.

    A public RPC can rate-limit mid-poll -- HTTP 403 was observed from a public
    Polygon endpoint immediately after a successful broadcast. By that point the
    transaction is already sent and the gas is already committed, so this must
    never surface as a bare traceback, and it must never invite the caller to
    simply re-run: it retries, and then says what to go and check.

    `action` finishes the sentence "re-running would ...", e.g.
    "deploy a SECOND contract". It is the caller's job to have written the hash
    somewhere durable before calling this (deploy_token.py and mint_token.py
    both write their record first), so the advice can point at a real file.
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
        f"  check  : {explorer_tx(net, shown)}\n"
        f"Do NOT re-run it with --execute: that would {action}.\n"
        "A record naming this transaction was already written, so once it shows as\n"
        "confirmed you can read the outcome from it instead of resending."
    )


# NOTE: deploy_token.py keeps its own copy of this retry loop, written before
# this shared one existed and covered by its own tests. New callers should use
# this function; the duplication is deliberate rather than an oversight.
