# Live trading checklist

This document exists because the code refers to it, and the code refers to it
because parts of the system are **deliberately unfinished** for live trading.
Nothing here is boilerplate caution: each item is either a gap in this repository
or a decision only you can make.

> **Current status: not ready.** The live execution path has never been run. Items
> marked ⛔ below are missing functionality, not just unchecked boxes.

---

## Contents

- [How arming actually works](#how-arming-actually-works)
- [Part 1 — engineering gaps that must be closed](#part-1--engineering-gaps-that-must-be-closed)
- [Part 2 — evidence you should demand first](#part-2--evidence-you-should-demand-first)
- [Part 3 — operational readiness](#part-3--operational-readiness)
- [Part 4 — exchange and credential hygiene](#part-4--exchange-and-credential-hygiene)
- [Part 5 — the arming procedure](#part-5--the-arming-procedure)
- [Part 6 — the first week](#part-6--the-first-week)
- [How to stop](#how-to-stop)

---

## How arming actually works

Four things must all be true. Any one missing and `effective_mode` is `paper`,
silently and safely:

```
TRADING_MODE=live
ENABLE_LIVE_TRADING=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
EXCHANGE_API_KEY and EXCHANGE_API_SECRET present
```

`GET /health` lists every unmet condition, always. `GET /bot/live-readiness` gives
the mechanical checklist. Neither of them means you *should* go live — they only
report what is true.

Adding exchange credentials on their own does nothing. That is deliberate: a key
pasted into the wrong `.env` must not start trading.

---

## Part 1 — engineering gaps that must be closed

These are **not done** in this repository. Do not go live until they are.

### ⛔ Order reconciliation after restart

If the service restarts while orders are open, it has no procedure for rebuilding
state from the exchange. `LiveExchangeAdapter.get_positions()` raises
`NotImplementedError` on purpose rather than returning a plausible guess — spot has
no exchange-side positions, so our `positions` table must be reconciled against
exchange balances and order history at startup.

Required: a startup reconciliation that fetches open orders and balances, matches
them to stored positions by `client_order_id`, and **halts the bot** on any
mismatch rather than trading from an unknown state.

### ⛔ Real partial-fill handling

The paper engine models partial fills; the live adapter maps a ccxt order snapshot
once. Real exchanges fill over time, and a position whose size the system has wrong
has a stop loss that is wrong by the same factor.

Required: poll or stream fills until an order is terminal, update position size
from actual fills, and resize or cancel the protective stop accordingly.

### ⛔ Exchange-specific error and rate-limit handling

Every exchange has its own error taxonomy, its own rate limits, and its own
maintenance behaviour. `ccxt` normalises some of this and not enough of it.

Required: classify errors into retryable and terminal for *your* exchange; back off
on rate limits rather than hammering; and treat "unknown order state" as a halt
condition, never as a retry.

### ⛔ Stop-loss orders resting at the exchange

Stops are currently enforced by the position monitor, which runs every minute. If
the service, its container, or its network dies, **nothing is protecting the
position.** A minute is also a long time in crypto.

Required (pick one, and know which you chose): place a real stop order at the
exchange alongside the entry and reconcile it with the monitor; or accept
monitor-only stops and size positions on the assumption that a gap can exceed them.

### ⛔ Time synchronisation

Exchanges reject requests whose timestamp drifts. Required: NTP on the host, and an
alert if drift exceeds the exchange's recv window.

### ☐ Fee and precision accuracy

Paper uses configured fee and step sizes. Required: fetch the real symbol spec
(tick size, step size, minimum notional) and your actual fee tier, and confirm the
rounding rules against a real filled order.

---

## Part 2 — evidence you should demand first

Not of the code — of the **strategies**. The engineering above can be perfect while
the strategies lose money.

- ☐ **Backtests on real exchange history**, not synthetic data. The bundled figures
  are wiring checks and mean nothing about profitability.
- ☐ **Walk-forward validation** (`POST /backtest/walkforward`) with a verdict from
  *pooled* out-of-sample results, across at least one bull, one bear and one
  ranging period.
- ☐ **Paper trading for a meaningful period** — long enough to cover a regime
  change, not a quiet fortnight. At minimum 30 closed trades, which
  `/bot/live-readiness` tracks, but 30 trades is a low bar for statistical
  confidence, not a high one.
- ☐ **Paper expectancy measured and understood.** `GET /performance` gives
  expectancy in R. Negative expectancy in paper will not become positive live —
  live is strictly worse, because real spread, real slippage and real rejections
  are all costs the backtest under-models.
- ☐ **A sober comparison of paper against backtest.** If they disagree, you do not
  understand the system yet. Find out why before risking money.
- ☐ **You can explain every losing trade.** Not rationalise — explain, from the
  stored features, regime and risk decision.

If the evidence is not there, the honest conclusion is not "go live smaller". It is
"do not go live".

---

## Part 3 — operational readiness

- ☐ The kill switch has been **exercised**, not just read about
  (`/bot/live-readiness` tracks this).
- ☐ You know what each `halt_reason` means and what you will do about it
  ([RUNBOOK.md](RUNBOOK.md#the-bot-is-halted)).
- ☐ Alerts reach a human quickly. `NOTIFY_WEBHOOK_URL` is set, and you have
  confirmed a test alert actually arrives.
- ☐ Database backups run automatically and a **restore has been tested**. An
  untested backup is not a backup.
- ☐ Monitoring covers the host, not just the app: disk, memory, and whether the
  container is alive.
- ☐ Someone is on call who can halt the system from their phone.
- ☐ You have decided in advance what daily and total loss will make you stop
  entirely — and written it down where you will see it when it happens.

---

## Part 4 — exchange and credential hygiene

- ☐ API key has **trade** permission only.
- ☐ **Withdrawals are disabled** on the key. Not "unused" — disabled.
- ☐ Key is **IP-allowlisted** to the host running the service.
- ☐ Credentials live in environment variables, never in workflow JSON, never in
  code, never in a commit. `.env` is git-ignored; verify with `git check-ignore .env`.
- ☐ Logs have been checked for leakage. The JSON logger redacts secret-looking
  keys, but confirm it on your own configuration rather than trusting it.
- ☐ You have a tested procedure for rotating the key, including what the system
  does mid-rotation.
- ☐ Only the capital you can afford to lose is in the trading account. Keep the
  rest somewhere the key cannot reach.
- ☐ Two-factor authentication is on the exchange account.

---

## Part 5 — the arming procedure

Only after Parts 1–4 are genuinely complete.

1. **Re-read Part 1.** If any ⛔ item is still open, stop here.

2. **Start small enough that being wrong is cheap.** Deposit the smallest amount
   the exchange and the minimum-notional rules allow you to trade meaningfully.
   Consider lowering `RISK_PER_TRADE` for the first period.

3. **Check readiness:**
   ```bash
   curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/bot/live-readiness | jq
   ```

4. **Set the switches in `.env`:**
   ```
   TRADING_MODE=live
   ENABLE_LIVE_TRADING=true
   LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
   EXCHANGE_API_KEY=...
   EXCHANGE_API_SECRET=...
   ```

5. **Restart and verify the service agrees with you:**
   ```bash
   docker compose restart python-trading-service
   curl -s localhost:8000/health | jq '{mode, live_trading_armed, live_mode_blockers}'
   ```
   `live_mode_blockers` must be empty. If it is not, it tells you exactly what is
   still wrong.

6. **Confirm credentials work before any signal fires:**
   ```bash
   curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/portfolio/balances | jq
   ```
   Real balances mean the key is valid and correctly scoped.

7. **Watch the first trade from start to finish.** Do not walk away. Verify the
   order appeared on the exchange, the fill price is sane, the stop is where the
   system says it is, and the position record matches the exchange.

---

## Part 6 — the first week

- Check every trade individually, every day.
- Compare live fills to paper fills. If live slippage is materially worse than the
  model, `PAPER_SLIPPAGE_BPS` was optimistic and every backtest was too.
- Confirm the books reconcile daily (`books_balance` in the daily report).
- Keep the position count and size small. Adding capital is easy; unwinding a bad
  week is not.
- Do not change strategy parameters in response to a handful of trades. That is
  curve-fitting with real money.

---

## How to stop

**Halt new entries** (positions stay managed):

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"reason":"MANUAL","detail":"stopping","source":"operator"}' localhost:8000/bot/halt
```

**Close everything:**

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{}' localhost:8000/positions/flatten
```

**Disarm completely** — set `ENABLE_LIVE_TRADING=false` and restart. The service
returns to paper mode and cannot touch the exchange.

**Then revoke the API key at the exchange.** Software you trust is still software.
