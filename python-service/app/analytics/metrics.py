"""Performance metrics — one implementation for backtests, paper and live.

Everything is computed from realised trades and the equity curve. There is no
smoothing, no dropping of "outlier" trades, and no annualising of a two-week
sample into a headline number without saying so: when the sample is too small to
support a statistic, the field is ``None`` and ``insufficient_data`` is set.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.core.numeric import ZERO, round_money, safe_div
from app.models.backtest import PerformanceMetrics

# Below this many trades, ratios are noise rather than evidence.
MIN_TRADES_FOR_RATIOS = 10
SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True)
class TradeSummary:
    """Normalised view of a closed trade, whatever produced it."""

    pnl: Decimal
    fees: Decimal
    entry_time: datetime
    exit_time: datetime
    strategy: str | None = None
    regime: str | None = None
    symbol: str | None = None
    r_multiple: float | None = None
    bars_held: int | None = None
    exit_reason: str | None = None
    notional: Decimal | None = None

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl < 0


@dataclass(frozen=True)
class EquityPointLite:
    timestamp: datetime
    equity: Decimal


def trade_from_record(record) -> TradeSummary:
    return TradeSummary(
        pnl=record.pnl,
        fees=record.fees or ZERO,
        entry_time=record.entry_time,
        exit_time=record.exit_time,
        strategy=record.strategy,
        regime=record.regime,
        symbol=record.symbol,
        r_multiple=record.r_multiple,
        bars_held=None,
        exit_reason=record.exit_reason,
        notional=(record.entry_price or ZERO) * (record.quantity or ZERO),
    )


def compute_metrics(
    trades: Sequence[TradeSummary],
    equity_curve: Sequence[EquityPointLite],
    starting_equity: Decimal,
    *,
    ending_equity: Decimal | None = None,
) -> PerformanceMetrics:
    notes: list[str] = []
    wins = [trade for trade in trades if trade.is_win]
    losses = [trade for trade in trades if trade.is_loss]
    breakeven = [trade for trade in trades if not trade.is_win and not trade.is_loss]

    gross_profit = sum((trade.pnl for trade in wins), start=ZERO)
    gross_loss = sum((abs(trade.pnl) for trade in losses), start=ZERO)
    net_pnl = sum((trade.pnl for trade in trades), start=ZERO)
    fees_paid = sum((trade.fees for trade in trades), start=ZERO)

    final_equity = ending_equity
    if final_equity is None:
        final_equity = (
            equity_curve[-1].equity if equity_curve else starting_equity + net_pnl
        )

    metrics = PerformanceMetrics(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(breakeven),
        starting_equity=round_money(starting_equity, 8),
        ending_equity=round_money(final_equity, 8),
        gross_profit=round_money(gross_profit, 8),
        gross_loss=round_money(gross_loss, 8),
        net_pnl=round_money(net_pnl, 8),
        fees_paid=round_money(fees_paid, 8),
        total_return_pct=safe_div(final_equity - starting_equity, starting_equity),
    )

    if trades:
        metrics.win_rate = round(len(wins) / len(trades), 6)
        metrics.average_win = round_money(safe_div(gross_profit, Decimal(len(wins))), 8) if wins else None
        metrics.average_loss = (
            round_money(safe_div(gross_loss, Decimal(len(losses))), 8) if losses else None
        )
        metrics.largest_win = max((trade.pnl for trade in wins), default=None)
        metrics.largest_loss = min((trade.pnl for trade in losses), default=None)
        metrics.expectancy = round_money(safe_div(net_pnl, Decimal(len(trades))), 8)
        if gross_loss > ZERO:
            metrics.profit_factor = round(float(gross_profit / gross_loss), 6)
        elif gross_profit > ZERO:
            # No losses at all: profit factor is undefined, not infinite.
            metrics.profit_factor = None
            notes.append("profit factor undefined: no losing trades in the sample")
        r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
        if r_values:
            metrics.average_r = round(sum(r_values) / len(r_values), 6)
            metrics.expectancy_r = metrics.average_r
        win_r = [t.r_multiple for t in wins if t.r_multiple is not None]
        loss_r = [t.r_multiple for t in losses if t.r_multiple is not None]
        metrics.per_strategy = _group_metrics(trades, key=lambda t: t.strategy or "unknown")
        metrics.per_regime = _group_metrics(trades, key=lambda t: t.regime or "unknown")
        metrics.consecutive_wins_max, metrics.consecutive_losses_max = _streaks(trades)
        bars = [t.bars_held for t in trades if t.bars_held is not None]
        if bars:
            metrics.average_bars_held = round(sum(bars) / len(bars), 3)
        if win_r:
            metrics.avg_win_r = round(sum(win_r) / len(win_r), 6)
        if loss_r:
            metrics.avg_loss_r = round(sum(loss_r) / len(loss_r), 6)

    drawdown, duration = max_drawdown(equity_curve)
    metrics.max_drawdown_pct = drawdown
    metrics.max_drawdown_duration_bars = duration

    returns, periods_per_year = _periodic_returns(equity_curve)
    if len(returns) >= 3:
        metrics.sharpe_ratio = sharpe_ratio(returns, periods_per_year)
        metrics.sortino_ratio = sortino_ratio(returns, periods_per_year)
        if drawdown > ZERO:
            years = _years_covered(equity_curve)
            if years > 0:
                annualised = (
                    float(safe_div(final_equity, starting_equity)) ** (1 / years) - 1
                    if starting_equity > ZERO
                    else 0.0
                )
                metrics.calmar_ratio = round(annualised / float(drawdown), 6)
    else:
        notes.append("equity curve too short for risk-adjusted ratios")

    if len(trades) < MIN_TRADES_FOR_RATIOS:
        metrics.insufficient_data = True
        notes.append(
            f"only {len(trades)} trades: treat every ratio here as indicative, not evidence"
        )

    metrics.notes = notes
    return metrics


def max_drawdown(equity_curve: Sequence[EquityPointLite]) -> tuple[Decimal, int]:
    """Largest peak-to-trough decline and how long (in points) it lasted."""
    if not equity_curve:
        return ZERO, 0
    peak = equity_curve[0].equity
    peak_index = 0
    max_dd = ZERO
    max_duration = 0
    for index, point in enumerate(equity_curve):
        if point.equity > peak:
            peak = point.equity
            peak_index = index
        elif peak > ZERO:
            drawdown = (peak - point.equity) / peak
            if drawdown > max_dd:
                max_dd = drawdown
                max_duration = index - peak_index
    return max_dd, max_duration


def sharpe_ratio(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 3:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return round(mean / std * math.sqrt(periods_per_year), 6)


def sortino_ratio(
    returns: Sequence[float], periods_per_year: float, target: float = 0.0
) -> float | None:
    if len(returns) < 3:
        return None
    mean = sum(returns) / len(returns)
    downside = [min(0.0, value - target) for value in returns]
    downside_variance = sum(value**2 for value in downside) / (len(returns) - 1)
    downside_std = math.sqrt(downside_variance)
    if downside_std == 0:
        # No downside deviation in the sample — meaningless rather than infinite.
        return None
    return round((mean - target) / downside_std * math.sqrt(periods_per_year), 6)


def _periodic_returns(
    equity_curve: Sequence[EquityPointLite],
) -> tuple[list[float], float]:
    if len(equity_curve) < 2:
        return [], 1.0
    returns: list[float] = []
    for previous, current in pairwise(equity_curve):
        if previous.equity > ZERO:
            returns.append(float((current.equity - previous.equity) / previous.equity))
    deltas = [
        (current.timestamp - previous.timestamp).total_seconds()
        for previous, current in pairwise(equity_curve)
        if (current.timestamp - previous.timestamp).total_seconds() > 0
    ]
    if deltas:
        deltas.sort()
        median = deltas[len(deltas) // 2]
        periods_per_year = SECONDS_PER_YEAR / median if median > 0 else 1.0
    else:
        periods_per_year = 1.0
    return returns, periods_per_year


def _years_covered(equity_curve: Sequence[EquityPointLite]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    span = (equity_curve[-1].timestamp - equity_curve[0].timestamp).total_seconds()
    return span / SECONDS_PER_YEAR if span > 0 else 0.0


def _group_metrics(trades: Sequence[TradeSummary], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[TradeSummary]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)
    output: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        wins = [t for t in group if t.is_win]
        losses = [t for t in group if t.is_loss]
        gross_profit = sum((t.pnl for t in wins), start=ZERO)
        gross_loss = sum((abs(t.pnl) for t in losses), start=ZERO)
        r_values = [t.r_multiple for t in group if t.r_multiple is not None]
        output[name] = {
            "trades": len(group),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(group), 6) if group else None,
            "net_pnl": float(sum((t.pnl for t in group), start=ZERO)),
            "profit_factor": round(float(gross_profit / gross_loss), 6)
            if gross_loss > ZERO
            else None,
            "expectancy_r": round(sum(r_values) / len(r_values), 6) if r_values else None,
        }
    return output


def _streaks(trades: Sequence[TradeSummary]) -> tuple[int, int]:
    best_wins = best_losses = current_wins = current_losses = 0
    for trade in trades:
        if trade.is_win:
            current_wins += 1
            current_losses = 0
        elif trade.is_loss:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        best_wins = max(best_wins, current_wins)
        best_losses = max(best_losses, current_losses)
    return best_wins, best_losses
