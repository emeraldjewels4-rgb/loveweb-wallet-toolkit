#!/usr/bin/env python3
"""Broadcast already-signed transactions, and never pay twice doing it.

Every tool in this repo signs its own transaction and then calls
`send_raw_transaction` once, so each one grew its own retry story: a public RPC
that answers HTTP 403 mid-run ends the run, and the retry that followed it is
what turned a lost receipt into a second payment of real POL.

This module is the shared path. It takes SIGNED transactions only:

    it has no key, and nothing here can sign.

That is the whole safety argument. A retry can only re-send the identical raw
bytes, and two copies of one signature carry the same nonce, so at most one of
them can land. Retrying is therefore safe *here* in a way it is not safe in a
caller that re-signs with a fresh nonce.

What it adds over a bare `send_raw_transaction`:

  * transient failures (HTTP 403/429/5xx, timeouts, dropped connections) are
    retried with exponential backoff instead of ending the run;
  * "already known" / "nonce too low" -- the network already holds this exact
    transaction -- is reported as success, not as an error to re-send;
  * the hash is journalled BEFORE the send, fsynced, so an interrupted run still
    knows which transaction may already have cost gas;
  * a throttle paces sends and slows down when the endpoint pushes back, rather
    than hammering a rate-limited RPC with the rest of a batch;
  * anything else is fatal and stops the batch: a rejected transaction is a
    reason to go and look, not a reason to try again.

    from broadcast import broadcast_many, Journal, Throttle
    outcomes = broadcast_many(
        w3, [s.raw_transaction for s in signed],
        journal=Journal("broadcast-journal.jsonl"), throttle=Throttle(),
    )
    print(summarise(outcomes))

Receipts are deliberately NOT handled here: waiting for a receipt is a read, it
costs nothing to repeat, and the tools already do it (`eth_common.wait_for_receipt`
for the token pipeline). This module is only about the one irreversible step.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Matched against "<ExceptionType>: <message>", lowercased. Substrings, because
# web3 wraps the JSON-RPC error in a ValueError and every client words it
# differently: geth says "already known", Besu "Known transaction", Erigon
# "already known", and several say "nonce too low" for the same situation.
#
# "nonce too low" is safe to read as already-known ONLY because this module
# never re-signs: the transaction whose nonce was consumed is byte-for-byte the
# one being sent. A caller that re-signs would have to treat it as fatal.
ALREADY_KNOWN_MARKERS = (
    "already known",
    "known transaction",
    "already imported",
    "already exists",
    "already in pool",
    "nonce too low",
)

# A 403 belongs here rather than in fatal: the observed cause on a public
# Polygon endpoint was rate limiting, not an invalid request, and the
# transaction had already been accepted.
TRANSIENT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "403",
    "forbidden",
    "502",
    "503",
    "504",
    "server error",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "temporarily",
    "timed out",
    "timeout",
    "connection",
    "econnreset",
    "broken pipe",
    "remote end closed",
    "remotedisconnected",
)

SENT, ALREADY_KNOWN, FAILED, SKIPPED, DUPLICATE, UNDERPRICED = (
    "sent",
    "already_known",
    "failed",
    "skipped",
    "duplicate",
    "underpriced",
)

# A replacement with the same nonce was refused because its fee did not clear the
# bar. Distinct from "fatal" on purpose: the fix is a higher fee, not a look at
# the logs, and it is the one failure worth re-signing for.
REPLACEMENT_MARKERS = (
    "replacement transaction underpriced",
    "replacement underpriced",
    "replacement fee too low",
    "transaction underpriced",
    "fee too low",
    "max fee per gas less than block base fee",
    "underpriced",
)


def classify_text(text: str) -> str:
    """'already_known', 'underpriced', 'transient' or 'fatal' for one failure."""
    lowered = (text or "").lower()
    for marker in ALREADY_KNOWN_MARKERS:
        if marker in lowered:
            return ALREADY_KNOWN
    for marker in REPLACEMENT_MARKERS:
        if marker in lowered:
            return UNDERPRICED
    for marker in TRANSIENT_MARKERS:
        if marker in lowered:
            return "transient"
    return "fatal"


def classify(exc: BaseException) -> str:
    """'already_known', 'underpriced', 'transient' or 'fatal' for one send failure."""
    return classify_text(f"{type(exc).__name__}: {exc}")


@dataclass
class Outcome:
    """What happened to one signed transaction."""

    tx_hash: str
    status: str
    attempts: int = 0
    error: str = ""
    # Which endpoint answered, redacted. Empty when a caller passed a bare Web3.
    endpoint: str = ""

    @property
    def ok(self) -> bool:
        """True when the network has the transaction, however it got there."""
        return self.status in (SENT, ALREADY_KNOWN)


class Journal:
    """Append-only JSONL record of every broadcast attempt.

    Written and fsynced BEFORE the send, so a crash, a Ctrl-C or a killed shell
    still leaves the hash of anything that might have reached the network. That
    is the file a human reads to answer "did it go?" instead of re-running.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, tx_hash: str, status: str, attempts: int = 0,
               error: str = "", **extra: Any) -> dict:
        row = {
            "tx_hash": tx_hash,
            "status": status,
            "attempts": attempts,
            "error": error,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row


def load_journal(path: str | Path) -> dict[str, dict]:
    """{tx_hash: newest record}. Later rows win, so a status only moves forward."""
    out: dict[str, dict] = {}
    file = Path(path)
    if not file.is_file():
        return out
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written trailing line from a kill is not a record
        if isinstance(row, dict) and row.get("tx_hash"):
            out[row["tx_hash"]] = row
    return out


@dataclass
class Throttle:
    """Space out sends, and widen the gap when the endpoint pushes back.

    A distribution of a few hundred transactions through a keyless public RPC is
    exactly the traffic that gets rate limited. Pacing costs seconds; getting
    banned mid-batch costs a partial run and a manual resume.
    """

    min_interval: float = 0.25
    max_interval: float = 8.0
    factor: float = 2.0
    interval: float | None = None
    clock: Any = time.monotonic
    _last: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.interval is None:
            self.interval = self.min_interval

    def wait(self, sleep: Any = time.sleep) -> float:
        """Sleep as long as needed to honour the current interval. Returns it."""
        if self._last is not None:
            delay = self._last + self.interval - self.clock()
            if delay > 0:
                sleep(delay)
        self._last = self.clock()
        return self.interval

    def push_back(self) -> float:
        """The endpoint objected: send the next one more slowly."""
        self.interval = min(self.max_interval, self.interval * self.factor)
        return self.interval

    def relax(self) -> float:
        """A send succeeded: creep back towards the configured minimum."""
        self.interval = max(self.min_interval, self.interval / self.factor)
        return self.interval


def as_raw_bytes(signed: Any) -> bytes:
    """Accept a SignedTransaction, a hex string, or raw bytes."""
    if hasattr(signed, "raw_transaction"):
        return bytes(signed.raw_transaction)
    if hasattr(signed, "rawTransaction"):  # web3 6 and older
        return bytes(signed.rawTransaction)
    if isinstance(signed, str):
        return bytes.fromhex(signed[2:] if signed.startswith("0x") else signed)
    return bytes(signed)


def tx_hash_of(raw: bytes) -> str:
    """The hash of a signed transaction is keccak of its raw bytes."""
    from web3 import Web3

    return Web3.to_hex(Web3.keccak(raw))


def broadcast(
    w3,
    signed: Any,
    *,
    tx_hash: str | None = None,
    journal: Journal | None = None,
    throttle: Throttle | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Any = time.sleep,
) -> Outcome:
    """Send one signed transaction, retrying only what is worth retrying.

    Never raises on a send failure: the caller gets an Outcome that says what
    happened, because "the endpoint rate limited us" must not surface as a bare
    traceback that looks like nothing was sent.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    raw = as_raw_bytes(signed)
    tx_hash = tx_hash or tx_hash_of(raw)
    if journal:
        # Before the network call: from here on the hash may have reached a node.
        journal.record(tx_hash, "sending")
    if throttle:
        throttle.wait(sleep=sleep)

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            w3.eth.send_raw_transaction(raw)
        except Exception as exc:  # noqa: BLE001 - classification decides, not type
            kind = classify(exc)
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            if kind == ALREADY_KNOWN:
                if journal:
                    journal.record(tx_hash, ALREADY_KNOWN, attempt, last_error)
                return Outcome(tx_hash, ALREADY_KNOWN, attempt, last_error)
            if kind == UNDERPRICED:
                # Re-sending these exact bytes cannot help: the fee is what was
                # refused. Stopping here lets the caller bump and re-sign.
                if journal:
                    journal.record(tx_hash, UNDERPRICED, attempt, last_error)
                return Outcome(tx_hash, UNDERPRICED, attempt, last_error)
            if kind == "fatal":
                if journal:
                    journal.record(tx_hash, FAILED, attempt, last_error)
                return Outcome(tx_hash, FAILED, attempt, last_error)
            if throttle:
                throttle.push_back()
            if attempt < attempts:
                sleep(min(max_delay, base_delay * (2 ** (attempt - 1))))
                continue
        else:
            if throttle:
                throttle.relax()
            if journal:
                journal.record(tx_hash, SENT, attempt)
            return Outcome(tx_hash, SENT, attempt)

    if journal:
        journal.record(tx_hash, FAILED, attempts, last_error)
    return Outcome(tx_hash, FAILED, attempts, last_error)


def broadcast_many(
    w3,
    signed_list: Iterable[Any],
    *,
    journal: Journal | None = None,
    throttle: Throttle | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Any = time.sleep,
) -> list[Outcome]:
    """Send a batch in order, stopping the batch at the first real failure.

    A transient error is retried; a fatal one (rejected, insufficient funds,
    wrong chain) means the next transaction will fail for the same reason, so the
    rest are reported as `skipped` rather than each paying for its own rejection.
    An identical raw transaction repeated in the batch is `duplicate` and is not
    sent again.
    """
    outcomes: list[Outcome] = []
    seen: set[str] = set()
    stopped: str | None = None

    for index, signed in enumerate(signed_list):
        raw = as_raw_bytes(signed)
        digest = tx_hash_of(raw)
        if stopped is not None:
            outcomes.append(Outcome(digest, SKIPPED, 0, f"not attempted: {stopped}"))
            continue
        if digest in seen:
            outcomes.append(Outcome(digest, DUPLICATE, 0,
                                    "identical signed transaction already in this batch"))
            continue
        seen.add(digest)
        outcome = broadcast(w3, raw, tx_hash=digest, journal=journal,
                            throttle=throttle, attempts=attempts,
                            base_delay=base_delay, max_delay=max_delay, sleep=sleep)
        outcomes.append(outcome)
        if outcome.status in (FAILED, UNDERPRICED):
            stopped = f"index {index} {outcome.status}: {outcome.error[:100]}"
    return outcomes


def summarise(outcomes: Iterable[Outcome]) -> str:
    """'sent=100  failed=1' -- the counts, for a log line."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return "  ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def broadcast_across(
    pool,
    signed: Any,
    *,
    connect: Any,
    tx_hash: str | None = None,
    journal: Journal | None = None,
    throttle: Throttle | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Any = time.sleep,
) -> Outcome:
    """Send through an endpoint pool, rotating away from a failing endpoint.

    `connect(endpoint)` builds a Web3 for that endpoint, so this module still
    constructs nothing and still holds no key.

    A transient refusal demotes the endpoint and the next one is tried with the
    same signed bytes -- which is the whole safety argument: rotation resends one
    signature, never a re-signed transaction with a fresh nonce. A rejection, an
    "already known" or an underpriced reply ends the loop immediately, because no
    other endpoint would answer differently.
    """
    raw = as_raw_bytes(signed)
    tx_hash = tx_hash or tx_hash_of(raw)
    last: Outcome | None = None

    for endpoint in pool.order():
        outcome = broadcast(
            connect(endpoint), raw,
            tx_hash=tx_hash, journal=journal, throttle=throttle,
            attempts=attempts, base_delay=base_delay, max_delay=max_delay, sleep=sleep,
        )
        outcome.endpoint = getattr(endpoint, "safe_url", "")
        if outcome.ok:
            pool.mark_ok(endpoint)
            return outcome
        last = outcome
        if classify_text(outcome.error) != "transient":
            return outcome
        pool.penalise(endpoint)

    return last if last is not None else Outcome(tx_hash, FAILED, 0, "no endpoints configured")


def _grow(value: int, percent: float, *, cap: int) -> int:
    """Raise an integer fee by at least one unit, never past `cap`."""
    grown = int(value * (1 + percent / 100.0))
    if grown <= value:
        grown = value + 1
    return min(grown, cap) if cap > value else grown


def fee_bump(
    tx: Mapping[str, Any],
    *,
    percent: float = 12.5,
    floor_percent: float = 10.0,
    max_multiplier: float = 2.0,
    base_fee_wei: int | None = None,
) -> dict:
    """A copy of `tx` with its fee raised, so a node will accept it as a replacement.

    Pure arithmetic on a transaction dict: no key, no network. Replacement rules
    differ between nodes, so this aims at the strictest common denominator -- a
    visible percentage bump, plus enough max fee to clear the current base fee.

    Everything that decides *what* the transaction does is copied unchanged:
    nonce, to, value, data, gas, chainId. A bump is only ever a fee change; a
    replacement that also moved value would be a different payment.
    """
    if "nonce" not in tx:
        raise ValueError("fee_bump needs a transaction with a nonce")
    percent = max(percent, floor_percent)
    bumped: dict = dict(tx)

    if tx.get("maxFeePerGas") is not None and tx.get("maxPriorityFeePerGas") is not None:
        tip0 = int(tx["maxPriorityFeePerGas"])
        fee0 = int(tx["maxFeePerGas"])
        tip = _grow(tip0, percent, cap=int(tip0 * max_multiplier))
        fee = _grow(fee0, percent, cap=int(fee0 * max_multiplier))
        if base_fee_wei is not None:
            required = 2 * int(base_fee_wei) + tip
            if fee < required:
                fee = required
        bumped["maxPriorityFeePerGas"] = tip
        bumped["maxFeePerGas"] = max(fee, tip)
        return bumped

    if tx.get("gasPrice") is not None:
        price0 = int(tx["gasPrice"])
        price = _grow(price0, percent, cap=int(price0 * max_multiplier))
        if base_fee_wei is not None:
            price = max(price, 2 * int(base_fee_wei))
        bumped["gasPrice"] = price
        return bumped

    raise ValueError("transaction has no maxFeePerGas or gasPrice to raise")


def replace_stuck(
    w3,
    tx: Mapping[str, Any],
    *,
    signer: Any,
    pool=None,
    connect: Any = None,
    signed_hash: str | None = None,
    base_fee_wei: int | None = None,
    bumps: int = 2,
    percent: float = 12.5,
    journal: Journal | None = None,
    throttle: Throttle | None = None,
    attempts: int = 3,
    sleep: Any = time.sleep,
) -> tuple[Outcome, dict, list[dict]]:
    """Push a stuck transaction through by re-signing it, same nonce, higher fee.

    `signer(tx_dict)` is supplied by the CALLER and is the only thing that can
    produce a signature. This module still cannot sign, so the re-signed bytes
    differ from the original in exactly one respect: the fee the caller chose.

    Returns (outcome, final_tx, history). Every attempt keeps the nonce, to,
    value and data of the original, so at most one of them can ever land.
    """
    history: list[dict] = []
    current = dict(tx)

    if signed_hash:
        try:
            receipt = w3.eth.get_transaction_receipt(signed_hash)
        except Exception:  # noqa: BLE001 - a missing receipt is the normal path
            receipt = None
        if receipt is not None:
            return (Outcome(signed_hash, ALREADY_KNOWN, 0, "already mined"), current, history)

    outcome = Outcome("", FAILED, 0, "no bump attempted")
    for bump in range(1, max(1, bumps) + 1):
        current = fee_bump(current, percent=percent, base_fee_wei=base_fee_wei)
        signed = signer(current)
        raw = as_raw_bytes(signed)
        digest = tx_hash_of(raw)
        if pool is not None:
            if connect is None:
                raise ValueError("replace_stuck needs connect= when given a pool")
            outcome = broadcast_across(pool, raw, connect=connect, tx_hash=digest,
                                       journal=journal, throttle=throttle,
                                       attempts=attempts, sleep=sleep)
        else:
            outcome = broadcast(w3, raw, tx_hash=digest, journal=journal,
                                throttle=throttle, attempts=attempts, sleep=sleep)
        history.append({
            "bump": bump,
            "tx_hash": digest,
            "nonce": current.get("nonce"),
            "fee_wei": int(current.get("maxFeePerGas", current.get("gasPrice", 0))),
            "status": outcome.status,
        })
        if outcome.ok:
            return outcome, current, history
        if outcome.status != UNDERPRICED:
            return outcome, current, history

    return outcome, current, history


if __name__ == "__main__":
    # Importable helper, not a CLI: it has no key and no plan of its own. The
    # tools (distribute_eth.py, distribute_token.py, ...) are the entry points.
    raise SystemExit(
        "broadcast.py is a library used by the distribution tools; it takes "
        "already-signed transactions and is not run directly."
    )
