#!/usr/bin/env python3
"""Generate the n8n workflow JSON files in ``n8n/workflows/``.

Why a generator rather than hand-written JSON: node IDs, positions and the
``connections`` map have to stay consistent across nine workflows, and hand-editing
that by hand is how you end up with a graph that imports but does not run.

The generated files are the artifact and are committed. After importing them into
n8n, the UI becomes the source of truth — edit there and re-export. Re-running this
script regenerates from scratch and will discard UI edits.

    python scripts/generate_n8n_workflows.py

Design decisions baked in here:

* **Workflows chain over webhooks, not Execute-Workflow nodes.** Execute-Workflow
  references a workflow *ID*, which differs on every n8n instance, so an exported
  chain breaks on import. Fixed webhook paths (``/webhook/ai-evaluation`` …) are
  stable everywhere and make each stage independently callable for testing.
* **Secrets never appear in the JSON.** The API key comes from an n8n credential
  ("Trading API Key", Header Auth); the base URL and the internal webhook secret
  come from environment variables.
* **Every stage can stop the pipeline.** Bad data, a vetoing AI, a risk rejection
  and a failed order each route to an explicit branch — nothing continues by
  default.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "n8n" / "workflows"

NAMESPACE = uuid.UUID("6f1b6f0e-2c5a-4a1a-9a2e-9c7f1f0b3d21")

# Resolved inside n8n. TRADING_API_BASE_URL is set by docker-compose.
API = "={{ $env.TRADING_API_BASE_URL || 'http://python-trading-service:8000' }}"
HOOK = "={{ $env.N8N_WEBHOOK_URL || 'http://localhost:5678/' }}webhook/"
# Shared secret for internal workflow-to-workflow calls. Same value as the API key
# by default; set WORKFLOW_SECRET to separate them.
#
# There is deliberately NO literal fallback. An earlier version fell back to
# 'dev-secret' when neither variable was set, which is the worst possible
# behaviour: caller and receiver both used it, so the chain worked perfectly while
# every internal webhook was in practice guarded by a string published in this
# repository. Anyone who could reach n8n could inject a trade proposal. Missing
# configuration must break loudly, not quietly authorise strangers.
SECRET = "={{ $env.WORKFLOW_SECRET || $env.SERVICE_API_KEY }}"
SECRET_JS = "($env.WORKFLOW_SECRET || $env.SERVICE_API_KEY)"

CREDENTIAL = {
    "httpHeaderAuth": {
        "id": "trading-api-key",
        "name": "Trading API Key",
    }
}

TAGS = [{"name": "trading"}]


# --------------------------------------------------------------------- helpers
def node_id(workflow: str, name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{workflow}:{name}"))


def node(
    workflow: str,
    name: str,
    type_: str,
    version: float,
    parameters: dict[str, Any],
    position: tuple[int, int],
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "parameters": parameters,
        "id": node_id(workflow, name),
        "name": name,
        "type": type_,
        "typeVersion": version,
        "position": [position[0], position[1]],
    }
    payload.update(extra)
    return payload


def http(
    workflow: str,
    name: str,
    method: str,
    path: str,
    position: tuple[int, int],
    *,
    body: str | None = None,
    timeout: int = 30000,
    never_error: bool = False,
    retries: int = 0,
    on_error: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """HTTP node pointed at the Python service, authenticated by credential."""
    parameters: dict[str, Any] = {
        "method": method,
        "url": f"{API}{path}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": timeout},
    }
    if body is not None:
        parameters["sendBody"] = True
        parameters["contentType"] = "json"
        parameters["specifyBody"] = "json"
        parameters["jsonBody"] = body
    if never_error:
        parameters["options"]["response"] = {"response": {"neverError": True}}

    extra: dict[str, Any] = {"credentials": CREDENTIAL}
    if retries:
        extra["retryOnFail"] = True
        extra["maxTries"] = retries
        extra["waitBetweenTries"] = 2000
    if on_error:
        extra["onError"] = on_error
    if notes:
        extra["notes"] = notes
        extra["notesInFlow"] = True
    return node(
        workflow,
        name,
        "n8n-nodes-base.httpRequest",
        4.5,
        parameters,
        position,
        **extra,
    )


def webhook_call(
    workflow: str,
    name: str,
    hook_path: str,
    body: str,
    position: tuple[int, int],
    *,
    notes: str = "",
) -> dict[str, Any]:
    """Call another workflow in this instance by its webhook path."""
    parameters = {
        "method": "POST",
        "url": f"{HOOK}{hook_path}",
        "sendHeaders": True,
        "specifyHeaders": "keypair",
        "headerParameters": {
            "parameters": [{"name": "x-workflow-secret", "value": SECRET}]
        },
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": body,
        "options": {"timeout": 120000},
    }
    extra: dict[str, Any] = {"onError": "continueRegularOutput"}
    if notes:
        extra["notes"] = notes
        extra["notesInFlow"] = True
    return node(
        workflow, name, "n8n-nodes-base.httpRequest", 4.5, parameters, position, **extra
    )


def webhook_trigger(
    workflow: str, name: str, path: str, position: tuple[int, int], *, notes: str = ""
) -> dict[str, Any]:
    return node(
        workflow,
        name,
        "n8n-nodes-base.webhook",
        2.1,
        {
            "httpMethod": "POST",
            "path": path,
            "responseMode": "lastNode",
            "options": {},
        },
        position,
        webhookId=node_id(workflow, f"webhook:{path}"),
        notes=notes or f"POST /webhook/{path}",
        notesInFlow=True,
    )


def schedule(
    workflow: str, name: str, interval: dict[str, Any], position: tuple[int, int], *, notes: str = ""
) -> dict[str, Any]:
    return node(
        workflow,
        name,
        "n8n-nodes-base.scheduleTrigger",
        1.4,
        {"rule": {"interval": [interval]}},
        position,
        notes=notes,
        notesInFlow=bool(notes),
    )


def code(
    workflow: str, name: str, js: str, position: tuple[int, int], *, notes: str = ""
) -> dict[str, Any]:
    extra = {"notes": notes, "notesInFlow": True} if notes else {}
    return node(
        workflow,
        name,
        "n8n-nodes-base.code",
        2,
        {"mode": "runOnceForAllItems", "jsCode": js.strip() + "\n"},
        position,
        **extra,
    )


def condition(
    left: str, operator_type: str, operation: str, right: Any = None, cid: str = "c1"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": cid,
        "leftValue": left,
        "rightValue": right if right is not None else "",
        "operator": {"type": operator_type, "operation": operation},
    }
    if operation in {"true", "false", "exists", "notExists", "empty", "notEmpty"}:
        payload["operator"]["singleValue"] = True
    return payload


def if_node(
    workflow: str,
    name: str,
    conditions: list[dict[str, Any]],
    position: tuple[int, int],
    *,
    combinator: str = "and",
    notes: str = "",
) -> dict[str, Any]:
    extra = {"notes": notes, "notesInFlow": True} if notes else {}
    return node(
        workflow,
        name,
        "n8n-nodes-base.if",
        2.3,
        {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "loose",
                    "version": 2,
                },
                "conditions": conditions,
                "combinator": combinator,
            },
            "looseTypeValidation": True,
            "options": {},
        },
        position,
        **extra,
    )


def noop(workflow: str, name: str, position: tuple[int, int], *, notes: str = "") -> dict[str, Any]:
    extra = {"notes": notes, "notesInFlow": True} if notes else {}
    return node(workflow, name, "n8n-nodes-base.noOp", 1, {}, position, **extra)


def sticky(
    workflow: str, name: str, content: str, position: tuple[int, int], size: tuple[int, int]
) -> dict[str, Any]:
    return node(
        workflow,
        name,
        "n8n-nodes-base.stickyNote",
        1,
        {"content": content, "height": size[1], "width": size[0], "color": 4},
        position,
    )


def report_error(
    workflow: str,
    name: str,
    position: tuple[int, int],
    *,
    message_expr: str,
    halt: str = "false",
    severity: str = "ERROR",
) -> dict[str, Any]:
    """POST to the service error sink so no failure is silently swallowed."""
    body = (
        "={{ JSON.stringify({"
        f"workflow: $workflow.name, message: {message_expr}, "
        "node: $json.node || null, execution_id: $execution.id, "
        f"severity: '{severity}', halt_bot: {halt}, "
        "context: { error: $json.error || null } }) }}"
    )
    return http(
        workflow,
        name,
        "POST",
        "/workflow/error",
        position,
        body=body,
        never_error=True,
        notes="Failures are recorded server-side and surfaced on the dashboard",
    )


def notify(workflow: str, name: str, text_expr: str, position: tuple[int, int]) -> dict[str, Any]:
    """Generic outgoing notification (Slack/Discord/Teams incoming webhooks all accept this)."""
    return node(
        workflow,
        name,
        "n8n-nodes-base.httpRequest",
        4.5,
        {
            "method": "POST",
            "url": "={{ $env.NOTIFY_WEBHOOK_URL }}",
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": f"={{{{ JSON.stringify({{ text: {text_expr} }}) }}}}",
            "options": {"timeout": 15000},
        },
        position,
        onError="continueRegularOutput",
        notes="Set NOTIFY_WEBHOOK_URL to a Slack/Discord/Teams incoming webhook",
        notesInFlow=True,
    )


def notify_configured(workflow: str, name: str, position: tuple[int, int]) -> dict[str, Any]:
    return if_node(
        workflow,
        name,
        [condition("={{ $env.NOTIFY_WEBHOOK_URL }}", "string", "notEmpty", cid="notify")],
        position,
        notes="Skips silently when no notification channel is configured",
    )


VALIDATE_REQUEST_JS = """
// Reject calls that do not carry the shared internal secret. These webhooks are
// reachable from anywhere n8n is reachable, so an unauthenticated caller must not
// be able to push a trade into the pipeline.
const expected = {secret};
const items = $input.all();
const out = [];
for (const item of items) {{
  const headers = item.json.headers || {{}};
  const supplied = headers['x-workflow-secret'] || headers['X-Workflow-Secret'];
  if (!supplied || supplied !== expected) {{
    throw new Error('unauthorized: missing or invalid x-workflow-secret header');
  }}
  const body = item.json.body || item.json;
  {extra}
  out.push({{ json: body }});
}}
return out;
"""


# Workflows with both a webhook and a schedule trigger cannot use the strict
# validator: scheduled items carry no headers. This checks the secret only for
# items that actually arrived over HTTP.
GUARD_WEBHOOK_JS = """
const expected = {secret};
const first = $input.first()?.json ?? {{}};
if (first.headers) {{
  const supplied = first.headers['x-workflow-secret'] || first.headers['X-Workflow-Secret'];
  if (!supplied || supplied !== expected) {{
    throw new Error('unauthorized: missing or invalid x-workflow-secret header');
  }}
}}
"""


def webhook_guard() -> str:
    return GUARD_WEBHOOK_JS.format(secret=SECRET_JS)


def validate_request(workflow: str, name: str, position: tuple[int, int], extra: str = "") -> dict[str, Any]:
    return code(
        workflow,
        name,
        VALIDATE_REQUEST_JS.format(secret=SECRET_JS, extra=extra),
        position,
        notes="Shared-secret check on the inbound webhook",
    )


def build(
    name: str,
    nodes: list[dict[str, Any]],
    connections: dict[str, Any],
    description: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "saveManualExecutions": True,
            "saveExecutionProgress": True,
            "timezone": "UTC",
            # Set this to the "99 - Error Handler" workflow's ID after import.
            "callerPolicy": "workflowsFromSameOwner",
        },
        "staticData": None,
        "pinData": {},
        "tags": TAGS,
        "meta": {"description": description},
        "versionId": str(uuid.uuid5(NAMESPACE, f"version:{name}")),
    }


def link(*pairs: tuple[str, str] | tuple[str, str, int]) -> dict[str, Any]:
    """Build the connections map. Each pair is (from, to) or (from, to, output_index)."""
    connections: dict[str, Any] = {}
    for pair in pairs:
        source, target = pair[0], pair[1]
        output = pair[2] if len(pair) > 2 else 0
        entry = connections.setdefault(source, {"main": []})
        while len(entry["main"]) <= output:
            entry["main"].append([])
        entry["main"][output].append({"node": target, "type": "main", "index": 0})
    return connections


# ------------------------------------------------------------------ workflows
def workflow_01() -> dict[str, Any]:
    w = "01 - Market Data Collection"
    nodes = [
        sticky(
            w,
            "About",
            "## 1 · Market Data Collection\n\nFetch → validate → store, then release the "
            "analysis stage **only if the data is clean**.\n\nValidation (gaps, duplicates, "
            "stale timestamps, invalid OHLC, missing volume) runs in the Python service; this "
            "workflow branches on its verdict.\n\nBad data routes to the safety check instead "
            "of to analysis — no trade is ever taken on data we do not trust.",
            (-460, -40),
            (420, 320),
        ),
        schedule(
            w,
            "Every 5 minutes",
            {"field": "minutes", "minutesInterval": 5},
            (0, 120),
            notes="Match your fastest timeframe",
        ),
        http(
            w,
            "Collect market data",
            "POST",
            "/market/collect",
            (220, 120),
            body="={{ JSON.stringify({ store_candles: true }) }}",
            timeout=120000,
            retries=2,
            on_error="continueErrorOutput",
            notes="Fetches, validates and persists every symbol/timeframe",
        ),
        if_node(
            w,
            "All data valid?",
            [condition("={{ $json.all_data_ok }}", "boolean", "true", cid="data_ok")],
            (460, 100),
            notes="NO TRADE on bad data",
        ),
        code(
            w,
            "Summarise good data",
            """
