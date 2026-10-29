#!/usr/bin/env python3
"""Compile contracts/ProjectToken.sol into a reproducible build artifact.

No Node.js, Hardhat or Foundry is involved: this drives the real `solc` binary
through py-solc-x, which downloads the official compiler build on first use and
caches it (by default in ~/.solcx, outside this project).

What it produces, build/ProjectToken.json, is everything the later steps need:

  * the ABI and creation bytecode, for deploy_token.py
  * the EXACT Standard JSON Input that produced that bytecode, for verify_token.py
  * the full compiler version string and settings, so Polygonscan can reproduce
    the build and accept the contract

Reproducibility is the whole point. Polygonscan does not verify that your source
"looks right" -- it recompiles the source with the settings you send and checks
the resulting bytecode against what is actually on chain, byte for byte.
Anything that changes the bytecode (compiler version, optimizer, EVM target,
metadata hash, source contents) breaks verification. So the input is frozen at
build time and the artifact records hashes of both.

    # compile (downloads solc + OpenZeppelin sources on first run)
    python build_token.py

    # prove the saved input still reproduces the artifact's bytecode
    python build_token.py --recheck

    # pin a different compiler or OpenZeppelin release
    python build_token.py --solc 0.8.28 --oz-tag 5.7.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONTRACT_NAME = "ProjectToken"
CONTRACT_SOURCE = "contracts/ProjectToken.sol"

# Pinned so a later release cannot silently change the bytecode you verify.
DEFAULT_OZ_TAG = "5.7.0"
OZ_PREFIX = "@openzeppelin/contracts/"
OZ_RAW_BASE = "https://raw.githubusercontent.com/OpenZeppelin/openzeppelin-contracts"

# 18 decimals, and the standard OpenZeppelin pragma is ^0.8.20, so anything from
# 0.8.20 up works. The newest installed/installable release is used unless
# --solc pins one; the artifact records the exact version either way.
MIN_SOLC = (0, 8, 20)

# paris is the conservative default: it emits no PUSH0/MCOPY/transient-storage
# opcodes, so the bytecode runs on every EVM chain and every test EVM, including
# older Polygon Bor nodes. shanghai/cancun only make bytecode smaller.
DEFAULT_EVM_VERSION = "paris"
DEFAULT_RUNS = 200

# EIP-170: deployed code may not exceed 24576 bytes.
CODE_SIZE_LIMIT = 24576

IMPORT_RE = re.compile(r"""import\s+(?:[^;]*?\bfrom\s+)?["']([^"']+)["']""")


# --------------------------------------------------------------------------- #
# Solidity source resolution
# --------------------------------------------------------------------------- #
def canonical_source_path(import_path: str, importing: str) -> str:
    """Resolve an import to the key used in the Standard JSON 'sources' map.

    Relative imports are resolved against the importing file; OpenZeppelin
    imports keep their canonical `@openzeppelin/contracts/...` form, which is
    what the library's own internal imports use too.
    """
    if import_path.startswith(OZ_PREFIX):
        return import_path
    if import_path.startswith("."):
        import posixpath

        base = posixpath.dirname(importing)
        return posixpath.normpath(posixpath.join(base, import_path))
    return import_path


def fetch_url(url: str, attempts: int = 3) -> str:
    import requests

    last = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                return response.text
            last = f"HTTP {response.status_code}"
            if response.status_code == 404:
                break
        except Exception as exc:  # network hiccup, DNS, TLS...
            last = str(exc)
        time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"Error: could not fetch {url} ({last}).")


def collect_sources(tag: str, cache_dir: Path, offline: bool = False) -> dict[str, str]:
    """Walk imports from our contract and return {source path: source text}."""
    local_path = Path(CONTRACT_SOURCE)
    if not local_path.is_file():
        raise SystemExit(f"Error: {local_path} not found (run from the project root).")

    sources: dict[str, str] = {}
    pending = [CONTRACT_SOURCE]
    download_count = 0

    while pending:
        path = pending.pop()
        if path in sources:
            continue

        if path == CONTRACT_SOURCE:
            text = local_path.read_text(encoding="utf-8")
        else:
            cached = cache_dir / path[len(OZ_PREFIX):] if path.startswith(OZ_PREFIX) else None
            if cached and cached.is_file():
                text = cached.read_text(encoding="utf-8")
            elif offline:
                raise SystemExit(
                    f"Error: {path} is not cached and --offline was given. "
                    "Re-run without --offline to download it."
                )
            else:
                url = f"{OZ_RAW_BASE}/v{tag}/contracts/{path[len(OZ_PREFIX):]}"
                text = fetch_url(url)
                download_count += 1
                if cached:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_text(text, encoding="utf-8")

        sources[path] = text
        importing_from = path
        for match in IMPORT_RE.finditer(text):
            resolved = canonical_source_path(match.group(1), importing_from)
            if resolved not in sources:
                pending.append(resolved)

    if download_count:
        print(f"Downloaded {download_count} OpenZeppelin source file(s) at tag v{tag}.")
    return sources


# --------------------------------------------------------------------------- #
# Compiler
# --------------------------------------------------------------------------- #
def version_tuple(text: str) -> tuple[int, ...]:
    """'0.8.28+commit.7893614a' -> (0, 8, 28). Sorting is done on this."""
    numeric = text.lstrip("v").split("+")[0]
    parts = []
    for chunk in numeric.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts)


def solc_long_version(version: str) -> tuple[str, str]:
    """Return (Etherscan compiler string, raw `solc --version` output).

    py-solc-x's short form drops the commit hash, but the verification API
    requires the full string, e.g. v0.8.37+commit.f401782d -- not v0.8.37,
    and not the platform suffix solc prints locally (...Windows.msvc).
    """
    import subprocess
    from solcx.install import get_executable

    try:
        binary = get_executable(version)
    except Exception:
        return f"v{version}", ""

    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
    raw = (result.stdout or "").strip()
    match = re.search(r"Version:\s*(\S+)", raw)
    if not match:
        return f"v{version}", raw

    reported = match.group(1)  # e.g. 0.8.37+commit.f401782d.Windows.msvc
    base, _, build = reported.partition("+")
    if build:
        # Keep 'commit.<hash>' and drop whatever platform/arch tail follows.
        commit = ".".join(build.split(".")[:2])
        return f"v{base}+{commit}", raw
    return f"v{base}", raw


def resolve_solc_version(requested: str | None) -> str:
    """Return the version string to compile with, installing it if needed."""
    import solcx

    installed = [str(v) for v in solcx.get_installed_solc_versions()]

    if requested:
        want = requested.lstrip("v")
        if want in installed:
            return want
        available = [str(v) for v in solcx.get_installable_solc_versions()]
        if want not in available:
            newest = ", ".join(sorted(available, key=version_tuple)[-5:])
            raise SystemExit(
                f"Error: solc {want} is not installable. Recent releases: {newest}"
            )
        print(f"Installing solc {want} (one-off, cached in ~/.solcx)...")
        solcx.install_solc(want)
        return want

    usable = sorted((v for v in installed if version_tuple(v) >= MIN_SOLC),
                    key=version_tuple)
    if usable:
        return usable[-1]

    available = sorted((str(v) for v in solcx.get_installable_solc_versions()
                        if version_tuple(str(v)) >= MIN_SOLC), key=version_tuple)
    if not available:
        raise SystemExit("Error: no solc release >= 0.8.20 is available to install.")
    newest = available[-1]
    print(f"Installing solc {newest} (one-off, cached in ~/.solcx)...")
    solcx.install_solc(newest)
    return newest


def standard_json_input(sources: dict[str, str], optimize: bool, runs: int,
                        evm_version: str) -> dict:
    """The input object, exactly as it will be sent to Polygonscan for verification."""
    return {
        "language": "Solidity",
        "sources": {path: {"content": text} for path, text in sorted(sources.items())},
        "settings": {
            "optimizer": {"enabled": optimize, "runs": runs},
            "evmVersion": evm_version,
            # Written out rather than left implicit: this hash goes into the
            # bytecode, so if it differs from what Polygonscan assumes, the
            # bytecode differs too and verification fails with a confusing
            # "no matching contract" error.
            "metadata": {"bytecodeHash": "ipfs"},
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.deployedBytecode.object",
                        "evm.deployedBytecode.immutableReferences",
                    ],
                }
            },
        },
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compile_artifact(sources: dict[str, str], solc_version, optimize: bool, runs: int,
                     evm_version: str, oz_tag: str) -> dict:
    import solcx

    input_json = standard_json_input(sources, optimize, runs, evm_version)
    try:
        output = solcx.compile_standard(input_json, solc_version=str(solc_version))
    except Exception as exc:
        raise SystemExit(f"Error: solc failed.\n{exc}")

    try:
        contract = output["contracts"][CONTRACT_SOURCE][CONTRACT_NAME]
    except KeyError:
        raise SystemExit(
            f"Error: solc produced no contract '{CONTRACT_NAME}' in {CONTRACT_SOURCE}."
        )

    bytecode = contract["evm"]["bytecode"]["object"]
    deployed = contract["evm"]["deployedBytecode"]["object"]
    long_version, raw_version = solc_long_version(str(solc_version))

    immutable_slots = contract.get("evm", {}).get("deployedBytecode", {}).get("immutableReferences") or {}

    return {
        "contract": CONTRACT_NAME,
        "source_path": CONTRACT_SOURCE,
        "qualified_name": f"{CONTRACT_SOURCE}:{CONTRACT_NAME}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compiler": {
            "version": str(solc_version),
            "long_version": long_version,
            "raw_version_output": raw_version,
        },
        "settings": input_json["settings"],
        "openzeppelin": {
            "tag": oz_tag,
            "sources": {
                path: sha256_text(text)
                for path, text in sorted(sources.items())
                if path.startswith(OZ_PREFIX)
            },
        },
        "source_sha256": {path: sha256_text(text) for path, text in sorted(sources.items())},
        "standard_json_input": input_json,
        "abi": contract["abi"],
        # solc rewrites immutable values into the runtime code at deployment
        # time, so these positions hold zeros here and the real values on chain.
        "immutable_references": immutable_slots,
        "bytecode": "0x" + bytecode,
        "deployed_bytecode": "0x" + deployed,
        "bytecode_sha256": hashlib.sha256(bytes.fromhex(bytecode)).hexdigest(),
        "deployed_bytecode_sha256": hashlib.sha256(bytes.fromhex(deployed)).hexdigest(),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def selectors(abi: list) -> list[tuple[str, str]]:
    from eth_utils import keccak

    out = []
    for item in abi:
        if item.get("type") != "function":
            continue
        types = ",".join(i["type"] for i in item.get("inputs", []))
        signature = f"{item['name']}({types})"
        out.append((signature, "0x" + keccak(text=signature)[:4].hex()))
    return sorted(out)


def describe(artifact: dict) -> None:
    settings = artifact["settings"]
    created = (len(artifact["bytecode"]) - 2) // 2
    deployed = (len(artifact["deployed_bytecode"]) - 2) // 2

    print("=" * 68)
    print("BUILD")
    print("=" * 68)
    print(f"contract        : {artifact['qualified_name']}")
    print(f"compiler        : {artifact['compiler']['long_version']}")
    print(f"optimizer       : {'on' if settings['optimizer']['enabled'] else 'off'}"
          f" (runs={settings['optimizer']['runs']})")
    print(f"evm version     : {settings['evmVersion']}")
    print(f"metadata        : bytecodeHash={settings['metadata']['bytecodeHash']}")
    print(f"openzeppelin    : v{artifact['openzeppelin']['tag']} "
          f"({len(artifact['openzeppelin']['sources'])} file(s))")
    print(f"creation code   : {created} bytes")
    print(f"deployed code   : {deployed} bytes (limit {CODE_SIZE_LIMIT})")
    print(f"bytecode sha256 : {artifact['bytecode_sha256'][:32]}...")
    print("\npublic interface (what the contract exposes):")
    for signature, selector in selectors(artifact["abi"]):
        print(f"  {selector}  {signature}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def recheck(artifact_path: Path) -> int:
    """Recompile the SAVED input and confirm it reproduces the saved bytecode.

    This is the same test Polygonscan performs: same source, same settings,
    same compiler, byte-for-byte identical output.
    """
    import solcx

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    version = artifact["compiler"]["version"]
    print(f"Recompiling {artifact_path} with solc {version} from its stored input...")

    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if version not in installed:
        raise SystemExit(
            f"Error: solc {version} is not installed here. Run build_token.py "
            f"--solc {version} first."
        )

    output = solcx.compile_standard(artifact["standard_json_input"], solc_version=version)
    if artifact["compiler"]["version"] != version:  # pragma: no cover - defensive
        raise SystemExit("Error: artifact records an inconsistent compiler version.")
    contract = output["contracts"][CONTRACT_SOURCE][CONTRACT_NAME]
    rebuilt = "0x" + contract["evm"]["bytecode"]["object"]
    rebuilt_deployed = "0x" + contract["evm"]["deployedBytecode"]["object"]

    ok = True
    for label, expected, actual in (
        ("creation bytecode", artifact["bytecode"], rebuilt),
        ("deployed bytecode", artifact["deployed_bytecode"], rebuilt_deployed),
    ):
        if expected == actual:
            print(f"  OK   {label}: identical ({len(expected) // 2 - 1} bytes)")
        else:
            ok = False
            print(f"  FAIL {label}: rebuilt bytecode differs")
            print(f"       stored : {expected[:74]}...")
            print(f"       rebuilt: {actual[:74]}...")

    if ok:
        print("\nThe stored Standard JSON Input reproduces the artifact exactly,\n"
              "so it is the input to submit for verification.")
        return 0
    print("\nFAILED: the artifact does not match its own input. Rebuild before deploying.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile contracts/ProjectToken.sol to build/ProjectToken.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--solc", help="pin a solc version, e.g. 0.8.28 "
                                       "(default: newest available >= 0.8.20)")
    parser.add_argument("--oz-tag", default=DEFAULT_OZ_TAG,
                        help=f"OpenZeppelin release tag to pin (default {DEFAULT_OZ_TAG})")
    parser.add_argument("--evm-version", default=DEFAULT_EVM_VERSION,
                        help=f"solc --evm-version target (default {DEFAULT_EVM_VERSION}; "
                             "shanghai/cancun emit smaller code but newer opcodes)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"optimizer runs (default {DEFAULT_RUNS})")
    parser.add_argument("--no-optimize", action="store_true",
                        help="disable the optimizer (bigger, costlier bytecode)")
    parser.add_argument("--out", default="build/ProjectToken.json", help="artifact path")
    parser.add_argument("--cache-dir", default="build/vendor",
                        help="where downloaded OpenZeppelin sources are cached")
    parser.add_argument("--offline", action="store_true",
                        help="use only cached sources; never download")
    parser.add_argument("--recheck", nargs="?", const="build/ProjectToken.json",
                        help="recompile an existing artifact from its stored input and "
                             "compare bytecode; build nothing new")
    args = parser.parse_args(argv)

    if args.recheck:
        return recheck(Path(args.recheck))

    try:
        import solcx  # noqa: F401
    except ImportError:
        raise SystemExit(
            "Error: py-solc-x is not installed. Run:\n"
            "    pip install -r requirements.txt"
        )

    sources = collect_sources(args.oz_tag, Path(args.cache_dir), offline=args.offline)
    solc_version = resolve_solc_version(args.solc)

    artifact = compile_artifact(
        sources=sources,
        solc_version=str(solc_version),
        optimize=not args.no_optimize,
        runs=args.runs,
        evm_version=args.evm_version,
        oz_tag=args.oz_tag,
    )

    deployed_size = len(artifact["deployed_bytecode"]) // 2 - 1
    if deployed_size > CODE_SIZE_LIMIT:
        raise SystemExit(
            f"Error: deployed code is {deployed_size} bytes, over the {CODE_SIZE_LIMIT}-byte "
            "EIP-170 limit. It would deploy and then fail on every call."
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    describe(artifact)
    print(f"\nArtifact written to {out}")
    print(f"Next: python deploy_token.py --network amoy --key-file wallets-operator.json "
          f"--name \"My Token\" --symbol MTK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
