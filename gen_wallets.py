#!/usr/bin/env python3
"""Generate fresh EVM (MetaMask-compatible) and Bitcoin wallets, export as JSON.

This CREATES new wallets. It does not read, extract or "download" any existing
wallet or key from MetaMask or anywhere else -- that is not possible and any
tool claiming to do it is malware.

Typical use:
    python gen_wallets.py --count 100 --chains evm --out wallets.json
    python gen_wallets.py --verify-file wallets.json

WARNING: the output file holds private keys. Anyone who reads it can move the
funds. Keep it offline, never commit it, never paste it into a chat.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1

# MetaMask uses exactly these paths. Do not change them if you want the wallets
# to be recoverable by restoring the mnemonic in MetaMask.
EVM_PATH = "m/44'/60'/0'/0/{i}"
BTC_PATH = "m/84'/0'/0'/0/{i}"

WARNING = (
    "THIS FILE CONTAINS PRIVATE KEYS. Anyone who reads it can move all funds. "
    "Keep it offline, never commit it, never paste it into a chat, website or "
    "smart contract. Delete it once the wallets are imported and backed up."
)

PASSPHRASE_HINT = "(MetaMask does not use a BIP39 passphrase, so leave this empty)"


# --------------------------------------------------------------------------- #
# EVM
# --------------------------------------------------------------------------- #
def _evm_account_cls():
    """eth-account's HD features are opt-in because they are unaudited."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    return Account


def _hex_key(raw) -> str:
    """hexbytes >=1.0 already prefixes 0x, older versions do not."""
    text = raw.hex()
    return text if text.startswith("0x") else "0x" + text


def derive_evm(mnemonic: str, index: int, passphrase: str = "") -> dict:
    Account = _evm_account_cls()
    path = EVM_PATH.format(i=index)
    acct = Account.from_mnemonic(mnemonic, passphrase=passphrase, account_path=path)
    return {"address": acct.address, "private_key": _hex_key(acct.key), "path": path}


def verify_evm(entry: dict) -> bool:
    Account = _evm_account_cls()
    try:
        acct = Account.from_key(entry["private_key"])
    except Exception:
        return False
    return acct.address.lower() == entry["address"].lower()