const data = $input.first().json;
const symbols = (data.symbols || []).map((s) => ({
  symbol: s.symbol,
  timeframes: Object.keys(s.timeframes || {}),
  last_price: Object.values(s.timeframes || {})[0]?.last_price ?? null,
}));
return [{ json: { symbols: symbols.map((s) => s.symbol), detail: symbols, provider: data.provider } }];
""",
            (700, 20),
        ),
        webhook_call(
            w,
            "Start market analysis",
            "market-analysis",
            "={{ JSON.stringify({ symbols: $json.symbols, source: 'workflow-01' }) }}",
            (940, 20),
            notes="Hands off to workflow 02",
        ),
        code(
            w,
            "Extract unusable symbols",
            """
const data = $input.first().json;
const bad = [];
for (const entry of data.symbols || []) {
  const issues = [];
  for (const [timeframe, info] of Object.entries(entry.timeframes || {})) {
    if (!info.data_ok) {
      issues.push(`${timeframe}: ${(info.issues || []).map((i) => i.code).join(',')}`);
    }
  }
  for (const error of entry.errors || []) {
    issues.push(`${error.timeframe}: ${error.error}`);
  }
  if (issues.length) bad.push({ symbol: entry.symbol, issues });
}
return [{ json: { bad_symbols: bad.map((b) => b.symbol), detail: bad } }];
""",
            (700, 220),
        ),
        http(
            w,
            "Run safety check",
            "POST",
            "/risk/safety-check",
            (940, 220),
            body="={{ JSON.stringify({ auto_halt: true, stale_symbols: $json.bad_symbols }) }}",
            never_error=True,
            notes="Stale data is a kill-switch trigger",
        ),
        if_node(
            w,
            "Bot halted?",
            [condition("={{ $json.halted }}", "boolean", "true", cid="halted")],
            (1180, 220),
        ),
        webhook_call(
            w,
            "Escalate to emergency workflow",
            "emergency-shutdown",
            "={{ JSON.stringify({ reason: 'MARKET_DATA_STALE', "
            "detail: 'data collection reported unusable market data', "
            "source: 'workflow-01' }) }}",
            (1420, 160),
        ),
        noop(
            w,
            "Analysis skipped (bad data)",
            (1420, 300),
            notes="Not halted, but no analysis this cycle",
        ),
        report_error(
            w,
            "Report collection failure",
            (460, 300),
            message_expr="'market data collection request failed: ' + ($json.error?.message || 'unknown error')",
            halt="false",
        ),
    ]
    connections = link(
        ("Every 5 minutes", "Collect market data"),
        ("Collect market data", "All data valid?", 0),
        ("Collect market data", "Report collection failure", 1),
        ("All data valid?", "Summarise good data", 0),
        ("All data valid?", "Extract unusable symbols", 1),
        ("Summarise good data", "Start market analysis"),
        ("Extract unusable symbols", "Run safety check"),
        ("Run safety check", "Bot halted?"),
        ("Bot halted?", "Escalate to emergency workflow", 0),
        ("Bot halted?", "Analysis skipped (bad data)", 1),
    )
    return build(
        w,
        nodes,
        connections,
        "Scheduled market data collection with validation gating.",
    )


def workflow_02() -> dict[str, Any]:
    w = "02 - Market Analysis"
    nodes = [
        sticky(
            w,
            "About",
            "## 2 · Market Analysis\n\nFor each symbol: features → regime → strategies → "
            "trade candidate.\n\nThe LLM is **not** called here. `/strategy/evaluate` returns "
            "`should_call_ai`, which is only true when a real setup exists, the data is clean, "
            "the bot is running and the symbol is not already held. That gate is what keeps "
            "LLM cost and latency bounded.",
            (-460, -40),
            (420, 300),
        ),
        webhook_trigger(w, "From data collection", "market-analysis", (0, 40)),
        schedule(
            w,
            "Every 15 minutes",
            {"field": "minutes", "minutesInterval": 15},
            (0, 220),
            notes="Independent safety net if workflow 01 is paused",
        ),
        code(
            w,
            "Resolve symbols",
            webhook_guard()
            + """
