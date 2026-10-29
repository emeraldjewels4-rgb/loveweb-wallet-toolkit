#!/usr/bin/env python3
"""Tests for check_funding.py.

No network: both providers are replaced by fakes, and time.sleep is recorded
instead of waited on. That keeps the retry logic testable rather than a comment.

The behaviours that matter here are the ones that decide whether a REPORT can be
trusted:

  * the three states (never funded / funded then swept / holds funds) come from
    total_received, NOT from the current balance -- a balance-based check would
    call a swept address untouched;
  * the control addresses must clear and raise correctly. If one is wrong the
    run has to exit 1 and say the numbers are untrustworthy, because a silent
    all-clear on a broken detector is the failure mode with real money behind it;
  * a funded candidate is only "confirmed" after a second, independent provider
    agrees, and a disagreement is reported as unverified rather than printed as
    a holder.

    python test_check_funding.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path

import check_funding as c

FAILED: list[str] = []

FUNDED_CTRL = c.CONTROLS[0][1]   # genesis: funded, still holding
SWEPT_CTRL = c.CONTROLS[1][1]    # funded then swept: balance 0, received > 0
FRESH_CTRL = c.CONTROLS[2][1]    # never funded

USER_A = "bc1quser000000000000000000000000000000a"
USER_B = "bc1quser1111111111111111111111111111111b"


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


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def row(total_received=0, final_balance=0, n_tx=0) -> dict:
    return {"total_received": total_received, "final_balance": final_balance, "n_tx": n_tx}


def healthy_rows(user_a=None, user_b=None) -> dict:
    """A provider that gets the controls right."""
    return {
        FUNDED_CTRL: row(5_000_000_000_000, 5_000_000_000_000, 1),
        SWEPT_CTRL: row(100_000, 0, 212),
        FRESH_CTRL: row(0, 0, 0),
        USER_A: user_a or row(0, 0, 0),
        USER_B: user_b or row(0, 0, 0),
    }


class FakeChain:
    def __init__(self, rows: dict, fail_on_batch: set[int] | None = None,
                 esplora: dict | None = None):
        self.rows = rows
        self.fail_on_batch = fail_on_batch or set()
        self.esplora = esplora or {}
        self.batches: list[list[str]] = []
        self.esplora_calls: list[str] = []
        self.sleeps: list[float] = []

    def batch_lookup(self, addresses, retries=4):
        self.batches.append(list(addresses))
        if len(self.batches) in self.fail_on_batch:
            raise c.ProviderError("fake provider down")
        return {a: self.rows[a] for a in addresses if a in self.rows}

    def esplora_funded(self, address: str) -> bool:
        self.esplora_calls.append(address)
        if address not in self.esplora:
            raise RuntimeError("no second opinion available")
        return self.esplora[address]

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def write_wallets(path: Path, addresses: list[str], extra_evm_only: bool = True) -> Path:
    doc = {"wallets": []}
    for i, addr in enumerate(addresses):
        doc["wallets"].append({"index": i, "btc": {"address": addr}})
    if extra_evm_only:
        doc["wallets"].append({"index": 99, "evm": {"address": "0x" + "11" * 20}})
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def run_main(chain: FakeChain, argv: list[str]) -> tuple[int, str]:
    originals = (c.batch_lookup, c.esplora_funded, c.time.sleep)
    c.batch_lookup, c.esplora_funded, c.time.sleep = (
        chain.batch_lookup, chain.esplora_funded, chain.sleep)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = c.main(argv)
    except SystemExit as exc:
        code = exc.code
        return (code if isinstance(code, int) else 1), buf.getvalue()
    finally:
        c.batch_lookup, c.esplora_funded, c.time.sleep = originals
    return int(rc), buf.getvalue()


# --------------------------------------------------------------------------- #
# collect()
# --------------------------------------------------------------------------- #
def collect_checks(tmp: Path) -> None:
    section("collect(): addresses out of wallet exports")

    f1 = write_wallets(tmp / "a.json", [USER_A, USER_B])
    f2 = write_wallets(tmp / "b.json", [USER_B, "bc1qthird"])   # USER_B repeated
    got = c.collect([str(f1), str(f2)])

    check(len(got) == 3, f"duplicates collapse across files ({len(got)} unique)")
    check([a for _s, _i, a in got] == [USER_A, USER_B, "bc1qthird"], "first-seen order is kept")
    check(got[1][0] == "a.json", "the source file of a duplicate is the first one seen")
    check(all(i is not None and a for _s, i, a in got), "index travels with the address")

    mixed = tmp / "mixed.json"
    mixed.write_text(json.dumps({"wallets": [
        {"index": 0, "evm": {"address": "0x" + "22" * 20}},
        {"index": 1, "btc": {"address": USER_A}},
    ]}), encoding="utf-8")
    got = c.collect([str(mixed)])
    check([a for _s, _i, a in got] == [USER_A], "an EVM-only entry is skipped, not crashed on")

    enc = tmp / "enc.json"
    enc.write_text(json.dumps({"encrypted": True, "wallets": []}), encoding="utf-8")
    try:
        c.collect([str(enc)])
        check(False, "an encrypted export is refused")
    except SystemExit:
        check(True, "an encrypted export is refused")


# --------------------------------------------------------------------------- #
# batch_lookup() and esplora_funded()
# --------------------------------------------------------------------------- #
def provider_checks() -> None:
    section("batch_lookup(): batching, retries, backoff")
    originals = (c.http_get_json, c.time.sleep)

    seen_urls: list[str] = []

    def ok(url, timeout=30):
        seen_urls.append(url)
        return {"addresses": [{"address": "a1", "total_received": 1},
                              {"address": "a2", "total_received": 0}]}

    c.http_get_json, c.time.sleep = ok, lambda s: None
    try:
        rows = c.batch_lookup(["a1", "a2"])
        check(set(rows) == {"a1", "a2"}, "rows are keyed by address")
        check("active=a1%7Ca2" in seen_urls[0], "addresses go in one batched 'active' param")
    finally:
        c.http_get_json, c.time.sleep = originals

    calls = {"n": 0}

    def throttled_once(url, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", Message(), None)
        return {"addresses": [{"address": "a1", "total_received": 7}]}

    slept: list[float] = []
    c.http_get_json, c.time.sleep = throttled_once, lambda s: slept.append(s)
    try:
        rows = c.batch_lookup(["a1"])
        check(rows["a1"]["total_received"] == 7, "a 429 is retried and then succeeds")
        check(slept and slept[0] > 0, f"the retry backs off before retrying ({slept})")
    finally:
        c.http_get_json, c.time.sleep = originals

    def bad_payload(url, timeout=30):
        return {"nope": "not a list"}

    c.http_get_json, c.time.sleep = bad_payload, lambda s: None
    try:
        c.batch_lookup(["a1"], retries=2)
        check(False, "an unexpected payload raises ProviderError")
    except c.ProviderError:
        check(True, "an unexpected payload raises ProviderError")
    finally:
        c.http_get_json, c.time.sleep = originals

    def always_down(url, timeout=30):
        raise urllib.error.URLError("network unreachable")

    c.http_get_json, c.time.sleep = always_down, lambda s: None
    try:
        c.batch_lookup(["a1"], retries=3)
        check(False, "a dead provider gives up with ProviderError, not a traceback")
    except c.ProviderError as exc:
        check("gave up after 3 attempt(s)" in str(exc),
              f"giving up names the attempt count ({exc})")
        check("network unreachable" in str(exc),
              "giving up also carries the underlying cause")
        check(isinstance(exc.__cause__, urllib.error.URLError),
              "the original exception is chained, not swallowed")
    finally:
        c.http_get_json, c.time.sleep = originals

    attempts = {"n": 0}

    def not_found(url, timeout=30):
        attempts["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    slept.clear()
    c.http_get_json, c.time.sleep = not_found, lambda s: slept.append(s)
    try:
        c.batch_lookup(["a1"], retries=4)
        check(False, "a non-retryable 404 fails immediately")
    except c.ProviderError as exc:
        check(attempts["n"] == 1 and not slept,
              f"a non-retryable 404 is not retried ({attempts['n']} attempt, {len(slept)} sleeps)")
        check("HTTP 404" in str(exc), f"and the message names the code ({exc})")
    finally:
        c.http_get_json, c.time.sleep = originals

    attempts["n"] = 0
    slept.clear()

    def always_busy(url, timeout=30):
        attempts["n"] += 1
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", Message(), None)

    c.http_get_json, c.time.sleep = always_busy, lambda s: slept.append(s)
    try:
        c.batch_lookup(["a1"], retries=2)
        check(False, "a retryable 503 that never clears gives up")
    except c.ProviderError as exc:
        check("gave up after 2 attempt(s)" in str(exc) and "HTTP 503" in str(exc),
              f"an exhausted 503 retry reports both the count and the code ({exc})")
        check(attempts["n"] == 2 and len(slept) == 1,
              f"two attempts, one backoff ({attempts['n']} attempts, {len(slept)} sleeps)")
    finally:
        c.http_get_json, c.time.sleep = originals

    section("esplora_funded(): the second opinion")

    for stats, expected, label in (
        ({"funded_txo_count": 3}, True, "funding transactions count as funded"),
        ({"funded_txo_count": 0}, False, "zero funding transactions is not funded"),
        ({}, False, "a missing chain_stats block is not funded"),
    ):
        c.http_get_json = lambda url, timeout=30, _s=stats: {"chain_stats": _s}
        try:
            check(c.esplora_funded("addr") is expected, label)
        finally:
            pass
    c.http_get_json = originals[0]
    c.time.sleep = originals[1]


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def main_checks(tmp: Path) -> None:
    section("main(): three states, and the controls that prove them")

    wallets = write_wallets(tmp / "w.json", [USER_A])
    chain = FakeChain(healthy_rows(), esplora={FUNDED_CTRL: True, SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--pause", "0"])
    check(rc == 0, "a clean run exits 0")
    check("controls pass" in out, "controls clear in both directions")
    check("flagged=True " in out and "flagged=False" in out, "both control verdicts are printed")
    check("never funded      : 1" in out, "the untouched address is counted as never funded")
    check("funded then swept : 0" in out, "and nothing is miscounted as swept")
    check("none has ever received funds" in out, "an all-clear sentence is printed when true")

    section("a swept address is 'used' even with a zero balance")

    rows = healthy_rows(user_a=row(250_000, 0, 4))               # funded, swept
    chain = FakeChain(rows, esplora={FUNDED_CTRL: True, SWEPT_CTRL: True, USER_A: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--pause", "0"])
    check("funded then swept : 1" in out, "received>0 with balance 0 counts as swept")
    check("CONFIRMED funded addresses (1)" in out, "and is confirmed by the second provider")
    check(USER_A in out, "the address itself is listed")
    check("none has ever received funds" not in out, "no all-clear when something was found")

    section("one provider is not enough")

    rows = healthy_rows(user_b=row(999, 999, 2))
    chain = FakeChain(rows, esplora={FUNDED_CTRL: True, SWEPT_CTRL: True, USER_B: False})
    rc, out = run_main(chain, ["--wallets", str(write_wallets(tmp / "b2.json", [USER_B])),
                               "--pause", "0"])
    check("NOT confirmed (1)" in out, "a disagreement is reported as unconfirmed")
    check("CONFIRMED funded addresses" not in out, "and never printed as a holder")
    check(rc == 0, "an unconfirmed candidate does not fail the run on its own")

    section("a broken detector must not report an all-clear")

    rows = healthy_rows()
    rows[FUNDED_CTRL] = row(0, 0, 0)          # genesis reported as never funded
    chain = FakeChain(rows, esplora={SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--pause", "0"])
    check(rc == 1, "a wrong control exits 1")
    check("CONTROLS FAILED" in out, "and says the report cannot be trusted")
    check("WRONG" in out, "the specific control is marked WRONG")

    rows = healthy_rows()
    del rows[FRESH_CTRL]                        # control absent from the payload
    chain = FakeChain(rows, esplora={FUNDED_CTRL: True, SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--pause", "0"])
    check(rc == 1 and "MISSING -- results are UNRELIABLE" in out,
          "a control missing from the payload is a failure, not a pass")

    section("resume, budget and interruption")

    results = tmp / "resume.json"
    results.write_text(json.dumps({USER_A: {"source": "w.json", "index": 0,
                                            "total_received": 0, "final_balance": 0,
                                            "n_tx": 0}}), encoding="utf-8")
    chain = FakeChain(healthy_rows(), esplora={FUNDED_CTRL: True, SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--results", str(results),
                               "--pause", "0"])
    check("resuming: 1" in out, "an existing results file is resumed")
    sent = [a for batch in chain.batches for a in batch]
    check(USER_A not in sent, "an already-checked address is not asked about again")
    check(len(sent) == 3, f"only the three controls are queried ({len(sent)})")

    many = write_wallets(tmp / "many.json", [USER_A, USER_B, "bc1qthird"])
    chain = FakeChain(healthy_rows(), esplora={FUNDED_CTRL: True, SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(many), "--limit", "1", "--pause", "0"])
    check("addresses checked : 1/1" in out, "--limit trims the wallet list before checking")

    chain = FakeChain(healthy_rows(), fail_on_batch={2},
                      esplora={FUNDED_CTRL: True, SWEPT_CTRL: True})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--batch-size", "1",
                               "--pause", "0"])
    check("FAILED" in out and "stopping" in out, "a provider failure stops the run cleanly")
    check(rc == 1, "and the exit code is nonzero (controls could not be verified)")

    section("results that cannot be matched are an error, not an empty report")

    bogus = tmp / "bogus.json"
    bogus.write_text(json.dumps({"bc1qbogus": {"source": "x", "index": 0,
                                               "total_received": 0, "final_balance": 0,
                                               "n_tx": 0}}), encoding="utf-8")
    chain = FakeChain({}, esplora={})
    rc, out = run_main(chain, ["--wallets", str(wallets), "--results", str(bogus),
                               "--pause", "0"])
    check("INTERNAL ERROR" in out, "a results/list mismatch refuses to print a report")
    check(rc == 1, "and exits 1")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="check_funding_test_"))
    collect_checks(tmp)
    provider_checks()
    main_checks(tmp)

    print()
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("All check_funding checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
