#!/usr/bin/env python3
"""Tests for gen_wallets.py -- the tool that CREATES the wallets.

The values asserted below are published BIP39/BIP44/BIP84 test vectors for the
standard phrase "abandon abandon ... about", not outputs captured from this
code. That matters: a rounded-off derivation path, a swapped change/index level
or a library default changing under us would still be self-consistent and pass a
"does it reproduce itself" test, while producing wallets MetaMask cannot
restore. These vectors are the independent witness.

    python test_gen_wallets.py
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import gen_wallets as g

FAILED: list[str] = []

# The canonical BIP39 test phrase, and what the standards say it derives to.
M = ("abandon abandon abandon abandon abandon abandon abandon abandon "
     "abandon abandon abandon about")
EVM_ADDR_0 = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
EVM_ADDR_1 = "0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0"
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
            rc = g.main(argv)
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else (0 if not code else 1)
        return rc, buf.getvalue() + str(exc)
    return int(rc), buf.getvalue()


def vector_checks() -> None:
    section("derivation matches the published BIP39/BIP44/BIP84 vectors")

    evm0 = g.derive_evm(M, 0)
    check(evm0["address"] == EVM_ADDR_0, f"EVM index 0 = {EVM_ADDR_0} ({evm0['address']})")
    check(g.derive_evm(M, 1)["address"] == EVM_ADDR_1, f"EVM index 1 = {EVM_ADDR_1}")
    check(evm0["path"] == "m/44'/60'/0'/0/0", f"MetaMask path is a literal, not a default ({evm0['path']})")
    check(evm0["private_key"].startswith("0x") and len(evm0["private_key"]) == 66,
          "private key is 0x + 64 hex")
    check(g.derive_evm(M, 0)["address"] == evm0["address"], "derivation is deterministic")

    btc0 = g.derive_btc(M, 0)
    check(btc0["address"] == BTC_ADDR_0, f"BTC index 0 = {BTC_ADDR_0} ({btc0['address']})")
    check(btc0["path"] == "m/84'/0'/0'/0/0", "BIP84 native-segwit path is what is stored")
    check(len(btc0["private_key"]) == 64, "BTC key is raw 32-byte hex, no 0x")
    check(btc0.get("wif", "")[:1] in {"K", "L"}, f"a compressed WIF is exported ({btc0.get('wif', '-')[:4]}...)")

    section("self-verification catches a tampered entry")

    check(g.verify_evm(dict(evm0)), "a correct EVM entry verifies")
    check(not g.verify_evm({**evm0, "address": "0x" + "11" * 20}),
          "an address that does not match its key is rejected")
    check(not g.verify_evm({**evm0, "private_key": "0xnotakey"}),
          "a malformed key is rejected rather than raising")
    check(g.verify_btc(dict(btc0), M, 0), "a correct BTC entry verifies")
    check(not g.verify_btc({**btc0, "address": BTC_ADDR_0.replace("bc1q", "bc1p")}, M, 0),
          "a BTC address that does not re-derive is rejected")


def mnemonic_checks() -> None:
    section("mnemonics")

    twelve = g.new_mnemonic(12)
    check(len(twelve.split()) == 12, f"12 words requested, 12 produced ({len(twelve.split())})")
    check(len(g.new_mnemonic(24).split()) == 24, "24 words requested, 24 produced")
    check(g.new_mnemonic(12) != twelve, "a new phrase is actually new")

    check(g.check_mnemonic("  " + M.replace(" ", "   ") + "  ") == M,
          "check_mnemonic normalises runs of whitespace")
    try:
        g.check_mnemonic("abandon abandon abandon")
        check(False, "an invalid phrase is refused")
    except SystemExit:
        check(True, "an invalid phrase is refused")


def build_checks(tmp: Path) -> None:
    section("build(): hd mode shares one phrase")

    doc = g.build(chains=["evm", "btc"], count=3, mode="hd", words=12,
                  mnemonic=M, passphrase="", verify=True)
    check(doc["mnemonic"] == M, "the phrase is stored once, at the top level")
    check("mnemonic" not in doc["wallets"][0], "individual entries do not repeat it")
    check([w["index"] for w in doc["wallets"]] == [0, 1, 2], "indices run 0..count-1")
    check(doc["version"] == g.VERSION and doc["mode"] == "hd" and doc["count"] == 3,
          "version, mode and count are recorded")
    check(doc["chains"] == ["evm", "btc"], "chains are recorded")
    check("PRIVATE KEYS" in doc["warning"], "the warning travels with the file")
    check(doc["evm_path"] == g.EVM_PATH and doc["btc_path"] == g.BTC_PATH,
          "the derivation paths are recorded for later re-derivation")
    check("bip39_passphrase" not in doc, "an empty passphrase is not stored")
    check(doc["wallets"][0]["evm"]["address"] == EVM_ADDR_0, "wallet 0 is the vector address")
    check(doc["wallets"][0]["btc"]["address"] == BTC_ADDR_0, "and its BTC side too")

    doc_p = g.build(chains=["evm"], count=1, mode="hd", words=12,
                    mnemonic=M, passphrase="hunter2", verify=False)
    check(doc_p["bip39_passphrase"] == "hunter2", "a non-empty passphrase is stored for --verify-file")
    check(doc_p["wallets"][0]["evm"]["address"] != EVM_ADDR_0,
          "a passphrase produces a different wallet (so it is really used)")

    section("build(): random mode gives every wallet its own phrase")

    doc_r = g.build(chains=["evm"], count=3, mode="random", words=12,
                    mnemonic=None, passphrase="", verify=True)
    check("mnemonic" not in doc_r, "no top-level phrase in random mode")
    phrases = [w.get("mnemonic") for w in doc_r["wallets"]]
    check(all(p and len(p.split()) == 12 for p in phrases), "each entry carries its own 12-word phrase")
    check(len(set(phrases)) == 3, "the phrases are distinct")
    check(len({w["evm"]["address"] for w in doc_r["wallets"]}) == 3, "so are the addresses")

    try:
        g.build(chains=["evm"], count=1, mode="random", words=12,
                mnemonic=M, passphrase="", verify=False)
        check(False, "--mnemonic is refused in random mode")
    except SystemExit:
        check(True, "--mnemonic is refused in random mode")


def encryption_checks() -> None:
    section("encryption round-trip")

    doc = g.build(chains=["evm"], count=1, mode="hd", words=12,
                  mnemonic=M, passphrase="", verify=False)
    blob = g.encrypt_doc(doc, "correct horse")
    check(blob["encrypted"] is True and blob["cipher"] == "fernet", "the blob is marked encrypted")
    check(blob["kdf"]["name"] == "pbkdf2-hmac-sha256"
          and blob["kdf"]["iterations"] >= 600_000, "PBKDF2 parameters are recorded")
    serialised = json.dumps(blob)
    check(doc["wallets"][0]["evm"]["private_key"] not in serialised,
          "no private key appears in the encrypted file")
    check("abandon" not in serialised, "no phrase appears in the encrypted file")

    back = g.decrypt_doc(blob, "correct horse")
    check(back == doc, "decrypting returns the original document exactly")

    try:
        g.decrypt_doc(blob, "wrong password")
        check(False, "a wrong password is refused")
    except SystemExit:
        check(True, "a wrong password is refused")


def file_checks(tmp: Path) -> None:
    section("write_private(): refuses to clobber, locks the file down")

    target = tmp / "out" / "wallets.json"
    note = g.write_private(target, '{"a": 1}\n', force=False)
    check(target.is_file() and target.read_text(encoding="utf-8") == '{"a": 1}\n',
          "the file is written, creating parent directories")
    check(isinstance(note, str) and bool(note.strip()),
          f"a protection note is returned ({note[:40]})")

    try:
        g.write_private(target, "second", force=False)
        check(False, "an existing file is not overwritten without --force")
    except SystemExit:
        check(True, "an existing file is not overwritten without --force")

    g.write_private(target, "second", force=True)
    check(target.read_text(encoding="utf-8") == "second", "--force overwrites")

    if os.name != "nt":
        mode = target.stat().st_mode & 0o777
        check(mode == 0o600, f"POSIX permissions are 0600 ({oct(mode)})")

    section("warn_if_in_git_repo()")

    plain = tmp / "plain"
    plain.mkdir(exist_ok=True)
    err = io.StringIO()
    with redirect_stderr(err):
        g.warn_if_in_git_repo(plain / "wallets.json")
    check(err.getvalue() == "", "no warning outside a repository")

    repo = tmp / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    err = io.StringIO()
    with redirect_stderr(err):
        g.warn_if_in_git_repo(repo / "wallets.json")
    check("WARNING" in err.getvalue() and ".gitignore" in err.getvalue(),
          "a file inside a repository produces the .gitignore warning")


def main_checks(tmp: Path) -> None:
    section("main(): generating, verifying and refusing")

    out = tmp / "w1.json"
    rc, text = run_main(["--count", "2", "--chains", "evm", "--out", str(out), "--quiet"])
    check(rc == 0, "generating exits 0")
    check(text.strip() == "", "--quiet prints nothing")
    doc = json.loads(out.read_text(encoding="utf-8"))
    check(len(doc["wallets"]) == 2 and doc["count"] == 2, "two wallets in the file")
    # Not the vector address: main() generates a NEW random phrase each run.
    # What must hold is that every entry uses the MetaMask path and that the
    # addresses differ -- --verify-file below then proves each key matches.
    check(all(w["evm"]["path"] == g.EVM_PATH.format(i=w["index"]) for w in doc["wallets"])
          and len({w["evm"]["address"] for w in doc["wallets"]}) == 2,
          "each wallet uses the MetaMask path for its own index, with distinct addresses")

    rc, text = run_main(["--verify-file", str(out), "--quiet"])
    check(rc == 0 and "All 2 wallets verified" in text, "--verify-file re-derives every key")

    tampered = tmp / "tampered.json"
    bad = json.loads(out.read_text(encoding="utf-8"))
    bad["wallets"][1]["evm"]["private_key"] = bad["wallets"][0]["evm"]["private_key"]
    tampered.write_text(json.dumps(bad), encoding="utf-8")
    rc, text = run_main(["--verify-file", str(tampered), "--quiet"])
    check(rc == 1 and "FAIL evm index 1" in text, "a swapped key fails verification with exit 1")

    rc, text = run_main(["--count", "1", "--chains", "evm", "--out", str(tmp / "nk.json"),
                         "--show-keys"])
    check("evm_key=0x" in text, "--show-keys prints the keys it warns about")

    rc, text = run_main(["--count", "1", "--chains", "evm", "--out", str(tmp / "nv.json"),
                         "--no-verify"])
    check("Self-check OFF" in text, "--no-verify says so instead of pretending")

    rc, text = run_main(["--count", "1", "--chains", "evm", "--out", str(tmp / "enc.json"),
                         "--encrypt", "--password-file", str(_pw(tmp / "pw.txt"))])
    check(rc == 0, "an encrypted export succeeds")
    blob = json.loads((tmp / "enc.json").read_text(encoding="utf-8"))
    check(blob.get("encrypted") is True, "and the file is really encrypted")
    rc, text = run_main(["--verify-file", str(tmp / "enc.json"),
                         "--password-file", str(tmp / "pw.txt")])
    check(rc == 0, "the encrypted export verifies with the same password, no prompt")

    for argv, label in (
        (["--count", "0"], "--count 0 is refused"),
        (["--chains", "doge"], "an unknown chain is refused"),
        (["--mnemonic", M, "--mnemonic-file", str(tmp / "m.txt")],
         "--mnemonic together with --mnemonic-file is refused"),
        (["--mnemonic-file", str(tmp / "missing.txt")], "a missing mnemonic file is refused"),
    ):
        rc, text = run_main(argv + ["--out", str(tmp / "never.json")])
        check(rc != 0, f"{label} ({text.strip().splitlines()[-1][:70]})")

    rc, text = run_main(["--count", "1", "--chains", "evm", "--out", str(tmp / "existing.json")])
    check(rc == 0, "first write to a new path succeeds")
    rc, text = run_main(["--count", "1", "--chains", "evm", "--out", str(tmp / "existing.json")])
    check(rc != 0 and "already exists" in text, "a second write without --force is refused")


def _pw(path: Path) -> Path:
    path.write_text("file-password\n", encoding="utf-8")
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gen_wallets_test_"))
    vector_checks()
    mnemonic_checks()
    build_checks(tmp)
    encryption_checks()
    file_checks(tmp)
    main_checks(tmp)

    print()
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("All gen_wallets checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
