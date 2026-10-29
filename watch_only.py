#!/usr/bin/env python3
"""Derive a Bitcoin account extended public key (zpub) for a wallet export.

This turns a wallets JSON file into a WATCH-ONLY key: an extended public key
that can build every receiving address in the account but can never sign a
transaction or reveal a private key.

    python watch_only.py --wallets wallets-btc-900.json --out watch-only-900.txt

The extended public key is derived from the mnemonic at the ACCOUNT level
(m/84'/0'/0'), which is the deepest level that is still safe to hand to a
watch-only wallet. Derived child keys below it are not usable this way.

PRIVACY: the zpub is not a secret you can lose funds with, but anyone holding
it can enumerate every address in the account and see its balance and history.
Share it only with a wallet or service you control.

The account xpub CANNOT be recovered from individual addresses or from the
public keys embedded in them: derivation only goes one way, downward from the
seed. So keep this file, or the mnemonic, if you ever need it again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ACCOUNT_PATH = "m/84'/0'/0'"
WARNING = (
    "WATCH-ONLY EXTENDED PUBLIC KEY. It cannot spend, but it reveals every "
    "address in this account plus its balances and full history. Keep it "
    "private; share only with a wallet you control. It cannot be re-derived "
    "from the addresses alone, only from the seed phrase."
)


def load_doc(path: Path, password: str | None) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not doc.get("encrypted"):
        return doc
    if password is None:
        raise SystemExit(
            f"Error: {path} is encrypted. Pass --password-file to decrypt it."
        )
    # Reuse the generator's decryptor rather than duplicating the KDF.
    from gen_wallets import decrypt_doc

    return decrypt_doc(doc, password)


def account_extended_pubkey(mnemonic: str, passphrase: str = "") -> str:
    from bip_utils import Bip39SeedGenerator, Bip84, Bip84Coins

    seed = Bip39SeedGenerator(mnemonic).Generate(passphrase)
    # SLIP-132: a BIP84 account key is exported as a zpub, not a plain xpub.
    return (
        Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        .Purpose()
        .Coin()
        .Account(0)
        .PublicKey()
        .ToExtended()
    )


def address_from_extended(zpub: str, index: int) -> str:
    """Derive one receiving address from the zpub alone -- no seed involved."""
    from bip_utils import Bip44Changes, Bip84, Bip84Coins

    ctx = (
        Bip84.FromExtendedKey(zpub, Bip84Coins.BITCOIN)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    return ctx.PublicKey().ToAddress()


def check_value(text: str) -> str:
    """A project-local sha256 label for the zpub.

    Deliberately NOT a BIP32 fingerprint: that is a different value and users
    comparing it against a wallet's displayed fingerprint would be misled.
    """
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


def build_report(doc: dict, zpub: str, mismatches: list) -> str:
    wallets = doc.get("wallets", [])
    addrs = [w["btc"]["address"] for w in wallets if "btc" in w]
    first, last = (addrs[0], addrs[-1]) if addrs else ("(none)", "(none)")
    indices = [w["index"] for w in wallets if "btc" in w]

    lines = [
        "WATCH-ONLY BITCOIN WALLET KEY",
        "=" * 62,
        "",
        f"Source file ......: {doc.get('source_file', '(unknown)')}",
        f"Wallets made .....: {doc.get('generated_at', '(unknown)')}",
        f"Account path .....: {ACCOUNT_PATH}  (BIP84, native segwit)",
        (f"Addresses ........: {len(addrs)} (indices {indices[0]}..{indices[-1]})"
         if indices else "Addresses ........: none"),
        f"First address ....: {first}",
        f"Last address .....: {last}",
        f"Check value ......: {check_value(zpub)}",
        "                    (sha256 of this zpub, for your own records;",
        "                    NOT the BIP32 fingerprint a wallet displays)",
        "",
        "ACCOUNT EXTENDED PUBLIC KEY (zpub) -- paste this into the watch-only wallet:",
        "",
        zpub,
        "",
        "HOW TO USE IT",
        "=" * 62,
        "",
        "Sparrow Wallet:  File > New Wallet > (name it) > 'Airgapped / Watch-only'"
        " on the Keystore step, then paste the zpub. It will show addresses and",
        " balances and refuse to sign.",
        "Electrum:        File > New/Restore > 'Standard wallet' >"
        " 'Use a master key', then paste the zpub.",
        "BlueWallet:      Add wallet > Bitcoin > 'Import wallet' > paste the zpub.",
        "",
        "These are watch-only: no private key is imported, so nothing can be spent.",
        "",
        "IMPORTANT -- THE GAP LIMIT",
        "=" * 62,
        "",
        "A restored wallet scans only about 20 consecutive unused addresses and",
        "then stops. Nothing here has been used on-chain, so a default restore",
        "will show roughly the first 20 addresses and hide the rest. Raise the",
        "gap limit to at least the number of addresses above:",
        "",
        f"  Electrum: console > wallet.change_gap_limit({len(addrs)})",
        "  Sparrow:  Settings > Advanced > 'Gap limit', then restart",
        "",
        "Your keys are not lost either way -- the gap limit only affects address",
        "discovery.",
        "",
        "VERIFICATION",
        "=" * 62,
        "",
    ]
    if mismatches:
        lines.append(
            f"FAILED: {len(mismatches)} of {len(addrs)} addresses re-derived from "
            f"the zpub did not match the stored addresses: {mismatches[:5]}"
        )
    else:
        lines.append(
            f"All {len(addrs)} addresses were re-derived from the zpub alone and "
            "match the stored addresses exactly."
        )
    lines += ["", WARNING, ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a watch-only zpub for a Bitcoin wallet file."
    )
    parser.add_argument("--wallets", required=True, help="wallets JSON to read")
    parser.add_argument("--out", help="write the report here")
    parser.add_argument("--password-file", help="password if the input is encrypted")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only check the first N addresses (default: all)",
    )
    args = parser.parse_args(argv)

    src = Path(args.wallets)
    password = None
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").rstrip("\n")
    doc = load_doc(src, password)
    doc.setdefault("source_file", str(src))

    mnemonic = doc.get("mnemonic")
    if not mnemonic:
        raise SystemExit(
            f"Error: {src} has no stored mnemonic (was it generated in random "
            "mode?). An account zpub cannot be built from addresses alone."
        )
    if not any("btc" in w for w in doc.get("wallets", [])):
        raise SystemExit(f"Error: {src} contains no Bitcoin wallets.")

    passphrase = doc.get("bip39_passphrase", "")
    zpub = account_extended_pubkey(mnemonic, passphrase)

    wallets = [w for w in doc["wallets"] if "btc" in w]
    if args.limit is not None:
        wallets = wallets[: args.limit]
    mismatches = [
        w["index"]
        for w in wallets
        if address_from_extended(zpub, w["index"]) != w["btc"]["address"]
    ]

    report = build_report(doc, zpub, mismatches)
    if args.out:
        out = Path(args.out)
        out.write_text(report, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(report)

    checked = len(wallets)
    if mismatches:
        print(f"\nFAILED: {len(mismatches)}/{checked} addresses did not match.")
        return 1
    print(
        f"\nzpub: {zpub[:14]}...{zpub[-8:]}  (check value {check_value(zpub)})"
    )
    print(
        f"OK: all {checked} addresses re-derived from the zpub alone match the "
        f"stored addresses (account {ACCOUNT_PATH})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
