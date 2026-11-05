#!/usr/bin/env python3
"""Choose RPC endpoints: your paid key first, the free public RPC last.

The public endpoints this toolkit defaults to are fine for reading a balance and
hopeless for a few hundred sends: `broadcast.py` exists because a public Polygon
endpoint answered 403 mid-distribution. This module is the other half of that
answer -- instead of retrying one rate-limited URL forever, hold a list, prefer
the endpoint you pay for, and rotate away from one that is failing.

Ordering, highest priority first:

  1. what the caller passed explicitly (`--rpc`, or <NETWORK>_RPC_URLS / ETH_RPC_URLS)
  2. paid providers, from the environment:
        ALCHEMY_API_KEY, INFURA_API_KEY, QUICKNODE_<NETWORK>_URL
  3. the public default for that network

Nothing here reads, needs or logs a private key. The only secret it touches is an
RPC API key, which is exactly why `Endpoint.safe_url` exists: every log line,
error and journal row should use the redacted form. A provider key in a pasted
traceback is a bill somebody else can run up.

    from rpc_endpoints import endpoints_from_env, EndpointPool
    pool = EndpointPool(endpoints_from_env("polygon", public_default=net["default_rpc"]))
    for endpoint in pool.order():
        print(endpoint.tier, endpoint.safe_url)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

# A path segment that looks like a credential: provider keys are long, random
# and alphanumeric. 20 characters is comfortably below any real key length and
# comfortably above a normal path word ("v2", "rpc", "mainnet").
_KEYLIKE = re.compile(r"^[A-Za-z0-9_\-]{20,}$")

ALCHEMY_TEMPLATES = {
    "polygon": "https://polygon-mainnet.g.alchemy.com/v2/{key}",
    "amoy": "https://polygon-amoy.g.alchemy.com/v2/{key}",
}

INFURA_TEMPLATES = {
    "polygon": "https://polygon-mainnet.infura.io/v3/{key}",
    "amoy": "https://polygon-amoy.infura.io/v3/{key}",
}

# QuickNode hands out one full URL per endpoint, so there is nothing to template.
QUICKNODE_ENV = {
    "polygon": "QUICKNODE_POLYGON_URL",
    "amoy": "QUICKNODE_AMOY_URL",
}

EXPLICIT_TIERS = ("explicit", "paid", "public")


def redact_url(url: str) -> str:
    """The same URL with credentials and API keys replaced by ***.

    Used for every human-visible string: log lines, error messages, journal
    rows. A provider key that reaches a log is a key that has leaked.
    """
    if not url:
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = "***@" + authority.rsplit("@", 1)[1]
    if path:
        path = "/".join("***" if _KEYLIKE.match(seg) else seg for seg in path.split("/"))
    return f"{scheme}://{authority}/{path}" if slash else (f"{scheme}://{authority}" if scheme else authority)


@dataclass(frozen=True)
class Endpoint:
    """One RPC URL, with the tier that decided its position in the list."""

    url: str
    tier: str = "public"
    label: str = ""

    def __post_init__(self) -> None:
        if not self.url or not self.url.strip():
            raise ValueError("an Endpoint needs a URL")
        if self.tier not in EXPLICIT_TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; expected one of {', '.join(EXPLICIT_TIERS)}")

    @property
    def safe_url(self) -> str:
        """The URL without its API key. Log this, never `url`."""
        return redact_url(self.url)

    def __str__(self) -> str:  # so a stray print() cannot leak a key
        return f"{self.label or self.tier}: {self.safe_url}"


def endpoints_from_env(
    network: str,
    *,
    env: Mapping[str, str] | None = None,
    public_default: str | None = None,
) -> list[Endpoint]:
    """Build the ordered endpoint list for one network.

    Paid endpoints come before the public one on purpose: the whole point is to
    stop a distribution depending on a free endpoint that rate limits it.
    """
    source = os.environ if env is None else env
    net = (network or "").strip().lower()
    ordered: list[Endpoint] = []
    seen: set[str] = set()

    def add(url: str | None, tier: str, label: str) -> None:
        candidate = (url or "").strip()
        if not candidate:
            return
        key = candidate.rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(Endpoint(url=candidate, tier=tier, label=label))

    # 1. explicit: the caller knows best, and a per-network name beats the generic one
    for name in (f"{net.upper()}_RPC_URLS", "ETH_RPC_URLS", f"{net.upper()}_RPC_URL", "ETH_RPC_URL"):
        for url in (source.get(name) or "").split(","):
            add(url, "explicit", name)

    # 2. paid providers
    alchemy_key = (source.get("ALCHEMY_API_KEY") or "").strip()
    if alchemy_key and net in ALCHEMY_TEMPLATES:
        add(ALCHEMY_TEMPLATES[net].format(key=alchemy_key), "paid", "alchemy")

    infura_key = (source.get("INFURA_API_KEY") or "").strip()
    if infura_key and net in INFURA_TEMPLATES:
        add(INFURA_TEMPLATES[net].format(key=infura_key), "paid", "infura")

    quicknode_var = QUICKNODE_ENV.get(net, "")
    if quicknode_var:
        add(source.get(quicknode_var), "paid", "quicknode")

    # 3. public last
    add(public_default, "public", "default")
    return ordered


@dataclass
class EndpointPool:
    """A list of endpoints that remembers which ones are currently failing.

    One refused send is not evidence of a bad endpoint -- a 429 can be luck. Two
    in a row puts it in cooldown, and cooldown endpoints drop to the back of the
    list rather than being dropped, so a pool of one still works.
    """

    endpoints: list[Endpoint]
    failures_before_cooldown: int = 2
    cooldown_seconds: float = 60.0
    clock: Any = time.monotonic
    _failures: dict = field(default_factory=dict, repr=False)
    _until: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.endpoints:
            raise ValueError("EndpointPool needs at least one endpoint")
        self.endpoints = [ep for ep in self.endpoints if ep.url and ep.url.strip()]
        if not self.endpoints:
            raise ValueError("EndpointPool needs at least one non-empty endpoint")
        if self.failures_before_cooldown < 1:
            raise ValueError("failures_before_cooldown must be at least 1")

    def is_cooling(self, endpoint: Endpoint) -> bool:
        until = self._until.get(endpoint.url)
        return until is not None and self.clock() < until

    def order(self) -> list[Endpoint]:
        """Healthy endpoints first, in configured order, then cooling ones."""
        healthy = [ep for ep in self.endpoints if not self.is_cooling(ep)]
        cooling = [ep for ep in self.endpoints if self.is_cooling(ep)]
        return healthy + cooling

    def penalise(self, endpoint: Endpoint) -> float:
        """Record a failure. Returns the cooldown seconds applied (0 if none)."""
        count = self._failures.get(endpoint.url, 0) + 1
        self._failures[endpoint.url] = count
        if count < self.failures_before_cooldown:
            return 0.0
        self._until[endpoint.url] = self.clock() + self.cooldown_seconds
        return self.cooldown_seconds

    def mark_ok(self, endpoint: Endpoint) -> None:
        """A send succeeded: forget its failures and end any cooldown."""
        self._failures.pop(endpoint.url, None)
        self._until.pop(endpoint.url, None)

    def state(self) -> list[dict]:
        """Per-endpoint health, key-free, for a log line or a test assertion."""
        return [
            {
                "label": ep.label or ep.tier,
                "tier": ep.tier,
                "url": ep.safe_url,
                "failures": self._failures.get(ep.url, 0),
                "cooling": self.is_cooling(ep),
            }
            for ep in self.endpoints
        ]
