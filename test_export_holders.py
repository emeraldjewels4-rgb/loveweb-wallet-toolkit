#!/usr/bin/env python3
"""Tests for export_holders.py.

No network. A fake JSON-RPC endpoint stands in for the chain and serves
eth_blockNumber, eth_call, and eth_getLogs -- the last one refusing ranges wider
than a configurable cap, exactly as publicnode does, so the window-halving path
in fetch_logs() is genuinely exercised rather than assumed.

Two failure modes matter and both are covered:

  * the log scan MISSES transfer(s), so the balances no longer add up to
    totalSupply(). The tool must exit 1 and write nothing, because a plausible
    CSV that quietly omits someone's balance is worse than no CSV;
  * an address nets to zero or is only ever a mint/burn endpoint and gets listed
    as a holder, or an ERC-721 style Transfer is counted as 1 wei of an ERC-20.

    python test_export_holders.py
"""

from __future__ import annotations

import csv
import io
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import export_holders as e

FAILED: list[str] = []

ZERO = "0x" + "0" * 40


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


def abi_string(text: str) -> str:
    """ABI-encoded return value for a string, as eth_call would give it."""
    raw = text.encode()
    padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + padded.hex()


def transfer(src: str, dst: str, value: int, block: int, index: int = 0) -> dict:
    return {
        "blockNumber": block,
        "logIndex": index,
        "topics": [e.TRANSFER_TOPIC,
                   "0x" + src[2:].rjust(64, "0"),
                   "0x" + dst[2:].rjust(64, "0")],
        "data": "0x" + value.to_bytes(32, "big").hex(),
    }


class FakeChain:
    """Serves the four RPC calls export_holders.py makes, over a fixed log list."""

    def __init__(self, logs, supply, decimals=18, tip=25_000, max_range=10_000,
                 name="mindglitchofdecimal.loveweb", symbol="LOVEWEB"):
        self.logs = logs
        self.supply = supply
        self.decimals = decimals
        self.tip = tip
        self.max_range = max_range
        self.name = name
        self.symbol = symbol
        self.calls: list[tuple[str, list]] = []    # served
        self.rejected: list[tuple[int, int]] = []  # asked for, refused, retried

    def __call__(self, url, method, params, retries=5):
        if method == "eth_blockNumber":
            self.calls.append((method, params))
            return hex(self.tip)
        if method == "eth_call":
            self.calls.append((method, params))
            selector = params[0]["data"]
            if selector == "0x06fdde03":
                return abi_string(self.name)
            if selector == "0x95d89b41":
                return abi_string(self.symbol)
            if selector == "0x313ce567":
                return hex(self.decimals)
            if selector == "0x18160ddd":
                return hex(self.supply)
        if method == "eth_getLogs":
            start = int(params[0]["fromBlock"], 16)
            end = int(params[0]["toBlock"], 16)
            if end - start + 1 > self.max_range:
                self.rejected.append((start, end))
                # fetch_logs() catches SystemExit and halves the window; the real
                # endpoint answers -32701 ("block range too wide").
                raise SystemExit("block range too wide")
            # Only a served window counts: a rejected one was never scanned, and
            # counting it is what made the halving check read a 10,000-block max.
            self.calls.append((method, params))
            return [lg for lg in self.logs if start <= lg["blockNumber"] <= end]
        raise AssertionError(f"unexpected RPC method {method}")

    def ranges(self) -> list[tuple[int, int]]:
        return [(int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16))
                for m, p in self.calls if m == "eth_getLogs"]


@contextmanager
def patched(chain: FakeChain):
    """Point the module's rpc() at the fake chain for the duration of a block.

    fetch_logs() and main() look up `rpc` at call time, so replacing the module
    attribute is enough; forgetting it is why the first window count read 0.
    """
    original = e.rpc
    e.rpc = chain
    try:
        yield chain
    finally:
        e.rpc = original


def run_main(chain: FakeChain, argv: list[str]) -> tuple[int, str, str]:
    """Run export_holders.main() against the fake chain, capturing output."""
    original = e.rpc
    e.rpc = chain
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = e.main(argv)
    finally:
        e.rpc = original
    return int(rc), out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# A small world used by several tests: one mint, three transfers, 1,000 tokens.
# --------------------------------------------------------------------------- #
A = "0x" + "aa" * 20   # minted to, then sends 300 away
B = "0x" + "bb" * 20   # receives 300, sends 100 back
C = "0x" + "cc" * 20   # ends with 100
SUPPLY = 1_000 * 10**18


def world() -> list[dict]:
    # Blocks 100..500, not the real deploy height: the fake chain's tip has to
    # be ABOVE --from-block or the scan loop never iterates at all.
    return [
        transfer(ZERO, A, 1_000 * 10**18, 100, 0),   # mint to A
        transfer(A, B, 300 * 10**18, 200, 0),
        transfer(B, C, 100 * 10**18, 300, 0),
        transfer(A, B, 100 * 10**18, 400, 0),
        transfer(B, A, 100 * 10**18, 500, 0),         # back: A and B both move
    ]


