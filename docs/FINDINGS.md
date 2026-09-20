# Findings: do these strategies have an edge?

**No.** Measured over 1,065 out-of-sample trades across three symbols and 20
months of real Binance history, every strategy in this repository loses money.
The result is consistent, statistically meaningful, and not close.

One thing did measure positive — **funding carry**, at about 3% a year on
deployed capital, which is probably not worth the risk it carries. See
[the funding carry section](#the-one-thing-that-did-measure-positive-funding-carry).

This document exists so the evidence outlives the memory of it. If you return to
this project in six months, read this before rebuilding anything.

**Date of test:** 19 September 2026
**Data:** Binance spot, 1h bars, ~14,800 bars per symbol (≈20 months)
**Mode:** paper throughout. No real money was ever at risk.

---

## Contents

- [Headline result](#headline-result)
- [Walk-forward: the decisive test](#walk-forward-the-decisive-test)
- [Why this is not an overfitting problem](#why-this-is-not-an-overfitting-problem)
- [The cost hurdle](#the-cost-hurdle)
- [What was tested and rejected](#what-was-tested-and-rejected)
- [What the result is not](#what-the-result-is-not)
- [The one thing that did measure positive: funding carry](#the-one-thing-that-did-measure-positive-funding-carry)
- [Independent replication: two outside repositories](#independent-replication-two-outside-repositories)
- [What would have to be different](#what-would-have-to-be-different)
- [Reproducing this](#reproducing-this)

---

## Headline result

| Symbol | Trades | Win rate | Expectancy (R) | Profit factor | Strategy | Buy & hold | Cash |
|---|---|---|---|---|---|---|---|
| BTC/USDT | 128 | 21.9% | **−0.391** | 0.48 | −8.98% | −16.72% | 0% |
| ETH/USDT | 250 | 31.6% | **−0.194** | 0.71 | −10.38% | −23.98% | 0% |
| SOL/USDT | 160 | 33.1% | **−0.216** | 0.68 | −8.81% | −46.27% | 0% |

Profit factor below 1.0 on every symbol: gross losses exceed gross wins before
costs are even considered.

**The strategies beat buy-and-hold on all three, and this means nothing.** The
window was a bear market — BTC fell 17%, SOL fell 46%. Any system that sits in
cash most of the time "beats" a falling asset. That is non-participation, not
edge. The honest ranking is:

```
cash (0%)  >  strategies (−9%)  >  buy and hold (−17% to −46%)
```

You would have finished ahead by leaving the money alone.

The one genuine positive: **maximum drawdown of ~10% against buy-and-hold's
54–79%**. The risk controls — position sizing from stop distance, the caps, the
kill switch — did exactly their job across 538 trades. They are sound. They were
controlling risk on a system with no edge.

## Walk-forward: the decisive test

Parameters selected on a training window, validated on a second, then scored
once on data never touched. 44 rolling windows per symbol.

| Symbol | OOS trades | OOS expectancy (R) | Profit factor | Verdict |
|---|---|---|---|---|
| BTC/USDT | 331 | −0.373 | 0.49 | `no_out_of_sample_edge` |
| ETH/USDT | 364 | −0.220 | 0.66 | `no_out_of_sample_edge` |
| SOL/USDT | 370 | −0.307 | 0.62 | `no_out_of_sample_edge` |

**1,065 out-of-sample trades. All negative.** That is a large enough sample that
luck is not a plausible explanation.

## Why this is not an overfitting problem

This is the most informative part of the result. Compare the three stages:

| Symbol | Train | Validate | Out-of-sample |
|---|---|---|---|
| BTC | −0.360 | −0.371 | −0.373 |
| ETH | −0.220 | −0.260 | −0.220 |
| SOL | −0.316 | −0.329 | −0.307 |

Overfitting looks like *strong in-sample, weak out-of-sample*. That is not this.
The numbers are negative and nearly identical at every stage.

There is no edge that failed to generalise. **There was never an edge.** No
amount of parameter tuning reaches a positive number from here, because tuning
recovers a signal that is being obscured — it cannot manufacture one that is
absent.

## The cost hurdle

Every trade must clear its own costs before it earns anything:

```
hurdle (in R) = round-trip cost × position notional ÷ risk amount
              = round-trip cost % ÷ stop distance %      (the same thing)
```

Round-trip cost at the defaults (10bps taker each way, 5bps slippage, 4bps
spread) is **0.34%**. With risk at 0.5% of equity and positions bound by the 20%
notional cap, that gives **+0.136 R per trade** — equivalent to a stop about 2.5%
wide. The evaluation script computes this from the live configuration rather than
hard-coding it, so it stays correct when the settings change.

Strip the costs back out to see the raw signal quality:

| Symbol | Net expectancy | + costs | **Gross** |
|---|---|---|---|
| BTC | −0.391 | +0.136 | **−0.255** |
| ETH | −0.194 | +0.136 | **−0.058** |
| SOL | −0.216 | +0.136 | **−0.080** |

**Even with zero fees and zero slippage, all three still lose.** Costs are not
the binding constraint. The signals have negative predictive value on their own.
Cheaper execution does not rescue this, which rules out a whole category of
"fixes".

Note also how the hurdle scales, because it explains why short-term trading is
structurally hard:

| Stop distance | Cost hurdle |
|---|---|
| 1% (scalping) | 0.34 R |
| 2.5% (this system) | 0.14 R |
| 3% | 0.11 R |
| 10% (swing) | 0.03 R |

The tighter the stop, the larger the share of every trade taken by the exchange.

## What was tested and rejected

Six strategies, all evaluated on the same footing:

| Strategy | Idea |
|---|---|
| `trend_following` | ADX + EMA stack + MACD continuation |
| `ema_momentum` | Fast/slow EMA cross with RSI and volume confirmation |
| `breakout` | Donchian channel break with volume expansion |
| `mean_reversion` | Bollinger band fade inside a range |
| `volatility_breakout` | Expansion after a squeeze |
| `short_term_reversal` | Fade a stretched multi-bar fall, maker entry |

`short_term_reversal` was added last and deliberately: short-horizon reversal is
the one short-term effect with genuine academic support (Jegadeesh 1990 and the
literature after it), unlike the indicator crossovers that make up the rest. It
was given maker-order entries to halve its cost hurdle.

**It did not help.** BTC out-of-sample expectancy moved from −0.345 to −0.373 —
slightly worse. The one evidence-backed idea in the set made no difference.

Also tested and found not to be the problem:

- **Execution costs.** See above: negative gross, before any cost.
- **Sample size.** An early run on 33 days and ~25 trades per symbol was
  correctly rejected as meaningless; the full run at 20 months agrees with it.
- **Timeframe.** The cost arithmetic argues against going shorter, not for it.

## What the result is not

**Not a statement about the software.** The platform works. 560 tests pass. The
data gates, risk engine, sizing, execution, position monitoring and kill switch
all behaved correctly across more than a thousand trades. A bot that runs
flawlessly and loses money is still a bot that loses money — but the machinery
is reusable and sound.

**Not a statement about the AI layer.** The LLM was disabled (`AI_ENABLED=false`)
for every test, and the backtester does not replay it by design. All figures come
from the deterministic layer. This does not change the conclusion: the AI can
only *veto* a candidate the strategies already produced, so it cannot create an
edge that is not there.

**Not a claim that algorithmic trading cannot work.** It is a claim that *these*
strategies, on *these* symbols, at *this* timeframe, do not.

**Not final for all time.** It is one 20-month window. It happened to be a bear
market, which is exactly when trend-following struggles most. A bull-market
window might score differently — though the negative gross expectancy suggests
not much.

## The one thing that did measure positive: funding carry

Tested separately, and it is not a strategy — it is arithmetic on Binance's
published funding history. On perpetual futures, longs pay shorts a funding fee
when positioning is lopsided. Hold spot and short the perp against it and the
price moves cancel, leaving the funding.

Same 625-day window, 1,875 funding periods per symbol:

| Symbol | Periods paid | Total collected | Net/yr on notional | **Net/yr on capital (3x)** | Worst stretch |
|---|---|---|---|---|---|
| BTCUSDT | **81.3%** | +7.14% | +4.00% | **+3.00%** | −0.41% |
| ETHUSDT | **77.4%** | +6.11% | +3.40% | **+2.55%** | −0.60% |
| SOLUSDT | 56.4% | −0.64% | −0.55% | −0.41% | −2.62% |

**This is real.** 81% of 1,875 periods paid, with a worst drawdown of 0.41%
against 54–79% for holding the asset. That is a mechanism working, not luck, and
it is the only positive result in this repository.

**But read the capital column, not the headline.** Funding accrues on notional
while the trade ties up spot capital *and* futures margin. Per $10,000 of
notional at 3x on the short: $10,000 spot + $3,333 margin = $13,333 deployed to
earn $400. That is **3.0%**, not 4%.

Raising leverage improves that number and shortens the distance to ruin:

| Short leverage | Return on capital | Rally that wipes the short leg |
|---|---|---|
| 2x | 2.67% | ~50% |
| 3x | 3.00% | ~33% |
| 5x | 3.33% | ~20% |
| 10x | 3.64% | ~10% |

Bitcoin has moved 10% in a day more than once. A liquidated short leaves you
long spot into the rally that killed it — which is how this trade actually ends,
and nothing in the funding history can show it.

**The comparison that decides it: short-dated government bonds or a savings
account.** If risk-free pays roughly 3%, this earns the same while adding
exchange counterparty risk, liquidation risk, daily management and a taxable
event every eight hours. Same return, more risk, more work. That is a worse
trade however smooth the funding looks.

SOL did not pay at all, which is worth noting: the effect depends on persistent
long demand and is not a property of crypto in general.

**Independently replicated, and independently killed.** An unrelated researcher
(`EstebanSP23/crypto_systematic_research`) ran the same trade over a longer
window — Jan 2022 to May 2026, 4.4 years against our 625 days — and measured
**+3.13% APY** net. We measured **+3.00%**. They found 83.9% of funding events
positive; we found 81.3%. Different code, different window, same answer, which
is about as good as external validation gets.

They killed it, for the reason stated above made concrete: **USDT lending pays
~6%**, so carry earns roughly half of the no-effort alternative. Their threshold
variants — only carry when funding is high — did *worse* (+1.13%, +0.87%,
+0.56%), because each entry and exit pays fees that the funding never recovers.

Treat funding carry as dead at retail size. It is a real mechanism that does not
pay enough to cover what it costs to run.

Reproduce with:

```bash
python scripts/funding_carry.py --days 625
```

## Independent replication: two outside repositories

Two third-party crypto research repositories were audited against this work.
Neither was written by us, and both were found by someone else.

### `Gotodataru/binance-futures-backtest`

Methodologically sound: documented no-lookahead (`merge_asof` backward, entry on
the next bar's open), walk-forward 6-month train / 2-month test, and an explicit
execution model of 0.04% commission plus 0.01% slippage per side. Three
strategies — funding mean reversion, taker-pressure momentum, OI/price
divergence — across 10 symbols on 3 years of Binance data.

Its own committed `metrics_per_symbol.csv` files:

| Strategy | Symbols positive | Mean return |
|---|---|---|
| Funding MR | BTC only: **−0.23%**, Sharpe −1.01, PF 0.62 | — |
| Taker momentum | **1 / 10** | −0.28% |
| OI divergence | **3 / 10** | −0.22% |

**17 of 21 symbol-runs negative.** The README headlines "XRPUSDT Sharpe +1.58"
for the first strategy, but XRP is absent from that strategy's committed CSV and
is the *worst* symbol in the other two (−3.59 and −1.81 Sharpe). Read the data
files, not the README.

The relevance: an independent author, with correct walk-forward and real fees,
on different strategies, reached the same conclusion this repository did. That
is evidence the harness is measuring the market rather than a bug in our code.

### `EstebanSP23/crypto_systematic_research`

Publishes a "kill list" of 8 rejected strategies with reasons — rare and good
practice. Its funding-carry kill is discussed above. Its recurring kill reasons
are worth internalising: curve-fitting to a favourable regime, fee drag on fast
timeframes, and positive return masking an undeployable drawdown.

Its one surviving, live-deployed strategy is where the discipline breaks. A
grep of `01_quattro_donchian/backtest.py` for fees, slippage or commission
returns **nothing**; P&L is `sum((price - entry_price) * size)`, raw price
difference, on a *perpetual futures* strategy paying funding every 8 hours and
held 3.2 days on average. Three of the repo's four scripts model costs. The one
that does not is the one carrying the +1,107% headline chart.

In fairness: the fee drag is roughly 0.03 R per unit round-trip against a
+13.4 R average winner, so costs alone probably would not kill it. The decisive
problems are statistical — **94 trades, of which 11 produce most of the P&L**,
an effective sample size near 11; 2% risk per unit compounding on a 12× growing
account, so late trades are sized 12× early ones; no buy-and-hold benchmark
computed anywhere for a long-only BTC strategy; and `MAX_LEVERAGE = 20`. Live
since May 2026, with no live results published.

### What both confirm

Costed, walk-forward, honestly-benchmarked crypto strategy research keeps
returning the same answer. Three independent sources now agree. The absence of
an edge in this repository is not a defect in this repository.

## What would have to be different

Tuning these parameters is the trap. With 65 features and enough attempts you
can always produce a green backtest; the walk-forward exists to catch that, and
tuning hard enough to beat the walk-forward is just fitting the walk-forward.

A genuine edge generally comes from a structural reason someone is willing to
lose money to you:

- **market making** — earning the spread for providing liquidity;
- **funding-rate carry** — perpetual futures paying holders when positioning is
  lopsided;
- **cross-exchange arbitrage** — the same asset priced differently;
- **liquidation cascades** — forced selling overshooting.

None of these is a pattern drawn on a price chart. EMA crossovers and RSI on BTC
hourly are the most-traded ideas that exist, which is precisely why the measured
gross expectancy is −0.255 R.

Any future idea should arrive with a reason *why* it should work, and then be put
through this same harness before anything else happens.

## Reproducing this

```bash
# .env: MARKET_DATA_PROVIDER=ccxt
cd python-service && uvicorn app.main:app --port 8000

# second terminal
python scripts/evaluate_strategies.py --api-key "$SERVICE_API_KEY" --limit 15000
```

Takes a few minutes. The script refuses to run against synthetic data unless
forced, computes the cost hurdle from the live configuration rather than a
constant, reports buy-and-hold and lending-yield baselines, flags any symbol with
too few trades to be meaningful, and is written to be able to say there is no
edge.

`--lending-apy` overrides the benchmark rate (default `BENCHMARK_YIELD_APY`,
0.04) without re-running the backtests, so `--lending-apy 0.06` re-scores every
symbol against a 6% bar and `--lending-apy 0` reproduces the old cash floor.

Exact figures will drift as the window moves. The conclusion should not.
