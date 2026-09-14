# Credentials

**No credential value belongs in this repository.** Workflow JSON is committed;
credential values are not, and `tests/test_n8n_workflows.py` fails the build if a
secret-looking literal appears in a workflow file.

## Required

### Trading API Key (Header Auth)

Used by every HTTP node that calls the Python service.

| Field | Value |
|-------|-------|
| Type | Header Auth |
| Name | `Trading API Key` (exact — the workflows reference this name) |
| Header Name | `X-API-Key` |
| Header Value | the value of `SERVICE_API_KEY` from your `.env` |

Generate a key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the same value in `.env` (`SERVICE_API_KEY=...`) and in this credential. The
Python service compares it in constant time and rejects everything else with
`401 UNAUTHORIZED`.

## Optional

### Notification webhook

Not an n8n credential — an environment variable, `NOTIFY_WEBHOOK_URL`. Slack,
Discord and Teams incoming webhooks all accept the `{"text": "..."}` body the
workflows send. When it is unset, notification nodes are skipped and the workflow
still completes.

### Exchange API keys

These belong in the Python service's environment (`EXCHANGE_API_KEY`,
`EXCHANGE_API_SECRET`), never in n8n. n8n does not talk to the exchange; it only
talks to the Python service, which is what makes the risk engine impossible to
route around.

When you eventually create exchange keys:

- **Disable withdrawals.** Trading permission only.
- **Restrict by IP** to the host running the service.
- Start with the smallest amount you are willing to lose entirely.
- Read `docs/LIVE_TRADING_CHECKLIST.md` first — keys alone do not enable live
  trading, and are not meant to.

## What n8n stores, and where

n8n encrypts credentials with `N8N_ENCRYPTION_KEY` and stores them in the `n8n`
Postgres database. Two consequences:

- **Keep `N8N_ENCRYPTION_KEY` stable.** Change it and every stored credential
  becomes unreadable; you will re-enter them all.
- **Back it up separately from the database.** A database backup without the key
  cannot be restored into a working instance.

## Rotating the API key

1. Generate a new key.
2. Update `SERVICE_API_KEY` in `.env`.
3. `docker compose up -d python-trading-service` to restart with the new value.
4. Update the `Trading API Key` credential in n8n.

Between steps 3 and 4 the workflows will get `401`s, which the error workflow
records. Rotate during a quiet period, or halt the bot first
(`POST /bot/halt`) and reset afterwards.