def log_scan_checks() -> None:
    section("log scan: paging and window halving")

    chain = FakeChain(world(), SUPPLY, tip=25_000)
    with patched(chain):
        logs = e.fetch_logs("fake", e.DEFAULT_TOKEN, 1, 25_000, 10_000)
    ranges = chain.ranges()
    check(len(ranges) == 3, f"25,000 blocks in 10,000-block windows = 3 calls ({len(ranges)})")
    check(ranges[0][0] == 1, "first window starts at --from-block")
    check(ranges[-1][1] == 25_000, "last window ends at the tip")
    check(all(b[0] == a[1] + 1 for a, b in zip(ranges, ranges[1:])),
          "windows are contiguous, with no gap and no overlap")

    chain = FakeChain(world(), SUPPLY, tip=25_000, max_range=3_000)
    try:
        with patched(chain):
            logs = e.fetch_logs("fake", e.DEFAULT_TOKEN, 1, 25_000, 10_000)
        survived = True
    except SystemExit:
        # Report this as a failed CHECK, not as a traceback: a guard that
        # disappears should light up the assertion it belongs to.
        logs, survived = [], False
    widths = {b - a + 1 for a, b in chain.ranges()}
    check(survived, "the scan completes when the first window is too wide")
    check(widths and max(widths) <= 3_000,
          f"a 3,000-block cap is discovered and respected (max {max(widths) if widths else 0:,})")
    check(all(b[0] == a[1] + 1 for a, b in zip(chain.ranges(), chain.ranges()[1:])),
          "halving still leaves no gap between windows")
    check(len(logs) == len(world()), f"every transfer is returned exactly once ({len(logs)})")

    chain = FakeChain(world(), SUPPLY, tip=25_000, max_range=500)
    try:
        with patched(chain):
            e.fetch_logs("fake", e.DEFAULT_TOKEN, 1, 25_000, 10_000)
        check(False, "a cap below 1,000 blocks is not silently accepted")
    except SystemExit:
        check(True, "a cap below 1,000 blocks is not silently accepted")


def abi_checks() -> None:
    section("ABI decoding")

    original = e.rpc
    e.rpc = lambda url, method, params, retries=5: abi_string("LOVEWEB")
    try:
        check(e.call_string("fake", "0xt", "0x95d89b41") == "LOVEWEB",
              "dynamic string decodes via its offset/length header")
    finally:
        e.rpc = original

    e.rpc = lambda url, method, params, retries=5: "0x" + ("4c4f5645" + "00" * 28)
    try:
        check(e.call_string("fake", "0xt", "0x95d89b41") == "LOVE",
              "bytes32-style short return is stripped of null padding")
    finally:
        e.rpc = original

    e.rpc = lambda url, method, params, retries=5: hex(10**27)
    try:
        check(e.call_uint("fake", "0xt", "0x18160ddd") == 10**27, "uint256 decodes")
    finally:
        e.rpc = original


def guard_checks(tmp: Path) -> None:
    section("supply guard: an incomplete scan must not produce a CSV")

    out = tmp / "incomplete.csv"
    chain = FakeChain(world(), SUPPLY + 500 * 10**18, tip=25_000)  # 500 unaccounted
    rc, stdout, stderr = run_main(chain, ["--rpc", "fake", "--out", str(out),
                                          "--from-block", "1"])
    check(rc == 1, "missing transfers exit 1")
    check("FAIL" in stderr and "difference" in stderr, "the failure names the difference")
    check(not out.exists(), "no CSV is written when the sums do not match")

    out2 = tmp / "exact.csv"
    chain = FakeChain(world(), SUPPLY, tip=25_000)
    rc, stdout, stderr = run_main(chain, ["--rpc", "fake", "--out", str(out2),
                                          "--from-block", "1"])
    check(rc == 0, "an exact scan exits 0")
    check(out2.exists(), "the CSV exists on success")
    check("supply matched exactly" in stdout, "success line reports the match")


