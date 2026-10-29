#!/usr/bin/env python3
"""Tests for watch_only.py -- the zpub export.

The property that matters is the one that makes a watch-only wallet usable:
addresses derived from the EXTENDED PUBLIC KEY ALONE must equal the addresses in
the wallet file, which were derived from the seed. If those two disagree the
wallet shows an empty account while the funds sit at addresses it never
enumerates -- and nothing about the report looks wrong.

So the core check is a cross-check between two independent derivations:
`gen_wallets.derive_btc(mnemonic, i)` (seed -> address) and
`watch_only.address_from_extended(zpub, i)` (public key -> address), compared at
several indices. Both must also match the published BIP84 vector address.

    python test_watch_only.py
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import gen_wallets as g
import watch_only as w

FAILED: list[str] = []

M = ("abandon abandon abandon abandon abandon abandon abandon abandon "
     "abandon abandon abandon about")
BTC_ADDR_0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


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


def run_main(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = w.main(argv)
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else (0 if not code else 1)
        return rc, buf.getvalue() + str(exc)
    return int(rc), buf.getvalue()


def make_doc(tmp: Path, count: int = 3, name: str = "btc.json") -> Path:
    doc = g.build(chains=["btc"], count=count, mode="hd", words=12,
                  mnemonic=M, passphrase="", verify=True)
    path = tmp / name
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #
def derivation_checks() -> None:
    section("the zpub derives the same addresses the seed does")

    zpub = w.account_extended_pubkey(M)
    check(zpub.startswith("zpub"), f"a BIP84 account key is exported as zpub ({zpub[:10]}...)")
    check(w.account_extended_pubkey(M) == zpub, "derivation is deterministic")
    check(w.address_from_extended(zpub, 0) == BTC_ADDR_0,
          f"index 0 matches the published BIP84 vector ({BTC_ADDR_0})")

    seeded = [g.derive_btc(M, i)["address"] for i in range(4)]
    from_pub = [w.address_from_extended(zpub, i) for i in range(4)]
    check(seeded == from_pub, "seed-derived and zpub-derived address lists are identical")
    check(len(set(from_pub)) == 4, "and each index gives a distinct address")
    check(from_pub[0] != w.address_from_extended(zpub, 5), "indices do not collide")

    other = w.account_extended_pubkey(M, "a-passphrase")
    check(other != zpub, "a BIP39 passphrase changes the account key")
    check(w.address_from_extended(other, 0) != BTC_ADDR_0,
          "and therefore the addresses, so the passphrase is really applied")

    section("check_value()")

    value = w.check_value(zpub)
    check(len(value) == 16 and all(ch in "0123456789abcdef" for ch in value),
          f"16 lowercase hex characters ({value})")
    check(value == hashlib.sha256(zpub.encode("ascii")).hexdigest()[:16],
          "it is the sha256 prefix the docstring promises")
    check(value != w.check_value(w.account_extended_pubkey(M, "x")), "it changes with the key")
    check(len(value) != 8, "it is NOT an 8-character BIP32 fingerprint (which would mislead)")


# --------------------------------------------------------------------------- #
# Report and document loading
# --------------------------------------------------------------------------- #
def report_checks(tmp: Path) -> None:
    section("build_report()")

    doc = json.loads(make_doc(tmp).read_text(encoding="utf-8"))
    zpub = w.account_extended_pubkey(M)
    text = w.build_report(doc, zpub, [])

    check("Addresses ........: 3 (indices 0..2)" in text, "the address count and index range")
    check(BTC_ADDR_0 in text, "the first address is shown")
    check(w.check_value(zpub) in text, "the check value is shown")
    check(zpub in text, "the zpub itself is shown, on one line")
    check("change_gap_limit(3)" in text, "the gap-limit advice uses THIS wallet's count")
    check("All 3 addresses were re-derived from the zpub alone" in text,
          "success says all three were verified")
    check("CANNOT" not in text and "cannot spend" in text, "the warning states it cannot spend")
    check("NOT the BIP32 fingerprint" in text, "the check value is not passed off as a fingerprint")

    failed = w.build_report(doc, zpub, [2])
    check("FAILED: 1 of 3" in failed, "a mismatch count is reported")
    check("[2]" in failed, "and the mismatching index is named")
    check("All 3 addresses were re-derived" not in failed, "no success line when it failed")

    empty = w.build_report({"wallets": [{"index": 0}]}, zpub, [])
    check("Addresses ........: none" in empty and "(none)" in empty,
          "a document with no BTC wallets produces no fake addresses")

    section("load_doc()")

    plain = tmp / "plain.json"
    plain.write_text(json.dumps({"a": 1}), encoding="utf-8")
    check(w.load_doc(plain, None) == {"a": 1}, "a plain document passes through")

    blob = g.encrypt_doc({"mnemonic": M, "wallets": []}, "pw123")
    enc = tmp / "enc.json"
    enc.write_text(json.dumps(blob), encoding="utf-8")
    try:
        w.load_doc(enc, None)
        check(False, "an encrypted document without a password is refused")
    except SystemExit:
        check(True, "an encrypted document without a password is refused")
    check(w.load_doc(enc, "pw123")["mnemonic"] == M, "and decrypts with the right one")


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def main_checks(tmp: Path) -> None:
    section("main(): report, verification and refusals")

    src = make_doc(tmp)
    out = tmp / "report.txt"
    rc, text = run_main(["--wallets", str(src), "--out", str(out)])
    check(rc == 0, "a clean wallet file exits 0")
    check(out.is_file(), "the report is written to --out")
    body = out.read_text(encoding="utf-8")
    check("All 3 addresses were re-derived from the zpub alone and match" in body,
          "the written report records the verification")
    check("zpub6" in body and w.ACCOUNT_PATH in body, "the report carries the key and the path")
    check("OK: all 3 addresses re-derived" in text, "and stdout confirms it")

    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["wallets"][2]["btc"]["address"] = BTC_ADDR_0
    tampered = tmp / "tampered.json"
    tampered.write_text(json.dumps(doc), encoding="utf-8")
    rc, text = run_main(["--wallets", str(tampered), "--out", str(tmp / "bad.txt")])
    check(rc == 1, "a mismatching address exits 1")
    check("FAILED: 1/3 addresses did not match" in text, "and says how many failed")
    check("FAILED: 1 of 3" in (tmp / "bad.txt").read_text(encoding="utf-8"),
          "the written report records the failure too")

    rc, text = run_main(["--wallets", str(tampered), "--limit", "2",
                         "--out", str(tmp / "limit.txt")])
    check(rc == 0, "--limit 2 skips the tampered index 2, so the run passes")
    check("all 2 addresses" in text, "and reports 2 checked, not 3")

    no_mn = tmp / "nomnemonic.json"
    no_mn.write_text(json.dumps({"wallets": [{"index": 0, "btc": {"address": BTC_ADDR_0}}]}),
                     encoding="utf-8")
    rc, text = run_main(["--wallets", str(no_mn), "--out", str(tmp / "x.txt")])
    check(rc != 0 and "no stored mnemonic" in text,
          "random-mode wallets cannot produce a zpub, and say why")

    no_btc = g.build(chains=["evm"], count=1, mode="hd", words=12,
                     mnemonic=M, passphrase="", verify=False)
    evm_only = tmp / "evm.json"
    evm_only.write_text(json.dumps(no_btc), encoding="utf-8")
    rc, text = run_main(["--wallets", str(evm_only), "--out", str(tmp / "y.txt")])
    check(rc != 0 and "no Bitcoin wallets" in text, "an EVM-only file is refused")

    blob = g.encrypt_doc(json.loads(src.read_text(encoding="utf-8")), "s3cret")
    enc = tmp / "enc_wallets.json"
    enc.write_text(json.dumps(blob), encoding="utf-8")
    pw = tmp / "pw.txt"
    pw.write_text("s3cret\n", encoding="utf-8")
    rc, text = run_main(["--wallets", str(enc), "--password-file", str(pw),
                         "--out", str(tmp / "enc_report.txt")])
    check(rc == 0, "an encrypted wallet file works with --password-file")
    rc, text = run_main(["--wallets", str(enc), "--out", str(tmp / "no_pw.txt")])
    check(rc != 0 and "--password-file" in text,
          "and without it the error names the flag that fixes it")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="watch_only_test_"))
    derivation_checks()
    report_checks(tmp)
    main_checks(tmp)

    print()
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("All watch_only checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
