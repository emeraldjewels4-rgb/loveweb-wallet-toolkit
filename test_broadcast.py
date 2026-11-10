#!/usr/bin/env python3
"""Tests for broadcast.py against a real in-memory chain.

The point of this module is what happens when a send goes wrong, so most
sections replace `w3.eth.send_raw_transaction` with a sender that fails in a
specific way and then delegates to the real one. The chain is still real: every
transaction that is reported as sent is mined and its value actually moves, so
a "success" that did not land would fail these checks.

    python test_broadcast.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import broadcast as b
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

# eth-tester's first funded account.
SOURCE_KEY = "0x" + "00" * 31 + "01"
SOURCE = Account.from_key(SOURCE_KEY).address
# A key nothing else in these tests uses, so balances cannot be confused.
DEST_KEY = "0x" + "00" * 31 + "99"
DEST = Account.from_key(DEST_KEY).address
DEST2 = Account.from_key("0x" + "00" * 31 + "98").address
DEST3 = Account.from_key("0x" + "00" * 31 + "97").address
VALUE = 1  # wei: enough to see the transfer, small enough to be free

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    # Convention here is (condition, label), matching test_distribute.py.
    # Guard both slots: a string condition is always truthy (a check that can
    # never fail) and a boolean label means the arguments are swapped.
    if isinstance(cond, str):
        raise SystemExit(f"check() got a string as its condition: {cond!r}")
    if not isinstance(label, str):
        raise SystemExit(f"check() got a non-string label: {label!r}")
    cond = bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def sign(w3, nonce: int, to: str = DEST, value: int = VALUE):
    tx = {
        "to": to,
        "value": value,
        "gas": 21_000,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
        "gasPrice": w3.eth.gas_price,
        "from": SOURCE,
    }
    return Account.from_key(SOURCE_KEY).sign_transaction(tx)


def next_nonce(w3) -> int:
    return w3.eth.get_transaction_count(SOURCE, block_identifier="pending")


def failing_sender(real, messages: list[str]):
    """Raise each message in turn, then delegate to the real sender."""
    state = {"i": 0}

    def send(raw):
        if state["i"] < len(messages):
            msg = messages[state["i"]]
            state["i"] += 1
            raise ValueError(msg)
        return real(raw)

    return send


def fatal_after(real, ok_sends: int, message: str = "insufficient funds for gas * price + value"):
    """Succeed `ok_sends` times, then fail fatally on every later send."""
    state = {"n": 0}

    def send(raw):
        if state["n"] >= ok_sends:
            raise ValueError(message)
        state["n"] += 1
        return real(raw)

    return send


class FakeClock:
    """Deterministic time, so throttle checks do not depend on the machine."""

    def __init__(self) -> None:
        self.now_value = 1_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now_value += seconds


def main() -> int:
    w3 = Web3(EthereumTesterProvider())
    real_send = w3.eth.send_raw_transaction
    tmp = Path(tempfile.mkdtemp(prefix="broadcast_test_"))
    no_sleep = lambda seconds: None  # noqa: E731 - retries must not really wait

    print("\n=== 1. failures are classified by what they mean, not by type ===")
    check(b.classify(ValueError("HTTP 403 Forbidden")) == "transient",
          "a 403 is transient (a rate-limited endpoint, not a bad request)")
    check(b.classify(ValueError("429 Too Many Requests")) == "transient",
          "a 429 is transient")
    check(b.classify(TimeoutError("read timed out")) == "transient",
          "a timeout is transient")
    check(b.classify(ValueError("already known")) == b.ALREADY_KNOWN,
          "'already known' means the network has it")
    check(b.classify(ValueError("nonce too low")) == b.ALREADY_KNOWN,
          "'nonce too low' means this exact signature landed")
    check(b.classify(ValueError("insufficient funds for gas * price + value")) == "fatal",
          "insufficient funds is fatal")
    check(b.classify(ValueError("execution reverted")) == "fatal",
          "a revert is fatal, not something to resend")

    print("\n=== 2. a clean send is reported sent and actually lands ===")
    stx = sign(w3, next_nonce(w3))
    outcome = b.broadcast(w3, stx, sleep=no_sleep)
    check(outcome.status == b.SENT, "status is sent")
    check(outcome.ok, "outcome.ok is true")
    check(outcome.attempts == 1, "one attempt was needed")
    check(outcome.tx_hash == Web3.to_hex(stx.hash), "the reported hash is the signed hash")
    receipt = w3.eth.wait_for_transaction_receipt(stx.hash)
    check(receipt["status"] == 1, "the transaction is mined successfully")
    check(w3.eth.get_balance(DEST) == VALUE, "the value actually moved")

    print("\n=== 3. the same signed transaction twice is not paid twice ===")
    before = w3.eth.get_balance(DEST2)
    dup = sign(w3, next_nonce(w3), to=DEST2)
    outcomes = b.broadcast_many(w3, [dup.raw_transaction, dup.raw_transaction], sleep=no_sleep)
    check(len(outcomes) == 2, "both entries produce an outcome")
    check(outcomes[0].status == b.SENT, "the first is sent")
    check(outcomes[1].status == b.DUPLICATE, "the second is a duplicate, not a resend")
    check(w3.eth.get_balance(DEST2) - before == VALUE, "the recipient was paid exactly once")

    print("\n=== 4. a transient failure is retried, not fatal ===")
    flaky = sign(w3, next_nonce(w3), to=DEST3)
    w3.eth.send_raw_transaction = failing_sender(real_send, ["HTTP 403 Forbidden",
                                                              "503 Service Unavailable"])
    try:
        outcome = b.broadcast(w3, flaky, attempts=4, base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(outcome.status == b.SENT, "the send eventually succeeds")
    check(outcome.attempts == 3, "it took three attempts (two refusals, then success)")
    check(w3.eth.get_balance(DEST3) == VALUE, "and the transfer really landed")

    print("\n=== 5. 'already known' is success, not a reason to resend ===")
    known = sign(w3, next_nonce(w3), to=DEST3)
    w3.eth.send_raw_transaction = failing_sender(real_send, ["already known"])
    try:
        outcome = b.broadcast(w3, known, attempts=4, base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(outcome.status == b.ALREADY_KNOWN, "status is already_known")
    check(outcome.ok, "already_known counts as ok")
    check(outcome.attempts == 1, "it is not retried")

    low = sign(w3, next_nonce(w3), to=DEST3)
    w3.eth.send_raw_transaction = failing_sender(real_send, ["nonce too low"])
    try:
        outcome = b.broadcast(w3, low, attempts=4, base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(outcome.status == b.ALREADY_KNOWN, "'nonce too low' is already_known too")

    print("\n=== 6. a fatal rejection is reported, never retried ===")
    rejected = sign(w3, next_nonce(w3))
    w3.eth.send_raw_transaction = failing_sender(real_send, ["insufficient funds for gas * price + value"])
    try:
        outcome = b.broadcast(w3, rejected, attempts=4, base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(outcome.status == b.FAILED, "status is failed")
    check(outcome.attempts == 1, "a fatal error is not retried")
    check("insufficient funds" in outcome.error, "the error text is preserved")

    print("\n=== 7. exhausted retries fail honestly instead of raising ===")
    never = sign(w3, next_nonce(w3))
    w3.eth.send_raw_transaction = failing_sender(real_send, ["429 Too Many Requests"] * 10)
    try:
        outcome = b.broadcast(w3, never, attempts=3, base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(outcome.status == b.FAILED, "status is failed after the last attempt")
    check(outcome.attempts == 3, "every configured attempt was used")
    check("429" in outcome.error, "the last error is kept for the report")
    check(not outcome.ok, "and it is not reported as ok")

    print("\n=== 8. the hash is journalled before the send, so a crash leaves evidence ===")
    journal_path = tmp / "journal.jsonl"
    doomed = sign(w3, next_nonce(w3))
    w3.eth.send_raw_transaction = failing_sender(real_send, ["insufficient funds"])
    try:
        b.broadcast(w3, doomed, journal=b.Journal(journal_path), attempts=1,
                    base_delay=0, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    lines = [json.loads(line) for line in journal_path.read_text().splitlines()]
    check(len(lines) == 2, "two rows: the intent and the result")
    check(lines[0]["status"] == "sending", "the first row was written before the send")
    check(lines[0]["tx_hash"] == Web3.to_hex(doomed.hash), "and it names the transaction")
    check(lines[-1]["status"] == "failed", "the last row records the outcome")
    resumed = b.load_journal(journal_path)
    check(resumed[Web3.to_hex(doomed.hash)]["status"] == "failed",
          "load_journal reports the newest status for that hash")

    ok_journal = tmp / "ok.jsonl"
    good = sign(w3, next_nonce(w3), to=DEST3)
    b.broadcast(w3, good, journal=b.Journal(ok_journal), sleep=no_sleep)
    ok_rows = [json.loads(line) for line in ok_journal.read_text().splitlines()]
    check([r["status"] for r in ok_rows] == ["sending", "sent"],
          "a successful send journals sending then sent")

    print("\n=== 9. the throttle paces sends and widens when pushed back ===")
    clock = FakeClock()
    throttle = b.Throttle(min_interval=0.5, max_interval=2.0, factor=2.0, clock=clock.now)
    check(throttle.wait(sleep=clock.sleep) == 0.5, "first wait returns the minimum interval")
    check(clock.slept == [], "the first send does not wait")
    throttle.wait(sleep=clock.sleep)
    check(clock.slept == [0.5], "the second waits the full interval")
    check(throttle.push_back() == 1.0, "a pushback doubles the interval")
    throttle.wait(sleep=clock.sleep)
    check(clock.slept[-1] == 1.0, "the next send waits the wider interval")
    throttle.push_back()
    check(throttle.push_back() == 2.0, "the interval is capped at max_interval")
    check(throttle.relax() == 1.0, "a success narrows it again")
    check(b.Throttle().interval == 0.25, "the default interval is 250ms")

    print("\n=== 10. a fatal failure stops the batch instead of failing every one ===")
    first = sign(w3, next_nonce(w3), to=DEST2)
    second = sign(w3, next_nonce(w3) + 1, to=DEST2)
    third = sign(w3, next_nonce(w3) + 2, to=DEST2)
    before2 = w3.eth.get_balance(DEST2)
    w3.eth.send_raw_transaction = fatal_after(real_send, 1)
    try:
        batch = b.broadcast_many(w3, [first, second, third], attempts=2, base_delay=0,
                                 sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check([o.status for o in batch] == [b.SENT, b.FAILED, b.SKIPPED],
          "statuses are sent, failed, skipped")
    check(batch[2].attempts == 0, "the skipped one was never sent")
    check("not attempted" in batch[2].error, "and it says why it was skipped")
    check(w3.eth.get_balance(DEST2) - before2 == VALUE, "only the first was paid")
    check(b.summarise(batch) == "failed=1  sent=1  skipped=1", "the summary counts each status")

    print("\n=== 11. every accepted input form hashes to the same transaction ===")
    raw = first.raw_transaction
    check(b.as_raw_bytes(first) == raw, "a SignedTransaction is accepted")
    check(b.as_raw_bytes(raw) == raw, "raw bytes are accepted")
    check(b.as_raw_bytes("0x" + raw.hex()) == raw, "a hex string is accepted")
    check(b.tx_hash_of(raw) == Web3.to_hex(first.hash),
          "keccak of the raw bytes is the transaction hash")

    print("\n=== 12. endpoints: your paid key first, the public RPC last, keys redacted ===")
    import rpc_endpoints as ep

    check(ep.redact_url("https://polygon-mainnet.g.alchemy.com/v2/" + "a" * 32)
          == "https://polygon-mainnet.g.alchemy.com/v2/***",
          "an API key in the path is redacted")
    check(ep.redact_url("https://user:secret@rpc.example.com/x") == "https://***@rpc.example.com/x",
          "credentials in the authority are redacted")
    check(ep.redact_url("https://polygon-bor-rpc.publicnode.com")
          == "https://polygon-bor-rpc.publicnode.com",
          "a keyless public URL is left alone")

    env = {"ALCHEMY_API_KEY": "k" * 32,
           "POLYGON_RPC_URLS": "https://own.example/a,https://own.example/b"}
    chosen = ep.endpoints_from_env("polygon", env=env, public_default="https://public.example")
    check([e.tier for e in chosen] == ["explicit", "explicit", "paid", "public"],
          f"explicit, paid, then public (got {[e.tier for e in chosen]})")
    check(chosen[2].safe_url.endswith("/***") and "k" * 32 not in str(chosen[2]),
          "printing a paid endpoint cannot leak its key")
    check(ep.endpoints_from_env("polygon", env={}, public_default="https://p")[0].tier == "public",
          "with no provider keys the public endpoint is the whole pool")
    check(len(ep.endpoints_from_env("amoy",
                                    env={"ETH_RPC_URLS": "https://same.example/,https://same.example"},
                                    public_default="https://same.example")) == 1,
          "one URL given three ways is listed once")

    pool_clock = FakeClock()
    pool = ep.EndpointPool([ep.Endpoint("https://a.example", "paid", "a"),
                            ep.Endpoint("https://b.example", "public", "b")],
                           failures_before_cooldown=2, cooldown_seconds=60,
                           clock=pool_clock.now)
    check([e.label for e in pool.order()] == ["a", "b"], "order starts as configured")
    check(pool.penalise(pool.endpoints[0]) == 0.0, "one failure is not yet a cooldown")
    check(pool.penalise(pool.endpoints[0]) == 60.0, "the second failure starts a 60s cooldown")
    check([e.label for e in pool.order()] == ["b", "a"],
          "a cooling endpoint drops to the back, not out of the pool")
    pool_clock.now_value += 61
    check([e.label for e in pool.order()] == ["a", "b"], "and returns to the front when it expires")
    pool.mark_ok(pool.endpoints[0])
    check(pool.state()[0]["failures"] == 0, "a success clears the failure count")

    print("\n=== 13. rotation resends one signature; a rejection is not shopped around ===")
    check(b.classify(ValueError("replacement transaction underpriced")) == b.UNDERPRICED,
          "an underpriced replacement is its own kind")
    check(b.classify(ValueError("insufficient funds for gas")) == "fatal",
          "and stays distinct from fatal")

    def flaky(message: str):
        class _Fake:
            def __init__(self) -> None:
                self.eth = self

            def send_raw_transaction(self, raw):
                raise ValueError(message)

        return _Fake()

    two = ep.EndpointPool([ep.Endpoint("https://dead.example", "paid", "dead"),
                           ep.Endpoint("https://live.example", "public", "live")],
                          failures_before_cooldown=1, clock=pool_clock.now)
    connected: list[str] = []

    def connect(endpoint):
        connected.append(endpoint.label)
        return flaky("503 Service Unavailable") if endpoint.label == "dead" else w3

    moved_before = w3.eth.get_balance(DEST2)
    rotation = b.broadcast_across(two, sign(w3, next_nonce(w3), to=DEST2), connect=connect,
                                  attempts=2, base_delay=0, sleep=no_sleep)
    check(rotation.status == b.SENT, f"the second endpoint carried it ({rotation.status})")
    check(connected == ["dead", "live"], f"the paid endpoint was tried first ({connected})")
    check(rotation.endpoint == "https://live.example",
          "the outcome names the endpoint that worked")
    check(w3.eth.get_balance(DEST2) - moved_before == VALUE, "and exactly one payment landed")
    check(two.state()[0]["cooling"], "the failing endpoint is cooling down")
    check(not two.state()[1]["cooling"], "the healthy one is not")

    rejected = ep.EndpointPool([ep.Endpoint("https://a.example", "paid", "a"),
                                ep.Endpoint("https://b.example", "public", "b")],
                               clock=pool_clock.now)
    tried: list[str] = []

    def connect_reject(endpoint):
        tried.append(endpoint.label)
        return flaky("insufficient funds for gas * price + value") if endpoint.label == "a" else w3

    fatal = b.broadcast_across(rejected, sign(w3, next_nonce(w3), to=DEST3),
                               connect=connect_reject, attempts=1, sleep=no_sleep)
    check(fatal.status == b.FAILED, "a rejection stays a rejection")
    check(tried == ["a"], f"and it is not re-tried on every endpoint ({tried})")

    print("\n=== 14. a stuck transaction is replaced: same nonce, higher fee only ===")
    legacy = {"nonce": 7, "to": DEST, "value": VALUE, "gas": 21_000,
              "gasPrice": 30_000_000_000, "chainId": 80002}
    bumped = b.fee_bump(legacy)
    check(bumped["gasPrice"] == 33_750_000_000, f"a 12.5% bump (got {bumped['gasPrice']})")
    check(all(bumped[k] == legacy[k] for k in ("nonce", "to", "value", "gas", "chainId")),
          "the bump changes the fee and nothing else")
    check(b.fee_bump(legacy, percent=1)["gasPrice"] == 33_000_000_000,
          "the 10% floor wins over a smaller request")
    check(b.fee_bump({"nonce": 1, "gasPrice": 100}, percent=200)["gasPrice"] == 200,
          "growth stops at max_multiplier")
    check(b.fee_bump({"nonce": 1, "gasPrice": 0})["gasPrice"] == 1,
          "a zero fee still moves by one unit")

    eip = {"nonce": 3, "maxFeePerGas": 30_000_000_000, "maxPriorityFeePerGas": 30_000_000_000}
    raised = b.fee_bump(eip, base_fee_wei=40_000_000_000)
    check(raised["maxFeePerGas"] >= 2 * 40_000_000_000 + raised["maxPriorityFeePerGas"],
          "a replacement always covers twice the current base fee")
    check(raised["maxFeePerGas"] >= raised["maxPriorityFeePerGas"], "tip never exceeds max fee")

    for bad, why in (({"gasPrice": 1}, "no nonce"), ({"nonce": 1}, "no fee field")):
        try:
            b.fee_bump(bad)
        except ValueError:
            check(True, f"fee_bump refuses a transaction with {why}")
        else:
            check(False, f"fee_bump refuses a transaction with {why}")

    signer_calls: list[dict] = []

    def signer(tx_dict):
        signer_calls.append(dict(tx_dict))
        tx = dict(tx_dict)
        tx["from"] = SOURCE
        tx["chainId"] = w3.eth.chain_id
        return Account.from_key(SOURCE_KEY).sign_transaction(tx)

    base = {"to": DEST3, "value": VALUE, "gas": 21_000, "nonce": next_nonce(w3),
            "gasPrice": w3.eth.gas_price, "chainId": w3.eth.chain_id}
    paid_before = w3.eth.get_balance(DEST3)
    w3.eth.send_raw_transaction = failing_sender(
        real_send, ["replacement transaction underpriced"] * 2)
    try:
        replaced, final_tx, history = b.replace_stuck(w3, base, signer=signer, bumps=3,
                                                     attempts=1, sleep=no_sleep)
    finally:
        w3.eth.send_raw_transaction = real_send
    check(replaced.ok, f"the third attempt landed ({replaced.status})")
    check(len(signer_calls) == 3, f"the caller's signer ran once per bump (ran {len(signer_calls)})")
    check(all(call["nonce"] == base["nonce"] for call in signer_calls),
          "every replacement kept the original nonce")
    fees = [row["fee_wei"] for row in history]
    check(fees == sorted(fees) and len(set(fees)) == 3, f"each bump raised the fee ({fees})")
    check(final_tx["nonce"] == base["nonce"] and final_tx["to"] == base["to"],
          "the final transaction still pays the same person")
    check(w3.eth.get_balance(DEST3) - paid_before == VALUE,
          "and only one payment landed, not one per bump")

    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