def csv_checks(tmp: Path) -> None:
    section("balances, toggles and the CSV that comes out")

    out = tmp / "holders.csv"
    chain = FakeChain(world(), SUPPLY, tip=25_000)
    rc, stdout, stderr = run_main(chain, ["--rpc", "fake", "--out", str(out),
                                          "--from-block", "1"])
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    by_addr = {r["address"]: r for r in rows}

    check(set(by_addr) == {A, B, C}, f"only positive net positions are holders ({len(rows)})")
    check(ZERO not in by_addr, "the mint's zero-address sender is not a holder")
    check(by_addr[A]["balance_wei"] == str(700 * 10**18), "A = 1000 - 300 - 100 + 100 = 700")
    check(by_addr[B]["balance_wei"] == str(200 * 10**18), "B = 300 + 100 - 100 - 100 = 200")
    check(by_addr[C]["balance_wei"] == str(100 * 10**18), "C = 100")
    check(sum(int(r["balance_wei"]) for r in rows) == SUPPLY, "rows add up to totalSupply")

    check(by_addr[A]["transfers_in"] == "2", "A has 2 transfers in (the mint, and 100 back from B)")
    check(by_addr[A]["transfers_out"] == "2", "A has 2 transfers out")
    check(by_addr[B]["transfers_in"] == "2", "B has 2 transfers in")
    check(by_addr[B]["transfers_out"] == "2", "B has 2 transfers out")

    check([r["address"] for r in rows] == [A, B, C], "sorted by balance, descending")
    check([r["rank"] for r in rows] == ["1", "2", "3"], "ranks are 1..n in order")
    check(by_addr[A]["balance"] == "700.000000000000000000", "balance string carries full precision")
    check(abs(float(by_addr[A]["share_pct"]) - 70.0) < 1e-9, "share_pct is a percentage")
    check(by_addr[A]["symbol"] == "LOVEWEB" and by_addr[A]["token"] == e.DEFAULT_TOKEN,
          "token address and symbol are stamped on every row")

    section("net-zero and non-ERC-20 rows")

    d = "0x" + "dd" * 20
    logs = [
        transfer(ZERO, A, 10 * 10**18, 100, 0),
        transfer(A, d, 5 * 10**18, 101, 0),
        transfer(d, A, 5 * 10**18, 102, 0),   # d is back to zero
        {"blockNumber": 103, "logIndex": 0,
         "topics": [e.TRANSFER_TOPIC, "0x" + "00" * 32], "data": "0x"},
    ]
    chain = FakeChain(logs, 10 * 10**18, tip=200)
    out2 = tmp / "zero.csv"
    try:
        rc, stdout, stderr = run_main(chain, ["--rpc", "fake", "--out", str(out2),
                                              "--from-block", "1"])
    except (IndexError, KeyError) as exc:
        # If the len(topics) < 3 skip is removed, topics[2] blows up here.
        rc, exc_name = -1, type(exc).__name__
        print(f"  (scan raised {exc_name} on a two-topic log)")
    rows2 = list(csv.DictReader(out2.open(encoding="utf-8"))) if out2.exists() else []
    check(rc == 0, "a two-topic (ERC-721 style) Transfer does not crash the scan")
    check([r["address"] for r in rows2] == [A], f"the net-zero address is not a holder "
                                                f"({[r['address'] for r in rows2]})")
    check(by_addr and rows2[0]["balance_wei"] == str(10 * 10**18), "A keeps the full mint")

    section("burns reduce the sender")

    logs = [
        transfer(ZERO, A, 10 * 10**18, 100, 0),
        transfer(A, ZERO, 4 * 10**18, 101, 0),
    ]
    chain = FakeChain(logs, 6 * 10**18, tip=200)
    out3 = tmp / "burn.csv"
    rc, stdout, stderr = run_main(chain, ["--rpc", "fake", "--out", str(out3),
                                          "--from-block", "1"])
    rows3 = list(csv.DictReader(out3.open(encoding="utf-8")))
    check(rc == 0 and rows3[0]["balance_wei"] == str(6 * 10**18),
          "burn to the zero address subtracts from the holder")
    check(len(rows3) == 1, "the burned balance is not listed as a holder")


def retry_checks() -> None:
    section("rpc(): a flaky endpoint is retried, not fatal")

    import json as _json
    import urllib.error

    calls = {"n": 0}

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def flaky(req, timeout=45):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("connection reset")
        if calls["n"] == 2:
            return Resp(_json.dumps({"jsonrpc": "2.0", "id": 1,
                                     "error": {"message": "rate limited"}}).encode())
        return Resp(_json.dumps({"jsonrpc": "2.0", "id": 1, "result": hex(42)}).encode())

    original_open, original_sleep = e.urllib.request.urlopen, e.time.sleep
    e.urllib.request.urlopen, e.time.sleep = flaky, lambda *_: None
    try:
        result = e.rpc("http://fake", "eth_blockNumber", [])
        check(result == hex(42), "a transport error and an error body are both retried through")
        check(calls["n"] == 3, f"exactly three attempts were made ({calls['n']})")

        def always_fail(req, timeout=45):
            raise urllib.error.URLError("down")

        e.urllib.request.urlopen = always_fail
        try:
            e.rpc("http://fake", "eth_blockNumber", [], retries=2)
            check(False, "a dead endpoint raises SystemExit rather than returning junk")
        except SystemExit:
            check(True, "a dead endpoint raises SystemExit rather than returning junk")
    finally:
        e.urllib.request.urlopen, e.time.sleep = original_open, original_sleep


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="export_holders_test_"))
    log_scan_checks()
    abi_checks()
    guard_checks(tmp)
    csv_checks(tmp)
    retry_checks()

    print()
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for label in FAILED:
            print(f"  - {label}")
        return 1
    print("All export_holders checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
