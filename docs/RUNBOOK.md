# Runbook

Operating the system: what to check, what the alerts mean, and what to do when
something is wrong.

## Contents

- [Daily routine](#daily-routine)
- [Reading the dashboard](#reading-the-dashboard)
- [Common situations](#common-situations)
- [Incidents](#incidents)
- [The kill switch](#the-kill-switch)
- [Maintenance](#maintenance)
- [Verification log](#verification-log)

---

## Daily routine

**Morning (5 minutes).**

```bash
curl -s localhost:8000/health | jq '{status, mode, live_trading_armed, bot_status}'
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/bot/state | jq
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/reports/daily/latest | jq -r '.body_markdown'
```

Three questions, in order of importance:

1. **Is the bot halted?** If yes, find out why *before* resetting it.
2. **Do the books reconcile?** The daily report says so explicitly. If it says they
   do not, stop trading and investigate — every other figure is unverified.
3. **Did anything trade, and was it sensible?** `GET /positions` and
   `GET /performance/trades`.

**Weekly.**

- Review `GET /ai/evaluation`. If the reviewer is not changing outcomes, consider
  `AI_ENABLED=false` and save the cost.
- Review `GET /risk/decisions` for rejections. A check that fires constantly is
  either a badly calibrated limit or a strategy proposing trades it should not.
- Re-run a backtest on recent real data and compare to paper results. Divergence
  between them is the earliest warning that something is wrong.
- Check the n8n executions list for failures. Workflow 09 should have recorded any,
  but an n8n-level failure (out of memory, container restart) may not reach it.

---

## Reading the dashboard

<http://localhost:8000/?key=YOUR_SERVICE_API_KEY>

| Panel | Healthy | Investigate |
|---|---|---|
| Mode | `paper`, live not armed | Anything else, unless you intend it |
| Bot status | `RUNNING` | `HALTED` — read `halt_reason` |
| Equity / drawdown | Drawdown well under `MAX_DRAWDOWN_PCT` | Approaching the limit |
| Open positions | ≤ `MAX_OPEN_POSITIONS`, each with a stop | Any position without a stop loss |
| Data freshness | Staleness within one or two bars | Growing staleness = the feed has stopped |
| Recent errors | Empty | Anything repeating |

---

## Common situations

### Nothing is trading

This is usually correct behaviour, not a fault. Walk the pipeline and let it tell
you where it stopped:

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"symbols":["BTC/USDT"],"timeframe":"1h","dry_run":true}' \
  localhost:8000/pipeline/run | jq '.outcomes[] | {symbol, stage_reached, reason}'
```

| `stage_reached` | Meaning |
|---|---|
| `data` | Data quality gate failed — stale, gappy or too few bars |
| `regime` | Regime is `unknown` (not enough warm history) |
| `strategy` | No strategy produced an actionable signal. **The normal case.** |
| `ai` | The reviewer vetoed, or the provider was unavailable with `AI_REQUIRED_FOR_ENTRY=true` |
| `risk` | A risk check blocked it — the response names which |
| `portfolio` | Position limits, e.g. one already open in that symbol |
| `executed` | A trade was placed |

A system that trades every hour is not a good system. Long quiet periods are the
design working.

### The bot is halted

```bash
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/bot/state | jq '{status, halt_reason, halt_detail, halted_at, halted_by}'
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/bot/emergencies | jq
```

| `halt_reason` | What happened | Before resetting |
|---|---|---|
| `MAX_DAILY_LOSS` | Daily loss limit hit | Wait for the next day. Resetting to keep trading a losing day is exactly what the limit exists to prevent. |
| `MAX_DRAWDOWN` | Peak-to-trough limit hit | Do not reset casually. Review every trade in the drawdown first. |
| `CONSECUTIVE_LOSSES` | Loss streak limit | Check whether the regime changed under the strategies. |
| `DATA_QUALITY` | Feed failed repeatedly | Fix the feed, confirm `/health`, then reset. |
| `MANUAL` | Someone halted it | Whatever they were worried about. |

Reset requires an explicit confirmation and an operator name, and both are
recorded:

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"confirmation":"RESET","operator":"your-name","note":"cause addressed: ..."}' \
  localhost:8000/bot/reset
```

### A position is not being managed

The monitor runs from workflow 06. If a stop is not trailing:

```bash
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/positions | jq '.positions[].exit_plan'
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{}' localhost:8000/positions/monitor | jq
```

If running the monitor by hand works, the problem is in n8n — check that workflow
06 is **Active** and look at its execution history.

### Data quality failures

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"symbols":["BTC/USDT"],"timeframes":["1h"],"limit":400}' \
  localhost:8000/market/collect | jq '.symbols[] | {symbol, data_ok, errors}'
```

| Issue | Likely cause |
|---|---|
| `staleness` | Exchange API down, rate limit, or network. Check `/health`. |
| `insufficient_bars` | Symbol newly added, or history was purged |
| `gap_ratio` | Exchange had an outage; usually self-heals on the next fetch |
| `ohlc_invalid` | Provider bug or corrupted response. Do not trade this symbol. |

### The LLM is failing

```bash
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/ai/decisions | jq '.decisions[0]'
curl -s localhost:8000/health | jq '.components[] | select(.name | startswith("llm"))'
```

With `AI_REQUIRED_FOR_ENTRY=true` (the default) a failing provider means no new
entries — that is the intended failure mode. Existing positions are still managed.
If you want to keep trading on the deterministic layer alone, set
`AI_REQUIRED_FOR_ENTRY=false` **deliberately**, and know that you have removed a
safety layer rather than fixed a problem.

### Accounting does not reconcile

The daily report leads with a warning when `books_balance` is false.

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"include_ai_summary":false}' localhost:8000/reports/daily \
  | jq '.facts.account | {equity, reconciliation_error, books_balance}'
```

Treat this as a stop-trading event. Halt the bot, then compare
`GET /portfolio/balances`, `GET /positions` and `GET /performance/trades` against
`GET /orders` to find where they diverge. A residual larger than a few units of the
eighth decimal place is a real defect, not rounding.

---

## Incidents

### The service will not start

```bash
docker compose logs python-trading-service --tail=100
```

| Symptom | Cause | Fix |
|---|---|---|
| `error parsing value for field "trading_symbols"` | A list setting is malformed | Comma-separated, no JSON, no quotes: `TRADING_SYMBOLS=BTC/USDT,ETH/USDT` |
| `could not connect to server` | Postgres not ready | `docker compose ps`; compose waits on a healthcheck, but a slow volume can outlast it |
| `alembic ... target database is not up to date` | Migrations not applied | `docker compose exec python-trading-service alembic upgrade head` |
| `SERVICE_API_KEY is required in production` | Auth unset with `ENVIRONMENT=production` | Set it. Do not lower `ENVIRONMENT` to dodge it. |

The service deliberately **does** start when the database is unreachable, and
reports `degraded` on `/health`. A crash loop hides the failure; a degraded health
check surfaces it to n8n.

### n8n workflows are not firing

1. Is the workflow **Active**? Production webhook URLs only respond when it is.
2. Is the `Trading API Key` credential set on every HTTP node?
3. Does workflow 09 have any recorded errors? `GET /bot/events`.
4. Test a stage directly with `curl` (see [n8n/README.md](../n8n/README.md)) to
   isolate n8n from the Python service.

### An order was rejected

```bash
curl -s -H "X-API-Key: $SERVICE_API_KEY" localhost:8000/bot/events | jq '.events[] | select(.event=="ORDER_REJECTED")'
```

A `403` from `/orders` is the risk approval mechanism working. The detail says
which condition failed: not found, expired (older than `RISK_APPROVAL_TTL_SECONDS`),
already consumed, or fingerprint mismatch (the trade changed after approval). All
four are correct refusals — get a fresh approval rather than working around them.

---

## The kill switch

**Halt immediately:**

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"reason":"MANUAL","detail":"why","source":"operator"}' localhost:8000/bot/halt
```

Halting stops **new entries**. Open positions are still monitored and their stops
still work — abandoning positions is usually worse than holding them.

**Halt and close everything:**

```bash
curl -s -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{}' localhost:8000/positions/flatten
```

**Practise it.** Run the drill in paper mode before you ever need it:

```bash
curl -s -X POST localhost:5678/webhook/emergency-shutdown \
  -H 'Content-Type: application/json' -H "x-workflow-secret: $SERVICE_API_KEY" \
  -d '{"reason":"MANUAL","detail":"kill switch drill","source":"operator"}'
```

`GET /bot/live-readiness` tracks whether the kill switch has ever been exercised,
precisely because an untested kill switch is not a kill switch.

---

## Maintenance

**Back up before anything else.** The database is the entire audit trail.

```bash
docker compose exec postgres pg_dump -U trader trading | gzip > backup-$(date +%F).sql.gz
```

**Change a risk limit.** Edit `.env`, then `docker compose restart
python-trading-service`. Limits are read from settings on every check, so no code
change is involved. Tightening a limit takes effect immediately; it does not
retroactively close positions that now exceed it.

**Add a symbol.** Append to `TRADING_SYMBOLS`, restart, and let workflow 01 build
history. Give it at least `MIN_CANDLES_FOR_ANALYSIS` bars on the primary timeframe
before expecting signals — until then the regime will read `unknown`.

**Upgrade.** Back up, `git pull`, `docker compose build`, `alembic upgrade head`,
restart, then re-run `scripts/paper_smoke_test.py`.

**Regenerate the n8n workflows.** `python scripts/generate_n8n_workflows.py`
rewrites `n8n/workflows/` and **discards UI edits**. Once you start editing in the
n8n UI, export from there instead and treat the JSON as the artifact.

---

## Verification log

Recorded so that claims about this system can be checked rather than believed.
**These are paper-mode results on synthetic data. They say nothing about
profitability.**

### Test suite

Run on 2026-09-14, Python 3.11, SQLite backend:

```
$ python -m pytest
497 passed in 49.32s

$ python -m pytest --cov=app --cov-report=term
TOTAL   7104   792   89%

$ ruff check .
All checks passed!
```

Breakdown: 18 test modules — unit tests for indicators (including a look-ahead
suite that recomputes every indicator on a prefix and asserts the overlap is
unchanged), sizing, the risk engine, the fill model, exit rules, regime detection,
strategies, safety config and AI parsing; integration tests for the trade
lifecycle, the API, backtesting, monitoring and orders; failure-injection tests for
risk bypass attempts and degraded conditions; and a structural validator for the
n8n workflow JSON.

### End-to-end paper smoke test

`scripts/paper_smoke_test.py`, run against a local instance on a fresh database
with `MARKET_DATA_PROVIDER=synthetic` and the AI layer disabled. **39 checks
passed, 0 failed.** What it actually observed:

| Stage | Observed |
|---|---|
| Health | mode `paper`, live not armed, 4 blockers listed, all 4 components healthy |
| Auth | unauthenticated `401`, authenticated `200` |
| Market data | 3 symbols × 4 timeframes × 400 bars, all gates passed, staleness 0s |
| Pipeline | 1 of 3 symbols traded; the other two stopped at `strategy` with "no strategy produced an actionable signal" |
| Forged approval | `403 risk approval ra_forged_approval_id not found` |
| No-edge proposal | `REJECTED`; failed `regime_known`, `stop_distance_min`, `min_risk_reward`; no approval issued |
| Sizing | quantity 19.99601, notional 1999.60, risked **0.0200%** of equity, `capped_by: max_position_pct_equity` |
| Monitor | checked 1 position, 1 trailing-stop update, 0 exits, 0 errors |
| Portfolio | equity 9996.60 from 10000.00, unrealised −1.40, fees 1.99, 1 open position — down on entry costs alone, as expected |
| Position integrity | stop loss present, `risk_approval_id` present |
| Kill switch | halt → `HALTED` → pipeline traded 0 → reset → `RUNNING` |
| Reporting | daily report built, `books_balance: true`, residual 0.0 |
| Backtest | 1292 bars, 21 trades, net P&L +151.16, win rate 0.43, max drawdown 1.73% |

The backtest line is a **wiring check on synthetic data**. Its P&L carries no
information about real market performance, and the run emitted three warnings
saying so: one position was still open at the end of the data, the LLM is not
replayed, and spread/liquidity filters are not evaluated.

**Your numbers will not match these**, and that is expected rather than a fault.
The synthetic provider anchors its series to the current 5-minute bar, so the bar
alignment shifts every five minutes and the path is regenerated after UTC midnight.
Which symbol trades, how many backtest trades occur and what the P&L is all move
between runs. What must not move is the list of *checks*: 39 pass, 0 fail.

### Not verified

- **Docker Compose has never been run.** `docker compose config` parses cleanly,
  but no Docker daemon was available in the build environment, so container boot,
  service dependencies and healthchecks are unverified. Everything above was run
  locally in a virtualenv against SQLite.
- **No real exchange data has been fetched.** The `ccxt` provider is implemented
  and unit-tested against recorded shapes, but no live API call was made.
- **No LLM provider has been called.** The AI layer was disabled for the smoke
  test; its parsing, veto policy and rate limiting are covered by unit tests
  against recorded and adversarial responses, not by live calls.
- **The live execution path has never been exercised**, by design. See
  [LIVE_TRADING_CHECKLIST.md](LIVE_TRADING_CHECKLIST.md).
