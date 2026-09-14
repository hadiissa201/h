# API reference

The Python service exposes 52 endpoints. This document groups them by the workflow
that drives them and shows real request/response shapes.

An always-current, interactive version is served by the running service at
**`/docs`** (Swagger UI), with the raw schema at `/openapi.json`. Where the two
disagree, the running service is right.

## Contents

- [Conventions](#conventions)
- [Health and dashboard](#health-and-dashboard)
- [Market data](#market-data)
- [Analysis](#analysis)
- [AI review](#ai-review)
- [Risk](#risk)
- [Execution](#execution)
- [Portfolio and positions](#portfolio-and-positions)
- [Performance and reporting](#performance-and-reporting)
- [Bot control](#bot-control)
- [Backtesting](#backtesting)

---

## Conventions

**Authentication.** Every endpoint except `/health`, `/health/live` and the
dashboard requires the shared secret:

```
X-API-Key: <SERVICE_API_KEY>
```

The dashboard also accepts `?key=<SERVICE_API_KEY>` so it can be opened in a
browser. Missing or wrong key → `401`. If `SERVICE_API_KEY` is empty the API is
open; the config refuses that when `ENVIRONMENT=production`.

**Money is a string.** Decimals serialise as JSON strings (`"116957.03918616"`),
not floats, so no precision is lost in transit. Parse them as decimals, not as
doubles.

**Errors share one flat envelope.**

```json
{
  "error_code": "NOT_FOUND",
  "detail": "position does-not-exist not found",
  "context": {}
}
```

| Status | `error_code` | Meaning |
|---|---|---|
| 400 | `VALIDATION_ERROR` | The request was understood but is not valid |
| 401 | `UNAUTHORIZED` | Missing or wrong API key |
| 403 | `RISK_REJECTED`, `LIVE_TRADING_BLOCKED` | Refused by a safety rule |
| 404 | `NOT_FOUND` | No such order, position or run |
| 409 | `CONFLICT` | State does not permit this (e.g. closing a closed position) |
| 422 | `REQUEST_VALIDATION_ERROR` | Payload failed schema validation; `context.errors` lists fields |
| 503 | `EXCHANGE_ERROR`, `PROVIDER_ERROR` | A dependency failed |

**Timestamps** are UTC ISO-8601.

**Collections come wrapped in an envelope**, never as a bare array, so a response
has room to grow a `count` or a cursor without breaking callers:

```
GET /positions          -> {"count": 1, "positions": [...]}
GET /orders             -> {"orders": [...]}
GET /risk/decisions     -> {"decisions": [...]}
GET /bot/events         -> {"events": [...]}
GET /performance/equity-curve -> {"points": [...]}
```

With `jq`, that means `.positions[]`, not `.[]`.

---

## Health and dashboard

### `GET /health`

Unauthenticated, so orchestrators can probe it. Always states the mode and every
reason live trading is blocked.

```json
{
  "status": "ok",
  "version": "0.1.0",
  "mode": "paper",
  "live_trading_armed": false,
  "bot_status": "RUNNING",
  "timestamp": "2026-09-14T11:20:35.603951Z",
  "components": [
    {"name": "database",      "healthy": true, "detail": "ok", "latency_ms": 2.11},
    {"name": "market_data",   "healthy": true, "detail": "synthetic: BTC/USDT @ 116874.92289", "latency_ms": 58.06},
    {"name": "exchange:paper","healthy": true, "detail": "paper engine ready", "latency_ms": null},
    {"name": "llm:disabled",  "healthy": true, "detail": "AI disabled by configuration", "latency_ms": null}
  ],
  "live_mode_blockers": [
    "TRADING_MODE is not 'live'",
    "ENABLE_LIVE_TRADING is not true",
    "LIVE_TRADING_CONFIRMATION does not match the required phrase",
    "exchange API credentials are not configured"
  ]
}
```

`status` is `ok`, `degraded` (a non-critical component is down) or `unhealthy`.

### `GET /health/live`

Liveness only. Returns 200 whenever the process is up, without touching the
database — a readiness probe that hits the database will restart the container
during a database blip and make an outage worse.

### `GET /` and `GET /dashboard/summary`

A single-page HTML dashboard and its JSON backing. Both need the key
(`?key=...` works for the browser).

---

## Market data

### `POST /market/collect`

Fetch, validate and store candles. Driven by workflow 01.

```json
{"symbols": ["BTC/USDT"], "timeframes": ["5m", "1h"], "limit": 400}
```

```json
{
  "symbols": [{
    "symbol": "BTC/USDT",
    "fetched_at": "2026-09-14T11:20:41.080921+00:00",
    "provider": "synthetic",
    "timeframes": {
      "1h": {
        "bars": 400,
        "last_candle": "2026-09-14T10:00:00+00:00",
        "staleness_seconds": 0.0,
        "data_ok": true,
        "issues": [],
        "last_price": 116668.68921179403
      }
    },
    "data_ok": true,
    "errors": []
  }]
}
```

**`data_ok: false` means do not trade this symbol.** Workflow 01 branches on it.

### `GET /market/{symbol}/candles`

Stored candles. Query: `timeframe`, `limit`, `start`, `end`.

### `GET /market/{symbol}`

Ticker, order book summary and the data-quality verdict in one call.

---

## Analysis

### `POST /features`

Computes the feature matrix for a symbol/timeframe. `{"symbol": "...", "timeframe": "1h"}`.

### `POST /regime`

Regime classification with the metrics behind it.

```json
{
  "regime": "strong_bull_trend",
  "trend_state": "strong_up",
  "volatility_state": "normal",
  "confidence": 0.99,
  "is_abnormal": false,
  "abnormal_reasons": [],
  "metrics": {
    "adx": 66.08, "di_spread": 41.16, "ema_alignment": 1.0,
    "trend_slope": 0.0017, "efficiency_ratio": 0.4228,
    "atr_pct": 0.00498, "atr_pct_rank": 0.515, "vol_ratio": 1.25,
    "bb_width": 0.0418, "structure": 1.0, "last_bar_move_pct": -0.0023
  },
  "notes": ["higher timeframe 4h agrees"]
}
```

`regime: "unknown"` means not enough warm data — strategies will not fire.

### `POST /strategy/evaluate`

Runs every enabled strategy allowed in the current regime and returns each signal
plus the selected candidate. A signal always carries its own invalidation:

```json
{
  "strategy": "trend_following",
  "signal": "BUY",
  "confidence": 0.95,
  "entry": "116668.68921179",
  "stop_loss": "115506.6381212",
  "take_profit": "120154.84248359",
  "reason": "Uptrend continuation: ADX 66.1, EMA stack bullish, +DI>-DI, MACD histogram positive, RSI 75.6",
  "invalidation_condition": "close below 115506.6381 (2.0x ATR)",
  "trailing_stop_atr_multiple": 3.0,
  "breakeven_at_r": 1.0,
  "partial_exit_at_r": 2.0,
  "partial_exit_fraction": 0.5
}
```

### `GET /strategies`

Each strategy's description, permitted regimes, required features and default
parameters.

### `POST /pipeline/run`

The whole chain — analysis, AI review, risk, execution — in one call, for one or
more symbols. This is what the smoke test drives and what you want when testing by
hand. Accepts `dry_run: true` to stop before execution.

```json
{"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h"}
```

```json
{
  "mode": "paper",
  "dry_run": false,
  "traded": 1,
  "outcomes": [
    {"symbol": "BTC/USDT", "stage_reached": "executed", "traded": true,  "reason": null},
    {"symbol": "ETH/USDT", "stage_reached": "strategy", "traded": false,
     "reason": "no strategy produced an actionable signal"}
  ]
}
```

`stage_reached` is one of `data`, `features`, `regime`, `strategy`, `ai`, `risk`,
`portfolio`, `executed`, and `reason` says why it stopped. Both are recorded, so
"why did nothing trade today" is always answerable.

---

## AI review

### `POST /ai/context`

The exact structured snapshot that would be sent to the model, without calling it.
Useful for reviewing prompts and for debugging cost.

### `POST /ai/evaluate`

Calls the provider and returns the parsed decision plus the risk proposal it
produced. A malformed reply comes back as `HOLD` — it is never retried into
agreement.

### `GET /ai/decisions`

Recent decisions with latency, token counts and estimated cost.

### `GET /ai/evaluation`

**Is the AI layer earning its place?** Compares outcomes of trades it confirmed
against the deterministic baseline. It is entirely possible for this to show the
reviewer adds nothing; that is the point of measuring it.

---

## Risk

### `POST /risk/check`

The gate. Runs every check, sizes the position, and — only on approval — issues a
single-use approval.

```json
{
  "symbol": "ETH/USDT", "direction": "BUY",
  "entry": 13416.710046, "stop_loss": 13014.208745, "take_profit": 14490.046850,
  "confidence": 0.85, "strategy": "trend_following", "timeframe": "1h",
  "regime": "strong_bull_trend", "regime_abnormal": false, "data_quality_ok": true
}
```

```json
{
  "decision": "APPROVED",
  "approval_id": "ra_74f6a5fadd6149cabff1c5bc",
  "expires_at": "2026-09-14T11:37:15.077528Z",
  "sizing": {
    "equity": "9996.59085010",
    "risk_pct": "0.005",
    "risk_amount": "49.98295425",
    "entry": "13416.710046",
    "stop_loss": "13014.208745",
    "stop_distance": "402.501301",
    "stop_distance_pct": "0.02999999997167711020830389346",
    "raw_quantity": "0.1241808514054964508052608754",
    "quantity": "0.12418",
    "notional": "1666.08705351",
    "notional_pct_equity": "0.1666655241264909274132541803",
    "effective_risk_amount": "49.98261156",
    "effective_risk_pct": "0.004999965719263182950823048010",
    "capped_by": [],
    "rejected_reason": null
  },
  "checks": [
    {"name": "bot_running", "passed": true, "detail": "bot status is RUNNING",
     "value": "RUNNING", "limit": "RUNNING", "blocking": true}
  ],
  "reasons": []
}
```

Note the relationship between the numbers, because it is the single most
misunderstood setting in the system: equity is ~9,997, `risk_pct` is 0.005, so
`risk_amount` is ~50 — **that is what is lost if the stop is hit.** The position
itself is ~1,666 notional, about 17% of equity. `RISK_PER_TRADE` sizes the loss,
not the position.

`raw_quantity` is the unrounded result; `quantity` is rounded **down** to the
symbol's step size, which is why `effective_risk_amount` is fractionally below
`risk_amount` and never above it.

Omitting `regime` makes the `regime_known` check fail and the proposal is rejected
— the risk engine will not size a trade in a market it cannot classify.

`decision` is `APPROVED` or `REJECTED`. **A rejection returns `approval_id: null`**
— there is no partial approval.

`capped_by` names every cap that bound the size. An empty list means the stop
distance alone determined it.

The 22 checks, all blocking:

```
bot_running              cooldown                market_data_quality
market_conditions_normal regime_known            direction_allowed
symbol_active            stop_loss_present       stop_distance_min
stop_distance_max        min_confidence          min_risk_reward
max_open_positions       max_positions_per_symbol max_daily_loss
max_drawdown             equity_positive         max_spread
min_liquidity            position_sizing         risk_within_budget
sufficient_cash
```

### `GET /risk/limits`

The configured limits, as the engine sees them.

```json
{
  "risk_per_trade": "0.005", "max_position_pct_equity": "0.20",
  "max_portfolio_exposure_pct": "0.50", "max_open_positions": 3,
  "max_positions_per_symbol": 1, "max_daily_loss_pct": "0.02",
  "max_drawdown_pct": "0.10", "consecutive_loss_limit": 3,
  "cooldown_minutes": 120, "min_confidence": "0.60", "min_risk_reward": "1.5",
  "max_spread_bps": "15", "min_24h_quote_volume": "5000000",
  "require_stop_loss": true, "min_stop_distance_pct": "0.003",
  "max_stop_distance_pct": "0.15", "abnormal_price_move_pct": "0.10",
  "allow_short": false
}
```

### `GET /risk/state`

Live account risk: equity, exposure, drawdown, daily P&L, consecutive losses,
cooldown, remaining capacity.

### `GET /risk/decisions`

The audit trail — every proposal, every check result, approved or not.

### `POST /risk/safety-check`

Runs the account-level limits (daily loss, drawdown, loss streak) and halts the bot
if any is breached. Workflow 06 calls it each minute.

---

## Execution

### `POST /orders`

Place the approved entry. **This is the only way a position is opened.**

```json
{"risk_approval_id": "ra_0180bd1e99084c6c8cb08c68",
 "strategy": "trend_following", "regime": "strong_bull_trend"}
```

Every failure mode is a `403`:

```json
{"error_code": "RISK_REJECTED",
 "detail": "risk approval ra_forged_approval_id not found", "context": {}}
```

— unknown, expired, already consumed, or fingerprint mismatch (any field of the
trade changed since approval).

Retrying with the same `client_order_id` returns the original order rather than
placing a second one, and does so *before* approval validation, so a network
timeout on a successful order is recoverable.

### `POST /paper/order`

Places an order directly on the paper engine, bypassing strategy and AI but **not**
the risk engine. For testing execution mechanics. Refused outside paper mode.

### `GET /orders`, `GET /orders/open`, `GET /orders/{id}`, `POST /orders/{id}/cancel`

Order listing, lookup and cancellation.

---

## Portfolio and positions

### `GET /portfolio`

Everything about the account in one call.

```json
{
  "mode": "paper",
  "cash": "7996.86091945",
  "positions_value": "1999.72993065",
  "equity": "9996.59085010",
  "starting_equity": "10000.00000000",
  "peak_equity": "10000.00000000",
  "realized_pnl": "0E-8",
  "unrealized_pnl": "-1.40801196",
  "total_pnl": "-3.40914990",
  "drawdown_pct": "0.00034091499",
  "exposure_pct": "0.2000411901053243447479364993",
  "open_positions": 1,
  "fees_paid": "2.00113794",
  "balances": [
    {"currency": "BTC",  "free": "0.01711000", "locked": "0E-8"},
    {"currency": "USDT", "free": "7996.86091945", "locked": "0E-8"}
  ],
  "positions": [ "..." ]
}
```

Read that example carefully: one position is open, nothing has been realised, and
the account is already **down 3.41** on entry costs alone. That is what opening a
position costs, and it is charged in paper mode exactly as it would be live.

`fees_paid` counts fees that have actually left the account, including entry fees
on still-open positions.

### `GET /positions`, `GET /positions/closed`, `GET /positions/{id}`

Each position carries its exit plan and its provenance:

```json
{
  "position_id": "pos_fcd0a6e94e074f1f87893838",
  "symbol": "BTC/USDT", "side": "LONG", "status": "OPEN",
  "quantity": "0.01714", "entry_price": "116957.21546674",
  "initial_risk_amount": "24.86289570",
  "stop_loss": "115506.63812120", "take_profit": "120154.84248359",
  "exit_plan": {
    "trailing_stop_atr_multiple": 3.0, "trailing_stop_price": null,
    "breakeven_at_r": 1.0, "breakeven_applied": false,
    "partial_exit_at_r": 2.0, "partial_exit_fraction": 0.5,
    "partial_exit_done": false, "time_stop_bars": null,
    "invalidation_condition": "close below 115506.6381 (2.0x ATR)"
  },
  "strategy": "trend_following", "regime": "strong_bull_trend",
  "risk_approval_id": "ra_dde7dc53aab6425892e20cd4",
  "max_favorable_price": "116957.21546674",
  "max_adverse_price": "116957.21546674"
}
```

`risk_approval_id` is present on every position: there is no path to a position
that did not pass risk.

### `POST /positions/monitor`

The per-minute job. Marks positions, applies trailing stops, breakeven moves,
partial exits, stop/target/time exits, and runs the account safety checks. Returns
exactly what it did:

```json
{
  "checked": 1,
  "exits": [], "stop_updates": [], "resting_fills": [],
  "stale_symbols": [], "abnormal_symbols": [], "errors": [],
  "equity": 9996.59, "drawdown_pct": 0.00034, "daily_pnl_pct": -0.00034,
  "emergency_triggers": [], "bot_status": "RUNNING"
}
```

A non-empty `emergency_triggers` means the safety checks fired and `bot_status`
will have changed to `HALTED`.

### `POST /positions/{id}/close`

Manual close. `{"fraction": 0.5}` for a partial. `{"reason": "MANUAL", "detail": "..."}`
is recorded on the trade.

### `POST /positions/flatten`

Close everything. Used by the emergency workflow when `EMERGENCY_FLATTEN=true`.

### `GET /portfolio/balances`

Per-currency free and locked balances.

---

## Performance and reporting

### `GET /performance`

Trades, win rate, expectancy in R, profit factor, average win/loss, max drawdown,
Sharpe, fees. Query `start`, `end`, `strategy`, `symbol`.

### `GET /performance/daily`, `GET /performance/equity-curve`, `GET /performance/trades`

Daily aggregates, the equity curve for charting, and the full trade list.

### `POST /reports/daily`

Builds the daily report from stored rows only. `{"include_ai_summary": false}` to
skip the LLM narration.

The response carries both `facts` (structured) and `markdown` (for Slack/email).
The facts include the accounting check:

```json
{"account": {"equity": 9996.59, "fees_paid": 2.0011,
             "reconciliation_error": 0.0, "books_balance": true}}
```

If `books_balance` is false the markdown leads with a warning that every other
figure in the report is unverified.

### `GET /reports/daily/latest`

The most recent stored report.

### `POST /workflow/error`

Where workflow 09 posts n8n failures so they land in the trading database
alongside everything else.

---

## Bot control

### `GET /bot/state`

```json
{
  "status": "RUNNING", "mode": "paper", "live_trading_armed": false,
  "halted_at": null, "halt_reason": null, "requires_manual_reset": false,
  "consecutive_losses": 0, "cooldown_until": null, "trading_allowed": true
}
```

`status` is `RUNNING`, `HALTED` or `PAUSED`.

### `POST /bot/halt`

The kill switch. Halts new entries immediately; existing positions are still
managed by the monitor.

```json
{"reason": "MANUAL", "detail": "why", "source": "operator"}
```

### `POST /bot/reset`

Deliberately awkward: it requires an explicit confirmation string, an operator
name, and records both.

```json
{"confirmation": "RESET", "operator": "your-name", "note": "cause addressed"}
```

### `GET /bot/live-readiness`

An itemised checklist of what is and is not true before live trading. It does not
arm anything — it tells you what is missing.

```json
{
  "ready": false,
  "items": [
    {"key": "paper_mode_default",     "satisfied": true,  "detail": "effective mode: paper"},
    {"key": "backtest_completed",     "satisfied": true,  "detail": "1 stored backtest run(s)"},
    {"key": "walkforward_completed",  "satisfied": false, "detail": "0 stored walk-forward run(s)"},
    {"key": "paper_trades_recorded",  "satisfied": false, "detail": "0 closed trades"},
    {"key": "paper_expectancy_known", "satisfied": false, "detail": "expectancy_r=None"},
    {"key": "kill_switch_exercised",  "satisfied": true,  "detail": "1 recorded emergency event(s)"},
    {"key": "bot_not_halted",         "satisfied": true},
    {"key": "exchange_credentials",   "satisfied": false},
    {"key": "api_auth_enabled",       "satisfied": true},
    {"key": "live_switches",          "satisfied": false}
  ]
}
```

`ready: true` means the mechanical preconditions are met. It is **not** a
recommendation to go live — see [LIVE_TRADING_CHECKLIST.md](LIVE_TRADING_CHECKLIST.md).

### `GET /bot/emergencies`, `GET /bot/events`

Emergency-event history and the system event log.

---

## Backtesting

### `POST /backtest/run`

```json
{
  "data": {"symbol": "BTC/USDT", "timeframe": "1h", "source": "exchange", "limit": 1500},
  "starting_balance": 10000,
  "label": "baseline",
  "persist": true
}
```

`data.source` is `exchange` (fetch history), `csv` (with `csv_path`), or
`synthetic` (with `synthetic_seed`). Optional overrides: `strategies`,
`risk_per_trade`, `taker_fee_bps`, `slippage_bps`, `spread_bps`, `warmup_bars`.

The response carries metrics, the trade list, the equity curve, risk rejections —
and **warnings you should read before believing the metrics**:

```json
{
  "bars": 1292,
  "data_source": "exchange:synthetic",
  "metrics": {"trades": 20, "net_pnl": 94.34, "win_rate": 0.35,
              "max_drawdown_pct": 0.0137},
  "warnings": [
    "a position was still open at the end of the data and was closed at the final close; treat that trade as incomplete",
    "The LLM layer is not replayed in backtests: this measures the deterministic strategy + risk system only.",
    "Spread and liquidity risk filters are not evaluated in backtests (no historical order-book data), so live rejections will be more frequent."
  ]
}
```

### `POST /backtest/walkforward`

Rolling in-sample/out-of-sample windows. The verdict is computed from **pooled**
out-of-sample results, not from an average of per-window metrics — averaging lets
one small lucky window carry the whole verdict.

### `GET /backtest/runs`, `GET /backtest/runs/{id}`

Stored runs and their full detail.
