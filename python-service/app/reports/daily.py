"""Daily report generation (n8n Workflow 7).

Every number comes from stored rows — trades, equity snapshots, risk decisions,
AI decisions. The LLM is handed those figures and asked to summarise them; it is
explicitly told not to invent numbers, and the deterministic metrics are stored
alongside the prose so the two can always be compared.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.ai.parser import extract_json_object
from app.core.errors import LLMError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.database.repositories import new_id
from app.utils.time import start_of_utc_day, utcnow

logger = get_logger(__name__)

REPORT_SYSTEM_PROMPT = """\
You are writing the daily operations report for an automated crypto trading system.

Rules:
- Use ONLY the figures provided. Never invent, extrapolate or round away a loss.
- If the day had no trades, say so plainly; do not fill space.
- Point out anything that looks wrong: repeated risk rejections of the same kind,
  AI parse failures, an unusual number of stop-outs, a growing drawdown.
- Small samples prove nothing. Do not describe a handful of trades as an edge.
- Be blunt about bad days. The reader needs an accurate picture, not reassurance.

Reply with one JSON object:
{
  "summary": "2-4 sentences on what happened",
  "traded": "what was traded and why, or 'nothing'",
  "rejected": "what was rejected and the dominant reasons",
  "pnl_assessment": "plain reading of the day's P&L and drawdown",
  "best_strategy": "name or 'insufficient data'",
  "worst_strategy": "name or 'insufficient data'",
  "market_regime": "what regimes dominated",
  "unusual_events": ["..."],
  "system_health": "normal" | "degraded" | "needs_attention",
  "recommendations": ["..."]
}
"""


def build_daily_report(
    services,  # noqa: ANN001 - app.container.Services
    *,
    day_offset: int = 0,
    include_ai_summary: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    start = start_of_utc_day() + timedelta(days=day_offset)
    end = start + timedelta(days=1)
    summary = services.analytics.daily_summary(start)
    snapshot = services.portfolio.snapshot()
    state = services.bot_repo.get()
    events = services.event_repo.recent(200)
    errors = [
        {
            "event_type": record.event_type,
            "message": record.message,
            "symbol": record.symbol,
            "at": record.created_at.isoformat(),
        }
        for record in events
        if record.level in ("ERROR", "CRITICAL") and record.created_at >= start
    ]
    emergencies = [
        {
            "reason": record.reason,
            "detail": record.detail,
            "at": record.triggered_at.isoformat(),
        }
        for record in services.event_repo.recent_emergencies(10)
        if record.triggered_at >= start
    ]

    facts: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "mode": services.mode,
        "bot_status": state.status,
        "halt_reason": state.halt_reason,
        "account": {
            "equity": float(snapshot.equity),
            "cash": float(snapshot.cash),
            "starting_equity": float(snapshot.starting_equity),
            "daily_pnl": float(snapshot.daily_pnl),
            "daily_pnl_pct": float(snapshot.daily_pnl_pct),
            "total_pnl_pct": float(snapshot.total_pnl_pct),
            "drawdown_pct": float(snapshot.drawdown_pct),
            "exposure_pct": float(snapshot.exposure_pct),
            "open_positions": snapshot.open_positions,
        },
        "metrics": summary["metrics"],
        "trades": summary["trades"],
        "risk_rejections": summary["risk_rejections"],
        "ai_decisions": summary["ai_decisions"],
        "ai_evaluation": summary["ai_evaluation"],
        "open_positions": summary["open_positions"],
        "errors": errors[:20],
        "emergency_events": emergencies,
        "strategy_signals": services.signal_repo.signal_counts_by_strategy(start),
    }

    ai_summary: str | None = None
    ai_provider: str | None = None
    if include_ai_summary:
        ai_summary, ai_provider = _ai_summary(services, facts)

    markdown = _render_markdown(facts, ai_summary)
    report_id = new_id("rep_")

    if persist:
        services.performance_repo.save_report(
            {
                "id": report_id,
                "kind": "daily",
                "period_start": start,
                "period_end": end,
                "mode": services.mode,
                "metrics": facts,
                "ai_summary": ai_summary,
                "ai_provider": ai_provider,
                "body_markdown": markdown,
            }
        )
        services.analytics.persist_strategy_performance()

    log_event(
        logger,
        EventType.DAILY_REPORT_GENERATED,
        report_id=report_id,
        trades=facts["metrics"].get("trades"),
        daily_pnl=facts["account"]["daily_pnl"],
        drawdown_pct=facts["account"]["drawdown_pct"],
        ai_summary_included=bool(ai_summary),
    )
    return {
        "report_id": report_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "facts": facts,
        "ai_summary": ai_summary,
        "ai_provider": ai_provider,
        "markdown": markdown,
        "generated_at": utcnow().isoformat(),
    }


def _ai_summary(services, facts: dict[str, Any]) -> tuple[str | None, str | None]:  # noqa: ANN001
    provider = services.ai.provider
    if not provider.enabled:
        return None, "disabled"
    try:
        response = provider.complete_json(
            system_prompt=REPORT_SYSTEM_PROMPT,
            user_prompt=(
                "Write today's report from these figures.\n\n"
                + json.dumps(facts, indent=2, default=str)
            ),
            temperature=0.2,
            max_tokens=1200,
        )
    except LLMError as exc:
        logger.warning(
            "daily report AI summary failed",
            extra={"event": str(EventType.SYSTEM_ERROR), "error": str(exc.detail)},
        )
        return None, provider.name

    payload = extract_json_object(response.text)
    if payload is None:
        return response.text.strip()[:4000] or None, response.provider
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return response.text.strip()[:4000] or None, response.provider
    return json.dumps(parsed, indent=2), response.provider


def _render_markdown(facts: dict[str, Any], ai_summary: str | None) -> str:
    account = facts["account"]
    metrics = facts["metrics"]
    lines = [
        f"# Daily trading report — {facts['period']['start'][:10]}",
        "",
        f"- Mode: **{facts['mode']}** (bot status: {facts['bot_status']})",
        f"- Equity: **{account['equity']:.2f}** "
        f"(daily P&L {account['daily_pnl']:+.2f} / {account['daily_pnl_pct'] * 100:+.2f}%)",
        f"- Drawdown from peak: {account['drawdown_pct'] * 100:.2f}%",
        f"- Open positions: {account['open_positions']}, exposure "
        f"{account['exposure_pct'] * 100:.1f}% of equity",
        "",
        "## Trading",
        f"- Closed trades: {metrics.get('trades', 0)} "
        f"(wins {metrics.get('wins', 0)}, losses {metrics.get('losses', 0)})",
        f"- Net P&L: {float(metrics.get('net_pnl', 0)):+.2f}, fees "
        f"{float(metrics.get('fees_paid', 0)):.2f}",
        f"- Expectancy (R): {metrics.get('expectancy_r')}, profit factor "
        f"{metrics.get('profit_factor')}",
    ]
    if metrics.get("insufficient_data"):
        lines.append(
            "- **Sample too small for any statistical claim.** "
            + "; ".join(metrics.get("notes", []))
        )

    lines += ["", "## Risk", f"- Rejections: {facts['risk_rejections'] or 'none'}"]
    ai = facts["ai_decisions"]
    lines += [
        "",
        "## AI layer",
        f"- Decisions: {ai['total']} (buy {ai['buy']}, sell {ai['sell']}, hold {ai['hold']})",
        f"- Parse failures: {ai['parse_failures']}, fallbacks to HOLD: {ai['fallbacks']}",
        f"- Value assessment: {facts['ai_evaluation']['verdict']} — "
        f"{facts['ai_evaluation']['detail']}",
    ]
    if facts["emergency_events"]:
        lines += ["", "## Emergency events"]
        lines += [
            f"- {event['at']}: {event['reason']} — {event['detail']}"
            for event in facts["emergency_events"]
        ]
    if facts["errors"]:
        lines += ["", "## Errors"]
        lines += [
            f"- {error['at']}: [{error['event_type']}] {error['message']}"
            for error in facts["errors"][:10]
        ]
    if ai_summary:
        lines += ["", "## AI summary", "```json", ai_summary, "```"]
    lines += [
        "",
        "---",
        "_Figures are computed from stored trades and equity snapshots. Paper-mode "
        "results overstate live performance: real fills, latency and liquidity are worse._",
    ]
    return "\n".join(lines)