// Symbols come from the caller, or fall back to the configured universe.
const configured = ($env.TRADING_SYMBOLS || 'BTC/USDT,ETH/USDT,SOL/USDT')
  .split(',').map((s) => s.trim()).filter(Boolean);
const first = $input.first()?.json ?? {};
const body = first.body || first;
const symbols = Array.isArray(body.symbols) && body.symbols.length ? body.symbols : configured;
const timeframe = body.timeframe || $env.PRIMARY_TIMEFRAME || '1h';
return symbols.map((symbol) => ({ json: { symbol, timeframe } }));
""",
            (240, 120),
        ),
        node(
            w,
            "For each symbol",
            "n8n-nodes-base.splitInBatches",
            3,
            {"batchSize": 1, "options": {}},
            (480, 120),
        ),
        http(
            w,
            "Evaluate strategies",
            "POST",
            "/strategy/evaluate",
            (720, 200),
            body="={{ JSON.stringify({ symbol: $json.symbol, timeframe: $json.timeframe }) }}",
            timeout=60000,
            on_error="continueErrorOutput",
            notes="features → regime → strategies → candidate",
        ),
        if_node(
            w,
            "Setup worth AI review?",
            [
                condition("={{ $json.has_setup }}", "boolean", "true", cid="setup"),
                condition("={{ $json.should_call_ai }}", "boolean", "true", cid="gate"),
            ],
            (960, 180),
            notes="Both must hold before we spend an LLM call",
        ),
        webhook_call(
            w,
            "Request AI evaluation",
            "ai-evaluation",
            "={{ JSON.stringify({ symbol: $json.symbol, timeframe: $json.timeframe, "
            "candidate_id: $json.candidate?.candidate_id ?? null, source: 'workflow-02' }) }}",
            (1200, 100),
        ),
        code(
            w,
            "Record no-setup",
            """
