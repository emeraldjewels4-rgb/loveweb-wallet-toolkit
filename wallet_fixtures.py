#!/usr/bin/env python3
"""Throwaway wallet exports for the suites that need one on disk.

Four suites read wallet exports from the working directory:

    test_token.py            wallets-operator.json, wallets-btc-900.json
    test_distribute_token.py wallets-500.json
    test_payment_request.py  wallets-btc-1000-a..d.json (4,000 addresses)

Those files hold private keys, so .gitignore keeps them out of the repository --
which meant a fresh clone failed three suites while the machine that already had
the exports passed. A test that only runs on its author's disk has verified
nothing about the code in the repository.

This builds files of the same *shape* from the BIP39 test vector (valid checksum,
published in the spec, funds: none). Each file gets its own BIP39 passphrase, so
the address sets are disjoint: the 500-wallet file cannot contain the operator
address, and the four 1,000-address files cannot repeat each other.

An existing file is NEVER overwritten, so a machine holding the real exports
still tests the real exports, and the generated ones stay out of git.

    import wallet_fixtures
    wallet_fixtures.ensure(["wallets-500.json"])
"""

from __future__ import annotations

import json
from pathlib import Path

# "abandon abandon ... about": the canonical BIP39 vector. Public, checksummed,
# and unlike a random phrase it is identical on every machine and every run.
VECTOR = " ".join(["abandon"] * 11 + ["about"])

# filename -> (chains, count, bip39 passphrase)
SPECS: dict[str, tuple[list[str], int, str]] = {
    "wallets-operator.json": (["evm"], 1, "fixture-operator"),
    "wallets-500.json": (["evm"], 500, "fixture-500"),
    "wallets-btc-900.json": (["btc"], 900, "fixture-btc-900"),
    "wallets-btc-1000-a.json": (["btc"], 1000, "fixture-btc-1000-a"),
    "wallets-btc-1000-b.json": (["btc"], 1000, "fixture-btc-1000-b"),
    "wallets-btc-1000-c.json": (["btc"], 1000, "fixture-btc-1000-c"),
    "wallets-btc-1000-d.json": (["btc"], 1000, "fixture-btc-1000-d"),
}


def ensure(names: list[str]) -> list[str]:
    """Create whichever of `names` is missing. Returns the names created."""
    import gen_wallets as g

    created = []
    for name in names:
        if name not in SPECS:
            raise SystemExit(f"wallet_fixtures has no fixture called {name!r}")
        path = Path(name)
        if path.exists():
            continue
        chains, count, passphrase = SPECS[name]
        doc = g.build(
            chains=chains,
            count=count,
            mode="hd",
            words=12,
            mnemonic=VECTOR,
            passphrase=passphrase,
            verify=False,
        )
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(
            f"  (fixture: no {name} here, generated {count} throwaway "
            f"{chains[0]} address(es) from the BIP39 test vector)"
        )
        created.append(name)
    return created
