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
| `collector/` | **The Phase 1 collector.** Runs continuously, records everything |
| `tests/` | Offline tests: sampling, scheduling, coverage, and the no-wallet guarantee |

## Running the collector

```bash
pip install -r requirements.txt
createdb memecoin_research

# see what this configuration can sustain, change nothing
python -m collector --plan

# collect
python -m collector --workers 4
```

Status while it runs: `http://127.0.0.1:8787/status`.

### Why it samples

Not a preference — arithmetic. Each token costs ~217 DexScreener calls over
7 tracked days. DexScreener was **measured** at ~300 req/min sustained (297/297
over 60s, no 429) and Jupiter at **~120/min** (429 after 121 requests in 24.9s,
far below what its burst suggested). That caps the collector near **11,100
concurrently tracked tokens**, i.e. **~1,590 new tokens/day** — against tens of
thousands of daily launches.

The sample is a deterministic hash of the mint address, and the rate is stored
on every token. That matters more than it looks: a *deliberate* random sample is
unbiased and can be weighted back to the population. Keeping "whatever we could
keep up with" is not a sample — it over-represents quiet periods, because quiet
is exactly when there is spare capacity.

`python -m collector --plan` recomputes all of this from the live config.

### Detection uses Helius, not the public RPC

Measured over comparable 300-second windows: public RPC ~29.6 creations/min,
Helius ~41.2/min. The public node drops messages under load **and reports no
error** — it simply looks like a quieter market. Set
`MEMECOIN_HELIUS_API_KEY` or the collector warns and under-counts.

## Run the probe first

```bash
pip install -r requirements-probe.txt            # httpx + websockets only
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
pip install -r requirements.txt                  # adds the database stack
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

## Paper trading

Runs alongside the collector, on the same live feed. **No wallet, no orders, no
money** — there is no key in this codebase to place one with. Its job is to turn
"would these rules have made money?" into an accumulating record instead of an
opinion.

Three default rule sets, deliberately ordinary and unoptimised (tuning before
the base rate is known is how a curve gets fitted to noise):

| Strategy | Entry | Exit |
|---|---|---|
| `early_200` | < 10 min old, ≥ $5k liquidity, ≥ 5 buys/5m | +200%, −50%, or 1h |
| `patient_200` | < 30 min old, ≥ $20k liquidity, ≥ 15 buys/5m | +200%, −50%, or 3h |
| `quick_50` | < 10 min old, ≥ $5k liquidity | +50%, −30%, or 15m |

**What makes the result trustworthy** — each rule exists because its absence is
a known way for a memecoin backtest to lie:

- **A position closes only when an exit genuinely existed.** If the token became
  unsellable while held, the position *stays open*, exactly as real money would
  be stuck. Closing at the last quoted price instead turns a rug that printed
  +300% into a clean winner.
- **The most recent exit attempt decides, and verdicts go stale.** Skipping past
  an unknown to reach an older success assumes our failure to get an answer is
  unrelated to the token — but a vanished pool is exactly what breaks a quote.
- **It never buys what has never been sellable.**
- **Costs include the measured price impact**, not just fees. On a thin pool the
  impact dwarfs the fee.
- **The chart peak and the sellable peak are tracked separately.** The gap
  between them is the measured cost of unsellability.

Progress shows up under `paper_trading` at the status endpoint. `stuck_no_exit`
counts positions the rules wanted to close but couldn't — those are **not**
counted as profit.

Turn it off with `MEMECOIN_PAPER_TRADING_ENABLED=false`.

## Rules Phase 2 must obey

`collector/research.py` exists so a strategy replay cannot accidentally cheat:

- **Entry is priced at `detected_ts`, never `launch_ts`.** Pricing at on-chain
  creation hands the backtest latency we do not possess.
- **Only `succeeded IS NOT NULL` counts as evidence.** A transport failure is
  not a rug.
- **Queries are restricted to covered windows.** Outside them, the absence of
  launches is the absence of a collector.
- **Fills happen only at observed prices.** Interpolating invents liquidity
  that was never demonstrated.
- **Weight by `population_weight()`** before quoting any population rate.

## Status

Every external service verified on the target machine: **28 checks, 0 failed,
0 unverified** (`python -m probe`). Offline: 98 tests covering parsing, storage,
idempotency, sampling uniformity, the cadence ladder, coverage accounting, and
the no-wallet guarantee.

The launch rate is the one number still settling: 1,025,280/day → 54,432/day as
three separate counting bugs were found and fixed. It is measured, never
assumed, and the collector is sized from the live configuration rather than
from any figure written down here.
