# AI crypto trading system

An autonomous, risk-managed crypto trading system. **n8n** orchestrates *when*
things happen; a **Python service** does every calculation that money depends on.
An LLM sits on top as a reviewer that can only ever say *no*.

> **Read this first.** The system runs in **paper mode** and cannot place a real
> order until three independent switches are set *and* credentials exist. Nothing
> in this repository is evidence that the strategies are profitable — no live
> results exist, and the backtests included run on synthetic data. Treat it as an
> engineering platform for research, not as a money printer. See
> [Honest limitations](#honest-limitations).

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Safety model](#safety-model)
- [Quick start](#quick-start)
- [Running without Docker](#running-without-docker)
- [Verifying it works](#verifying-it-works)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)
- [Honest limitations](#honest-limitations)
- [Documentation](#documentation)

---

## What it does

Every five minutes, for each configured symbol:

1. **Collect** candles from the exchange and refuse to go further if they are
   stale, gappy, too few, or internally inconsistent.
2. **Compute features** — 65 columns of indicators across four timeframes, every
   one of them shifted so no value uses information from the future.
3. **Classify the regime** — trend direction and strength, volatility state, and a
   Kaufman efficiency ratio that separates a real trend from an oscillation that
   merely registers as one.
4. **Run the strategies** that are permitted in that regime. Each returns a
   complete trade idea or nothing: direction, entry, stop, target, invalidation.
5. **Ask the LLM to review** the best candidate, if one exists. It may confirm or
   veto. It cannot change the direction, the size, the stop or the target.
6. **Put it through the risk engine** — 22 deterministic checks, then position
   sizing from the stop distance. If it passes, the engine issues a *single-use
   approval* bound to the exact trade.
7. **Execute** against the paper exchange, which charges spread, slippage and fees.
8. **Monitor** every open position each minute: mark, trail the stop, move to
   breakeven, take partials, exit on stop/target/time, and trip the kill switch if
   the account limits are breached.

## Architecture

```
  ┌──────────────────────── n8n (orchestration) ───────────────────────┐
  │  9 workflows: schedules, chaining, retries, alerting, escalation    │
  └───────────────────────────────┬────────────────────────────────────┘
                                  │  HTTP + X-API-Key
  ┌───────────────────────────────▼────────────────────────────────────┐
  │                  Python service (FastAPI, sync)                     │
  │                                                                     │
  │   data ▸ indicators ▸ features ▸ regime ▸ strategies ▸ candidate    │
  │                                              │                      │
  │                                        ai (veto only)               │
  │                                              │                      │
  │                                  ┌───────────▼──────────┐           │
  │                                  │   RISK ENGINE        │ ◀── final │
  │                                  │   sizing + approval  │   authority│
  │                                  └───────────┬──────────┘           │
  │                                              │ approval_id          │
  │                              execution ▸ paper exchange ▸ portfolio │
  └───────────────────────────────┬────────────────────────────────────┘
                                  │
                        ┌─────────▼─────────┐
                        │  PostgreSQL       │  candles, features, signals,
                        │                   │  risk decisions, orders,
                        │                   │  positions, trades, events
                        └───────────────────┘
```

The split is deliberate. Indicator maths, position sizing and order placement
belong in tested Python, not in n8n Code nodes that no test ever runs. n8n owns
scheduling, fan-out, retries and alerting, which is what it is good at.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety model

Four independent properties, each enforced in code and covered by tests:

**1. Live trading is disarmed by default and fails closed.**
Three switches *and* credentials must all be present:

```
TRADING_MODE=live
ENABLE_LIVE_TRADING=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
EXCHANGE_API_KEY / EXCHANGE_API_SECRET
```

Any one missing and `effective_mode` is `paper`. Adding exchange keys on their own
changes nothing. `/health` always lists every reason live trading is blocked, and
the live adapter re-checks arming on every mutating call, so a disarm takes effect
immediately even on an adapter that was built while armed.

**2. The AI cannot bypass the risk engine.**
Not by convention — structurally. `POST /risk/check` returns an `approval_id` bound
to a SHA-256 fingerprint of *(symbol, direction, entry, stop, quantity, mode)*.
Execution refuses any order without an approval that is valid, unexpired, unused,
and fingerprint-matching. Change one field of the trade after approval and the
order is rejected. The AI never sees an approval and cannot mint one.

**3. Risk is sized, not guessed.**
`RISK_PER_TRADE=0.005` means *0.5% of equity is lost if the stop is hit* — it is
not a position size. Quantity is derived from the stop distance, then reduced by
the position cap, the portfolio exposure cap and available cash, and always
rounded **down**. The response says which cap bound it.

**4. The kill switch is real.**
Daily loss, drawdown and consecutive-loss limits halt new entries and require a
manual, confirmed reset. A halted bot refuses entries at the risk layer, so every
path into execution is closed at once, not just the scheduled one.

## Quick start

```bash
git clone <this repo> && cd h
cp .env.example .env
```

Edit `.env` and set two values — everything else has a safe default:

```bash
# a shared secret n8n uses to call the Python service
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> SERVICE_API_KEY
# n8n's credential-encryption key; keep it stable or stored credentials break
python -c "import secrets; print(secrets.token_hex(24))"       # -> N8N_ENCRYPTION_KEY
```

Then:

```bash
docker compose up -d
docker compose logs -f python-trading-service
```

- API: <http://localhost:8000> — interactive docs at `/docs`
- Dashboard: <http://localhost:8000/?key=YOUR_SERVICE_API_KEY>
- n8n: <http://localhost:5678>

Import and activate the workflows following [n8n/README.md](n8n/README.md). Until
you do, the Python service is idle: it has no scheduler of its own, by design.

## Running without Docker

Useful for development and for the test suite.

```bash
cd python-service
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export DATABASE_URL="sqlite:///$PWD/local.db"   # or a real Postgres URL
export MARKET_DATA_PROVIDER=synthetic           # offline; no exchange needed
export SERVICE_API_KEY=dev-key
export AI_ENABLED=false AI_REQUIRED_FOR_ENTRY=false

alembic upgrade head
uvicorn app.main:app --port 8000
```

`MARKET_DATA_PROVIDER=synthetic` generates deterministic OHLCV offline. It is for
wiring tests only — it is not market data and says nothing about real behaviour.

## Verifying it works

Do not take this README's word for any of it. Run the checks.

```bash
cd python-service
python -m pytest          # unit, integration and failure-injection tests
ruff check .
```

Then drive a live instance end to end:

```bash
python scripts/paper_smoke_test.py \
  --base-url http://localhost:8000 --api-key "$SERVICE_API_KEY"
```

The smoke test exercises health and safety posture, authentication, the data
quality gates, the full analysis→risk→execution pipeline, a forged-approval
rejection, portfolio accounting, a kill-switch drill, reporting and a backtest. It
prints what it actually observed, including "no trade was taken" — a normal and
correct outcome when no setup qualifies.

Most recent full run is recorded in [docs/RUNBOOK.md](docs/RUNBOOK.md#verification-log).

## Configuration

Everything is environment variables; `.env.example` documents every one with its
default. Nothing is hard-coded, and no credential is ever written to a workflow
file, a log line or the database — the JSON logger redacts secret-looking keys.

The settings you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_SYMBOLS` | `BTC/USDT,ETH/USDT,SOL/USDT` | Spot pairs to trade |
| `PRIMARY_TIMEFRAME` | `1h` | Timeframe decisions are made on |
| `RISK_PER_TRADE` | `0.005` | Fraction of equity **risked** per trade |
| `MAX_POSITION_PCT_EQUITY` | `0.20` | Cap on one position's notional |
| `MAX_OPEN_POSITIONS` | `3` | Concurrent positions |
| `MAX_DAILY_LOSS_PCT` | `0.02` | Halts new entries for the day |
| `MAX_DRAWDOWN_PCT` | `0.10` | Trips the kill switch |
| `MIN_RISK_REWARD` | `1.5` | Minimum reward-to-risk to accept |
| `AI_ENABLED` | `true` | Whether the LLM reviewer is consulted |
| `AI_REQUIRED_FOR_ENTRY` | `true` | If the LLM is down, refuse rather than trade blind |
| `MARKET_DATA_PROVIDER` | `ccxt` | `ccxt` for real public data, `synthetic` offline |

## Repository layout

```
.
├── docker-compose.yml         postgres, redis (optional), python service, n8n
├── .env.example               every setting, every default safe
├── scripts/
│   ├── generate_n8n_workflows.py   regenerates the workflow JSON
│   ├── paper_smoke_test.py         end-to-end check against a running service
│   └── init-databases.sh           creates the n8n database on first boot
├── n8n/workflows/             9 importable workflow definitions
├── docs/                      architecture, API, runbook, live-trading checklist
└── python-service/
    ├── app/
    │   ├── core/              settings, logging, errors, events
    │   ├── data/              providers, validation gates, repositories
    │   ├── indicators/        trend, momentum, volatility, volume, structure
    │   ├── features/          multi-timeframe feature assembly
    │   ├── regime/            regime classification
    │   ├── strategies/        five strategies behind one interface
    │   ├── ai/                providers, prompt, strict parsing, veto policy
    │   ├── risk/              checks, sizing, single-use approvals
    │   ├── execution/         exchange abstraction, paper engine, live adapter
    │   ├── portfolio/         valuation, exit rules, reconciliation
    │   ├── backtesting/       event-driven engine, walk-forward harness
    │   ├── analytics/         performance metrics
    │   ├── api/               FastAPI routers, auth, error mapping
    │   └── reports/           daily report
    ├── migrations/            Alembic
    └── tests/                 unit, integration, failure injection
```

## Honest limitations

Stated plainly, because a trading system that oversells itself is dangerous.

- **No live track record exists.** Nothing here has traded real money. There is no
  evidence of profitability, and none should be inferred from the code, the
  backtester or this document.
- **The bundled backtests use synthetic data.** They verify wiring, accounting and
  no-look-ahead discipline. Their P&L figures carry **no** information about real
  market performance. Point the backtester at real exchange history before drawing
  any conclusion, and even then expect backtests to overstate live results.
- **The live execution path is structural, not finished.** `LiveExchangeAdapter`
  exists so that going live is a review step rather than a rewrite, but order
  reconciliation after restart, real partial-fill streams and exchange-specific
  error handling are **not** complete. See
  [docs/LIVE_TRADING_CHECKLIST.md](docs/LIVE_TRADING_CHECKLIST.md).
- **Backtests do not replay the LLM** and do not model spread or order-book
  liquidity (no historical data for either), so live rejections will be strictly
  more frequent than backtested ones.
- **Spot only, long only, no leverage.** `MAX_LEVERAGE` must stay `1`; the config
  refuses anything else. Short signals are generated and logged but cannot be
  executed.
- **The LLM is a reviewer, not a strategy.** It can only veto. Its usefulness is
  measured, not assumed — `GET /ai/evaluation` compares outcomes with and without
  its involvement, and it may well turn out to add nothing.
- **Docker Compose has been validated statically, not run.** `docker compose
  config` parses; no Docker daemon was available in the environment where this was
  built, so the container boot path itself is unverified. Everything else was run
  locally against SQLite and the synthetic provider.

## Documentation

| Document | What is in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the layers fit together and why |
| [docs/API.md](docs/API.md) | Every endpoint, with request and response shapes |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Daily operation, diagnosis, incident response |
| [docs/LIVE_TRADING_CHECKLIST.md](docs/LIVE_TRADING_CHECKLIST.md) | What must be true before real money |
| [n8n/README.md](n8n/README.md) | The nine workflows and how to import them |

## Licence and disclaimer

This software is provided as-is, for research and education. Cryptocurrency
trading carries a substantial risk of loss. You are responsible for any money you
put at risk with it. Do not trade money you cannot afford to lose.
