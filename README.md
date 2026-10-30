# gen_wallets

[![tests](https://github.com/emeraldjewels4-rgb/loveweb-wallet-toolkit/actions/workflows/tests.yml/badge.svg)](https://github.com/emeraldjewels4-rgb/loveweb-wallet-toolkit/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776ab)

Generates **new** EVM (MetaMask-compatible) and Bitcoin wallets and exports them as JSON.

It creates fresh keypairs. It does **not** read, extract or "download" keys from
MetaMask or anywhere else — there is no supported way to do that, and any tool
claiming to is malware.

## Install

```bash
py -3 -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt -c constraints.txt   # -c = the exact versions CI tests
```

On Windows, create the venv at a **short path** (e.g. `C:\wv`) if the install
fails with `[Errno 2] No such file or directory`. `bip-utils` pulls in a deep
dependency tree, and with Long Path support disabled this project's own path is
already long enough to blow past `MAX_PATH`. A shallow venv sidesteps it
without touching the registry.

## Generate 100 wallets

```bash
# 100 EVM accounts from one mnemonic — the MetaMask-native way
python gen_wallets.py --count 100 --chains evm --out wallets.json

# EVM + Bitcoin, both derived from the same seed
python gen_wallets.py --count 100 --chains evm,btc --out wallets.json

# 100 totally independent wallets, each with its own mnemonic
python gen_wallets.py --count 100 --mode random --out wallets.json

# password-encrypted output
python gen_wallets.py --count 100 --encrypt --out wallets.json
```

`--mode hd` (default) derives every wallet from a single mnemonic at
`m/44'/60'/0'/0/{index}` for EVM and `m/84'/0'/0'/0/{index}` for Bitcoin.
That means **one** thing to back up, and it is exactly the path MetaMask uses,
so the wallets are recoverable by restoring the phrase.

`--mode random` gives each wallet its own mnemonic. Better isolation, but 100
separate backups.

[test_gen_wallets.py](test_gen_wallets.py) (**65 checks**) pins this down against
the published BIP39/BIP44/BIP84 vectors for the standard phrase
`abandon abandon ... about`: EVM index 0 must be `0x9858EfFD…EcaEda94`, index 1
`0x6Fac4D18…4Ab9C0`, and BTC index 0 `bc1qcr8te4…z306fyu`. Those are values from
the standards, not captured from this code — a shifted path or a changed library
default would still reproduce itself, but it would not reproduce MetaMask.

## Check the output is actually correct

Every run re-derives each address from its private key and aborts if anything
disagrees — it will not silently write a broken file. You can re-check an
existing export at any time:

```bash
python gen_wallets.py --verify-file wallets.json
```

## Importing into MetaMask

**EVM, HD mode (recommended).** Restore the single mnemonic from `wallets.json`
(MetaMask → *Import an existing wallet* → *Secret Recovery Phrase*). MetaMask
shows **Account 1** = index 0. To reach the rest, click *Add account* — each
click derives the next index, `m/44'/60'/0'/0/1`, `/2`, `/3`…

Two gotchas worth knowing before you start:

- MetaMask only auto-adds accounts that hold a non-zero Ethereum Mainnet ETH
  balance. Your 100 fresh wallets are empty, so restoring the phrase will show
  **only Account 1** and you'll click *Add account* 99 times.
- Private-key import is strictly **one key at a time**. There is no bulk import
  for a list of raw EVM private keys, so for a large set the mnemonic route is
  far less clicking.

**Bitcoin.** MetaMask supports Bitcoin via Native SegWit (`bc1q…`), but it
**cannot import a raw Bitcoin private key or WIF at all** — the only documented
route is the Secret Recovery Phrase, same as above. The extra `wif` field in the
JSON is therefore useless for MetaMask but is what Electrum, Sparrow and most
other Bitcoin wallets want, so it's included.

**The gap limit.** A restored wallet scans only ~20 consecutive *unused*
addresses and then stops. Fresh wallets have no history, so a default restore of
a 900-address mnemonic shows roughly the first 20 and hides the rest — including
any you have funded. Nothing is lost (the addresses are still derived from the
seed; the gap limit is a discovery setting, not cryptography), but you must
raise it to see them all:

- **Electrum** — default 20; console: `wallet.change_gap_limit(900)`
- **Sparrow** — default 20 (40 for postmix); *Settings → Advanced*, then restart

The watch-only export prints the same command pre-filled with the right count
to raise `wallet.change_gap_limit` to.

Sources: [BIP44](https://github.com/bitcoin/bips/blob/master/bip-0044.mediawiki),
[Electrum FAQ](https://electrum.readthedocs.io/en/latest/faq.html),
[Sparrow FAQ](https://sparrowwallet.com/docs/faq.html).

## Watch-only (public) wallets from the mnemonic

In HD mode the mnemonic can also produce a **watch-only** wallet: a key that
builds every address and reads balances and history, but **cannot sign or
spend**. That is what "public wallet" means here — the private keys never leave
your machine.

```bash
python watch_only.py --wallets wallets-btc-900.json --out watch-only-900.txt
```

The exported key is the **account** extended public key at `m/84'/0'/0'`,
written as a **zpub** (SLIP-132, the BIP84 form). Paste it into the watch-only
route of any wallet — Sparrow (*Airgapped / Watch-only*), Electrum (*Use a
master key*), or BlueWallet (*Import wallet*) — and it shows the addresses and
refuses to sign. Pass `--password-file` if the input export is encrypted.

Two properties matter:

- **It only goes one way.** The zpub derives children; children never give back
  the zpub. So you cannot rebuild this from the addresses, or from the public
  keys inside them — only from the seed phrase. And a *child* public key is not
  usable this way: only the account level is.
- **It is private, even though it holds no funds.** Anyone with the zpub can
  enumerate every address in the account, see every balance and trace your
  history. It cannot steal, but it can surveil. `watch-only*.txt` is in
  `.gitignore` for that reason; share it only with a wallet you control.

The export re-derives all 900 addresses **from the zpub alone** and compares
them to the file, failing loudly on any mismatch, so a key that doesn't
reproduce your addresses is caught before you use it.

[test_watch_only.py](test_watch_only.py) (**41 checks**) proves that comparison
is between two genuinely different derivations — seed → address via `gen_wallets`
and zpub → address via `watch_only` — at several indices, with index 0 pinned to
the published BIP84 address `bc1qcr8te4…z306fyu`. It also covers the check value
being 16 hex characters and explicitly *not* an 8-character BIP32 fingerprint,
the gap-limit figure matching the real address count, and `--limit` actually
limiting.

Remember the **gap limit** above applies to the watch-only wallet too — it needs
the same raised limit to show all 900.

## The 4,000-wallet Bitcoin set (four files × 1,000)

`wallets-btc-1000-a.json` … `-d.json` hold **4,000 distinct Bitcoin wallets**,
1,000 per file. Every entry carries everything needed to spend from that one
address — no cross-file bookkeeping:

| File | Mode | Where its phrase lives |
|---|---|---|
| `wallets-btc-1000-a.json` | HD | one `mnemonic` at document level; entry `i` is `m/84'/0'/0'/0/i` |
| `wallets-btc-1000-b.json` | HD | same |
| `wallets-btc-1000-c.json` | random | **each entry has its own `mnemonic`**, at index 0 |
| `wallets-btc-1000-d.json` | random | same |

```bash
python gen_wallets.py --count 1000 --chains btc --mode random \
    --out wallets-btc-1000-c.json
```

Why the two shapes exist: HD mode is one backup for a thousand addresses, which
is what you want in a single wallet app, but restoring it means raising the gap
limit. Random mode costs one phrase per address — 2,000 phrases in the two
files — and in exchange each line stands alone, so a wallet can be handed out or
restored one at a time without exposing the others. Nothing about the
cryptography differs: both are BIP84 native SegWit (`bc1q…`).

Verified on the finished set: 4,000 addresses, **4,000 unique** (no wallet
repeated across or within files), and for every entry the stored `wif` and the
stored `private_key` both hash to exactly the stored `address`. Independent
re-derivation from each phrase reproduces the same addresses
(`--verify-file`), so a corrupted line would have shown up as a mismatch.

Two funding facts, because they decide whether 4,000 is a real number:

- **Native SegWit inputs are cheap, but not free.** Sending to all 4,000 costs
  one output each; spending them back costs a fee per UTXO. At a modest
  5 sat/vB a 1-input spend is roughly 4,000 × 68 vB × 5 sat ≈ 0.0136 BTC just to
  consolidate the set, before anything you actually sent.
- **A funded address should not be left empty afterwards.** Sweeping dust later
  can cost more than it moves, which is why the interesting number is the
  *minimum viable balance per address*, not the count of addresses.

Everything in all four files is **empty**: generation is free and offline, and
no coins have been sent to any of these addresses. Checked against the chain
rather than assumed — a sample of 12 addresses spread across the four files
(index 0, 499 and 999 of each) reports 0 sat funded and 0 transactions, and
`check_funding.py --wallets …` will sweep the full 4,000 when you want that.

## Getting the private keys from the phrase

In HD mode you don't need to "recover" anything — every private key is already
in the export. In `wallets-btc-900.json` each wallet carries:

| Field | Format | Use it for |
|---|---|---|
| `private_key` | 64 hex chars, **no** `0x` | scripts, EVM-style tooling |
| `wif` | 52 chars, starts `K` or `L` | importing into Electrum, Sparrow, etc. |
| `path` | `m/84'/0'/0'/0/i` | knowing where the key came from |

Bitcoin wallets almost always want the **WIF**, not the bare hex — a WIF encodes
the network and the compressed-pubkey flag, so it's the form that imports
cleanly without prompting for extra details. Confirmed by round-trip: all 900
WIFs in `wallets-btc-900.json` and all 100 in `wallets.json` decode back to
exactly the address stored in the same entry.

To re-derive from the phrase instead of reading it off disk — a different
phrase, or a different index range:

```bash
python gen_wallets.py --mnemonic-file "C:\Users\you\keys\phrase.txt" \
    --chains btc --count 900 --out recovered.json
```

`--mnemonic-file` reads the phrase from a file; `--mnemonic` takes it as a
command-line argument and should be avoided, because argv is written to shell
history and is visible in the process list to anything running on the machine.
The phrase is the single most valuable secret here, so it never belongs in a
command line, a chat, or a repository (`mnemonic.txt`, `phrase.txt` and
`*.seed` are in `.gitignore`).

Three things determine which key you get, and all three must match:

- **the index** — the same phrase at index 0 and index 1 gives different keys
- **the path** — BIP84 (`m/84'/0'/0'`) for these native segwit addresses;
  a BIP44 or BIP49 path gives different keys for the same phrase
- **the BIP39 passphrase** — if one was used, the same phrase *without* it
  derives a completely different, and useless-to-you, set of keys

**A phrase is strictly more powerful than any private key.** One leaked key
endangers one address; a leaked phrase regenerates all 900 — including indices
you have not generated yet. And it is one-way in the other direction: a leaked
*address* or the account `zpub` (see above) can never be turned back into a key.

## Checking which addresses have been funded

[check_funding.py](check_funding.py) reports which Bitcoin addresses in your
exports have ever received funds, so you can tell fresh wallets from ones in use:

```bash
python check_funding.py \
    --wallets wallets.json wallets-btc-900.json wallets-btc-1000-a.json wallets-btc-1000-b.json \
    --results _scan_results.json
```

**"In use" means `total_received > 0`, not a non-zero balance.** An address can be
paid and later swept, leaving a balance of 0 while still having a full history.
A balance-based check silently reports such an address as never used, so the
report separates three states: never funded, funded then swept, and currently
holding. The controls in every run include a *funded-then-swept* address
(BIP173's own test vector, 212 transactions and a zero final balance) precisely
so that a regression to a balance-based test fails loudly.

Batching matters for cost and speed: the API accepts many addresses per request,
so 3000 addresses is 61 requests rather than 3000. A provider with only a
single-address route (Blockstream Esplora publishes ~700 requests/hour per IP
unauthenticated) would need hours. Note Blockchair's batch endpoint answers HTTP
430 for keyless use, so it needs an API key.

Two safeguards are built in:

- **Three controls per run** — a known-funded address, a funded-then-swept one,
  and a known-fresh one. Detection must be right in both directions or the run
  exits 1 and says the numbers are untrustworthy.
- **Cross-verification** — anything reported funded is re-checked against a
  second, independent provider before being listed, so one provider's error
  can't produce a false positive.

Both safeguards are covered by [test_check_funding.py](test_check_funding.py)
(**46 checks**, both providers faked, no network). Switching the criterion to
`final_balance`, ignoring a wrong control, or trusting the first provider alone
each fails a named check — so the three guards cannot be removed without the
suite saying so.

**Privacy: this is not a neutral read.** Asking a public API about your own
addresses tells that operator the addresses are related and links your entire
set together, and batching means many addresses arrive per request from your IP.
There is no way to check an address against the public chain without revealing
which address you care about; to avoid it, run a pruning Bitcoin node locally and
query that instead. `_scan*.json` is in `.gitignore` because the results file is
an index of everything you own.

## Asking a payer to fund the set (payment request sheet)

[payment_request.py](payment_request.py) turns a wallet export into a request a
payer can act on: a CSV of addresses and amounts, plus a cover note with the
totals and the fee arithmetic.

```bash
# the command that produced the sheet in this repo
python payment_request.py \
    --wallets wallets-btc-1000-a.json wallets-btc-1000-b.json \
              wallets-btc-1000-c.json wallets-btc-1000-d.json \
    --amount 1000 --memo "Wallet set funding request" --out payment-request
```

```
payment-request.csv   n, batch, address, amount_btc, amount_sat, bip21
payment-request.md    totals, batch plan, fees, checklist
```

`--total` splits an amount evenly across every address, spreading the odd
satoshis over the first rows so the sheet sums to the requested figure exactly —
a request that does not add up is worse than no request. `--amount` is the other
mode: a fixed figure per address. The delivered sheet asks for
**4,000,000.00000000 BTC across 4,000 addresses, 1,000.00000000 BTC each**; for
a different ask, re-run with `--total 40` or `--amount 0.0001`.

**An ask above the money supply is refused, and a vast one is labelled.** Only
21,000,000 BTC will ever exist, so `check_supply` exits 1 before writing anything
if the sheet totals more than that — such a request cannot be paid by anyone.
Between 1% of supply and the cap, the cover note carries a *Reality check* block
stating the share of all bitcoin being asked for and that every address starts
empty. The delivered 4,000,000 BTC sheet is 19.05% of supply and says so on its
face; it is a statement of what is wanted, not funds that exist.

**There is no "maximum" to fill to.** An address's balance is whatever is sent;
only the payer's funds and the transaction limits below are real ceilings, so the
per-address figure is a choice rather than something computed. The cover note
says so in the sheet itself and prints the next few multiples (10x, 100x) with
their per-address equivalents, plus the exact command that regenerates it.

**The batch split is not cosmetic.** A standard transaction may weigh at most
400,000 weight units — 100,000 vbytes — and a native SegWit output is 31 vbytes,
so one relayable transaction carries roughly **3,225 outputs at most**. Four
thousand outputs in one transaction is a non-standard transaction most nodes
will not relay, and a payer who tries it gets a rejection they cannot diagnose.
So the sheet is batched (2,000 by default, `--batch-size` to change, refused
above the limit with the reason) and the cover note gives per-batch subtotals.

Three refusals built in, each because it would produce a sheet that cannot
deliver what it says:

- **Dust.** Any row below 546 sat is refused rather than emitted. The real
  native-SegWit threshold is lower (~294 sat); 546 sat is the conservative limit
  that is safe for every script type. An output below it can cost more to spend
  than it holds.
- **Duplicates.** The same address twice in one request is refused by name and
  source file instead of silently deduped — paying an address twice is never
  intentional.
- **Address shape.** Only mainnet `bc1q…` addresses pass, with the checksum
  verified by decoding. A testnet `tb1…` address in a mainnet request would send
  coins into a chain the payer never looked at.

Nothing here reads a private key: the sheet is public addresses and amounts, and
the payer verifies a payment by address and amount, nothing else. It still puts
your whole set in one file, so `payment-request*.csv` and `payment-request*.md`
are in `.gitignore` — hand it to the payer, don't publish it.

Checks: `python test_payment_request.py`. The end-to-end case builds the real
4,000-row sheet, then re-reads it and confirms the rows sum to exactly the
requested total, every address came from the exports, the batches partition the
set, and each `bitcoin:` URI points at its own row.

## Distributing ETH to the generated wallets

[distribute_eth.py](distribute_eth.py) sends native ETH from one wallet you own
to every address in `wallets.json` — gas money for the fresh wallets.

```bash
# always start here: shows the plan and cost, sends nothing
python distribute_eth.py --wallets wallets.json --rpc <YOUR_RPC_URL> --amount 0.002

# then actually send
python distribute_eth.py --wallets wallets.json --rpc <YOUR_RPC_URL> --amount 0.002 --execute
```

You supply the JSON-RPC endpoint (Alchemy, Infura, QuickNode, or a public one —
any provider works, none is bundled). The source private key comes from the
`SOURCE_PRIVATE_KEY` environment variable, `--private-key-file`, or a hidden
prompt. It is deliberately **not** a command-line flag: argv shows up in process
lists and gets saved to shell history.

Note this only moves native ETH that the source key already holds. Native ETH
has no approval mechanism, so no spender or operator can move it — which is why
"fill these wallets from a spender" only ever meant an ERC-20 approval, and is
not what this does.

Guardrails, all exercised by the test suite:

| Guard | Behaviour |
|---|---|
| Dry run by default | Without `--execute` it signs everything and broadcasts nothing |
| Typed confirmation | You must type `SEND`, unless you pass `--yes` |
| Balance pre-check | Aborts before sending if the source cannot cover value plus fees |
| Gas estimate | Each recipient's gas is measured; a recipient that cannot be priced is reported, never sent |
| `--max-total` | Hard cap on the total ETH sent |
| Resume | With a `--results` file, already-funded wallets are skipped |
| Audit trail | Every tx hash, nonce, block and gas figure is written to JSON |
| Retry safety | A retried transfer reuses its original nonce, so a lost receipt cannot pay twice |

The source address is skipped if it appears among the recipients, and duplicates
are collapsed. Fees use EIP-1559 where the chain supports it (`2x base + tip`)
and fall back to legacy `gasPrice` otherwise.

### Gas is measured per recipient, not assumed

A value transfer to a plain wallet costs exactly 21,000 gas, which is why a
transfer script is tempting to write with that number hardcoded. It is wrong as
soon as the recipient has code: a contract — or an **EIP-7702 delegated EOA**,
whose code is `0xef0100` followed by an address — executes that code on the way
in, so 21,000 runs out and the transaction **reverts while still charging the
fee**. A real 0.004 POL send did exactly that: it needed 21,220 gas, reverted at
21,000, and burned 0.0058 POL for nothing.

So each recipient's cost is asked of the chain with `eth_estimateGas`:

- **21,000 stays the floor.** A plain wallet still gets exactly 21,000, so an
  ordinary distribution costs what it did before — the estimate never pads the
  common case.
- **Above the floor, headroom is added** (10%, minimum 2,000 gas), because the
  estimate is taken in one block and the transaction lands in another.
- **A recipient that cannot be priced is not signed at all.** Estimation fails
  when the transfer would revert, and there is no honest limit to sign with —
  guessing is precisely what burns fees. That address is printed with the RPC's
  reason, recorded in the results file as `estimate_failed`, and left alone. The
  run exits `1` if nothing could be priced, `2` if only some failed. It is not
  retried either, since a retry would re-send it at the limit that cannot work.

The plan shows what it found, and the balance arithmetic uses those real limits:

```text
gas per tx      : estimated  21,000 min / 23,342 max
```

`--gas-limit` overrides the whole thing and applies one fixed limit to every
recipient, for when you know something the estimate does not:

```bash
# a recipient that gains code between blocks: give it room explicitly
python distribute_eth.py --wallets wallets.json --rpc <YOUR_RPC_URL> \
    --amount 0.002 --gas-limit 40000 --execute
```

A value below 21,000 is refused before anything is signed, because it cannot
succeed. Leave the flag off and the per-recipient estimate is used, which is
almost always what you want.

**A receipt that fails is not proof the transfer failed.** A rate-limited
endpoint answering HTTP 403 after the transaction was mined is the common case.
The retry path therefore looks the original tx hash up before doing anything,
settles it as success if it landed, and re-signs with the *same* nonce if it did
not. Two transactions sharing a nonce cannot both confirm, so a duplicate
payment is impossible — verified by a test that loses the first two receipts and
asserts each recipient was still paid exactly once.

Run the tests — they broadcast 100 real transactions on a local in-memory chain,
so no funds are at risk:

```bash
python test_distribute.py
```

## ERC-20 token on Polygon, verifiable on Polygonscan

[contracts/ProjectToken.sol](contracts/ProjectToken.sol) is a plain ERC-20 with a
**mintable** supply and an **owner**, plus the tooling to compile, deploy and
publish its source. Everything is Python: there is no Node.js, Hardhat or
Foundry anywhere in this pipeline, because none is installed here.

### Three powers, deliberately separated

| Role | Can do | Cannot do |
|---|---|---|
| **owner** | add or remove operators, transfer ownership | mint tokens |
| **operator** | mint, up to the cap | change who may mint |
| **holder** | transfer, approve, burn | mint |

The cap (`maxSupply`) is `immutable`: fixed at deployment and baked into the
bytecode, so no key can raise it afterwards. That is what makes "mintable" mean
"issue more" rather than "issue infinitely more". The owner deliberately cannot
mint — a leaked owner key hands over a role you can revoke, not the supply.

By default the deployer is both owner and operator, which is the simple case.
Splitting them (`--owner` / `--operator`) is the safer arrangement: deploy from a
hot operator wallet, hold ownership in a cold one.

The deliverable for the operator is [wallets-operator.json](wallets-operator.json),
a fresh single-wallet export with its own mnemonic (see *Generate* above). Back up
its phrase; deploying from it makes it the owner, the operator, and the account
holding the initial supply.

### Build, deploy, verify

```bash
# 1. compile. First run downloads solc 0.8.37 + OpenZeppelin 5.7.0.
python build_token.py
python build_token.py --recheck      # prove the stored input rebuilds the bytecode

# 2. deploy to Amoy. Without --execute this is a dry run.
python deploy_token.py --network amoy --key-file wallets-operator.json \
    --name "My Token" --symbol MTK --initial-supply 1000000 --max-supply 100000000

# 3. publish the source so Polygonscan shows it as verified
python verify_token.py --network amoy
```

| Network | Chain id | Explorer | Gas |
|---|---|---|---|
| `amoy` (default) | 80002 | amoy.polygonscan.com | free from [the faucet](https://faucet.polygon.technology/) |
| `polygon` | 137 | polygonscan.com | real POL |

Mainnet also requires `--i-know-this-is-mainnet`, because deployment is
irreversible and costs real money. On Amoy the RPC has a working public default;
on mainnet an endpoint is required explicitly, so a third-party URL is never
silently used for a real deployment.

### Verification is a bytecode comparison, not a matter of trust

Polygonscan does not check that your source "looks right". It recompiles the
source you submit with the settings you submit and compares the result to the
code on chain, byte for byte. So `build_token.py` **freezes** the compiler
version, optimizer runs, EVM target and metadata hash, and stores the exact
Standard JSON Input alongside the bytecode. `--recheck` recompiles that stored
input and fails loudly if anything differs — the same test the explorer performs,
run locally before you rely on it.

Two details follow from that, and both are load-bearing:

- **The compiler string must be complete.** Verification needs
  `v0.8.37+commit.f401782d`, not `v0.8.37`. py-solc-x's short form drops the
  commit hash, so the artifact records the compiler's own `--version` output.
- **Constructor arguments are ABI-encoded hex**, in declaration order, without a
  `0x` prefix. This is the most common cause of a failed verification, so
  `deploy_token.py` records the exact encoding it deployed with and
  `verify_token.py` submits that, refusing to guess.

`immutable` also explains an otherwise alarming observation: the artifact's
`deployedBytecode` differs from the code on chain in 32 bytes. solc leaves
immutable slots as zeros and the constructor patches the real values in at
deployment, so the deployed code is the artifact **plus** `maxSupply`. Both
scripts check that this is the *only* difference and report the comparison, so
"matches the artifact except 32 bytes in immutable slot(s)" is a pass, while any
difference outside those slots is a failure.

### Minting more supply

[mint_token.py](mint_token.py) issues the remaining supply, either as a flat
amount per round or as N successive doublings. Safe by default: without
`--execute` it reads the chain, prints the round-by-round plan and broadcasts
nothing. Mainnet needs `--i-know-this-is-mainnet` even to *plan*, so a testnet
command can never reach a real network by accident.

```bash
# plan only
python mint_token.py --network polygon --i-know-this-is-mainnet \
    --deployment deployment-polygon.json --key-file wallets-operator.json --doubles 3

# broadcast
python mint_token.py --network polygon --i-know-this-is-mainnet \
    --deployment deployment-polygon.json --key-file wallets-operator.json \
    --doubles 3 --execute --yes

# a flat amount, in as many rounds as you like
python mint_token.py --network polygon --i-know-this-is-mainnet \
    --token 0xCF2D…3B70 --amount 1000000 --rounds 8 --key-file wallets-operator.json
```

Every round is checked against `maxSupply` **before** anything is signed, and a
plan that breaches it is refused with the round number and the exact shortfall,
rather than signing a transaction that is certain to revert and burning gas on
it. `--allow-partial` runs only the rounds that fit. The receipt for each round
is fetched through the same retrying waiter the deployer uses, and the record is
written **before** the poll — so a rate-limited RPC can never lose a hash that
already cost gas.

**Eight doublings of the live token is arithmetically impossible**, and the cap
is what stops it. This is the real plan the tool prints against mainnet:

```
  round                        mint            resulting supply  fits
      1                 100,000,000                 200,000,000  yes
      2                 200,000,000                 400,000,000  yes
      3                 400,000,000                 800,000,000  yes
      4                 800,000,000               1,600,000,000  NO - over cap
      5               1,600,000,000               3,200,000,000  NO - over cap
      6               3,200,000,000               6,400,000,000  NO - over cap
      7               6,400,000,000              12,800,000,000  NO - over cap
      8              12,800,000,000              25,600,000,000  NO - over cap
  cap: 1,000,000,000 (immutable)
```

Eight doublings would need **25,600,000,000** tokens — 25.6x the permanent cap.
**Three** doublings fit, reaching 800,000,000 and leaving 200,000,000 of
headroom. Because `maxSupply` is `immutable` and baked into the deployed
bytecode, no key — not the operator's, not the owner's — can raise it. Growth
beyond that needs a **new deployment** with a higher cap, not another mint.

**Those three doublings were then executed on Polygon mainnet**, followed by one
last flat mint of the remaining headroom — which puts the supply **exactly on the
cap**, at 1,000,000,000, with `remainingMintable()` reading 0:

| round | mode | tx | block | minted | result |
|---|---|---|---|---|---|
| 1 | doubling | `0x0161da5d…b547b` | 95646180 | 100,000,000 | 200,000,000 |
| 2 | doubling | `0x88913dcb…df873` | 95646182 | 200,000,000 | 400,000,000 |
| 3 | doubling | `0x479a7efe…1b7f1` | 95646184 | 400,000,000 | 800,000,000 |
| 4 | flat | `0xc93f0d1f…e6932` | 95646303 | 200,000,000 | 1,000,000,000 |

180,464 gas in total, **0.0496 POL** (each plan reserved its cost at the ~510–526
gwei `maxFee` ceiling; the effective price came in far below every time). Each
round's `Transfer` log was decoded off the receipt, from the zero address,
summing to exactly the 900,000,000 minted across the four.
`mint-polygon.json` holds the record.

**With the cap reached, the contract now refuses more supply by itself.** Proving
that costs nothing: a simulated `mint` of even **one wei** from the operator
address reverts with `MaxSupplyExceeded(uint256=1000000000000000000000000001,
uint256=1000000000000000000000000000)`, decoded from the live RPC's revert data
rather than assumed. The 200,000,000 of headroom shown in the plan above is now
spent, and no key can restore it — only a new deployment can.

**A mint is not a split, and this is the part worth being clear about.** An
ERC-20 cannot be re-denominated: `decimals` is fixed at 18 and there is no split
function to call, so the only lever is the supply. A 2-for-1 *split* gives every
holder twice the units for the same share of the total. Doubling the *supply*
does the opposite: the holder keeps the same number of units while the total
doubles, so their share halves. Eight doublings would leave the original
100,000,000 out of 25,600,000,000 — **0.39%** of the token instead of 100%. If
the goal is more units at the same fraction, that is a new contract with a
migration, not a mint.

Guard rails, all covered by tests: the signer must actually be an operator (the
contract's own `NotOperator(address)` is checked up front, and the message names
the owner who could grant the role); the address must hold contract code; a
`--deployment` record is cross-checked against the chain's own `name`, `symbol`
and chain id, so a record pointing at the wrong token is refused before signing;
and a non-zero exit with no record entry is returned when nothing could run.

### Sending the supply out to many wallets

[distribute_token.py](distribute_token.py) sends an ERC-20 from one wallet to
many, one transaction per recipient, safe by default. Recipients come from a
`gen_wallets.py` export, a JSON array or a plain list of addresses.

```bash
# split a total evenly across every recipient
python distribute_token.py --network polygon --i-know-this-is-mainnet \
    --deployment deployment-polygon.json --key-file wallets-operator.json \
    --wallets wallets-500.json --total 1000000000

# a fixed amount each, funding only as many as the balance pays for
python distribute_token.py --network polygon --i-know-this-is-mainnet \
    --deployment deployment-polygon.json --key-file wallets-operator.json \
    --wallets wallets-500.json --amount 8771929 --spend-all --execute --yes
```

**`--spend-all` is the "use the whole balance" mode.** The default reserves
gas at twice the base fee, which is the right conservative choice but roughly
doubles what a transfer ends up costing -- on a long run that decides how many
wallets can be funded at all. `--spend-all` prices each transfer at
`base + tip` (what it actually pays), caps `maxFeePerGas` at that same figure so a
transaction can never cost more than planned, and trims the recipient list to
whichever limit binds -- **POL for gas, or the token balance** -- reporting which
one it was. A base-fee rise then rejects later transfers rather than overpaying:
a clean, free failure the record makes resumable.

The record is written **before** each receipt is fetched, and a resumed run
reconciles every entry left as `broadcast` against the chain first -- a receipt
settles it, and without one the sender's nonce decides. Only a REJECTED or
REVERTED transfer is treated as unpaid, so an interrupted run can never pay a
wallet twice.

**The live run, on Polygon mainnet:** 114 of the 500 wallets were funded with
**8,771,929 LOVEWEB each** -- 999,999,906 tokens, the entire remaining supply --
for **6,600,865 gas**, leaving 94 LOVEWEB and 0.356 POL in the operator wallet.
Verified afterwards from the chain: every one of the 114 recipients holds at
least its share, and `operator + recipients == totalSupply` exactly.

Three real defects surfaced during that run, and all three are now fixed with
tests:

- **A rate-limited RPC interrupted the run between broadcast and receipt.** The
  tool stopped cleanly and kept the hash, but the resume logic at that point
  would have re-sent to that wallet and paid it twice. `reconcile_broadcast()`
  now resolves such entries from the chain before anything is re-sent, and treats
  every broadcast entry as paid unless it is known to have failed.
- **A transfer reverted OUT OF GAS because the gas estimate was wrong.** A public
  RPC estimated 39,921 gas for a transfer that really needs 58,303 -- it priced
  the recipient's balance slot as already funded -- and the whole limit burned.
  A single probe is not a safe limit for a batch, so the limit is now also built
  from the **record's own observed `gas_used`** for transfers of the same shape,
  which would have prevented it. A revert now also says explicitly whether it ran
  out of gas or was refused by the contract.
- **The token balance, not POL, became the binding limit on resume**, and the
  plan was refused instead of trimmed. `--spend-all` now trims to the smaller of
  the two limits and names it.

### Seeding a liquidity pool

[seed_pool.py](seed_pool.py) creates the QuickSwap V2 token/POL pair and seeds
it in one go: `approve()` the router for the exact deposit, then
`addLiquidityETH`. Dry run by default, like every other script here.

```bash
python seed_pool.py --network polygon --i-know-this-is-mainnet \
    --token 0xCF2D6c41340Ae035BA76a581AcB9392D11533B70 \
    --amount 100000 --pol 5 --key-file key-seeder.txt          # plan only
python seed_pool.py ... --execute --yes                        # approve + seed
```

**The ratio is the price.** 100,000 tokens against 5 POL puts every token at
`0.00005` POL and implies a 50,000 POL valuation for the full 1,000,000,000
supply. There is no way to add liquidity without choosing that, and whatever it
is set to is what the first buyer pays.

Three things it refuses to do:

- **Trust a router it has not checked.** `factory()` and `WETH()` are read off
the router and compared with the known QuickSwap pair before anything is
approved, so a replaced router cannot be handed token allowance.
- **Seed twice.** The add-liquidity hash is written to `seed-pool-<network>.json`
**before** the receipt is awaited, and a `broadcast` record blocks any re-run,
exactly as `distribute_token.py` treats a broadcast transfer. A second
`addLiquidityETH` on the same pair would deposit another 100,000 tokens at a
different price -- `test_seed_pool.py` disables the guard to prove it does.
- **Read `100000` as base units.** The amount is scaled by the token's own
`decimals()`; 100,000 LOVEWEB is `1e23` wei, and 100,000 wei would be
`0.0000000000001` tokens, a mistake a confirmation screen does not show.

The plan also refuses to run short: it names the exact gap when the signer does
not hold the tokens, and suggests the `distribute_token.py` call that closes it.

### The API endpoint changed
The verification API is **`api.etherscan.io/v2/api` with `chainid=137`** (or
`80002`), not `api.polygonscan.com/api`. The old per-explorer endpoint was
retired with Etherscan API V1 and now answers HTTP 301, which is what every
older tutorial and most search results still tell you to use. One Etherscan API
key covers all chains, including Polygon — create it on Etherscan, not on
Polygonscan. Get the key into the environment or a file; it is never taken from
the command line:

```bash
export ETHERSCAN_API_KEY=...            # or
python verify_token.py --api-key-file etherscan.key
python verify_token.py --network amoy --status   # is this address verified?
```

If a contract was deployed elsewhere (Remix, another machine), verify it with
`--address` and `--constructor-args` instead of a deployment record.

### Test it

Four suites read the compiled contract, so on a fresh checkout build it first:

```bash
python build_token.py          # once; four suites read build/ProjectToken.json
python run_tests.py            # everything below + the audit: 725 checks, 68s

python test_token.py           # 162 checks, in-memory EVM, nothing broadcast
python test_mint.py            # 105 checks, in-memory EVM, nothing broadcast
python test_distribute.py      # 24 checks, 100 real transactions on a local chain
python test_distribute_token.py # 103 checks, real ERC-20 transfers on a local chain
python test_payment_request.py  # 99 checks, pure local computation
python test_seed_pool.py       # 39 checks, mock QuickSwap router on a local chain
python test_export_holders.py  # 41 checks, fake JSON-RPC chain, no network
python test_check_funding.py   # 46 checks, both providers faked, no network
python test_watch_only.py      # 41 checks, zpub vs seed derivation, BIP84 vectors
python test_gen_wallets.py     # 65 checks, BIP39/BIP44/BIP84 vectors, encryption
```

The wallet exports those suites read (`wallets-operator.json`, `wallets-500.json`,
`wallets-btc-900.json`, the 4,000-address set) hold private keys and are
gitignored, so [wallet_fixtures.py](wallet_fixtures.py) provisions throwaway
files of the same shape from the BIP39 test vector when they are absent — one
distinct passphrase per file, so the address sets never overlap. An existing
file is never touched, so on a machine that has the real exports the real
exports are what get tested.

**One command, one exit code.** [run_tests.py](run_tests.py) runs the ten
suites and `audit_test_checks.py`, prints one summary table, and exits nonzero
if any of them does -- a missing suite file and a timeout count as failures,
never as skips, because a suite that did not run has not verified anything.

```bash
python run_tests.py                  # all of it
python run_tests.py --only seed      # just the matching suite(s)
python run_tests.py -v               # stream each suite's output
python run_tests.py --fail-fast      # stop at the first failure
```

Counts come from lines that *begin* with `PASS`/`FAIL`. Grepping for `PASS`
naively inflated every total by exactly one, because five of the suites end
with the line `ALL CHECKS PASSED` -- which is a summary, not a check.

**CI runs exactly that command.** [.github/workflows/tests.yml](.github/workflows/tests.yml)
runs `python run_tests.py` on every push to `main` and on every pull request
across **Python 3.11, 3.12 and 3.13** (a matrix with `fail-fast: false`, so one
bad interpreter still reports the other two), and each job fails if any suite
exits nonzero -- so a red build always means a failing check. Branch pushes are
not a trigger: a bare `push:` also fires for feature branches, which ran the
whole matrix twice per PR commit, six identical jobs instead of three. 3.11 is the oldest version every
requirement allows and 3.13 the newest; the pins are ranges, not exact versions,
so the interpreter is part of what can drift. The suites are offline (in-memory EVMs, mock contracts, fake
providers), so no chain access and no secrets are involved; the only network
traffic is installing dependencies and fetching `solc` for the mock-router
fixture. `test_seed_pool.py` will fetch `solc` itself on a machine that has
none, so a fresh checkout works without a setup step.

**The matrix is pinned, not left to the resolver.** [constraints.txt](constraints.txt)
is the exact resolved closure (65 packages) and governs all three interpreters,
so every leg installs the same versions and a failure on one of them is a real
portability finding rather than two resolutions that quietly disagreed:

```bash
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
python run_tests.py
```

Each CI job installs through that file, then freezes what it actually got and
fails if any installed package is not at its pinned version -- including a
package that was installed but never pinned, because that means the file no
longer describes the closure. Pins for packages that do not exist on the
runner's platform are allowed and listed as inert (`pywin32` is the one case:
`web3` pulls it in on Windows only). The freeze is uploaded as the
`resolved-py<minor>` artifact, so drift arrives as a diff instead of a mystery.

That check has run: all three legs resolved the **identical 64-package set**, no
version differing on 3.11 or 3.13, and each leg passed every suite. A Linux leg
reports **726** checks rather than 725 because one assertion (POSIX permissions
end up `0600`) can only run off Windows.
If a pin genuinely cannot work on one interpreter, a
`constraints-py<minor>.txt` override is preferred for that leg alone.

## Who holds the token (holder export)

`export_holders.py` writes every current holder of the token, with balances read
from chain. It does not trust `distribution-token-polygon.json`: balances are
rebuilt from the contract's `Transfer` logs, which is the same arithmetic
`balanceOf()` performs, so an address that moved tokens after the distribution
still shows its real position.

```bash
python export_holders.py                      # -> holders-loveweb.csv
python export_holders.py --out holders.csv --from-block 95645544
```

The guard is the last step: the exported balances must add up to
`totalSupply()` exactly. If they do not, a `getLogs` window was truncated and
the script exits 1 without writing a file, rather than shipping a list that
silently omits someone.

Both halves are covered by [test_export_holders.py](test_export_holders.py)
(**41 checks**, no network): a fake JSON-RPC endpoint refuses any window wider
than a cap, so the halving path is exercised for real, and a deliberately
incomplete scan is asserted to exit 1 with no CSV on disk. The ERC-721 skip,
zero-address mint/burn handling, net-zero addresses and the retrying `rpc()`
wrapper are each asserted too -- removing any one of them fails a named check.

Columns: `rank, address, balance, balance_wei, share_pct, transfers_in,
transfers_out, token, symbol` — sorted by balance, 18 decimals.

**State as of block 96,305,822 (2026-10-28):** 121 holders, 134 transfers, and
the integer sum of `balance_wei` is exactly 10^27 — the 1,000,000,000 LOVEWEB
supply, to the wei. The log history is five mints (100M + 100M + 200M + 400M +
200M) to the operator, then 114 transfers of 8,771,929 each = 999,999,906,
which leaves the operator holding 94.

Fifteen transfers have happened since that distribution, and the export shows
all of them:

- **29 LOVEWEB left wallet #0** (8,771,929 -> 8,771,900) to the contract
  `0x4EEA541a…762b61a5`, which pushed 10 through the 0x exchange proxy
  `0xc590175E…74c02b` and on to `0x394767C0…f5d1B9`. Wallet #0 has a nonce of 2
  and 113 of the 114 have never sent anything: somebody else holds or held that
  key, which is why the phrase behind this set should be treated as leaked.
- **100,000 LOVEWEB moved from wallet #1 to a seeder wallet** for a liquidity
  pool, deliberately, one transaction.
- The remainder is dust (0.00000455, 0.0000000009, 0.0000000001 tokens) sitting
  at pass-through addresses, which is why the holder count rises from 115 to
  121 while the supply stays whole.

**Endpoint note:** this needs historical `eth_getLogs`. `polygon-rpc.com` now
answers 403, drpc's free plan rejects any `fromBlock` more than 10,000 blocks
behind the tip, and `1rpc.io` caps `getLogs` at 50 blocks. The default
`polygon-bor-rpc.publicnode.com` serves hex ranges up to 10,000 blocks, so the
script walks the deploy block to the tip in 10,000-block chunks (~33 requests)
and halves the window if a node refuses it.

## Security

The output file contains private keys. `wallets*.json` is already in
`.gitignore`, and the script warns if you point it inside a git repository.

**Permissions, honestly.** On Linux/macOS the file gets `0600`. On Windows
`chmod` is a silent no-op — the script applies NTFS restrictions via `icacls`
instead, but know the limits:

- An elevated process can read the file regardless of its ACL. On Windows you
  cannot lock a file away from an administrator.
- If the containing folder already limits access to your account, `icacls`
  changes nothing in practice. That is the case for this workspace.

So ACLs are not what protects this file — **encryption is**. Use `--encrypt` for
anything that lingers on disk. It is the only protection that survives the file
being copied, synced, backed up, or read by an admin process.

- Delete the file once the wallets are imported and the mnemonic is backed up
  on paper or steel — the phrase alone reconstructs everything in HD mode.
- Never paste a private key or mnemonic into a chat, a website, or an AI tool.
  No legitimate service needs it.
- Prefer `--encrypt` for anything that lingers on disk. Use a real passphrase;
  it's PBKDF2-HMAC-SHA256 (600k iterations) + Fernet.

## Testing the tests

The five suites each define their own `check()`, and **they do not agree on
argument order**: `test_token.py` is `check(label, condition)`, the other four are
`check(condition, label)`. That is a trap, and it caught a real failure. In
`test_payment_request.py` the crash-reporting wrapper was written with the args
the other way round, so when a test group actually crashed the suite printed
`PASS False` and exited **0** — a swallowed failure reported as success.

```bash
python audit_test_checks.py            # static audit + runtime guard probe
python audit_test_checks.py --sabotage # also prove each suite reports a failure
```

Three checks, because "the call sites look fine" is not evidence:

- **Static audit.** Parses every suite, infers each file's convention from the
  parameter its body branches on, and flags any call with a string in the
  condition slot (a string is always truthy, so that check can never fail) or a
  non-string in the label slot. Hard-coded `True`/`False` conditions are allowed —
  `check(False, "this should have raised")` is a normal idiom.
- **Runtime guard probe.** Imports each suite and confirms its `check()` accepts
  a correctly ordered call and **refuses** a swapped one, so the static result is
  not merely a claim about source text.
- **Sabotage.** Seeds a failure through each suite's own mechanism and requires
  the suite to name it in its summary and return 1. A suite that prints
  `ALL CHECKS PASSED` with a seeded failure fails this check.

Result on the current tree: **452 `check()` call sites, 0 swapped**, every guard
rejects a swapped call, and all five suites report a seeded failure with exit 1.
The auditor was itself verified by planting the exact original bug (an f-string
condition with a boolean label) in a copy of a suite and confirming it is caught.

Every check count quoted in this file counts `PASS` **check lines**, not
occurrences of the word `PASS` — the latter includes the `ALL CHECKS PASSED`
summary and inflated two of these counts by one until it was noticed.

Each `check()` now refuses the two shapes a swap produces — a string condition
and a non-string label — so the mistake crashes loudly instead of passing. The
convention of each file is stated in a comment at its own definition, since the
cheap fix would have been to rewrite 140 call sites.

## Contributing

```bash
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
python build_token.py      # once per checkout: four suites read build/ProjectToken.json
python run_tests.py        # every suite + the audit, one exit code
```

`run_tests.py` is the contract, and the CI badge above means one thing: that
command exited 0 on **3.11, 3.12 and 3.13** with the same pinned dependencies.
A red leg is a portability finding, not noise — do not widen a version range to
make it go away. If a single pin genuinely cannot work on one interpreter, add
`constraints-py<minor>.txt` with the pins that differ; CI prefers it for that
leg alone.

- **A new tool needs a suite.** Name it `test_<tool>.py`, add it to the suite
  list in [run_tests.py](run_tests.py), and keep the file's `check()` argument
  order — `audit_test_checks.py` refuses a swapped call, because a string in the
  condition slot is always truthy and that check can never fail.
- **Never commit a wallet export, key file, deployment record or address map.**
  [.gitignore](.gitignore) lists them; if a suite needs one, provision it with
  [wallet_fixtures.py](wallet_fixtures.py) instead of adding a file.
- **Suites stay offline.** In-memory EVMs, mock contracts, fake RPC and explorer
  providers. A test that needs a real chain or an API key will not run in CI.

### `main` only takes commits CI has passed

GitHub's own answer for that -- required status checks on `main` -- is not
available here. The repository is private on the Free plan, and both APIs refuse
with the same 403: *"Upgrade to GitHub Pro or make this repository public to
enable this feature."* So the rule is a pre-push hook instead:

```bash
git config core.hooksPath .githooks     # once per clone
```

It reads the check runs of the commit being pushed and refuses `main` unless
**all three** suites succeeded on it. A commit that was never pushed at all is
refused too -- open a pull request, let CI run, then merge; a direct push to a
commit no runner has seen can never satisfy the rule by construction.

Be clear about what this is: a guard on **this clone**. `git push --no-verify`,
the GitHub web UI, and any other machine all bypass it. Making the repository
public, or upgrading to Pro, is what moves the rule to the server.

```bash
git push --dry-run        # asks the hook without sending anything
git push --no-verify      # explicitly accept an unverified main
```

### The flow: branch, pull request, three greens, merge

Because a fresh commit has no run yet, **nothing** goes to `main` by direct push.
Branches are not gated (the hook only inspects `main`), so the work goes there:

```bash
git switch -c fix/whatever
git add -A && git commit -m "what changed and why"
git push -u origin fix/whatever        # branches are ungated; main is watched
```

Open the pull request with the link git prints, or the *Compare & pull request*
button on the repository page -- `gh` is not required and is not installed here.

The pull request head gets its own run. **Merge only when all three jobs are
green**; the checks are listed under the PR and the badge on this page reports
the same workflow. Then bring `main` down to the machine:

```bash
git switch main
git pull --ff-only                     # no hook runs on a pull, only on push
git branch -d fix/whatever
```

A merge made in the web UI is a single commit on `main` that no runner has seen,
which is fine: the work under it was verified on the PR head, and the new merge
commit gets its own run. The unavoidable case is an emergency -- `git push
--no-verify` lands it, the hook is skipped, and CI still runs afterwards; if it
comes back red, `main` is red and the fix is another branch.

## Status

Tested on Python 3.12.10 against published test vectors:

- **EVM:** the mnemonic `test test … junk` reproduces the Hardhat/Anvil accounts
  `0xf39Fd6…2266`, `0x709979…79C8`, `0x3C44Cd…93BC` at indices 0–2, matching
  including checksum casing.
- **Bitcoin:** `abandon abandon … about` reproduces the BIP84 spec vector
  `bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu`, with WIF
  `KyZpNDKnfs94vbrwhJneDi77V6jF64PWPF8x5cdJb8ifgg2DUc9d`.

Also exercised: 100 wallets generated and re-verified in 2.5 s with 100/100
unique addresses and keys, encrypted round-trip, wrong-password rejection,
random mode, 24-word phrases, and rejection of bad mnemonics, counts and chains.

For private-key derivation, the phrase in `wallets-btc-900.json` re-derived all
900 stored `private_key` values exactly, and driving that through the real CLI
via `--mnemonic-file` reproduced the same 900 keys — so the file's keys and the
phrase agree. All 900 WIFs (and all 100 in `wallets.json`) decoded back to their
own addresses. The zpub was
confirmed unable to produce a private key (raises `Bip32KeyError`), and index
change plus BIP39 passphrase were both confirmed to alter the resulting key.

For `check_funding.py`, a 3000-address scan completed in 61 batched requests with
all three controls passing (a funded address flagged, a funded-then-swept address
flagged despite a zero balance, and a freshly derived address left clear). All
3000 wallet addresses came back unfunded, and an independent sample of 12 was
re-checked against Blockstream Esplora — a different operator — which also
reported them clean while correctly reporting the genesis address as funded, so
the second path was proven live rather than assumed.

For `watch_only.py`, the account zpub at `m/84'/0'/0'` re-derived **all 900**
addresses from the zpub with no seed involved and matched the stored addresses
exactly. The check was shown to be meaningful rather than vacuous: a different
mnemonic's zpub yields different addresses, a single tampered address makes it
report `FAILED: 1 of 900` and exit 1, and a file without a stored mnemonic is
refused with a clear message. The zpub is confirmed as the SLIP-132 form
(`zpub…`, 111 chars) rather than a plain `xpub…`, and no private key is needed
or used at any point after the zpub exists.

For `distribute_eth.py` the suite actually broadcasts 100 signed transactions on
an in-memory chain and checks the resulting balances, distinct nonces, receipts
and recorded tx hashes. It also covers the dry run sending nothing, the resume
path not double-paying, insufficient balance and `--max-total` both aborting,
and invalid addresses / source mismatch / duplicate and self-send filtering.
For gas, it deploys two contracts: one that costs ~94,190 gas and must still be
paid (its limit comes back above 21,000 and the value arrives instead of
reverting), and one that rejects value, which must be reported as
`estimate_failed` rather than sent — with the mixed case paying the good wallet
exactly once and exiting `2`.
Reads were confirmed against a live mainnet RPC (`chain_id=1`, real fees and
balances); **nothing was ever broadcast to a real network**.

For the token pipeline, `python test_token.py` runs 162 checks on an in-memory
EVM and all pass. The load-bearing ones:

- the stored Standard JSON Input recompiles to **byte-identical** creation and
deployed bytecode with solc 0.8.37 — the same comparison Polygonscan performs,
run locally first;
- every role and revert path is exercised against the real contract: operator-only
minting, the cap enforced one wei past the limit, revocation, the owner being
unable to mint directly, ownership transfer, allowances, burns, and the
constructor's own refusals — each asserted against the exact **custom-error
selector**, not merely "it reverted";
- the code on chain was compared to the artifact and differs in exactly **32
bytes, all inside immutable slots** (8 significant bytes of `maxSupply` × 4
references, in slots that solc leaves zeroed). Flipping a single byte elsewhere
is reported as a failure and a wrong code length is caught, so the check is not
vacuous;
- separately: a live dry run against the public Amoy RPC succeeded end to end —
chain id 80002 confirmed, real gas estimate of 903,166 for a 4,945-byte creation,
and a correct refusal at a zero balance. Nothing was broadcast.

Two real bugs were caught by writing these tests rather than assuming: supply
parsing silently **rounded** large values, because `Decimal`'s default context
carries only 28 significant digits and a 30-digit supply is 48 digits of wei;
and the on-chain code comparison deleted a byte, because `HexBytes.hex()` omits
the `0x` prefix on hexbytes ≥ 2 and stripping two characters shifts every
offset by one. Both are fixed and both now have a test that fails without the
fix. Three further test-side errors of my own (mint arithmetic, a lowercased
address comparison, and a cap with no headroom) were corrected before being
relied on.

**The token pipeline has now been exercised on a live chain, end to end.**
ProjectToken was deployed to Polygon PoS mainnet at
`0xCF2D6c41340Ae035BA76a581AcB9392D11533B70` (block 95645544, gas 895,196, cost
0.2501 POL) and **Polygonscan returned `Pass - Verified` on the first poll**, so
building, constructor encoding, broadcasting, the receipt, the on-chain code
comparison and source verification are all observed rather than inferred. The
mined transaction was checked to carry this artifact's creation bytecode and
byte-for-byte identical constructor arguments, and the deployed contract was
read back over RPC: name, symbol, decimals, supply, cap, owner and operator all
matched. A stranger's `mint` reverted on mainnet with the `NotOperator(address)`
selector while the operator's simulated successfully.

That run also produced the one failure worth knowing about. A public RPC
answered **HTTP 403 while the receipt was being polled**, immediately after a
successful broadcast, which surfaced as a traceback and lost the deployment
record — the gas was already spent. `wait_for_receipt()` now retries, names the
transaction, warns that re-running with `--execute` would deploy a *second*
contract, and links to the explorer; the record is also written **before** the
receipt is fetched, so a hash can never be lost that way again. Both behaviours
are covered by tests. (The record for that deployment was rebuilt from the mined
transaction and is marked `record_reconstructed_from_chain`: true.)

For minting, `python test_mint.py` runs **105 checks** on an in-memory EVM and
all pass, with the live token's own numbers (100,000,000 of a 1,000,000,000 cap)
as the fixture. The arithmetic is asserted rather than eyeballed: three of eight
doublings fit, the fourth would need 16x, and all eight would need
25,600,000,000. Real signed mints then move the supply for real (100,000,000 ->
800,000,000 in three rounds, each round minting the supply that existed at the
time), with distinct nonces and per-round receipts recorded. The cap is tested at
its exact boundary — a token that lands precisely on 800 of an 800 cap — where a
further mint of half a token, and even the *first* round of a doubling, are both
refused before signing. Non-operators are refused with the owner named, records
are cross-checked against the chain, all five of the contract's custom errors
are decoded (from a live revert and from the `0x`-hex form a real RPC returns),
repeated runs append to one audit file instead of erasing it, and every guard
rail is exercised. Three of the failures in this suite were bugs in the *tests*
(a missing `--tester` that made the tool correctly refuse the wrong chain, and
two wrong expectations about which round breaches a cap that is already reached),
and were fixed rather than the assertions relaxed.

`mint_token.py` was then run against **live mainnet in dry-run mode**: it read
the deployed token, confirmed the signer is an operator, refused eight doublings
naming round 4 and the 600,000,000 overshoot, and costed the three that fit —
**45,493 gas per round**, EIP-1559 fees, **0.0884 POL** for all three at the
prevailing base fee.

**That dry run was then executed for real**, so the mint path is now observed end
to end on Polygon rather than only estimated: four signed mints took the supply
from 100,000,000 to **1,000,000,000** — the cap exactly — for **0.0496 POL**.
Every number was re-derived from the chain rather than read back out of the
tool's own record, in two passes (26 checks after the three doublings, 12 more
after the final mint), all passing over the receipts, the token's public getters
(`totalSupply`, `maxSupply`, `remainingMintable`, `balanceOf`) and the decoded
`Transfer` events. The immutable cap was confirmed to still read exactly
1,000,000,000, `remainingMintable()` was confirmed to equal `cap - totalSupply`
and then to reach **0**, name/symbol/decimals were confirmed unchanged, and the
record's per-round gas, blocks and totals matched the receipts exactly.

For `distribute_token.py`, **103 checks** run on an in-memory chain and all
pass, driving real signed ERC-20 transfers: every recipient funded, resume proven
not to double-pay, `--limit`, `--total` splitting including exact fractional
shares, `--max-total`, duplicate and self-send dropping, and refusal when the
balance, the token supply or the gas budget does not cover the plan. The three
defects above each have a test that fails without its fix, including one that
reproduces the interrupted-broadcast state exactly and asserts the wallet is
**not** paid a second time. Five of the failures during this work were wrong
expectations in my own tests (a stale balance, a record that was already
complete, a miscounted duplicate, and two boundary cases that price drift could
flip) and were corrected rather than loosened.

**Not covered:** the earlier deployer-side run had a bug that a mint cannot
reproduce, so that path is now the better tested one: the deployer crashed on a
rate-limited receipt poll and lost its record, and each of the four mints that
followed went through the retrying waiter without incident. No MetaMask import
was performed — the import steps are
documented behaviour from MetaMask's help pages, not something tested here. The
token has never been transferred: all 1,000,000,000 sits in the single operator
wallet, which is also where every mint was sent, so there is no market, no
holder and no price. And the supply is now permanently fixed — `totalSupply`
equals `maxSupply` and `remainingMintable()` is 0, so **the only way to more
tokens is a new deployment**, and the only way back down is a burn.
