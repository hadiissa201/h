# Architecture

How the pieces fit together, and — more usefully — why each boundary is where it
is. Every design note below reflects a decision that has a cost; the cost is
stated alongside it.

## Contents

- [The layer split](#the-layer-split)
- [Data flow of one decision](#data-flow-of-one-decision)
- [Layer by layer](#layer-by-layer)
- [The risk approval mechanism](#the-risk-approval-mechanism)
- [No-look-ahead discipline](#no-look-ahead-discipline)
- [Accounting invariants](#accounting-invariants)
- [Shared code between live and backtest](#shared-code-between-live-and-backtest)
- [Failure behaviour](#failure-behaviour)
- [Data model](#data-model)
- [Decisions and their trade-offs](#decisions-and-their-trade-offs)

---

## The layer split

```
n8n          when things happen, what happens next, retries, alerting, escalation
Python       every calculation and every decision that money depends on
PostgreSQL   the record: candles, features, signals, decisions, orders, trades
LLM          a reviewer that can veto a trade and nothing else
```

The rule: **n8n never computes anything financial.** No indicator maths, no
sizing, no P&L in a Code node. A Code node is untested by construction; a stray
edit in a browser changes trading behaviour with no review and no test run. Every
number comes from a Python endpoint that has a test around it.

The cost of this rule is chattiness — a single symbol's analysis is several HTTP
calls rather than one in-process pass. At one tick per symbol per timeframe, that
does not matter.

## Data flow of one decision

```
 01 collect ──▶ candles table
      │              ▲
      │  data_ok?    │
      ▼              │
 02 analyse ──▶ features ──▶ regime ──▶ strategies ──▶ candidate
      │                                                    │
      │ candidate worth an LLM call?                       │
      ▼                                                    │
 03 AI review ──────────────────────────────────────────────
      │  CONFIRM (same direction) or HOLD (veto)
      ▼
 04 risk check ──▶ 22 checks ──▶ sizing ──▶ APPROVAL (single use, fingerprinted)
      │
      │ approved?
      ▼
 05 execute ──▶ paper/live exchange ──▶ order ──▶ position
                                                    │
 06 monitor (every minute) ──▶ mark, trail, breakeven, partial, exit, kill switch
```

Any stage may terminate the flow. The terminating reason is recorded, because "no
trade today" needs to be explainable after the fact.

## Layer by layer

### `app/data` — market data and its gates

Providers (`ccxt` for real public data, `synthetic` for offline work) sit behind
one interface. Everything fetched passes validation before it is stored:

| Gate | Default | Rejects |
|---|---|---|
| staleness | 300s | a feed that has stopped updating |
| minimum bars | 120 | not enough history to warm up the indicators |
| gap ratio | 2% | missing candles that would corrupt rolling windows |
| OHLC sanity | — | `high < low`, `close` outside the bar, non-positive prices |
| duplicates | — | repeated timestamps |

A failed gate means **no trade**, not a best-effort trade.

One subtlety worth knowing: staleness is measured against the newest *closed*
candle, which is legitimately up to two bar-widths old (the current bar has not
closed, and the one before it only just did). The check subtracts `2 × bar` before
comparing, or every timeframe above 5m would look permanently stale.

### `app/indicators` — pure functions on series

Trend (EMA, MACD, ADX/DI, slope, Kaufman efficiency ratio), momentum (RSI,
Stochastic RSI, ROC), volatility (ATR, Bollinger, realised vol, squeeze), volume
(OBV, relative volume, VWAP) and structure (fractal pivots, market structure,
Donchian breakouts). No TA-Lib: pandas and numpy only, so there is no native build
step and every formula is readable and testable in place.

Two documented traps live here:

- **ADX can read 100 on a frozen or perfectly alternating market.** That is
  genuine Wilder behaviour, not a bug — with a directional movement of zero in one
  direction, the smoothed DX saturates. Guarding it inside ADX would falsify the
  indicator, so the protection lives in the regime layer instead (see below).
- **Fractal pivots are retrospective.** A pivot at bar `j` is only knowable `k`
  bars later, so every pivot-derived series is shifted by the confirmation delay.
  `pivot_high` is exposed for charting and is explicitly documented as unusable
  for signals.

### `app/features` — multi-timeframe assembly

Builds 65 feature columns per timeframe (default config) and aligns the higher timeframes onto the primary
one without leaking future information (a 4h value is only visible once that 4h bar
has closed). Percentile-rank features like `atr_pct_rank` use a longer window
(200 bars) than the indicators themselves — with a 50-bar window the rank saturated
at 1.0 constantly and every volatility filter became a no-op.

### `app/regime` — what kind of market is this

Classifies into `strong_bull_trend`, `weak_bull_trend`, `range`, `low_volatility`,
`weak_bear_trend`, `strong_bear_trend`, or `unknown`, plus a volatility state and
an `is_abnormal` flag.

The **Kaufman efficiency ratio** (net move ÷ total path travelled) is what stops an
oscillation from being called a trend. Measured on the test fixtures: a clean trend
scores 0.80, a sine wave 0.12, a perfectly alternating series 0.00. Below
`efficiency_min_trend` (0.12) no trend label is issued regardless of what ADX says
— which is exactly the ADX-saturation case above, handled where it belongs.

The regime returns `unknown` unless **all** of its inputs are present. Classifying
from half-warm indicator rows produced confident nonsense in early testing.

### `app/strategies` — five independent hypotheses

`trend_following`, `ema_momentum`, `breakout`, `mean_reversion`,
`volatility_breakout`. One interface: given features and a regime, return a
complete trade idea or nothing. Each declares which regimes it is allowed in, so a
mean-reversion idea cannot fire inside a strong trend.

A signal is never just a direction. It carries entry, stop, target, an
invalidation condition in words, and the exit plan (trailing multiple, breakeven
trigger, partial-exit level, time stop). If a strategy cannot state where it is
wrong, it does not get to trade.

### `app/ai` — the reviewer

Providers: Ollama, OpenAI, Anthropic, or disabled. The prompt is a compact
structured snapshot — not raw candles, not a chat history — and the model is told
plainly that it is a reviewer, that sizing and levels are decided downstream by a
deterministic engine, and that asking for more size has no effect.

The policy is enforced in code, not in the prompt:

- the returned direction must match the deterministic candidate, or it is a HOLD;
- final confidence is `min(candidate_confidence, ai_confidence)` — the AI can lower
  conviction, never raise it;
- entry, stop and target always come from the quant layer, whatever the model says;
- a malformed reply is a HOLD, never a retry-until-it-agrees loop.

Calls are rate-limited globally and per symbol. If the provider is down and
`AI_REQUIRED_FOR_ENTRY=true`, the entry is refused — the failure mode of an
unavailable reviewer is *no trade*, not *unreviewed trade*.

### `app/risk` — final authority

22 blocking checks, all pure functions of explicit inputs: bot status, cooldown,
data quality, abnormal conditions, regime known, direction allowed, symbol active,
stop present, stop distance min/max, confidence, reward-to-risk, open-position
counts, daily loss, drawdown, positive equity, spread, liquidity, sizing, risk
budget, cash.

Then sizing:

```
risk_amount    = equity × RISK_PER_TRADE
raw_quantity   = risk_amount ÷ (entry − stop)
quantity       = min(raw, position cap, exposure cap, affordable cash)
                 rounded DOWN to the symbol's step size
```

`RISK_PER_TRADE` is the amount **lost if the stop is hit**, not a position size.
The response reports `capped_by`, so an unexpectedly small position is always
explainable. Rounding is always down: rounding up would silently exceed the very
limit that was just computed.

### `app/execution` — orders

`ExchangeAdapter` has two implementations. The paper engine keeps per-currency
balances, is idempotent on `client_order_id`, supports resting limit and stop
orders, and charges a realistic cost stack: **spread → slippage (with a size-impact
term) → fee**. A stop that gaps through its trigger fills at the gap price, not at
the trigger — modelling stops as free is how a backtest lies to you.

The live adapter is present but disarmed, and re-checks arming on every mutating
call. `get_positions()` deliberately raises `NotImplementedError` rather than
returning a plausible-looking guess: spot has no exchange-side positions, and
reconciling our table against exchange balances is a checklist item, not something
to fake.

### `app/portfolio` — valuation and exits

Single source of truth for equity, exposure, drawdown and daily P&L. It never
accepts a caller-supplied equity figure; everything derives from stored balances,
positions and trades.

Exit rules are shared with the backtester and encode three rules that are easy to
get wrong:

- when one bar covers both the stop and the target, the **stop** is taken — the
  pessimistic assumption is the only honest one without tick data;
- a gap through a level fills at the bar's open, not at the level;
- a trailing stop never widens.

### `app/backtesting` — the same code, replayed

Event-driven, bar by bar, reusing `FeatureEngine`, `RegimeDetector`,
`StrategyEngine`, `RiskEngine`, `calculate_position_size`, the fill model and the
exit rules. Not a reimplementation — a reimplementation is how backtest and live
silently diverge.

The walk-forward harness splits data into rolling in-sample/out-of-sample windows
and judges on **pooled** out-of-sample metrics. Averaging per-window metrics lets a
single tiny window with two lucky trades dominate the verdict.

Every backtest result carries explicit warnings: the LLM is not replayed, spread
and liquidity filters are not evaluated, and a position still open at the end of
data is closed at the final close and flagged as incomplete.

## The risk approval mechanism

This is the part that makes "the AI cannot bypass risk management" a structural
property rather than a promise.

```
POST /risk/check  {symbol, direction, entry, stop_loss, take_profit, confidence}
   │
   ├── 22 checks ─── any blocking failure ──▶ REJECTED, no approval issued
   │
   └── sizing ──▶ APPROVED
                    approval_id  = ra_<random>
                    fingerprint  = sha256(symbol|direction|entry|stop|quantity|mode)
                    expires_at   = now + RISK_APPROVAL_TTL_SECONDS (120s)
                    consumed     = false

POST /orders  {risk_approval_id}
   │
   ├── approval unknown ──────────────▶ 403
   ├── expired ───────────────────────▶ 403
   ├── already consumed ──────────────▶ 403
   ├── fingerprint mismatch ──────────▶ 403
   └── valid ──▶ place order, mark consumed
```

Properties that follow from this, each covered by a test in
`tests/failure/test_risk_bypass.py`:

- an order cannot be placed without going through the risk engine first;
- an approval cannot be replayed to double a position;
- changing *any* parameter of the trade after approval invalidates it;
- a stale approval cannot be used after conditions have moved;
- the AI layer never handles an approval and has no route to mint one.

The one deliberate exception: a **retry of the same order** (same
`client_order_id`) short-circuits to the original order *before* approval
validation runs. Otherwise a network timeout on a successful order would leave the
caller unable to retry and unable to confirm — the worst possible state.

## No-look-ahead discipline

The single easiest way to build a backtest that shows wonderful returns and loses
money live. The rules, enforced in code and tested in
`tests/unit/test_indicators_no_lookahead.py`:

- **A signal computed on bar `i` fills at the open of bar `i+1`.** Never at the
  close of `i`, which is the price that produced the signal.
- **Rolling windows never include future bars.** Donchian channels explicitly
  exclude the current bar.
- **Pivots are shifted by the confirmation delay**, so `last_swing_high` means
  "the most recent high we could have known about by now".
- **Higher-timeframe features appear only after that bar closes.**
- The test suite computes each indicator on a prefix of the data and on the full
  series and asserts the overlapping values are identical. A value that changes
  when future data arrives is a look-ahead leak.

## Accounting invariants

If equity, cash, realised P&L and the trade records ever disagree, every
performance number the system reports is wrong. The books must close:

```
equity == starting_equity + realised P&L + unrealised P&L − unamortised entry fees
```

The last term exists because an entry fee is paid in full up front but only enters
realised P&L as exits amortise it. While a position is open, its share sits outside
both P&L figures. `PortfolioService.reconciliation_error()` returns the residual
and `books_balance()` checks it against a documented tolerance; the daily report
states the result, so an accounting break surfaces on its own rather than waiting
to be discovered.

The tolerance is not zero, and the reason is worth stating precisely: equity rounds
`price × quantity` per leg while P&L rounds `(mark − entry) × quantity`. The two
are algebraically identical before rounding, but each 8-decimal quantisation can
land one unit apart. The bound is a few units of the eighth decimal place —
a hundredth of a millionth of a quote unit. Anything larger is a real error.

Related: money is stored as `NUMERIC(28, 8)`. Scale 8 is what makes Decimal values
round-trip exactly through SQLite as well as Postgres; with a float column the
suite produced values like `9995.745819870001` and reconciliation became
impossible.

## Shared code between live and backtest

| Component | Live | Backtest |
|---|---|---|
| `FeatureEngine` | ✅ | ✅ same |
| `RegimeDetector` | ✅ | ✅ same |
| `StrategyEngine` | ✅ | ✅ same |
| `RiskEngine` | ✅ | ✅ same |
| `calculate_position_size` | ✅ | ✅ same |
| `fill_model` | ✅ | ✅ same |
| `exit_rules` | ✅ | ✅ same |
| LLM review | ✅ | ❌ not replayed |
| spread / liquidity filters | ✅ | ❌ no historical data |

The two ❌ rows both make backtests *more* permissive than live. Live will reject
trades the backtest took, never the reverse.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Exchange data unreachable | Data gate fails → no trade. Workflow 01 escalates. |
| Data stale or gappy | Rejected before features are computed. |
| LLM provider down | `AI_REQUIRED_FOR_ENTRY=true` → entry refused. |
| LLM returns malformed JSON | Parsed as HOLD. No retry loop. |
| Database unreachable at boot | Service starts, reports `degraded` on `/health`. Visible to n8n rather than crash-looping invisibly. |
| Order rejected by exchange | Recorded with the reason; no position is opened; approval stays consumed. |
| Daily loss / drawdown breached | Kill switch halts new entries; manual confirmed reset required. |
| A workflow throws | Workflow 09 records it and escalates the unsafe ones. |

Covered in `tests/failure/test_degraded_conditions.py`.

## Data model

21 domain tables. The ones that matter:

| Table | Holds |
|---|---|
| `candles` | OHLCV per symbol/timeframe |
| `feature_snapshots`, `regime_snapshots` | what was computed, when |
| `signals`, `trade_candidates` | what each strategy proposed |
| `ai_decisions` | prompt hash, raw reply, parsed decision, latency, cost |
| `risk_decisions` | every check result, sizing, approval, consumption |
| `orders`, `fills`, `positions`, `trades` | execution and outcomes |
| `equity_snapshots`, `daily_stats`, `strategy_performance` | performance over time |
| `bot_state`, `emergency_events`, `system_events` | operational state and audit |

The design principle is that **every decision is reconstructable after the fact**:
what the data looked like, what each strategy said, what the model said, which risk
check failed, and what was actually executed.

## Decisions and their trade-offs

**Synchronous Python, not async.** FastAPI runs `def` endpoints in a threadpool,
ccxt is synchronous, and financial bookkeeping is far easier to reason about
without interleaved awaits. The cost is throughput, which is irrelevant at one tick
per symbol per timeframe.

**`autoflush=True` on the session.** A query must see writes made earlier in the
same request. With it off, cancelling an order and then listing open orders
returned the cancelled one, and the position monitor could act on stale state. The
cost is mid-transaction flushes; read-after-write consistency is worth more.

**Webhooks between workflows, not Execute Workflow nodes.** An Execute Workflow
node stores the *ID* of its target. IDs are per-instance, so an exported chain
breaks silently on import — and the failure is invisible until a trade should have
happened. Webhook paths are fixed strings that survive export/import and can be
tested with `curl`. The cost is that receiving workflows must be active and
internal calls need a shared secret, which is why every webhook entry point
validates `x-workflow-secret`.

**Postgres is the record; Redis is optional.** Running without Redis costs nothing
but a little latency, and one fewer stateful service is one fewer thing to lose
data in.

**The LLM is a veto, not a generator.** A language model asked to produce entries
and stops will produce confident, plausible, unbacktestable numbers. Asked to
review a deterministic candidate, its worst case is that it declines a good trade —
which costs nothing but opportunity. The cost of this design is that the AI can
never find a setup the quant layer missed. That is the intended trade.
