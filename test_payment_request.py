#!/usr/bin/env python3
"""Tests for payment_request.py. No network, no funds, no real keys used.

    python test_payment_request.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import urllib.parse
from pathlib import Path

import gen_wallets as g
import payment_request as pr

FAILED: list[str] = []

REAL = ["wallets-btc-1000-a.json", "wallets-btc-1000-b.json",
        "wallets-btc-1000-c.json", "wallets-btc-1000-d.json"]


def check(cond: bool, label: str, detail: str = "") -> None:
    # Convention here is (condition, label). test_token.py uses the OPPOSITE
    # order, so copying an idiom from one file to the other inverts the call
    # silently: a string condition is always truthy (a check that can never
    # fail) and a boolean label means the two arguments are swapped. Guard both
    # slots so that mistake crashes instead of reporting a false pass.
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if not cond and detail else ""))
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def expect_exit(fn, *args, **kwargs):
    """Run something that should exit non-zero; return its message.

    argparse reports only through stderr and exits 2, so the stream is captured
    too -- otherwise those cases look like a bare "2" and pass for the wrong
    reason.
    """
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            fn(*args, **kwargs)
    except SystemExit as exc:
        return f"{exc} {err.getvalue()}".strip()
    return ""


def make_file(path: Path, count: int = 3) -> list:
    """Small wallet export with fresh addresses; returns the addresses."""
    doc = g.build(chains=["btc"], count=count, mode="random", words=12,
                  mnemonic=None, passphrase="", verify=False)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return [w["btc"]["address"] for w in doc["wallets"]]


def read_csv(path: Path) -> list:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------- #
def test_real_files_collected() -> None:
    section("Collecting the real 4,000-address set")
    entries = pr.collect(REAL)
    check(len(entries) == 4000, "4,000 addresses read", str(len(entries)))
    addresses = [a for _, _, a in entries]
    check(len(set(addresses)) == 4000, "all addresses unique")
    check(all(a.startswith("bc1q") for a in addresses), "every address is mainnet bc1q")
    first_doc = json.loads(Path(REAL[0]).read_text())
    check(entries[0][2] == first_doc["wallets"][0]["btc"]["address"],
          "file order preserved (a#0 first)")
    check(entries[0][0] == "wallets-btc-1000-a.json", "source file recorded")


def test_splitting() -> None:
    section("Amount splitting")
    parts = pr.split_even(100_000_000, 4000)
    check(sum(parts) == 100_000_000, "1 BTC over 4000 rows sums to exactly 1 BTC")
    check(set(parts) == {25_000}, "and is exactly 25,000 sat per row")

    parts = pr.split_even(100_001, 3)
    check(sum(parts) == 100_001, "remainder case sums exactly")
    check(parts == [33334, 33334, 33333], "remainder goes to the first rows", str(parts))

    check(pr.to_sat("0.0001") == 10_000, "0.0001 BTC is 10,000 sat")
    check(pr.to_sat("1") == 100_000_000, "1 BTC is 100,000,000 sat")
    check(pr.to_sat("0.00000001") == 1, "1 sat is expressible")

    msg = expect_exit(pr.to_sat, "0.000000015")
    check("whole number of satoshis" in msg, "sub-satoshi amount refused", msg[:80])
    msg = expect_exit(pr.to_sat, "0")
    check("greater than zero" in msg, "zero amount refused", msg[:80])
    msg = expect_exit(pr.to_sat, "abc")
    check("not a number" in msg, "non-numeric amount refused", msg[:80])


def test_dust_floor() -> None:
    section("Dust floor")
    check(pr.DUST_FLOOR_SAT == 546, "floor is the conservative 546 sat")
    pr.check_dust([546, 10_000])  # must not raise
    check(True, "546 sat is accepted")

    msg = expect_exit(pr.check_dust, [546, 545, 10_000])
    check("below the dust floor" in msg, "one thin row is refused", msg[:80])
    check("545" not in msg or "rows" in msg, "message counts rows")
    pr.check_dust([546] * 4000)
    check(True, "4,000 rows at exactly the floor are fine")


def test_batch_limits() -> None:
    section("Standardness limit on batch size")
    check(pr.MAX_P2WPKH_OUTPUTS == 3225, "100,000 vB / 31 vB = 3225 outputs")
    pr.check_batches(3225)
    check(True, "batch of 3225 accepted")
    for bad in (0, -1, 3226, 10_000):
        msg = expect_exit(pr.check_batches, bad)
        check(bool(msg), f"batch size {bad} refused", msg[:80])
    check("standard" in expect_exit(pr.check_batches, 5000),
          "the refusal explains the standardness limit")


def test_address_validation() -> None:
    section("Address validation")
    good = json.loads(Path(REAL[0]).read_text())["wallets"][0]["btc"]["address"]
    pr.validate_address(good)
    check(True, "a real export address validates")

    for bad, why in (
        ("tb1q06fcnpruauqnm302mt2a70z9805hsaewr7x8m2p", "testnet"),
        ("bc1q06fcnpruauqnm302mt2a70z9805hsaewr76y5h", "bad checksum"),
        ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "legacy"),
        ("", "empty"),
    ):
        msg = expect_exit(pr.validate_address, bad)
        check(bool(msg), f"{why} address refused", msg[:80])


def test_duplicates_refused() -> None:
    section("Duplicate addresses are refused, not silently deduped")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a = make_file(tmp / "one.json", 3)
        b = make_file(tmp / "two.json", 3)
        pr.collect([str(tmp / "one.json"), str(tmp / "two.json")])
        check(True, "two disjoint files collect 6 addresses")

        doc = json.loads((tmp / "one.json").read_text())
        doc["wallets"][0]["btc"]["address"] = a[0]  # unchanged, but prove the guard
        doc["wallets"][1]["btc"]["address"] = a[0]  # duplicate inside one file
        (tmp / "dup.json").write_text(json.dumps(doc), encoding="utf-8")
        msg = expect_exit(pr.collect, [str(tmp / "dup.json")])
        check("duplicate address" in msg, "duplicate inside a file refused", msg[:100])
        check(a[0] in msg, "the offending address is named")
        check("two" in msg or "dup.json" in msg, "the source file is named")


def test_input_refusals() -> None:
    section("Bad inputs")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        msg = expect_exit(pr.collect, [str(tmp / "missing.json")])
        check("no such wallet file" in msg, "missing file refused", msg[:80])

        (tmp / "bad.json").write_text("{not json", encoding="utf-8")
        msg = expect_exit(pr.collect, [str(tmp / "bad.json")])
        check("not valid JSON" in msg, "malformed JSON refused", msg[:80])

        (tmp / "enc.json").write_text(json.dumps({"encrypted": True, "payload": "x"}),
                                      encoding="utf-8")
        msg = expect_exit(pr.collect, [str(tmp / "enc.json")])
        check("encrypted" in msg, "encrypted export refused", msg[:80])

        (tmp / "evm.json").write_text(json.dumps({"wallets": [{"index": 0, "evm": {}}]}),
                                      encoding="utf-8")
        msg = expect_exit(pr.collect, [str(tmp / "evm.json")])
        check("no Bitcoin addresses" in msg, "EVM-only export refused", msg[:80])


def test_bip21() -> None:
    section("BIP21 URIs")
    uri = pr.bip21("bc1q06fcnpruauqnm302mt2a70z9805hsaewr76y5g", 25_000, "batch one")
    check(uri.startswith("bitcoin:bc1q06fcnpruauqnm302mt2a70z9805hsaewr76y5g?"),
          "URI starts with bitcoin:<address>?", uri[:60])
    query = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    check(query["amount"] == ["0.00025"], "amount is decimal BTC", str(query.get("amount")))
    check(query["label"] == ["batch one"], "label is encoded", str(query.get("label")))
    no_label = pr.bip21("bc1q06fcnpruauqnm302mt2a70z9805hsaewr76y5g", 100_000_000)
    check("label=" not in no_label, "no label when none given")
    check("amount=1" in no_label, "1 BTC renders without trailing zeros", no_label)


def test_end_to_end_csv() -> None:
    section("End-to-end: 4,000-row sheet")
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "request"
        rc = pr.main(["--wallets", *REAL, "--total", "1", "--out", str(stem),
                      "--memo", "distribution"])
        check(rc == 0, "main exits 0")
        csv_path, md_path = stem.with_suffix(".csv"), stem.with_suffix(".md")
        check(csv_path.is_file() and md_path.is_file(), "both outputs written")

        rows = read_csv(csv_path)
        check(list(rows[0].keys()) == pr.CSV_HEADER, "header is exactly the documented order")
        check(len(rows) == 4000, "4,000 data rows", str(len(rows)))
        check(sum(int(r["amount_sat"]) for r in rows) == 100_000_000,
              "rows sum to exactly the requested 1 BTC")
        check(all(r["amount_btc"] == "0.00025000" for r in rows),
              "every row is 0.00025000 BTC")
        check(all(int(r["amount_sat"]) >= pr.DUST_FLOOR_SAT for r in rows),
              "no row is below the dust floor")
        check([r["n"] for r in rows] == [str(i) for i in range(1, 4001)],
              "row numbers are 1..4000 in order")
        check(len({r["address"] for r in rows}) == 4000, "addresses unique in the sheet")

        batches = {}
        for r in rows:
            batches[r["batch"]] = batches.get(r["batch"], 0) + 1
        check(batches == {"1": 2000, "2": 2000}, "split into 2 batches of 2,000", str(batches))
        check(sum(int(r["amount_sat"]) for r in rows if r["batch"] == "1") == 50_000_000,
              "batch 1 totals 0.5 BTC")

        check(len({r["bip21"] for r in rows}) == 4000, "every BIP21 URI is distinct")
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(rows[0]["bip21"]).query)
        check(parsed["amount"] == ["0.00025"], "URI amount matches the row")

        note = md_path.read_text(encoding="utf-8")
        check("4,000" in note, "cover note reports the count")
        check("1.00000000 BTC" in note, "cover note reports the total")
        check("| 1 | 2,000 | 0.50000000 BTC |" in note, "cover note lists batch 1")
        check("100,000" in note or "standard" in note, "cover note explains the batch limit")
        check("no private keys" in note, "cover note states it holds no secrets")

        before = csv_path.read_text(encoding="utf-8")
        msg = expect_exit(pr.main, ["--wallets", *REAL, "--total", "1", "--out", str(stem)])
        check("already exists" in msg, "refuses to overwrite without --force", msg[:80])
        check(csv_path.read_text(encoding="utf-8") == before, "existing sheet untouched")

        rc = pr.main(["--wallets", *REAL, "--amount", "0.0001", "--out", str(stem),
                      "--no-bip21", "--force"])
        rows2 = read_csv(csv_path)
        check(rc == 0, "per-address mode with --force exits 0")
        check(sum(int(r["amount_sat"]) for r in rows2) == 40_000_000,
              "0.0001 BTC x 4000 = 0.4 BTC (40,000,000 sat)")
        check(all(r["bip21"] == "" for r in rows2), "--no-bip21 leaves the column empty")


def test_cover_note_is_actionable() -> None:
    section("Cover note states the ceiling honestly and shows how to change it")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        stem = tmp / "r"
        rc = pr.main(["--wallets", *REAL, "--total", "1", "--out", str(stem)])
        check(rc == 0, "generated")
        note = stem.with_suffix(".md").read_text(encoding="utf-8")

        check("An address has no maximum" in note,
              "says plainly that an address has no maximum")
        check("1.00000000 BTC (this sheet)" in note, "marks the current total")
        check("10.00000000 BTC" in note and "0.00250000 BTC" in note,
              "shows the 10x total and its per-address figure")
        check("100.00000000 BTC" in note and "0.02500000 BTC" in note,
              "shows the 100x total and its per-address figure")
        check(all(name in note for name in REAL),
              "the regenerate command names every wallet file")
        check("--amount 0.00025000" in note and "--force" in note,
              "the command reproduces this exact sheet, not a different one")

        # A multiple the supply cap would refuse is named as impossible instead
        # of being printed as a runnable command.
        big = tmp / "big"
        rc = pr.main(["--wallets", *REAL, "--amount", "1000", "--out", str(big)])
        big_note = big.with_suffix(".md").read_text(encoding="utf-8")
        check(rc == 0, "a 4,000,000 BTC sheet is generated (under the cap)")
        check("not possible" in big_note and "40,000,000.00000000 BTC" in big_note,
              "the 10x multiple over supply is marked impossible")
        check("--amount 1000.00000000" in big_note,
              "the big sheet's regen command is its own amount")

        # The per-address figures must be the real arithmetic for 4,000 rows.
        check(pr.to_sat("10") // 4000 == 250_000, "10 BTC / 4000 rows = 250,000 sat")
        check(pr.to_sat("100") // 4000 == 2_500_000, "100 BTC / 4000 rows = 2,500,000 sat")

        # A small set must not divide by zero or lose the table.
        small = tmp / "three.json"
        make_file(small, 3)
        rc = pr.main(["--wallets", str(small), "--amount", "0.001", "--out", str(tmp / "s")])
        note2 = (tmp / "s.md").read_text(encoding="utf-8")
        check(rc == 0 and "An address has no maximum" in note2,
              "small sets get the same section")
        check("0.00100000 BTC (this sheet)" not in note2 and "BTC total" in note2,
              "small set reports its own total")


def test_small_sets_and_batches() -> None:
    section("Batches over a small set")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        path = tmp / "three.json"
        make_file(path, 3)
        stem = tmp / "r"
        rc = pr.main(["--wallets", str(path), "--total", "0.00003", "--batch-size", "1",
                      "--out", str(stem)])
        rows = read_csv(stem.with_suffix(".csv"))
        check(rc == 0, "3 rows, 3 batches, exits 0")
        check([r["batch"] for r in rows] == ["1", "2", "3"], "one batch per row")
        check(sum(int(r["amount_sat"]) for r in rows) == 3000, "0.00003 BTC = 3,000 sat total")
        check([r["amount_sat"] for r in rows] == ["1000", "1000", "1000"],
              "even split of 3,000 over 3")


def test_supply_ceiling() -> None:
    section("Supply ceiling: an unpayable ask is refused, a huge one is labelled")
    cap = pr.TOTAL_SUPPLY_SAT
    check(cap == 21_000_000 * 100_000_000, "the cap is all 21,000,000 BTC, in sats")

    # Exactly the whole supply is still a request; one satoshi more is a
    # contradiction, because the coins do not exist.
    pr.check_supply(cap)
    check(True, "a request for exactly 21,000,000 BTC is allowed")
    msg = expect_exit(pr.check_supply, cap + 1)
    check("will ever exist" in msg, "one satoshi over supply is refused", msg[:90])

    check(pr.supply_note(pr.SUPPLY_WARN_SAT - 1) == [],
          "an ordinary ask carries no reality-check block")
    big = pr.supply_note(4_000_000 * 100_000_000)
    # The note is wrapped for the payer, so undo the quoting and line breaks
    # before asserting phrases: a sentence may legitimately span two lines.
    text = " ".join(line.removeprefix("> ").strip() for line in big)
    check("Reality check" in text, "a 4,000,000 BTC ask is labelled")
    check("19.05%" in text, "the block states the share of total supply", text[:120])
    check("not as funds that exist" in text, "the block says the funds are not there")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        path = tmp / "three.json"
        make_file(path, 3)
        stem = tmp / "r"
        # 3 x 7,000,001 BTC = 21,000,003 BTC: over the cap, so nothing is written.
        msg = expect_exit(pr.main, ["--wallets", str(path), "--amount", "7000001",
                                    "--out", str(stem)])
        check("will ever exist" in msg, "the CLI refuses an over-supply sheet", msg[:90])
        check(not stem.with_suffix(".csv").exists()
              and not stem.with_suffix(".md").exists(),
              "a refused sheet leaves no partial files behind")

        rc = pr.main(["--wallets", str(path), "--amount", "7000000", "--out", str(stem)])
        check(rc == 0, "exactly the full supply is generated, not refused")
        cover = stem.with_suffix(".md").read_text(encoding="utf-8")
        check("Reality check" in cover and "100.00%" in cover,
              "the full-supply sheet carries the reality check at 100.00%")


def test_cli_conflicts() -> None:
    section("CLI conflicts")
    msg = expect_exit(pr.main, ["--wallets", REAL[0], "--total", "1", "--amount", "0.001"])
    check("not allowed with argument" in msg or "mutually exclusive" in msg.lower(),
          "--total and --amount are mutually exclusive", msg[:100])
    msg = expect_exit(pr.main, ["--wallets", REAL[0]])
    check("required" in msg, "one of --total/--amount is required", msg[:100])


def main() -> int:
    print("=" * 68)
    print("PAYMENT REQUEST TESTS   (pure local computation; no network, no funds)")
    print("=" * 68)

    def run(fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a crash here is a failing test
            # Argument order matters: check(condition, label). Passing them the
            # other way round reported a crash as "PASS False" and hid it.
            import traceback
            traceback.print_exc()
            check(False, f"{fn.__name__} completed without crashing",
                  f"{type(exc).__name__}: {exc}")

    for fn in (test_real_files_collected, test_splitting, test_dust_floor,
               test_batch_limits, test_address_validation, test_duplicates_refused,
               test_input_refusals, test_bip21, test_end_to_end_csv,
               test_cover_note_is_actionable, test_small_sets_and_batches,
               test_supply_ceiling, test_cli_conflicts):
        run(fn)

    print("\n" + "=" * 68)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print("  -", label)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
