"""Performance analytics, including an honest assessment of whether the AI helps.

The AI evaluation here is built to be able to return "no, it doesn't". It
compares outcomes the model approved against outcomes it vetoed, buckets results
by stated confidence, and reports the sample size next to every number so a
12-trade "edge" is visible as the noise it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import Settings, get_settings
from app.core.numeric import ZERO, safe_div
from app.database.repositories import (
    AIRepository,
    ExecutionRepository,
    PerformanceRepository,
    RiskRepository,
)
from app.analytics.metrics import (
    EquityPointLite,
    TradeSummary,
    compute_metrics,
    trade_from_record,
)
from app.models.ai import PerformanceContext
from app.models.backtest import PerformanceMetrics
from app.utils.time import start_of_utc_day, utcnow

CONFIDENCE_BUCKETS = ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


class PerformanceAnalytics:
    def __init__(
        self,
        execution_repo: ExecutionRepository,
        performance_repo: PerformanceRepository,
        ai_repo: AIRepository,
        risk_repo: RiskRepository,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.execution = execution_repo
        self.performance = performance_repo
        self.ai = ai_repo
        self.risk = risk_repo

    # ----------------------------------------------------------------- core
    def metrics(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> PerformanceMetrics:
        end = end or utcnow()
        trades = (
            self.execution.trades_between(start, end)
            if start
            else self.execution.all_trades()
        )
        summaries = [trade_from_record(record) for record in trades]
        curve = [
            EquityPointLite(timestamp=point.taken_at, equity=point.equity)
            for point in self.performance.equity_curve(start=start)
        ]
        starting = (
            curve[0].equity
            if curve
            else Decimal(str(self.settings.paper_starting_balance))
        )
        return compute_metrics(summaries, curve, starting)

    def strategy_breakdown(self) -> dict[str, dict[str, Any]]:
        return self.metrics().per_strategy

    def regime_breakdown(self) -> dict[str, dict[str, Any]]:
        return self.metrics().per_regime

    def persist_strategy_performance(self) -> None:
        metrics = self.metrics()
        for strategy, stats in metrics.per_strategy.items():
            self.performance.upsert_strategy_performance(
                strategy,
                "all",
                {
                    "trades": stats.get("trades", 0),
                    "wins": stats.get("wins", 0),
                    "losses": stats.get("losses", 0),
                    "net_pnl": Decimal(str(stats.get("net_pnl", 0))),
                    "win_rate": stats.get("win_rate"),
                    "profit_factor": stats.get("profit_factor"),
                    "expectancy_r": stats.get("expectancy_r"),
                    "average_r": stats.get("expectancy_r"),
                },
            )

    # -------------------------------------------------------- AI evaluation
    def ai_evaluation(self, lookback_days: int = 90) -> dict[str, Any]:
        """Does the AI layer actually add value? Answered with data or not at all."""
        since = utcnow() - timedelta(days=lookback_days)
        decisions = self.ai.decisions_between(since, utcnow())
        trades = {
            record.ai_decision_id: record
            for record in self.execution.all_trades()
            if record.ai_decision_id
        }

        linked: list[tuple[Any, Any]] = []
        approvals = vetoes = 0
        for decision in decisions:
            if decision.decision in ("BUY", "SELL"):
                approvals += 1
            else:
                vetoes += 1
            trade = trades.get(decision.id)
            if trade is not None:
                linked.append((decision, trade))

        if not linked:
            return {
                "verdict": "insufficient_data",
                "detail": (
                    f"{len(decisions)} AI decisions in the window, none linked to a "
                    "closed trade yet — no claim can be made about AI value"
                ),
                "decisions": len(decisions),
                "approvals": approvals,
                "vetoes": vetoes,
                "linked_trades": 0,
                "confidence_buckets": [],
            }

        buckets: list[dict[str, Any]] = []
        for low, high in CONFIDENCE_BUCKETS:
            subset = [
                (decision, trade)
                for decision, trade in linked
                if low <= decision.confidence < high
            ]
            if not subset:
                continue
            r_values = [
                trade.r_multiple for _, trade in subset if trade.r_multiple is not None
            ]
            wins = sum(1 for _, trade in subset if trade.pnl > 0)
            buckets.append(
                {
                    "confidence_range": f"{low:.2f}-{high:.2f}",
                    "trades": len(subset),
                    "win_rate": round(wins / len(subset), 4),
                    "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
                    "net_pnl": float(sum((trade.pnl for _, trade in subset), start=ZERO)),
                }
            )

        correlation = _correlation(
            [decision.confidence for decision, trade in linked if trade.r_multiple is not None],
            [trade.r_multiple for _, trade in linked if trade.r_multiple is not None],
        )

        sample = len(linked)
        if sample < 30:
            verdict = "insufficient_data"
            detail = (
                f"{sample} AI-linked closed trades. At least 30 are needed before any "
                "statement about predictive value is more than noise."
            )
        elif correlation is None:
            verdict = "inconclusive"
            detail = "confidence values show no variance, so no relationship can be measured"
        elif correlation > 0.2:
            verdict = "positive_signal"
            detail = (
                f"confidence correlates {correlation:+.2f} with realised R over "
                f"{sample} trades — weak evidence the AI adds information"
            )
        elif correlation < -0.2:
            verdict = "negative_signal"
            detail = (
                f"confidence correlates {correlation:+.2f} with realised R over "
                f"{sample} trades — higher AI confidence has been *worse*; consider "
                "disabling the AI gate"
            )
        else:
            verdict = "no_measurable_edge"
            detail = (
                f"confidence correlates {correlation:+.2f} with realised R over "
                f"{sample} trades — no measurable relationship; the AI is not adding "
                "value beyond the deterministic layer"
            )

        return {
            "verdict": verdict,
            "detail": detail,
            "decisions": len(decisions),
            "approvals": approvals,
            "vetoes": vetoes,
            "linked_trades": sample,
            "confidence_vs_r_correlation": correlation,
            "confidence_buckets": buckets,
            "by_regime": _group_by(linked, lambda decision, _: decision.market_regime or "unknown"),
            "by_decision": _group_by(linked, lambda decision, _: decision.decision),
        }

    # --------------------------------------------------------- AI context
    def performance_context(self) -> PerformanceContext:
        all_trades = self.execution.all_trades()
        recent = self.execution.trades_between(utcnow() - timedelta(days=7), utcnow())
        metrics = self.metrics()
        per_strategy = metrics.per_strategy or {}
        ranked = sorted(
            (
                (name, stats)
                for name, stats in per_strategy.items()
                if stats.get("trades", 0) >= 3
            ),
            key=lambda item: item[1].get("expectancy_r") or -999,
        )
        return PerformanceContext(
            trades_total=len(all_trades),
            trades_last_7d=len(recent),
            win_rate=metrics.win_rate,
            profit_factor=metrics.profit_factor,
            expectancy_r=metrics.expectancy_r,
            avg_win_r=metrics.avg_win_r,
            avg_loss_r=metrics.avg_loss_r,
            best_strategy=ranked[-1][0] if ranked else None,
            worst_strategy=ranked[0][0] if ranked else None,
        )

    # ----------------------------------------------------------- reporting
    def daily_summary(self, day_start: datetime | None = None) -> dict[str, Any]:
        start = day_start or start_of_utc_day()
        end = start + timedelta(days=1)
        trades = self.execution.trades_between(start, end)
        summaries: list[TradeSummary] = [trade_from_record(t) for t in trades]
        curve = [
            EquityPointLite(timestamp=point.taken_at, equity=point.equity)
            for point in self.performance.equity_curve(start=start)
        ]
        starting = (
            curve[0].equity if curve else Decimal(str(self.settings.paper_starting_balance))
        )
        metrics = compute_metrics(summaries, curve, starting)
        rejections = self.risk.rejection_counts(start)
        ai_decisions = self.ai.decisions_between(start, end)

        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "mode": self.execution.mode,
            "metrics": metrics.model_dump(mode="json"),
            "trades": [
                {
                    "symbol": trade.symbol,
                    "strategy": trade.strategy,
                    "regime": trade.regime,
                    "entry_price": float(trade.entry_price),
                    "exit_price": float(trade.exit_price),
                    "pnl": float(trade.pnl),
                    "r_multiple": trade.r_multiple,
                    "exit_reason": trade.exit_reason,
                    "holding_minutes": trade.holding_minutes,
                }
                for trade in trades
            ],
            "risk_rejections": rejections,
            "risk_rejections_total": sum(rejections.values()),
            "ai_decisions": {
                "total": len(ai_decisions),
                "buy": sum(1 for d in ai_decisions if d.decision == "BUY"),
                "sell": sum(1 for d in ai_decisions if d.decision == "SELL"),
                "hold": sum(1 for d in ai_decisions if d.decision == "HOLD"),
                "parse_failures": sum(1 for d in ai_decisions if not d.parse_ok),
                "fallbacks": sum(1 for d in ai_decisions if d.fallback_used),
            },
            "ai_evaluation": self.ai_evaluation(),
            "open_positions": [
                {
                    "symbol": record.symbol,
                    "quantity": float(record.quantity),
                    "entry_price": float(record.entry_price),
                    "mark_price": float(record.mark_price or record.entry_price),
                    "strategy": record.strategy,
                }
                for record in self.execution.open_positions()
            ],
        }


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(covariance / ((var_x**0.5) * (var_y**0.5)), 4)


def _group_by(linked, key) -> dict[str, dict[str, Any]]:  # noqa: ANN001
    groups: dict[str, list[tuple[Any, Any]]] = {}
    for decision, trade in linked:
        groups.setdefault(key(decision, trade), []).append((decision, trade))
    output: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        r_values = [trade.r_multiple for _, trade in group if trade.r_multiple is not None]
        wins = sum(1 for _, trade in group if trade.pnl > 0)
        output[name] = {
            "trades": len(group),
            "win_rate": round(wins / len(group), 4),
            "avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "net_pnl": float(sum((trade.pnl for _, trade in group), start=ZERO)),
        }
    return output
