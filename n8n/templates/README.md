# Node templates

Copy-paste fragments for extending the workflows. Paste JSON directly onto an n8n
canvas (Ctrl+V) and it becomes a node.

## Authenticated call to the trading API

Every call to the Python service looks like this. Change `method`, the path and
the body; leave the auth block alone.

```json
{
  "parameters": {
    "method": "POST",
    "url": "={{ $env.TRADING_API_BASE_URL || 'http://python-trading-service:8000' }}/risk/check",
    "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth",
    "sendBody": true,
    "contentType": "json",
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ symbol: $json.symbol }) }}",
    "options": { "timeout": 30000 }
  },
  "type": "n8n-nodes-base.httpRequest",
  "typeVersion": 4.5,
  "position": [0, 0],
  "name": "Call trading API",
  "credentials": { "httpHeaderAuth": { "id": "trading-api-key", "name": "Trading API Key" } }
}
```

## Calling another workflow in the chain

```json
{
  "parameters": {
    "method": "POST",
    "url": "={{ $env.N8N_WEBHOOK_URL || 'http://localhost:5678/' }}webhook/risk-validation",
    "sendHeaders": true,
    "specifyHeaders": "keypair",
    "headerParameters": {
      "parameters": [
        { "name": "x-workflow-secret", "value": "={{ $env.WORKFLOW_SECRET || $env.SERVICE_API_KEY }}" }
      ]
    },
    "sendBody": true,
    "contentType": "json",
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ proposal: $json.proposal }) }}",
    "options": { "timeout": 120000 }
  },
  "type": "n8n-nodes-base.httpRequest",
  "typeVersion": 4.5,
  "position": [0, 0],
  "name": "Call next stage",
  "onError": "continueRegularOutput"
}
```

## Guarding a webhook entry point

First node after any webhook trigger. Without it, anyone who can reach n8n can
push an item into the pipeline.

```javascript
const expected = ($env.WORKFLOW_SECRET || $env.SERVICE_API_KEY || 'dev-secret');
const first = $input.first()?.json ?? {};
if (first.headers) {
  const supplied = first.headers['x-workflow-secret'] || first.headers['X-Workflow-Secret'];
  if (!supplied || supplied !== expected) {
    throw new Error('unauthorized: missing or invalid x-workflow-secret header');
  }
}
const body = first.body || first;
return [{ json: body }];
```

## Reporting a failure

Use on the error output of any node that matters. `halt_bot: true` trips the kill
switch — reserve it for failures that mean positions are unmanaged or risk cannot
be evaluated.

```json
{
  "parameters": {
    "method": "POST",
    "url": "={{ $env.TRADING_API_BASE_URL || 'http://python-trading-service:8000' }}/workflow/error",
    "authentication": "genericCredentialType",
    "genericAuthType": "httpHeaderAuth",
    "sendBody": true,
    "contentType": "json",
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ workflow: $workflow.name, message: $json.error?.message || 'unknown error', execution_id: $execution.id, severity: 'ERROR', halt_bot: false }) }}",
    "options": { "timeout": 30000, "response": { "response": { "neverError": true } } }
  },
  "type": "n8n-nodes-base.httpRequest",
  "typeVersion": 4.5,
  "position": [0, 0],
  "name": "Report failure",
  "credentials": { "httpHeaderAuth": { "id": "trading-api-key", "name": "Trading API Key" } }
}
```

## Adding a symbol

Nothing to change in n8n. Set `TRADING_SYMBOLS` in `.env` and restart the Python
service; workflow 02 reads the configured universe when the caller does not
specify symbols.

## Things not to do

- **Do not compute indicators in a Code node.** The Python service owns that, and
  a second implementation will drift from the tested one.
- **Do not call `/paper/order` without a `risk_approval_id`.** It will be
  rejected — that rejection is the safety property, not an obstacle.
- **Do not build a risk proposal by hand in a Set node.** Account state is
  resolved server-side; a hand-made payload cannot raise your size, but it can
  produce a trade the AI layer never reviewed.