const data = $input.first().json;
return [{ json: {
  symbol: data.symbol,
  timeframe: data.timeframe,
  regime: data.regime?.regime ?? null,
  has_setup: data.has_setup,
  skip_reason: data.skip_reason,
  signals: (data.signals || []).map((s) => `${s.strategy}:${s.signal}@${s.confidence}`),
} }];
""",
            (1200, 280),
            notes="Most passes end here — that is the system working",
        ),
        report_error(
            w,
            "Report analysis failure",
            (960, 380),
            message_expr="'strategy evaluation failed: ' + ($json.error?.message || 'unknown error')",
        ),
        code(
            w,
            "Summarise pass",
            """
const results = $input.all().map((item) => item.json);
return [{ json: {
  evaluated: results.length,
  setups: results.filter((r) => r.has_setup).length,
  symbols: results.map((r) => r.symbol),
  finished_at: new Date().toISOString(),
} }];
""",
            (720, 20),
        ),
    ]
    connections = link(
        ("From data collection", "Resolve symbols"),
        ("Every 15 minutes", "Resolve symbols"),
        ("Resolve symbols", "For each symbol"),
        ("For each symbol", "Summarise pass", 0),
        ("For each symbol", "Evaluate strategies", 1),
        ("Evaluate strategies", "Setup worth AI review?", 0),
        ("Evaluate strategies", "Report analysis failure", 1),
        ("Setup worth AI review?", "Request AI evaluation", 0),
        ("Setup worth AI review?", "Record no-setup", 1),
        ("Request AI evaluation", "For each symbol"),
        ("Record no-setup", "For each symbol"),
        ("Report analysis failure", "For each symbol"),
    )
    return build(w, nodes, connections, "Per-symbol quantitative analysis and AI gating.")


def workflow_03() -> dict[str, Any]:
    w = "03 - AI Trade Evaluation"
    nodes = [
        sticky(
            w,
            "About",
            "## 3 · AI Trade Evaluation\n\nThe model reviews a candidate the quant layer "
            "already built. It can **confirm or veto** — nothing else.\n\n`/ai/evaluate` "
            "validates the reply against a strict schema (unparseable ⇒ HOLD), caps the "
            "model's confidence at the deterministic confidence, refuses a direction flip, "
            "and returns a risk proposal only when the review passed.\n\nStops, targets and "
            "size never come from the model.",
            (-460, -40),
            (420, 320),
        ),
        webhook_trigger(w, "Trade candidate received", "ai-evaluation", (0, 140)),
        validate_request(
            w,
            "Validate request",
            (240, 140),
            extra="if (!body.symbol) { throw new Error('symbol is required'); }",
        ),
        http(
            w,
            "Call AI evaluation",
            "POST",
            "/ai/evaluate",
            (480, 140),
            body="={{ JSON.stringify({ symbol: $json.symbol, timeframe: $json.timeframe || null }) }}",
            timeout=120000,
            on_error="continueErrorOutput",
            notes="Provider is configured server-side (ollama / openai / anthropic)",
        ),
        code(
            w,
            "Validate AI response",
            """
// Defence in depth: the service already validates the model's JSON, but the
// workflow re-checks the shape it depends on before acting on it.
const data = $input.first().json;
const allowed = ['BUY', 'SELL', 'HOLD'];
const decision = data.decision?.decision ?? null;
const confidence = data.decision?.confidence ?? null;

const problems = [];
if (!data.evaluated) problems.push(data.reason || 'not evaluated');
if (data.evaluated && !allowed.includes(decision)) problems.push(`bad decision: ${decision}`);
if (data.evaluated && (typeof confidence !== 'number' || confidence < 0 || confidence > 1)) {
  problems.push(`bad confidence: ${confidence}`);
}
if (data.parse_ok === false) problems.push(`parse failure: ${data.parse_error}`);

return [{ json: {
  symbol: data.candidate?.symbol ?? null,
  evaluated: Boolean(data.evaluated),
  actionable: Boolean(data.actionable) && problems.length === 0,
  decision,
  confidence,
  reason: data.decision?.reason ?? data.reason ?? null,
  risk_assessment: data.decision?.risk_assessment ?? null,
  violations: data.violations || [],
  problems,
  risk_proposal: data.risk_proposal ?? null,
  decision_id: data.decision_id ?? null,
} }];
""",
            (720, 140),
        ),
        if_node(
            w,
            "Actionable after review?",
            [
                condition("={{ $json.actionable }}", "boolean", "true", cid="actionable"),
                condition("={{ $json.risk_proposal }}", "object", "exists", cid="proposal"),
            ],
            (960, 140),
        ),
        webhook_call(
            w,
            "Send to risk engine",
            "risk-validation",
            "={{ JSON.stringify({ proposal: $json.risk_proposal, "
            "decision_id: $json.decision_id, source: 'workflow-03' }) }}",
            (1200, 60),
            notes="Risk engine has the final say",
        ),
        code(
            w,
            "Record AI veto",
            """
