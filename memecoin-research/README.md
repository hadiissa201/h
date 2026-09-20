# Meme-coin research — Phase 1

Observation only. This package **cannot trade**. It holds no private key, imports
no signing library, and has no code path that can submit a transaction. Tests in
`tests/test_no_wallet.py` fail the build if anyone adds one.

Its job is to build the dataset nobody publishes: **the launches that went to
zero**, recorded alongside the winners, so a strategy can later be replayed
against the complete population instead of against screenshots.

## What is here

| Path | Purpose |
|---|---|
| `probe/` | Verifies every external service from the machine that will run the collector |
| `poc/` | Proof of concept: detect → market data → simulated exit → Postgres |
| `tests/` | Offline tests: parsing, storage, idempotency, and the no-wallet guarantee |

Not here: the continuous collector. It is not built until the probe proves the
services work and you approve the results.

## Run the probe first

```bash
pip install -r requirements.txt
python -m probe --ws-seconds 300                 # public endpoints
python -m probe --helius-key YOUR_KEY --ws-seconds 300
```

Writes `probe_report.json` with evidence for every check. It reports three
outcomes, never two: **PASS**, **FAIL**, and **UNVERIFIED** — a check that could
not run (no API key) is not the same as a service that is broken.

It also measures the **real launch rate**, which is what decides whether full
collection is affordable or sampling is forced. That number is measured, never
assumed.

## Then the proof of concept

```bash
createdb memecoin_poc
python -m poc --database-url postgresql+psycopg://localhost/memecoin_poc --detect-seconds 120

# deterministic re-run on a known mint
python -m poc --database-url ... --skip-detect --mint <MINT>
```

## The three-state rule

`simulated_exits.succeeded` is **nullable on purpose**:

| Value | Meaning |
|---|---|
| `True` | An exit was available at that instant, for that size |
| `False` | We asked, and there was genuinely no way out — **the finding** |
| `NULL` | We could not ask (timeout, 429, outage) — **not evidence of anything** |

Collapsing `NULL` into `False` would turn every network outage into a wave of
fake "unsellable" tokens and corrupt the exact base rate this research exists to
measure. **Phase 2 must count only rows where `succeeded IS NOT NULL`.**

The same discipline applies to `observations`: a field the source omitted is
stored as `NULL`, never `0`. "No data" and "zero liquidity" mean opposite things.

## What a successful simulation does not prove

It is evidence an exit was possible *at that instant, for that size, with no
competition*. It does not model MEV, the priority-fee auction, or everyone
selling in the same block. **Real exits are strictly worse than this record**, so
Phase 2's output is an optimistic bound, not an estimate.

## Deployment: use a VPS

Recommended: **a ~$5/month VPS**, not the PC.

| | PC | VPS |
|---|---|---|
| Uptime | Sleeps, reboots, Windows updates | 24/7 |
| Network | Home connection, NAT, ISP resets | Datacenter |
| Cost | £0 | ~$5/mo |
| Data quality | **Gaps** | Continuous |

The deciding factor is not cost or convenience — it is bias. Launches happen
around the clock, and a machine that sleeps overnight collects a dataset skewed
toward whatever launched while you were awake. That is not a smaller dataset; it
is a **differently-shaped** one, and no amount of later analysis repairs it.

The PC is workable for the probe and the proof of concept, which are one-shot.
For continuous collection, $5/month buys the difference between a dataset you
can trust and one you cannot.

Either way `collection_gaps` records every window we were blind, so Phase 2 can
restrict itself to covered time rather than reading our downtime as an absence
of launches.

## Status

Verified offline: parsing, storage, idempotency, the no-wallet guarantee
(60 tests). **Not yet verified: anything requiring the network.** The build
environment blocks Solana, Helius, DexScreener and Jupiter at the proxy, so
every endpoint, rate limit and program ID in `probe/constants.py` is a
**candidate** until the probe confirms it on the target machine.
