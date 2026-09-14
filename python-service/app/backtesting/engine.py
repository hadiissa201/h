"""Event-driven backtester.

It reuses the *live* code for everything that decides or prices a trade:
``FeatureEngine``, ``RegimeDetector``, ``StrategyEngine``, ``RiskEngine``,
``calculate_position_size``, the ``fill_model`` and ``exit_rules``. Only the
account and the order book are simulated in memory. That is the whole point: a
result produced by a parallel implementation would say nothing about the system
that actually trades.

Look-ahead protections, each of which is tested:

* a signal generated on bar *i* is filled at the **open of bar i+1**, never at
  bar *i*'s close;
* indicators are causal, and ``FeatureSet.row(i)`` exposes only rows up to *i*;
* swing/pivot features are shifted by their confirmation delay;
* when a bar's range covers both stop and target, the **stop** is taken;
* the first ``warmup_bars`` bars are never traded.

Deliberate limitations, stated rather than hidden:

* the LLM layer is **not** replayed — a backtest measures the deterministic
  system. AI contribution is evaluated from recorded live/paper decisions instead;
* one position at a time, one symbol per run;
* fills assume the full size trades at the modelled price: no queue position, no
  partial liquidity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from app.core.numeric import ZERO, round_money, safe_div, to_decimal
from app.execution.fill_model import CostModel, simulate_market_fill
from app.features import FeatureConfig, FeatureEngine
from app.models.backtest import (
    BacktestTrade,
    EquityPoint,
    PerformanceMetrics,
)
from app.models.enums import (
    BotStatus,
    ExitReason,
    PositionSide,
    Side,
    SignalDirection,
    TradingModeEnum,
)
from app.models.risk import AccountRiskState, RiskLimitsView, RiskProposal
from app.models.trading import SymbolSpec
from app.portfolio.exit_rules import BarPrices, PositionState, evaluate_exit, update_stops
from app.regime import RegimeDetector
from app.risk.engine import MarketConditions, RiskEngine
from app.strategies import StrategyEngine
from app.analytics.metrics import EquityPointLite, TradeSummary, compute_metrics
from app.utils.time import ensure_utc


@dataclass
class _OpenPosition:
    entry_time: datetime
    entry_price: Decimal
    quantity: Decimal
    state: PositionState
    strategy: str
    regime: str
    entry_fee: Decimal
    risk_amount: Decimal
    take_profit: Decimal | None
    bars_held: int = 0
    max_favorable: Decimal = ZERO
    max_adverse: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    exits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BacktestConfig:
    starting_balance: Decimal = Decimal("10000")
    cost_model: CostModel = field(default_factory=CostModel)
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    limits: RiskLimitsView | None = None
    spec: SymbolSpec | None = None
    warmup_bars: int | None = None
    allow_short: bool = False
    enforce_daily_loss_limit: bool = True
    enforce_drawdown_halt: bool = True


@dataclass
class BacktestOutput:
    trades: list[BacktestTrade]
    equity_curve: list[EquityPoint]
    metrics: PerformanceMetrics
    risk_rejections: dict[str, int]
    rejected_signals: int
    warnings: list[str]
    bars_tested: int
    period_start: datetime | None
    period_end: datetime | None
    halted: bool = False
    halt_reason: str | None = None


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        strategies: StrategyEngine,
        features: FeatureEngine | None = None,
        regime: RegimeDetector | None = None,
    ) -> None:
        self.config = config
        self.strategies = strategies
        self.features = features or FeatureEngine(config=config.feature_config)
        self.regime = regime or RegimeDetector()
        if config.limits is None:
            raise ValueError("BacktestConfig.limits is required")
        self.limits = config.limits
        self.risk = RiskEngine(self.limits, require_microstructure=False)
        self.spec = config.spec or SymbolSpec(
            symbol="BACKTEST/USDT", base="BACKTEST", quote="USDT"
        )

    # ------------------------------------------------------------------- run
    def run(self, candles: pd.DataFrame, symbol: str, timeframe: str) -> BacktestOutput:
        features = self.features.compute(candles, symbol, timeframe)
        frame = features.candles
        warmup = self.config.warmup_bars or self.config.feature_config.warmup_bars
        warnings: list[str] = []
        if len(frame) <= warmup + 10:
            return BacktestOutput(
                trades=[],
                equity_curve=[],
                metrics=compute_metrics([], [], self.config.starting_balance),
                risk_rejections={},
                rejected_signals=0,
                warnings=[
                    f"not enough bars: {len(frame)} with a warmup of {warmup}"
                ],
                bars_tested=0,
                period_start=None,
                period_end=None,
            )

        cash = round_money(self.config.starting_balance, 8)
        peak_equity = cash
        day_start_equity = cash
        current_day: date | None = None
        daily_halted_on: date | None = None
        halted = False
        halt_reason: str | None = None

        position: _OpenPosition | None = None
        pending: dict[str, Any] | None = None
        trades: list[BacktestTrade] = []
        equity_curve: list[EquityPoint] = []
        rejections: dict[str, int] = {}
        rejected_signals = 0

        for index in range(warmup, len(frame)):
            bar = frame.iloc[index]
            timestamp = ensure_utc(frame.index[index].to_pydatetime())
            bar_open = to_decimal(float(bar["open"]))
            bar_high = to_decimal(float(bar["high"]))
            bar_low = to_decimal(float(bar["low"]))
            bar_close = to_decimal(float(bar["close"]))
            bar_day = timestamp.date()

            if current_day != bar_day:
                current_day = bar_day
                day_start_equity = round_money(
                    cash + (position.quantity * bar_open if position else ZERO), 8
                )

            # --- 1. fill any entry decided on the previous bar, at this open
            if pending is not None and position is None and not halted:
                fill = simulate_market_fill(
                    bar_open, pending["quantity"], Side.BUY, self.config.cost_model
                )
                cost = fill.notional + fill.fee
                if cost <= cash:
                    cash = round_money(cash - cost, 8)
                    risk_per_unit = abs(fill.price - pending["stop_loss"])
                    state = PositionState(
                        side=PositionSide.LONG,
                        entry_price=fill.price,
                        quantity=fill.quantity,
                        stop_loss=pending["stop_loss"],
                        take_profit=pending["take_profit"],
                        initial_stop=pending["stop_loss"],
                        trailing_stop_atr_multiple=pending.get("trailing_stop_atr_multiple"),
                        breakeven_at_r=pending.get("breakeven_at_r"),
                        partial_exit_at_r=pending.get("partial_exit_at_r"),
                        partial_exit_fraction=pending.get("partial_exit_fraction"),
                        time_stop_bars=pending.get("time_stop_bars"),
                    )
                    position = _OpenPosition(
                        entry_time=timestamp,
                        entry_price=fill.price,
                        quantity=fill.quantity,
                        state=state,
                        strategy=pending["strategy"],
                        regime=pending["regime"],
                        entry_fee=fill.fee,
                        risk_amount=round_money(risk_per_unit * fill.quantity, 8),
                        take_profit=pending["take_profit"],
                        max_favorable=fill.price,
                        max_adverse=fill.price,
                    )
                else:
                    rejections["INSUFFICIENT_CASH_AT_FILL"] = (
                        rejections.get("INSUFFICIENT_CASH_AT_FILL", 0) + 1
                    )
                pending = None

            # --- 2. manage the open position on this bar
            if position is not None:
                position.bars_held += 1
                position.state.bars_held = position.bars_held
                position.max_favorable = max(position.max_favorable, bar_high)
                position.max_adverse = min(position.max_adverse, bar_low)

                prices = BarPrices(
                    close=bar_close, high=bar_high, low=bar_low, open=bar_open
                )
                decision = evaluate_exit(position.state, prices)
                if decision.should_exit and decision.price is not None:
                    quantity = round_money(
                        position.quantity * decision.fraction, 8
                    )
                    quantity = min(quantity, position.quantity)
                    exit_fill = simulate_market_fill(
                        decision.price, quantity, Side.SELL, self.config.cost_model
                    )
                    cash = round_money(cash + exit_fill.notional - exit_fill.fee, 8)
                    entry_fee_share = round_money(
                        safe_div(position.entry_fee, position.quantity) * quantity, 8
                    )
                    gross = round_money(
                        (exit_fill.price - position.entry_price) * quantity, 8
                    )
                    net = round_money(gross - exit_fill.fee - entry_fee_share, 8)
                    position.realized = round_money(position.realized + net, 8)
                    position.fees = round_money(
                        position.fees + exit_fill.fee + entry_fee_share, 8
                    )
                    position.exits.append(
                        {"price": exit_fill.price, "quantity": quantity}
                    )
                    remaining = round_money(position.quantity - quantity, 8)

                    if decision.is_partial and remaining > ZERO:
                        position.quantity = remaining
                        position.state.quantity = remaining
                        position.state.partial_exit_done = True
                    else:
                        trades.append(
                            self._build_trade(
                                position, symbol, timestamp, decision.reason, cash
                            )
                        )
                        position = None

                if position is not None:
                    atr = _feature_at(features, index, "atr")
                    update_stops(position.state, prices, atr)

            # --- 3. look for a new entry (executed next bar)
            equity = round_money(
                cash + (position.quantity * bar_close if position else ZERO), 8
            )
            peak_equity = max(peak_equity, equity)
            drawdown = safe_div(peak_equity - equity, peak_equity)
            daily_pnl_pct = safe_div(equity - day_start_equity, day_start_equity)

            if (
                self.config.enforce_drawdown_halt
                and not halted
                and drawdown >= self.limits.max_drawdown_pct
            ):
                halted = True
                halt_reason = (
                    f"max drawdown {drawdown:.2%} reached at {timestamp.isoformat()}; "
                    "trading halted for the remainder of the run (matching the live "
                    "kill switch)"
                )
                warnings.append(halt_reason)

            if (
                self.config.enforce_daily_loss_limit
                and daily_pnl_pct <= -self.limits.max_daily_loss_pct
                and daily_halted_on != bar_day
            ):
                daily_halted_on = bar_day
                warnings.append(
                    f"daily loss limit hit on {bar_day.isoformat()} "
                    f"({daily_pnl_pct:.2%}); no further entries that day"
                )

            can_enter = (
                position is None
                and pending is None
                and not halted
                and daily_halted_on != bar_day
                and index < len(frame) - 1  # need a next bar to fill on
            )
            if can_enter:
                assessment = self.regime.classify(features, index=index)
                output = self.strategies.evaluate(features, assessment, index=index)
                candidate = output.candidate
                if candidate is not None:
                    if candidate.direction is SignalDirection.SELL and not self.config.allow_short:
                        rejected_signals += 1
                    else:
                        account = AccountRiskState(
                            timestamp=timestamp,
                            mode=TradingModeEnum.PAPER,
                            bot_status=BotStatus.RUNNING,
                            equity=equity,
                            cash=cash,
                            starting_equity=self.config.starting_balance,
                            peak_equity=peak_equity,
                            open_positions=0,
                            open_symbols=[],
                            exposure_pct=ZERO,
                            daily_pnl_pct=daily_pnl_pct,
                            drawdown_pct=drawdown,
                            consecutive_losses=_recent_losses(trades),
                        )
                        proposal = RiskProposal(
                            symbol=symbol,
                            direction=candidate.direction,
                            entry=candidate.entry,
                            stop_loss=candidate.stop_loss,
                            take_profit=candidate.take_profit,
                            confidence=candidate.confidence,
                            strategy=",".join(candidate.aligned_strategies),
                            timeframe=timeframe,
                            regime=candidate.regime.regime,
                            regime_abnormal=candidate.regime.is_abnormal,
                        )
                        verdict = self.risk.evaluate(
                            proposal,
                            account,
                            self.spec,
                            MarketConditions(
                                data_quality_ok=True,
                                regime=candidate.regime.regime,
                                regime_abnormal=candidate.regime.is_abnormal,
                            ),
                            current_exposure_value=ZERO,
                        )
                        if verdict.approved and verdict.sizing is not None:
                            primary = candidate.primary_signal
                            pending = {
                                "quantity": verdict.sizing.quantity,
                                "stop_loss": candidate.stop_loss,
                                "take_profit": candidate.take_profit,
                                "strategy": ",".join(candidate.aligned_strategies),
                                "regime": str(candidate.regime.regime),
                                "trailing_stop_atr_multiple": primary.trailing_stop_atr_multiple
                                if primary
                                else None,
                                "breakeven_at_r": primary.breakeven_at_r if primary else None,
                                "partial_exit_at_r": primary.partial_exit_at_r
                                if primary
                                else None,
                                "partial_exit_fraction": primary.partial_exit_fraction
                                if primary
                                else None,
                                "time_stop_bars": primary.time_stop_bars if primary else None,
                            }
                        else:
                            rejected_signals += 1
                            for code in verdict.rejection_codes:
                                rejections[code] = rejections.get(code, 0) + 1

            equity_curve.append(
                EquityPoint(
                    timestamp=timestamp,
                    equity=equity,
                    cash=cash,
                    drawdown_pct=max(ZERO, drawdown),
                    open_positions=1 if position else 0,
                )
            )

        # --- close any position still open at the end of the data
        if position is not None:
            last_close = to_decimal(float(frame["close"].iloc[-1]))
            timestamp = ensure_utc(frame.index[-1].to_pydatetime())
            exit_fill = simulate_market_fill(
                last_close, position.quantity, Side.SELL, self.config.cost_model
            )
            cash = round_money(cash + exit_fill.notional - exit_fill.fee, 8)
            entry_fee_share = round_money(
                safe_div(position.entry_fee, position.quantity) * position.quantity, 8
            )
            gross = round_money(
                (exit_fill.price - position.entry_price) * position.quantity, 8
            )
            position.realized = round_money(
                position.realized + gross - exit_fill.fee - entry_fee_share, 8
            )
            position.fees = round_money(
                position.fees + exit_fill.fee + entry_fee_share, 8
            )
            position.exits.append(
                {"price": exit_fill.price, "quantity": position.quantity}
            )
            trades.append(
                self._build_trade(
                    position, symbol, timestamp, ExitReason.END_OF_BACKTEST, cash
                )
            )
            warnings.append(
                "a position was still open at the end of the data and was closed "
                "at the final close; treat that trade as incomplete"
            )
            position = None

        metrics = compute_metrics(
            [_to_summary(trade) for trade in trades],
            [
                EquityPointLite(timestamp=point.timestamp, equity=point.equity)
                for point in equity_curve
            ],
            self.config.starting_balance,
            ending_equity=equity_curve[-1].equity if equity_curve else cash,
        )
        bars_in_market = sum(trade.bars_held for trade in trades)
        if equity_curve:
            metrics.exposure_pct = round(bars_in_market / len(equity_curve), 6)

        return BacktestOutput(
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            risk_rejections=rejections,
            rejected_signals=rejected_signals,
            warnings=warnings,
            bars_tested=len(frame) - warmup,
            period_start=equity_curve[0].timestamp if equity_curve else None,
            period_end=equity_curve[-1].timestamp if equity_curve else None,
            halted=halted,
            halt_reason=halt_reason,
        )

    # ------------------------------------------------------------- internals
    def _build_trade(
        self,
        position: _OpenPosition,
        symbol: str,
        exit_time: datetime,
        reason: ExitReason | None,
        equity_after: Decimal,
    ) -> BacktestTrade:
        exit_price = _weighted_price(position.exits)
        quantity = sum(
            (to_decimal(exit_event["quantity"]) for exit_event in position.exits),
            start=ZERO,
        )
        gross = round_money((exit_price - position.entry_price) * quantity, 8)
        risk_per_unit = safe_div(position.risk_amount, quantity)
        return BacktestTrade(
            trade_id=f"bt_{position.entry_time.isoformat()}",
            symbol=symbol,
            side=PositionSide.LONG,
            strategy=position.strategy,
            regime=position.regime,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            quantity=quantity,
            stop_loss=position.state.initial_stop or position.state.stop_loss,
            take_profit=position.take_profit,
            exit_reason=reason or ExitReason.MANUAL,
            gross_pnl=gross,
            fees=position.fees,
            pnl=position.realized,
            pnl_pct=safe_div(position.realized, position.entry_price * quantity),
            r_multiple=float(safe_div(position.realized, position.risk_amount))
            if position.risk_amount > ZERO
            else None,
            max_favorable_excursion_r=float(
                safe_div(position.max_favorable - position.entry_price, risk_per_unit)
            )
            if risk_per_unit > ZERO
            else None,
            max_adverse_excursion_r=float(
                safe_div(position.max_adverse - position.entry_price, risk_per_unit)
            )
            if risk_per_unit > ZERO
            else None,
            bars_held=position.bars_held,
            equity_after=equity_after,
        )


def _weighted_price(exits: list[dict[str, Any]]) -> Decimal:
    total_quantity = sum((to_decimal(e["quantity"]) for e in exits), start=ZERO)
    if total_quantity <= ZERO:
        return ZERO
    notional = sum(
        (to_decimal(e["price"]) * to_decimal(e["quantity"]) for e in exits), start=ZERO
    )
    return round_money(notional / total_quantity, 8)


def _feature_at(features, index: int, name: str) -> Decimal | None:  # noqa: ANN001
    if name not in features.frame.columns:
        return None
    value = features.frame[name].iloc[index]
    if value is None or value != value or value <= 0:
        return None
    return to_decimal(float(value))


def _recent_losses(trades: list[BacktestTrade]) -> int:
    streak = 0
    for trade in reversed(trades):
        if trade.pnl < 0:
            streak += 1
        else:
            break
    return streak


def _to_summary(trade: BacktestTrade) -> TradeSummary:
    return TradeSummary(
        pnl=trade.pnl,
        fees=trade.fees,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        strategy=trade.strategy,
        regime=trade.regime,
        symbol=trade.symbol,
        r_multiple=trade.r_multiple,
        bars_held=trade.bars_held,
        exit_reason=str(trade.exit_reason),
        notional=trade.entry_price * trade.quantity,
    )