const data = $input.first().json;
return [{ json: {
  outcome: 'no_trade',
  symbol: data.symbol,
  decision: data.decision,
  confidence: data.confidence,
  reason: data.reason,
  problems: data.problems,
  violations: data.violations,
  at: new Date().toISOString(),
} }];
""",
            (1200, 240),
            notes="A veto is a normal, successful outcome",
        ),
        report_error(
            w,
            "Report AI failure",
            (720, 340),
            message_expr="'AI evaluation request failed: ' + ($json.error?.message || 'unknown error')",
            severity="WARNING",
        ),
        noop(
            w,
            "No trade (AI unavailable)",
            (960, 340),
            notes="Fails closed: no AI answer means no entry",
        ),
    ]
    connections = link(
        ("Trade candidate received", "Validate request"),
        ("Validate request", "Call AI evaluation"),
        ("Call AI evaluation", "Validate AI response", 0),
        ("Call AI evaluation", "Report AI failure", 1),
        ("Validate AI response", "Actionable after review?"),
        ("Actionable after review?", "Send to risk engine", 0),
        ("Actionable after review?", "Record AI veto", 1),
        ("Report AI failure", "No trade (AI unavailable)"),
    )
    return build(w, nodes, connections, "LLM review of a trade candidate, strictly validated.")


def workflow_04() -> dict[str, Any]:
    w = "04 - Risk Validation"
    nodes = [
        sticky(
            w,
            "About",
            "## 4 · Risk Validation\n\nThe deterministic gate. `/risk/check` re-resolves "
            "equity, exposure, drawdown, spread and liquidity **server-side** — values in the "
            "payload cannot inflate the account.\n\nIt checks exposure, position count, daily "
            "loss, drawdown, cooldown, confidence, reward/risk, spread, liquidity and the "
            "mandatory stop, then sizes the trade.\n\nOn approval it issues a **single-use "
            "approval id**. Execution is impossible without one.",
            (-460, -40),
            (420, 340),
        ),
        webhook_trigger(w, "Proposal received", "risk-validation", (0, 140)),
        validate_request(
            w,
            "Validate request",
            (240, 140),
            extra="if (!body.proposal) { throw new Error('proposal is required'); }",
        ),
        http(
            w,
            "Risk check",
            "POST",
            "/risk/check",
            (480, 140),
            body="={{ JSON.stringify($json.proposal) }}",
            timeout=60000,
            on_error="continueErrorOutput",
            notes="Deterministic. Same inputs ⇒ same verdict.",
        ),
        if_node(
            w,
            "Approved?",
            [condition("={{ $json.decision }}", "string", "equals", "APPROVED", cid="approved")],
            (720, 140),
        ),
        code(
            w,
            "Build execution request",
            """
const decision = $input.first().json;
if (!decision.approval_id) {
  throw new Error('approved decision carried no approval_id — refusing to execute');
}
return [{ json: {
  risk_approval_id: decision.approval_id,
  symbol: decision.symbol,
  direction: decision.direction,
  quantity: decision.sizing?.quantity ?? null,
  notional: decision.sizing?.notional ?? null,
  risk_pct: decision.sizing?.effective_risk_pct ?? null,
  expires_at: decision.expires_at,
} }];
""",
            (960, 60),
        ),
        webhook_call(
            w,
            "Execute trade",
            "trade-execution",
            "={{ JSON.stringify({ risk_approval_id: $json.risk_approval_id, "
            "symbol: $json.symbol, source: 'workflow-04' }) }}",
            (1200, 60),
        ),
        code(
            w,
            "Record rejection",
            """
const decision = $input.first().json;
return [{ json: {
  outcome: 'rejected',
  symbol: decision.symbol,
  direction: decision.direction,
  codes: decision.rejection_codes || [],
  reasons: decision.reasons || [],
  failed_checks: (decision.checks || []).filter((c) => !c.passed).map((c) => c.name),
  at: new Date().toISOString(),
} }];
""",
            (960, 240),
            notes="Rejections are logged server-side and shown on the dashboard",
        ),
        report_error(
            w,
            "Report risk check failure",
            (720, 340),
            message_expr="'risk check request failed: ' + ($json.error?.message || 'unknown error')",
            halt="true",
            severity="CRITICAL",
        ),
        noop(
            w,
            "No trade (risk engine unreachable)",
            (960, 420),
            notes="If risk cannot be evaluated, nothing trades — and the bot halts",
        ),
    ]
    connections = link(
        ("Proposal received", "Validate request"),
        ("Validate request", "Risk check"),
        ("Risk check", "Approved?", 0),
        ("Risk check", "Report risk check failure", 1),
        ("Approved?", "Build execution request", 0),
        ("Approved?", "Record rejection", 1),
        ("Build execution request", "Execute trade"),
        ("Report risk check failure", "No trade (risk engine unreachable)"),
    )
    return build(w, nodes, connections, "Deterministic risk validation and approval issuance.")


def workflow_05() -> dict[str, Any]:
    w = "05 - Trade Execution"
    nodes = [
        sticky(
            w,
            "About",
            "## 5 · Trade Execution\n\nPlaces the order the risk engine approved.\n\n`/paper/order` "
            "refuses anything without a valid, unconsumed, unexpired approval whose fingerprint "
            "matches the order, so this workflow cannot execute an unapproved trade even if it "
            "is called directly.\n\nRetries are safe: the request is idempotent on "
            "`client_order_id`, so a retried node returns the original position instead of "
            "opening a second one.",
            (-460, -40),
            (420, 320),
        ),
        webhook_trigger(w, "Approved trade received", "trade-execution", (0, 140)),
        validate_request(
            w,
            "Validate request",
            (240, 140),
            extra=(
                "if (!body.risk_approval_id) { "
                "throw new Error('risk_approval_id is required — execution needs an approval'); }"
            ),
        ),
        http(
            w,
            "Place order",
            "POST",
            "/paper/order",
            (480, 140),
            body="={{ JSON.stringify({ risk_approval_id: $json.risk_approval_id, "
            "symbol: $json.symbol || null }) }}",
            timeout=60000,
            retries=2,
            on_error="continueErrorOutput",
            notes="Idempotent on client_order_id — retry is safe",
        ),
        http(
            w,
            "Verify position",
            "GET",
            "=/positions/{{ $json.position.position_id }}",
            (720, 60),
            never_error=True,
            notes="Confirm the position exists as reported",
        ),
        http(w, "Refresh portfolio", "GET", "/portfolio", (960, 60)),
        code(
            w,
            "Execution summary",
            """