# --------------------------------------------------------------------------- #
# Bitcoin
# --------------------------------------------------------------------------- #
def derive_btc(mnemonic: str, index: int, passphrase: str = "") -> dict:
    from bip_utils import Bip39SeedGenerator, Bip44Changes, Bip84, Bip84Coins

    seed = Bip39SeedGenerator(mnemonic).Generate(passphrase)
    bip84 = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
    ctx = (
        bip84.Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    key = ctx.PrivateKey()
    entry = {
        "address": ctx.PublicKey().ToAddress(),
        "private_key": key.Raw().ToHex(),
        "path": BTC_PATH.format(i=index),
    }
    # WIF is what most Bitcoin wallets expect. Guarded in case of API drift.
    to_wif = getattr(key, "ToWif", None)
    if callable(to_wif):
        entry["wif"] = to_wif()
    return entry


def verify_btc(entry: dict, mnemonic: str, index: int, passphrase: str = "") -> bool:
    try:
        fresh = derive_btc(mnemonic, index, passphrase)
    except Exception:
        return False
    return fresh["address"] == entry["address"]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def new_mnemonic(words: int) -> str:
    from mnemonic import Mnemonic

    mnemo = Mnemonic("english")
    phrase = mnemo.generate(strength=256 if words == 24 else 128)
    if not mnemo.check(phrase):
        raise RuntimeError("Generated an invalid BIP39 mnemonic; aborting.")
    return phrase


def check_mnemonic(phrase: str) -> str:
    from mnemonic import Mnemonic

    phrase = " ".join(phrase.split())
    if not Mnemonic("english").check(phrase):
        raise SystemExit("Error: that is not a valid BIP39 English mnemonic.")
    return phrase


def build(chains, count, mode, words, mnemonic, passphrase, verify):
    wallets = []
    root = None
    if mode == "hd":
        root = check_mnemonic(mnemonic) if mnemonic else new_mnemonic(words)
    elif mnemonic:
        raise SystemExit("Error: --mnemonic only makes sense with --mode hd.")

    for i in range(count):
        entry = {"index": i}
        if mode == "hd":
            phrase, idx = root, i
        else:
            # Independent wallet: its own mnemonic, always at index 0.
            phrase, idx = new_mnemonic(words), 0
            entry["mnemonic"] = phrase

        if "evm" in chains:
            entry["evm"] = derive_evm(phrase, idx, passphrase)
        if "btc" in chains:
            entry["btc"] = derive_btc(phrase, idx, passphrase)

        if verify:
            if "evm" in chains and not verify_evm(entry["evm"]):
                raise RuntimeError(f"EVM self-check failed at index {i}; aborting.")
            if "btc" in chains and not verify_btc(entry["btc"], phrase, idx, passphrase):
                raise RuntimeError(f"BTC self-check failed at index {i}; aborting.")

        wallets.append(entry)

    doc = {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "chains": list(chains),
        "count": count,
        "warning": WARNING,
    }
    if mode == "hd":
        doc["mnemonic"] = root
        doc["evm_path"] = EVM_PATH
        doc["btc_path"] = BTC_PATH
    # Stored so --verify-file can re-derive. This is already a secrets file.
    if passphrase:
        doc["bip39_passphrase"] = passphrase
    doc["wallets"] = wallets
    return doc


# --------------------------------------------------------------------------- #
# Encryption
# --------------------------------------------------------------------------- #
PBKDF2_ITERATIONS = 600_000


def encrypt_doc(doc: dict, password: str) -> dict:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    payload = json.dumps(doc, indent=2).encode("utf-8")
    return {
        "version": VERSION,
        "encrypted": True,
        "warning": WARNING,
        "kdf": {
            "name": "pbkdf2-hmac-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "cipher": "fernet",
        "payload": Fernet(key).encrypt(payload).decode("ascii"),
    }


def decrypt_doc(blob: dict, password: str) -> dict:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf_cfg = blob["kdf"]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=base64.b64decode(kdf_cfg["salt"]),
        iterations=kdf_cfg["iterations"],
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    try:
        return json.loads(Fernet(key).decrypt(blob["payload"].encode("ascii")))
    except InvalidToken:
        raise SystemExit("Error: wrong password (or the file is corrupted).")


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #
def _restrict_windows_acl(path: Path) -> bool:
    """posix chmod is a silent no-op on Windows, so use icacls instead.

    Drops inherited ACEs and grants only the current user. Reversible with:
        icacls <file> /reset
    """
    import subprocess

    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def write_private(path: Path, text: str, force: bool) -> str:
    """Write the file and lock it down. Returns a note on what was achieved."""
    if path.exists() and not force:
        raise SystemExit(f"Error: {path} already exists. Use --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)

    if os.name == "nt":
        # chmod is meaningless here, so reporting "0600" would be a lie.
        if _restrict_windows_acl(path):
            return (
                "NTFS ACLs applied (icacls). chmod is a no-op on Windows; expect "
                "access by your account, SYSTEM and Administrators."
            )
        return (
            "WARNING: could not apply NTFS restrictions. chmod does nothing on "
            "Windows, so other accounts on this PC may be able to read this file."
        )
    try:
        os.chmod(path, 0o600)
        return "permissions set to 0600"
    except OSError:
        return "WARNING: could not set permissions to 0600."


def warn_if_in_git_repo(path: Path) -> None:
    for parent in [path.parent.resolve(), *path.parent.resolve().parents]:
        if (parent / ".git").exists():
            print(
                f"WARNING: {path} is inside a git repository ({parent}).\n"
                f"         Make sure '{path.name}' is in .gitignore before committing.",
                file=sys.stderr,
            )
            return


def verify_file(path: Path, password: str | None) -> int:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("encrypted"):
        if password is None:
            password = getpass.getpass("Password: ")
        doc = decrypt_doc(doc, password)

    # In random mode every wallet is derived at index 0, not at the wallet index.
    random_mode = doc.get("mode") == "random"
    passphrase = doc.get("bip39_passphrase", "")

    bad = 0
    for entry in doc.get("wallets", []):
        i = entry.get("index")
        deriv_index = 0 if random_mode else i
        if "evm" in entry and not verify_evm(entry["evm"]):
            print(f"  FAIL evm index {i}: {entry['evm'].get('address')}")
            bad += 1
        if "btc" in entry:
            phrase = entry.get("mnemonic") or doc.get("mnemonic")
            if phrase is None:
                print(f"  ?    btc index {i}: no mnemonic stored, cannot re-derive")
            elif not verify_btc(entry["btc"], phrase, deriv_index, passphrase):
                print(f"  FAIL btc index {i}: {entry['btc'].get('address')}")
                bad += 1

    total = len(doc.get("wallets", []))
    if bad:
        print(f"\n{bad} of {total} wallets FAILED verification.")
        return 1
    print(f"\nAll {total} wallets verified: every private key reproduces its address.")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate fresh EVM/Bitcoin wallets and export them as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-n", "--count", type=int, default=100, help="how many (default 100)")
    parser.add_argument(
        "--chains",
        default="evm",
        help="comma separated: evm, btc (default evm)",
    )
    parser.add_argument(
        "--mode",
        choices=("hd", "random"),
        default="hd",
        help="hd: one mnemonic, N accounts (MetaMask-style). random: N independent "
        "wallets with their own mnemonic (default hd)",
    )
    parser.add_argument("-o", "--out", default="wallets.json", help="output path")
    parser.add_argument("--words", type=int, choices=(12, 24), default=12)
    parser.add_argument("--mnemonic", help="reuse an existing BIP39 phrase instead of a new one")
    parser.add_argument(
        "--mnemonic-file",
        help="read the BIP39 phrase from a file. Preferred over --mnemonic: an "
        "argv phrase is stored in shell history and visible in process lists",
    )
    parser.add_argument(
        "--bip39-passphrase",
        default="",
        help=f"optional BIP39 passphrase {PASSPHRASE_HINT}",
    )
    parser.add_argument("--encrypt", action="store_true", help="password-protect the output")
    parser.add_argument("--password-file", help="read the password from a file instead of prompting")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip re-deriving each address from its private key",
    )
    parser.add_argument("--show-keys", action="store_true", help="print private keys to stdout")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verify-file", help="verify an existing export and exit")
    args = parser.parse_args(argv)

    if args.verify_file:
        # --password-file matters here too: getpass reads the console directly,
        # so a piped password cannot work and scripted checks would hang.
        pw = None
        if args.password_file:
            pw = Path(args.password_file).read_text(encoding="utf-8").rstrip("\n")
        return verify_file(Path(args.verify_file), pw)

    if args.count < 1:
        raise SystemExit("Error: --count must be at least 1.")

    chains = [c.strip().lower() for c in args.chains.split(",") if c.strip()]
    unknown = [c for c in chains if c not in ("evm", "btc")]
    if unknown:
        raise SystemExit(f"Error: unknown chain(s): {', '.join(unknown)}")

    # Resolve the phrase before anything is derived, so a bad path fails fast
    # and the phrase never has to appear in argv.
    mnemonic = args.mnemonic
    if args.mnemonic_file:
        if args.mnemonic:
            raise SystemExit("Error: pass either --mnemonic or --mnemonic-file, not both.")
        phrase_path = Path(args.mnemonic_file)
        if not phrase_path.is_file():
            raise SystemExit(f"Error: no such mnemonic file: {phrase_path}")
        mnemonic = phrase_path.read_text(encoding="utf-8").strip()
        if not mnemonic:
            raise SystemExit(f"Error: {phrase_path} is empty.")
        warn_if_in_git_repo(phrase_path)

    password = None
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").rstrip("\n")
        if not password:
            raise SystemExit("Error: the password file is empty.")
    elif args.encrypt:
        password = getpass.getpass("Password for the output file: ")
        if password != getpass.getpass("Confirm password: "):
            raise SystemExit("Error: passwords did not match.")

    if not args.quiet:
        print(f"Generating {args.count} wallet(s) [{args.mode}] for: {', '.join(chains)}")
        print("Self-check ON (each private key is re-derived and compared).\n"
              if not args.no_verify else
              "Self-check OFF -- you are trusting the library blindly.\n")

    doc = build(
        chains=chains,
        count=args.count,
        mode=args.mode,
        words=args.words,
        mnemonic=mnemonic,
        passphrase=args.bip39_passphrase,
        verify=not args.no_verify,
    )

    out = Path(args.out)
    blob = encrypt_doc(doc, password) if password else doc
    guard = write_private(out, json.dumps(blob, indent=2) + "\n", args.force)
    warn_if_in_git_repo(out)

    if not args.quiet:
        for entry in doc["wallets"]:
            bits = [f"#{entry['index']}"]
            for chain in chains:
                bits.append(f"{chain}={entry[chain]['address']}")
            if args.show_keys:
                for chain in chains:
                    bits.append(f"{chain}_key={entry[chain]['private_key']}")
            print("  " + "  ".join(bits))
        if args.mode == "hd":
            print("\nMnemonic (restore this in MetaMask to recover ALL of the above):")
            print(f"  {doc['mnemonic']}")
        print(f"\nWrote {args.count} wallet(s) to {out}"
              f"{' (encrypted)' if password else ' (PLAINTEXT)'}")
        print(f"Protection: {guard}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
