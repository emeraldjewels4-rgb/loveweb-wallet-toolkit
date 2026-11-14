#!/usr/bin/env python3
"""Nothing committed may hold a private key, a password or a wallet export.

This toolkit generates wallets, reads exports of real ones, and writes credentials
for a database on a shared host. A key committed once is not recoverable: it is in
the history, in every clone, and in whatever crawls the repository. Nothing looked.

The scan runs over the files git tracks, not the working directory, because the
question is what was published. Each finding is reported as a location and a kind,
never as the value -- a leak must not be echoed into a CI log by the check that
found it.

A scanner that finds nothing proves nothing, so every detector is also run against
a planted sample it must catch, and against a harmless line it must leave alone.
The one allowlisted value is the BIP84 specification vector: it is public, it is
derived from a published mnemonic, and the README quotes it to show the vector
reproduces.

    python test_no_secrets.py
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 2_000_000
FAILED: list[str] = []

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WIF_SHAPE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}"
                       r"(?![1-9A-HJ-NP-Za-km-z])")
EXTENDED_PRIVATE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[xyzuvt]prv[1-9A-HJ-NP-Za-km-z]{100,112}")
PEM_PRIVATE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
SECRET_WORD = re.compile(r"(?i)private|secret|seed|mnemonic|wif|keystore|passphrase|privkey")
BENIGN_WORD = re.compile(r"(?i)sha256|sha-256|digest|keccak|bytecode|checksum|fingerprint|hash")
DSN = re.compile(r"\b(?:mysql|mariadb|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqps?|ftps?|sftp)"
                 r"://[^\s:@/]+:[^\s@/]+@")
PASSWORD_LITERAL = re.compile(
    r"(?i)\b(?:password|passwd|pwd|passphrase)\b\s*[:=]\s*[\"']([^\"']{8,})[\"']")
PLACEHOLDER = re.compile(r"(?i)[$%{<]|your|example|changeme|redacted|placeholder|xxxx|"
                         r"test|dummy|sample|secret-here|\*\*\*")

# Public test vectors, not secrets: derivable by anyone from the published mnemonic,
# and the README quotes this one to show BIP84 reproduces. Exact strings only.
ALLOWED_WIF = {
    # BIP84 spec vector, m/84'/0'/0'/0/0 for "abandon abandon ... about".
    "KyZpNDKnfs94vbrwhJneDi77V6jF64PWPF8x5cdJb8ifgg2DUc9d",
}


def check(cond: bool, label: str) -> None:
    """Record one verdict, refusing anything that could not be a real condition."""
    if not isinstance(cond, bool):
        raise SystemExit("check() condition must be a bool, got %r" % (cond,))
    if not isinstance(label, str):
        raise SystemExit("check() label must be a string, got %r" % (label,))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED.append(label)


def section(title: str) -> None:
    print(f"\n--- {title}")


def b58decode(text: str) -> bytes:
    value = 0
    for char in text:
        value = value * 58 + BASE58.index(char)
    body = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def wif_is_real(candidate: str) -> bool:
    """A WIF is 37 or 38 bytes with a valid double-SHA256 checksum and version 0x80.

    The shape alone matches ordinary base58 words; the checksum is what makes a
    hit worth reporting.
    """
    try:
        raw = b58decode(candidate)
    except ValueError:
        return False
    if len(raw) not in (37, 38) or raw[0] != 0x80:
        return False
    body, checksum = raw[:-4], raw[-4:]
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == checksum


def mask(value: str) -> str:
    """Enough to recognise a hit, not enough to use it."""
    return value[:4] + "…" if len(value) > 4 else "…"


def scan_text(text: str) -> list[tuple[str, int, str, str]]:
    """(kind, line number, masked value, line excerpt) for one file's contents."""
    findings: list[tuple[str, int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if PEM_PRIVATE.search(line):
            findings.append(("PEM private key block", number, "", ""))
        for match in WIF_SHAPE.finditer(line):
            if wif_is_real(match.group(0)) and match.group(0) not in ALLOWED_WIF:
                findings.append(("WIF private key", number, mask(match.group(0)), ""))
        for match in EXTENDED_PRIVATE.finditer(line):
            findings.append(("extended private key", number, mask(match.group(0)), ""))
        for match in HEX64.finditer(line):
            window = line[max(0, match.start() - 70):match.end() + 70]
            if SECRET_WORD.search(window) and not BENIGN_WORD.search(window):
                findings.append(("64-hex value near a key word", number,
                                 mask(match.group(0)), line.strip()[:60]))
        for match in DSN.finditer(line):
            findings.append(("credential-bearing DSN", number, mask(match.group(0)), ""))
        for match in PASSWORD_LITERAL.finditer(line):
            if not PLACEHOLDER.search(match.group(1)):
                findings.append(("password literal", number, mask(match.group(1)), ""))
    return findings


SENSITIVE_NAMES = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$|sample$|template$)[^/]+$"),
    re.compile(r"\.keystore$", re.I),
    re.compile(r"(^|/)keystore[^/]*\.json$", re.I),
    re.compile(r"\.(?:pem|p12|pfx|jks|key)$", re.I),
    re.compile(r"(^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"(^|/)wallets[^/]*\.json$", re.I),
)


def sensitive_name(path: str) -> str:
    for pattern in SENSITIVE_NAMES:
        if pattern.search(path):
            return pattern.pattern
    return ""


def tracked_files() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        return []
    return [name for name in proc.stdout.split("\0") if name]


def read_text(path: Path) -> str:
    """Text of a file, or "" for binary and oversized ones."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if len(raw) > MAX_BYTES or b"\x00" in raw[:4096]:
        return ""
    return raw.decode("utf-8", "replace")


def sample_wif() -> str:
    """A WIF built here from synthetic bytes: valid checksum, no key behind it."""
    body = b"\x80" + bytes(range(1, 33)) + b"\x01"
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    value = int.from_bytes(body + checksum, "big")
    digits = ""
    while value:
        value, remainder = divmod(value, 58)
        digits = BASE58[remainder] + digits
    return "1" * 0 + digits


def main() -> int:
    print("tracked files only: no key, password or export may be committed")

    section("the scan has something to read")
    files = tracked_files()
    check(len(files) > 0, f"git lists tracked files ({len(files)})")
    if not files:
        print("  git ls-files returned nothing; a clean result here would mean nothing")
        return finish()

    section("each detector catches what it is for")
    wif = sample_wif()
    samples = {
        "PEM private key block": "-----BEGIN EC PRIVATE KEY-----\nMFECAQEw\n",
        "WIF private key": f"key: {wif}",
        "extended private key": "xprv" + "A" * 107,
        "64-hex value near a key word": "private_key = " + "ab" * 32,
        "credential-bearing DSN": "mysql://syncuser:hunter2@db.example/x",
        "password literal": 'password = "correct horse battery"',
    }
    for kind, sample in samples.items():
        found = [f[0] for f in scan_text(sample)]
        check(kind in found, f"it reports a planted {kind.lower()}")

    section("and leaves the harmless things alone")
    benign = {
        "a sha256 digest": "bytecode_sha256 = " + "cd" * 32,
        "a column name with a hash": "key_sha256 = row['key_sha256']",
        "a placeholder password": 'password = "changeme"',
        "a DSN without credentials": "mysql://db.example/toolkit",
        "an empty value": "password = None",
    }
    for label, sample in benign.items():
        check(scan_text(sample) == [], f"it ignores {label}")

    section("the tracked tree")
    findings: dict[str, list[str]] = {
        "PEM private key block": [], "WIF private key": [],
        "extended private key": [], "64-hex value near a key word": [],
        "credential-bearing DSN": [], "password literal": [],
    }
    for name in files:
        for kind, number, masked, excerpt in scan_text(read_text(ROOT / name)):
            detail = f"{name}:{number}"
            if masked:
                detail += f" ({masked})"
            if excerpt:
                detail += f"  {excerpt}"
            findings[kind].append(detail)

    for kind, hits in findings.items():
        check(not hits,
              f"no {kind.lower()} in {len(files)} tracked file(s)"
              + (f": {'; '.join(hits[:3])}" if hits else ""))

    section("files that should never be tracked at all")
    named = [name for name in files if sensitive_name(name)]
    check(not named,
          "no .env, keystore, .key, id_* or wallet export is tracked"
          + (f" (found: {', '.join(named[:5])})" if named else ""))

    section("the allowlist is only the published vector")
    # Re-typing the value here would defeat the point; tie it to the documentation
    # that calls it a vector, so an unrelated key cannot be added quietly.
    readme = read_text(ROOT / "README.md")
    check(len(ALLOWED_WIF) == 1 and all(value in readme for value in ALLOWED_WIF),
          f"the {len(ALLOWED_WIF)} allowlisted WIF is the vector the README quotes")

    return finish()


def finish() -> int:
    print("\n" + "=" * 60)
    if FAILED:
        print(f"{len(FAILED)} CHECK(S) FAILED:")
        for name in FAILED:
            print("  -", name)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