const portfolio = $input.first().json;
const order = $('Place order').first().json;
return [{ json: {
  outcome: 'executed',
  symbol: order.position.symbol,
  position_id: order.position.position_id,
  order_id: order.order.order_id,
  status: order.order.status,
  quantity: order.order.filled_quantity,
  entry_price: order.order.average_fill_price,
  stop_loss: order.position.stop_loss,
  take_profit: order.position.take_profit,
  fee: order.order.fee_paid,
  mode: order.mode,
  equity_after: portfolio.equity,
  open_positions: portfolio.open_positions,
  at: new Date().toISOString(),
} }];
""",
            (1200, 60),
        ),
        notify_configured(w, "Notify configured?", (1440, 60)),
        notify(
            w,
            "Send trade notification",
            "`[${$json.mode.toUpperCase()}] OPENED ${$json.symbol} qty ${$json.quantity} @ "
            "${$json.entry_price} | stop ${$json.stop_loss} target ${$json.take_profit} | "
            "equity ${$json.equity_after}`",
            (1680, -20),
        ),
        noop(w, "Execution recorded", (1680, 140)),
        report_error(
            w,
            "Report execution failure",
            (720, 300),
            message_expr="'order placement failed: ' + ($json.error?.message || 'unknown error')",
            severity="ERROR",
        ),
        noop(
            w,
            "Execution failed",
            (960, 300),
            notes="Repeated failures trip the kill switch server-side",
        ),
    ]
    connections = link(
        ("Approved trade received", "Validate request"),
        ("Validate request", "Place order"),
        ("Place order", "Verify position", 0),
        ("Place order", "Report execution failure", 1),
        ("Verify position", "Refresh portfolio"),
        ("Refresh portfolio", "Execution summary"),
        ("Execution summary", "Notify configured?"),
        ("Notify configured?", "Send trade notification", 0),
        ("Notify configured?", "Execution recorded", 1),
        ("Report execution failure", "Execution failed"),
    )
    return build(w, nodes, connections, "Approved-trade execution with verification.")


def workflow_06() -> dict[str, Any]:
    w = "06 - Position Monitoring"
    nodes = [
        sticky(
            w,
            "About",
            "## 6 · Position Monitoring\n\nRuns often. Each pass marks positions, fills resting "
            "orders, moves break-even and trailing stops, and exits where the rules say so — "
            "using the **same exit rules the backtester uses**.\n\nIt also reports stale data and "
            "abnormal moves, and runs the kill-switch checks.\n\nA monitoring failure is treated "
            "as unsafe: it halts the bot rather than leaving positions unwatched.",
            (-460, -40),
            (420, 320),
        ),
        schedule(
            w,
            "Every minute",
            {"field": "minutes", "minutesInterval": 1},
            (0, 140),
            notes="Polling granularity — see the docs on intrabar gaps",
        ),
        http(
            w,
            "Monitor positions",
            "POST",
            "/positions/monitor",
            (240, 140),
            timeout=120000,
            on_error="continueErrorOutput",
            notes="mark → resting orders → stops → exits → safety checks",
        ),
        if_node(
            w,
            "Emergency or halted?",
            [
                condition(
                    "={{ ($json.emergency_triggers || []).length }}", "number", "gt", 0, cid="emg"
                ),
                condition("={{ $json.bot_status }}", "string", "notEquals", "RUNNING", cid="halted"),
            ],
            (480, 120),
            combinator="or",
        ),
        webhook_call(
            w,
            "Trigger emergency workflow",
            "emergency-shutdown",
            "={{ JSON.stringify({ reason: $json.emergency_triggers?.[0]?.reason || 'BOT_HALTED', "
            "detail: $json.emergency_triggers?.[0]?.detail || ('monitor reported status ' + $json.bot_status), "
            "source: 'workflow-06' }) }}",
            (720, 20),
        ),
        if_node(
            w,
            "Anything happened?",
            [
                condition("={{ ($json.exits || []).length }}", "number", "gt", 0, cid="exits"),
                condition(
                    "={{ ($json.stop_updates || []).length }}", "number", "gt", 0, cid="stops"
                ),
                condition(
                    "={{ ($json.stale_symbols || []).length }}", "number", "gt", 0, cid="stale"
                ),
            ],
            (720, 220),
            combinator="or",
        ),
        code(
            w,
            "Summarise activity",
            """
const data = $input.first().json;
const exits = (data.exits || []).map((e) => `${e.symbol} ${e.reason} pnl=${e.realized_pnl}`);
const stops = (data.stop_updates || []).map((s) => `${s.symbol}: ${s.change}`);
return [{ json: {
  checked: data.checked,
  exits,
  stops,
  stale_symbols: data.stale_symbols || [],
  errors: data.errors || [],
  equity: data.equity,
  drawdown_pct: data.drawdown_pct,
  daily_pnl_pct: data.daily_pnl_pct,
  headline: [
    exits.length ? `${exits.length} exit(s): ${exits.join('; ')}` : null,
    stops.length ? `${stops.length} stop update(s)` : null,
    (data.stale_symbols || []).length ? `stale data: ${data.stale_symbols.join(',')}` : null,
  ].filter(Boolean).join(' | '),
} }];
""",
            (960, 160),
        ),
        notify_configured(w, "Notify configured?", (1200, 160)),
        notify(
            w,
            "Send position update",
            "`[MONITOR] ${$json.headline} | equity ${$json.equity} | dd "
            "${($json.drawdown_pct * 100).toFixed(2)}%`",
            (1440, 100),
        ),
        noop(w, "Nothing to report", (960, 320), notes="The usual outcome"),
        report_error(
            w,
            "Report monitoring failure",
            (480, 340),
            message_expr="'position monitoring failed: ' + ($json.error?.message || 'unknown error')",
            halt="true",
            severity="CRITICAL",
        ),
        webhook_call(
            w,
            "Escalate monitoring failure",
            "emergency-shutdown",
            "={{ JSON.stringify({ reason: 'EXCHANGE_FAILURE', "
            "detail: 'position monitoring failed — positions are unwatched', "
            "source: 'workflow-06' }) }}",
            (720, 420),
        ),
    ]
    connections = link(
        ("Every minute", "Monitor positions"),
        ("Monitor positions", "Emergency or halted?", 0),
        ("Monitor positions", "Report monitoring failure", 1),
        ("Emergency or halted?", "Trigger emergency workflow", 0),
        ("Emergency or halted?", "Anything happened?", 1),
        ("Anything happened?", "Summarise activity", 0),
        ("Anything happened?", "Nothing to report", 1),
        ("Summarise activity", "Notify configured?"),
        ("Notify configured?", "Send position update", 0),
        ("Notify configured?", "Nothing to report", 1),
        ("Report monitoring failure", "Escalate monitoring failure"),
    )
    return build(w, nodes, connections, "Scheduled position management and exit execution.")


def workflow_07() -> dict[str, Any]:
    w = "07 - Daily Performance Report"
    nodes = [
        sticky(
            w,
            "About",
            "## 7 · Daily Report\n\nBuilt from **stored rows** — trades, equity snapshots, risk "
            "decisions, AI decisions. The model summarises those figures and is told not to "
            "invent any.\n\nThe report includes the honest AI assessment: whether model "
            "confidence has predicted anything, or whether the sample is still too small to say.",
            (-460, -40),
            (420, 280),
        ),
        schedule(
            w,
            "Daily at 00:05 UTC",
            {"field": "cronExpression", "expression": "0 5 0 * * *"},
            (0, 140),
            notes="After the UTC day closes",
        ),
        http(
            w,
            "Generate daily report",
            "POST",
            "/reports/daily",
            (240, 140),
            body="={{ JSON.stringify({ day_offset: -1, include_ai_summary: true, persist: true }) }}",
            timeout=180000,
            on_error="continueErrorOutput",
        ),
        code(
            w,
            "Format report",
            """
