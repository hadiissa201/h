# n8n orchestration layer

n8n decides **when** things happen and **what happens next**. It does not do
quantitative work: every calculation, risk decision and order goes through the
Python service. That split is deliberate — indicator maths and position sizing
belong in tested Python, not in Code nodes that are hard to test and easy to
break with a stray edit.

## The nine workflows

| # | Workflow | Trigger | What it does |
|---|----------|---------|--------------|
| 01 | Market Data Collection | every 5 min | Fetch → validate → store. Releases analysis only if the data is clean. |
| 02 | Market Analysis | webhook `market-analysis` + every 15 min | Features → regime → strategies → candidate. Decides whether a setup is worth an LLM call. |
| 03 | AI Trade Evaluation | webhook `ai-evaluation` | Sends structured context to the model, validates the reply, produces a risk proposal. |
| 04 | Risk Validation | webhook `risk-validation` | Deterministic risk checks and sizing. Issues a single-use approval. |
| 05 | Trade Execution | webhook `trade-execution` | Places the approved order, verifies the position, refreshes the portfolio. |
| 06 | Position Monitoring | every minute | Marks positions, moves stops, executes exits, runs kill-switch checks. |
| 07 | Daily Performance Report | 00:05 UTC | Builds the daily report from stored data with an AI summary. |
| 08 | Emergency Shutdown | webhook `emergency-shutdown` + every 5 min | Halts trading and requires a manual reset. |
| 09 | Error Handler | error trigger | Records any workflow failure and escalates unsafe ones. |

Flow of a trade:

```
01 ──(data ok)──▶ 02 ──(setup + gate open)──▶ 03 ──(AI confirms)──▶ 04 ──(approved)──▶ 05
 │                 │                            │                    │
 └─(bad data)──▶ 08 └─(no setup: stop)          └─(veto: stop)        └─(rejected: stop)

06 runs on its own schedule and escalates to 08. 09 catches failures from all of them.
```

## Setup

### 1. Start the stack

```bash
cp .env.example .env      # then edit: set SERVICE_API_KEY and N8N_ENCRYPTION_KEY
docker compose up -d
```

### 2. Import the workflows

```bash
docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

Or, in the UI: **Workflows → Import from File**, once per file in `n8n/workflows/`.

### 3. Create the API credential (one time)

The workflows authenticate to the Python service with a Header Auth credential.
The key is never stored in the workflow files.

1. **Credentials → New → Header Auth**
2. Name: `Trading API Key` (the workflows look for this name)
3. Header Name: `X-API-Key`
4. Header Value: the same value as `SERVICE_API_KEY` in your `.env`
5. Save, then open any HTTP node and confirm the credential is selected.

### 4. Point each workflow at the error handler

For workflows 01–08: **Workflow menu → Settings → Error Workflow → `09 - Error Handler`**.
Without this, a failure is logged by n8n but not recorded in the trading database.

### 5. Activate

Activate 01, 02, 06, 07 and 08 (the ones with triggers), plus 03, 04 and 05 (their
webhooks only accept calls while the workflow is active).

> Production webhook URLs only respond when the workflow is **Active**. While
> testing in the editor, n8n uses a separate `/webhook-test/` URL that is live
> only while you have "Listen for test event" running.

## Environment variables n8n uses

| Variable | Purpose | Default |
|----------|---------|---------|
| `TRADING_API_BASE_URL` | Base URL of the Python service | `http://python-trading-service:8000` |
| `N8N_WEBHOOK_URL` | Used to build internal workflow-to-workflow calls | `http://localhost:5678/` |
| `SERVICE_API_KEY` | Also used as the internal webhook secret | — |
| `WORKFLOW_SECRET` | Separate secret for internal calls, if you want one | falls back to `SERVICE_API_KEY` |
| `TRADING_SYMBOLS` | Fallback symbol list for workflow 02 | `BTC/USDT,ETH/USDT,SOL/USDT` |
| `PRIMARY_TIMEFRAME` | Default analysis timeframe | `1h` |
| `NOTIFY_WEBHOOK_URL` | Slack/Discord/Teams incoming webhook for alerts | unset (notifications skipped) |
| `EMERGENCY_FLATTEN` | `true` closes positions on an emergency halt | unset (positions kept) |

To use these inside n8n, add `env_file: [.env]` to the n8n service or set them in
`docker-compose.yml`.

## Why webhooks instead of Execute Workflow nodes

An Execute Workflow node stores the *ID* of the workflow it calls. IDs are
assigned per instance, so an exported chain breaks the moment it is imported
somewhere else — the node points at nothing, and the failure is silent until a
trade should have happened.

Webhook paths are fixed strings. They survive export/import, they let you test
any stage with `curl`, and each stage gets its own retry and timeout behaviour.
The cost is that the receiving workflow must be active, and that internal calls
need a shared secret (which is why every webhook entry point validates
`x-workflow-secret`).

## Testing a stage by hand

```bash
# Analysis for one symbol
curl -X POST http://localhost:5678/webhook/market-analysis \
  -H 'Content-Type: application/json' \
  -H "x-workflow-secret: $SERVICE_API_KEY" \
  -d '{"symbols":["BTC/USDT"],"timeframe":"1h"}'

# Emergency halt drill (safe: halting only stops new entries)
curl -X POST http://localhost:5678/webhook/emergency-shutdown \
  -H 'Content-Type: application/json' \
  -H "x-workflow-secret: $SERVICE_API_KEY" \
  -d '{"reason":"MANUAL","detail":"kill switch drill","source":"operator"}'

# Then confirm, and reset
curl -H "X-API-Key: $SERVICE_API_KEY" http://localhost:8000/bot/state
curl -X POST -H "X-API-Key: $SERVICE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"confirmation":"RESET","operator":"you","note":"drill complete"}' \
  http://localhost:8000/bot/reset
```

## Regenerating the files

```bash
python scripts/generate_n8n_workflows.py
```

This rewrites every file in `n8n/workflows/` and **discards UI edits**. Once you
start editing in n8n, export from the UI instead and treat the JSON as the
artifact. `python-service/tests/test_n8n_workflows.py` validates whichever
version is on disk.
