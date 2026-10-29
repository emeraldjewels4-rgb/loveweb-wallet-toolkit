#!/usr/bin/env python3
"""Publish the contract source so Polygonscan shows it as VERIFIED.

This talks to Etherscan's unified V2 API, not to api.polygonscan.com. That old
per-explorer endpoint was retired along with API V1 and now answers HTTP 301 --
it is the address in every 2023-era tutorial and no longer works. The V2 API
serves every supported chain from one base URL (api.etherscan.io/v2/api) and
selects the chain with a `chainid` parameter, so Polygon mainnet is
chainid=137 and Polygon Amoy is chainid=80002. One Etherscan API key covers all
of them; get it from your Etherscan account (not from polygonscan.com).

What gets submitted is the Standard JSON Input stored in the build artifact --
the exact sources and settings that produced the deployed bytecode, which is
what makes the result checkable rather than a matter of trust. The API then
recompiles your source with the same compiler version and compares the result
against the code on chain, byte for byte.

    # verify the deployment recorded by deploy_token.py
    python verify_token.py --network amoy

    # just ask whether an address is verified already
    python verify_token.py --network amoy --status

    # a contract deployed elsewhere (Remix, another machine)
    python verify_token.py --network polygon --address 0x... \\
        --constructor-args <abi-encoded hex>

The API key is never taken from the command line: use the ETHERSCAN_API_KEY
environment variable, --api-key-file, or the hidden prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import eth_common
from eth_common import ETHERSCAN_V2_API

API_KEY_ENV = "ETHERSCAN_API_KEY"
# https://docs.etherscan.io/api-reference/endpoint/verifysourcecode
CODE_FORMAT = "solidity-standard-json-input"
LICENSE_TYPE_MIT = "3"
POLL_INTERVAL = 5
POLL_ATTEMPTS = 60
ALREADY_VERIFIED = "already verified"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def load_api_key(api_key_file: str | None) -> str:
    if api_key_file:
        key = Path(api_key_file).read_text(encoding="utf-8").strip()
    elif os.environ.get(API_KEY_ENV):
        key = os.environ[API_KEY_ENV].strip()
    else:
        import getpass

        print(f"No {API_KEY_ENV} in the environment; reading the API key now.")
        print("(Input is hidden and is never written to disk.)")
        key = getpass.getpass("Etherscan API key: ").strip()

    if not key:
        raise SystemExit("Error: no API key provided.")
    return key


def load_deployment(path: str | None, network: str) -> tuple[dict, Path]:
    target = Path(path) if path else Path(f"deployment-{network}.json")
    if not target.is_file():
        raise SystemExit(
            f"Error: no deployment record at {target}.\n"
            "Either run deploy_token.py first, or pass --address and "
            "--constructor-args to verify a contract deployed elsewhere."
        )
    return json.loads(target.read_text(encoding="utf-8")), target


def load_artifact(path: str | None, deployment: dict | None) -> dict:
    """Find the build artifact: an explicit --artifact, else the one the
    deployment record names, else the default build path. The last fallback
    matters for the --address flow, which has no record to consult."""
    if path:
        artifact_path = Path(path)
    elif deployment and deployment.get("artifact", {}).get("path"):
        artifact_path = Path(deployment["artifact"]["path"])
    else:
        artifact_path = Path("build/ProjectToken.json")
    if not artifact_path.is_file():
        raise SystemExit(
            f"Error: no artifact at {artifact_path}. Run build_token.py first."
        )
    return json.loads(artifact_path.read_text(encoding="utf-8"))


def check_same_build(artifact: dict, deployment: dict, force: bool) -> None:
    """The artifact must be the one that produced the deployed bytecode.

    Verifying a rebuilt artifact would submit source that does not correspond to
    the code on chain. The API would reject it, but only after a round trip, and
    the failure message ('bytecode does not match') would be misleading.
    """
    recorded = deployment["artifact"].get("bytecode_sha256")
    current = artifact.get("bytecode_sha256")
    if recorded == current:
        return
    message = (
        "Error: build/ artifact does not match the deployment record.\n"
        f"  deployed with : {recorded}\n"
        f"  on disk now   : {current}\n"
        "The artifact must be the exact build that was deployed. Re-verify with the\n"
        "original artifact, or pass --force if you know this one is correct."
    )
    if force:
        print(f"WARNING: {message}\n(continuing because --force was given)")
        return
    raise SystemExit(message)


def check_artifact_ready(artifact: dict) -> None:
    """Reject an artifact the API would silently misread.

    Split out from main() so it can be tested without a network call: every one
    of these produces a confusing remote error rather than an obvious local one.
    """
    long_version = artifact["compiler"]["long_version"]
    if not long_version.startswith("v") or "+commit." not in long_version:
        raise SystemExit(
            "Error: the artifact's compiler string is not the full form the API needs "
            f"({long_version!r}). Rebuild with build_token.py."
        )
    if artifact["qualified_name"] != f"{artifact['source_path']}:{artifact['contract']}":
        raise SystemExit("Error: the artifact's qualified contract name is inconsistent.")
    if "sources" not in artifact["standard_json_input"]:
        raise SystemExit("Error: the artifact has no Standard JSON Input to submit.")


def build_payload(net: dict, artifact: dict, address: str, constructor_args: str,
                  api_key: str) -> tuple[dict, dict]:
    """The exact query string and POST body sent to the API.

    Split out so it can be tested without touching the network: a wrong
    parameter here is the usual cause of a verification failure.
    """
    params = {
        "chainid": str(net["api_chainid"]),
        "module": "contract",
        "action": "verifysourcecode",
        "apikey": api_key,
    }
    data = {
        "contractaddress": address,
        "sourceCode": json.dumps(artifact["standard_json_input"]),
        "contractname": artifact["qualified_name"],
        "compilerversion": artifact["compiler"]["long_version"],
        "codeformat": CODE_FORMAT,
        # ABI-encoded, hex, WITHOUT a 0x prefix. A non-empty constructor makes
        # this mandatory, and it is the single most common thing to get wrong.
        "constructorArguments": constructor_args or "",
        "licenseType": LICENSE_TYPE_MIT,
    }
    return params, data


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def post_verification(params: dict, data: dict) -> dict:
    import requests

    try:
        response = requests.post(ETHERSCAN_V2_API, params=params, data=data, timeout=60)
    except Exception as exc:
        raise SystemExit(f"Error: could not reach {ETHERSCAN_V2_API} ({exc}).")
    try:
        return response.json()
    except ValueError:
        raise SystemExit(
            f"Error: the API returned non-JSON (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        )


def poll_status(api_key: str, chainid: str, guid: str, quiet: bool = False) -> str:
    import requests

    params = {
        "chainid": chainid,
        "module": "contract",
        "action": "checkverifystatus",
        "guid": guid,
        "apikey": api_key,
    }
    for attempt in range(POLL_ATTEMPTS):
        time.sleep(POLL_INTERVAL if attempt else 2)
        try:
            body = requests.get(ETHERSCAN_V2_API, params=params, timeout=30).json()
        except Exception as exc:
            if not quiet:
                print(f"  poll {attempt + 1}: {exc}")
            continue
        result = str(body.get("result", ""))
        if not quiet:
            print(f"  poll {attempt + 1}: {result}")
        if "pending" in result.lower():
            continue
        return result
    return "Timed out waiting for the verification result."


def query_verified(api_key: str, chainid: str, address: str) -> dict:
    import requests

    params = {
        "chainid": chainid,
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    return requests.get(ETHERSCAN_V2_API, params=params, timeout=30).json()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the ProjectToken source on Polygonscan (Etherscan V2 API).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    eth_common.add_network_arg(parser, default="amoy")
    parser.add_argument("--deployment", help="deployment record from deploy_token.py")
    parser.add_argument("--artifact", help="compiled artifact (default: the one the "
                                          "deployment record names)")
    parser.add_argument("--address", help="verify this address instead of a recorded deployment")
    parser.add_argument("--constructor-args", default="",
                        help="ABI-encoded constructor arguments, hex, no 0x prefix")
    parser.add_argument("--api-key-file", help=f"file holding the key (or set {API_KEY_ENV})")
    parser.add_argument("--status", action="store_true",
                        help="only report whether the address is verified already")
    parser.add_argument("--no-poll", action="store_true",
                        help="submit and return the submission guid without waiting")
    parser.add_argument("--force", action="store_true",
                        help="verify even if the artifact does not match the deployment record")
    args = parser.parse_args(argv)

    net = eth_common.get_network(args.network)
    api_key = load_api_key(args.api_key_file)

    deployment = None
    if not args.address:
        deployment, deployment_path = load_deployment(args.deployment, args.network)
        address = deployment["address"]
        constructor_args = deployment.get("constructor_arguments", "")
    else:
        deployment_path = None
        address = args.address
        constructor_args = args.constructor_args

    if not address.startswith("0x") or len(address) != 42:
        raise SystemExit(f"Error: {address} does not look like a contract address.")

    if args.status:
        body = query_verified(api_key, str(net["api_chainid"]), address)
        result = body.get("result")
        if isinstance(result, list) and result:
            entry = result[0]
            name = entry.get("ContractName")
            verified = bool(name) and entry.get("SourceCode") not in ("", None)
            print(f"{address} on {net['name']}")
            print(f"verified : {'YES' if verified else 'NO'}")
            if verified:
                print(f"contract : {name}")
                print(f"compiler : {entry.get('CompilerVersion')}")
                print(eth_common.explorer_address(net, address))
                return 0
            if body.get("message") == "NOTOK":
                print(f"api note : {body.get('result')}")
            return 1
        print(f"Unexpected API response: {json.dumps(body)[:300]}")
        return 1

    artifact = load_artifact(args.artifact, deployment)
    if deployment is not None:
        check_same_build(artifact, deployment, args.force)

    check_artifact_ready(artifact)

    params, data = build_payload(net, artifact, address, constructor_args, api_key)

    print("=" * 68)
    print("SOURCE VERIFICATION")
    print("=" * 68)
    print(f"network         : {net['name']} (chainid {params['chainid']})")
    print(f"address         : {address}")
    print(eth_common.explorer_address(net, address))
    print(f"api             : {ETHERSCAN_V2_API}")
    print(f"contract        : {data['contractname']}")
    print(f"compiler        : {data['compilerversion']}")
    print(f"format          : {data['codeformat']} "
          f"({len(artifact['standard_json_input']['sources'])} source file(s))")
    print(f"optimizer       : {artifact['settings']['optimizer']['enabled']} "
          f"runs={artifact['settings']['optimizer']['runs']}, "
          f"evm={artifact['settings']['evmVersion']}")
    print(f"constructorArgs : {constructor_args or '(none)'}"
          f"{f' ({len(constructor_args) // 2} bytes)' if constructor_args else ''}")

    print("\nSubmitting...")
    body = post_verification(params, data)
    result = body.get("result")
    message = str(body.get("message", ""))

    if body.get("status") != "1":
        text = json.dumps(result) if not isinstance(result, str) else result
        if ALREADY_VERIFIED in text.lower():
            print(f"Already verified: {text}")
            print(eth_common.explorer_address(net, address))
            return 0
        print(f"REJECTED: {message} - {text}")
        for hint in (
            ("already verified", "nothing to do: the source is published"),
            ("unable to locate", "the address has no contract code on this chain yet"),
            ("bytecode", "the artifact is not the build that was deployed"),
            ("constructor", "check --constructor-args against the deployment record"),
            ("apikey", f"check {API_KEY_ENV}"),
            ("rate limit", "wait a few seconds and try again"),
        ):
            if hint[0] in text.lower():
                print(f"  hint: {hint[1]}")
        return 1

    guid = str(result)
    print(f"Submission accepted. GUID: {guid}")

    if args.no_poll:
        print(f"Check later with:\n"
              f"  python verify_token.py --network {args.network} --status")
        return 0

    print("\nWaiting for the compiler comparison (this can take a minute)...")
    status = poll_status(api_key, params["chainid"], guid)
    print(f"\nResult: {status}")

    if status.strip() == "Pass - Verified":
        print("\nThe source on Polygonscan now matches the deployed bytecode.")
        print(eth_common.explorer_address(net, address))
        if deployment_path is not None and deployment is not None:
            deployment["source_verified_on_explorer"] = True
            deployment["verification_guid"] = guid
            deployment_path.write_text(json.dumps(deployment, indent=2) + "\n",
                                       encoding="utf-8")
            print(f"Record updated: {deployment_path}")
        return 0

    print("\nNot verified. The usual causes, in order of likelihood:")
    print("  1. the compiler version or settings differ from the deployment")
    print("  2. the constructor arguments are wrong or missing")
    print("  3. the artifact is not the build that was actually deployed")
    print("  4. verification was submitted from a different chainid")
    return 1


if __name__ == "__main__":
    sys.exit(main())