const report = $input.first().json;
const facts = report.facts || {};
const account = facts.account || {};
const metrics = facts.metrics || {};
const ai = facts.ai_decisions || {};
const lines = [
  `Daily report ${report.period_start?.slice(0, 10)} (${facts.mode})`,
  `Equity ${Number(account.equity).toFixed(2)} | day ${Number(account.daily_pnl).toFixed(2)} ` +
    `(${(Number(account.daily_pnl_pct) * 100).toFixed(2)}%) | drawdown ` +
    `${(Number(account.drawdown_pct) * 100).toFixed(2)}%`,
  `Trades ${metrics.trades ?? 0} (W${metrics.wins ?? 0}/L${metrics.losses ?? 0}) ` +
    `net ${Number(metrics.net_pnl ?? 0).toFixed(2)} fees ${Number(metrics.fees_paid ?? 0).toFixed(2)}`,
  `Risk rejections: ${facts.risk_rejections_total ?? 0}`,
  `AI: ${ai.total ?? 0} decisions (${ai.hold ?? 0} hold, ${ai.parse_failures ?? 0} parse failures)`,
  `AI value: ${facts.ai_evaluation?.verdict ?? 'unknown'}`,
  facts.bot_status !== 'RUNNING' ? `BOT STATUS: ${facts.bot_status} (${facts.halt_reason})` : null,
  metrics.insufficient_data ? 'Sample too small for statistical claims.' : null,
].filter(Boolean);
return [{ json: {
  report_id: report.report_id,
  headline: lines.join('\\n'),
  markdown: report.markdown,
  bot_status: facts.bot_status,
  trades: metrics.trades ?? 0,
} }];
""",
            (480, 140),
        ),
        notify_configured(w, "Notify configured?", (720, 140)),
        notify(w, "Send daily report", "$json.headline", (960, 60)),
        noop(w, "Report stored", (960, 220), notes="Also available at GET /reports/daily/latest"),
        report_error(
            w,
            "Report generation failure",
            (480, 320),
            message_expr="'daily report generation failed: ' + ($json.error?.message || 'unknown error')",
            severity="WARNING",
        ),
    ]
    connections = link(
        ("Daily at 00:05 UTC", "Generate daily report"),
        ("Generate daily report", "Format report", 0),
        ("Generate daily report", "Report generation failure", 1),
        ("Format report", "Notify configured?"),
        ("Notify configured?", "Send daily report", 0),
        ("Notify configured?", "Report stored", 1),
        ("Send daily report", "Report stored"),
    )
    return build(w, nodes, connections, "Daily performance report with an AI summary of stored data.")


def workflow_08() -> dict[str, Any]:
    w = "08 - Emergency Shutdown"
    nodes = [
        sticky(
            w,
            "About",
            "## 8 · Emergency Shutdown\n\nThree ways in: another workflow escalates, a human "
            "POSTs the webhook, or the 5-minute sweep finds a breach.\n\nHalting stops new "
            "entries and **requires a manual reset** (`POST /bot/reset` with "
            "`confirmation: \"RESET\"`). Automation cannot un-halt itself.\n\nOpen positions are "
            "left alone unless `EMERGENCY_FLATTEN=true`: forced liquidation into a disorderly "
            "market is often worse than a position that still has a stop.",
            (-460, -60),
            (420, 360),
        ),
        webhook_trigger(w, "Emergency triggered", "emergency-shutdown", (0, 100)),
        schedule(
            w,
            "Safety sweep every 5 minutes",
            {"field": "minutes", "minutesInterval": 5},
            (0, 300),
            notes="Independent of any other workflow running",
        ),
        code(
            w,
            "Normalise trigger",
            webhook_guard()
            + """
const first = $input.first()?.json ?? {};
const body = first.body || first;
return [{ json: {
  reason: body.reason || 'MANUAL',
  detail: body.detail || 'scheduled safety sweep',
  source: body.source || 'workflow-08',
  explicit: Boolean(body.reason),
} }];
""",
            (240, 200),
        ),
        http(
            w,
            "Run safety checks",
            "POST",
            "/risk/safety-check",
            (480, 200),
            body="={{ JSON.stringify({ auto_halt: true }) }}",
            never_error=True,
            notes="Daily loss, drawdown, repeated order failures",
        ),
        code(
            w,
            "Decide action",
            """
const checks = $input.first().json;
const trigger = $('Normalise trigger').first().json;
const triggered = checks.triggered || [];
const mustHalt = trigger.explicit || triggered.length > 0 || checks.halted;
return [{ json: {
  must_halt: mustHalt,
  already_halted: Boolean(checks.halted),
  bot_status: checks.bot_status,
  reason: triggered[0]?.reason || trigger.reason,
  detail: triggered[0]?.detail || trigger.detail,
  source: trigger.source,
  triggers: triggered,
} }];
""",
            (720, 200),
        ),
        if_node(
            w,
            "Halt required?",
            [condition("={{ $json.must_halt }}", "boolean", "true", cid="halt")],
            (960, 200),
        ),
        http(
            w,
            "Halt the bot",
            "POST",
            "/bot/halt",
            (1200, 100),
            body="={{ JSON.stringify({ reason: $json.reason, detail: $json.detail, "
            "source: $json.source, close_positions: $env.EMERGENCY_FLATTEN === 'true' }) }}",
            timeout=120000,
            never_error=True,
            notes="close_positions is opt-in via EMERGENCY_FLATTEN",
        ),
        http(w, "Confirm bot state", "GET", "/bot/state", (1440, 100)),
        code(
            w,
            "Build alert",
            """
const state = $input.first().json;
const halt = $('Halt the bot').first().json;
return [{ json: {
  status: state.status,
  reason: state.halt_reason,
  detail: state.halt_detail,
  halted_at: state.halted_at,
  positions_closed: halt.positions_closed ?? 0,
  requires_manual_reset: state.requires_manual_reset,
  headline:
    `TRADING HALTED: ${state.halt_reason} — ${state.halt_detail || ''} | ` +
    `positions closed: ${halt.positions_closed ?? 0} | ` +
    'manual reset required: POST /bot/reset {"confirmation":"RESET"}',
} }];
""",
            (1680, 100),
        ),
        notify_configured(w, "Notify configured?", (1920, 100)),
        notify(w, "Send emergency alert", "$json.headline", (2160, 20)),
        noop(
            w,
            "HALTED — awaiting manual reset",
            (2160, 180),
            notes="No workflow can clear this state",
        ),
        noop(w, "No action needed", (1200, 320), notes="All safety checks passed"),
    ]
    connections = link(
        ("Emergency triggered", "Normalise trigger"),
        ("Safety sweep every 5 minutes", "Normalise trigger"),
        ("Normalise trigger", "Run safety checks"),
        ("Run safety checks", "Decide action"),
        ("Decide action", "Halt required?"),
        ("Halt required?", "Halt the bot", 0),
        ("Halt required?", "No action needed", 1),
        ("Halt the bot", "Confirm bot state"),
        ("Confirm bot state", "Build alert"),
        ("Build alert", "Notify configured?"),
        ("Notify configured?", "Send emergency alert", 0),
        ("Notify configured?", "HALTED — awaiting manual reset", 1),
        ("Send emergency alert", "HALTED — awaiting manual reset"),
    )
    return build(w, nodes, connections, "Kill switch: halt trading and require a manual reset.")


def workflow_09() -> dict[str, Any]:
    w = "09 - Error Handler"
    nodes = [
        sticky(
            w,
            "About",
            "## 9 · Error Handler\n\nSet this workflow as the **Error Workflow** in every other "
            "workflow's settings (Settings → Error Workflow).\n\nIt records the failure "
            "server-side with workflow, node and execution id, and escalates to the emergency "
            "workflow when the failing stage means trading is no longer safe — execution and "
            "monitoring failures, not analysis ones.\n\nNo failure is swallowed.",
            (-460, -40),
            (420, 320),
        ),
        node(w, "On workflow error", "n8n-nodes-base.errorTrigger", 1, {}, (0, 160)),
        code(
            w,
            "Extract error context",
            """
const data = $input.first().json;
const workflowName = data.workflow?.name || 'unknown workflow';
const execution = data.execution || {};
const node = execution.lastNodeExecuted || data.node?.name || null;
const message = execution.error?.message || data.error?.message || 'unknown error';

// A failure in execution or monitoring means positions may be unmanaged, so it
// is unsafe to keep trading. An analysis failure only costs an opportunity.
const unsafe = /execution|monitoring|risk/i.test(workflowName);

return [{ json: {
  workflow: workflowName,
  node,
  execution_id: String(execution.id ?? ''),
  message,
  stack: execution.error?.stack ? String(execution.error.stack).slice(0, 2000) : null,
  mode: execution.mode || null,
  unsafe,
  severity: unsafe ? 'CRITICAL' : 'ERROR',
} }];
""",
            (240, 160),
        ),
        http(
            w,
            "Record failure",
            "POST",
            "/workflow/error",
            (480, 160),
            body="={{ JSON.stringify({ workflow: $json.workflow, message: $json.message, "
            "node: $json.node, execution_id: $json.execution_id, severity: $json.severity, "
            "halt_bot: $json.unsafe, context: { stack: $json.stack, mode: $json.mode } }) }}",
            never_error=True,
            notes="Recorded as a system event; halts the bot when unsafe",
        ),
        if_node(
            w,
            "Unsafe to continue?",
            [condition("={{ $json.halted }}", "boolean", "true", cid="halted")],
            (720, 160),
        ),
        webhook_call(
            w,
            "Escalate to emergency workflow",
            "emergency-shutdown",
            "={{ JSON.stringify({ reason: 'WORKFLOW_FAILURE', "
            "detail: $('Extract error context').first().json.workflow + ': ' + "
            "$('Extract error context').first().json.message, source: 'workflow-09' }) }}",
            (960, 80),
        ),
        notify_configured(w, "Notify configured?", (960, 260)),
        notify(
            w,
            "Send failure alert",
            "`[${$('Extract error context').first().json.severity}] "
            "${$('Extract error context').first().json.workflow} failed at node "
            "${$('Extract error context').first().json.node}: "
            "${$('Extract error context').first().json.message}`",
            (1200, 200),
        ),
        noop(w, "Failure recorded", (1200, 340)),
    ]
    connections = link(
        ("On workflow error", "Extract error context"),
        ("Extract error context", "Record failure"),
        ("Record failure", "Unsafe to continue?"),
        ("Unsafe to continue?", "Escalate to emergency workflow", 0),
        ("Unsafe to continue?", "Notify configured?", 1),
        ("Escalate to emergency workflow", "Notify configured?"),
        ("Notify configured?", "Send failure alert", 0),
        ("Notify configured?", "Failure recorded", 1),
        ("Send failure alert", "Failure recorded"),
    )
    return build(w, nodes, connections, "Central error handler for every trading workflow.")


WORKFLOWS = {
    "01-market-data-collection.json": workflow_01,
    "02-market-analysis.json": workflow_02,
    "03-ai-trade-evaluation.json": workflow_03,
    "04-risk-validation.json": workflow_04,
    "05-trade-execution.json": workflow_05,
    "06-position-monitoring.json": workflow_06,
    "07-daily-report.json": workflow_07,
    "08-emergency-shutdown.json": workflow_08,
    "09-error-handler.json": workflow_09,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, factory in WORKFLOWS.items():
        workflow = factory()
        path = OUT_DIR / filename
        path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        node_count = sum(
            1 for n in workflow["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"
        )
        print(f"wrote {path.relative_to(ROOT)} ({node_count} nodes)")


if __name__ == "__main__":
    main()
