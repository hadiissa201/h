"""Backtest and walk-forward schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import ExitReason, PositionSide


class BacktestDataSpec(BaseModel):
    symbol: str
    timeframe: str = "1h"
    source: Literal["exchange", "synthetic", "csv"] = "exchange"
    limit: int = Field(default=1500, ge=100, le=20000)
    start: datetime | None = None
    end: datetime | None = None
    csv_path: str | None = None
    synthetic_seed: int | None = None


class BacktestRequest(BaseModel):
    data: BacktestDataSpec
    strategies: list[str] | None = None
    starting_balance: Decimal = Field(default=Decimal("10000"), gt=0)
    risk_per_trade: Decimal | None = Field(default=None, gt=0, le=Decimal("0.05"))
    taker_fee_bps: Decimal | None = Field(default=None, ge=0)
    slippage_bps: Decimal | None = Field(default=None, ge=0)
    spread_bps: Decimal | None = Field(default=None, ge=0)
    feature_config: dict[str, Any] | None = None
    allow_short: bool = False
    warmup_bars: int | None = None
    label: str | None = None
    persist: bool = False


class BacktestTrade(BaseModel):
    trade_id: str
    symbol: str
    side: PositionSide
    strategy: str
    regime: str | None = None
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    quantity: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None = None
    exit_reason: ExitReason
    gross_pnl: Decimal
    fees: Decimal
    pnl: Decimal
    pnl_pct: Decimal
    r_multiple: float | None = None
    max_favorable_excursion_r: float | None = None
    max_adverse_excursion_r: float | None = None
    bars_held: int = 0
    equity_after: Decimal | None = None


class EquityPoint(BaseModel):
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    drawdown_pct: Decimal
    open_positions: int


class PerformanceMetrics(BaseModel):
    """Metrics are computed from realised trades and the equity curve only —
    nothing here is annotated, smoothed or hand-picked."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float | None = None
    total_return_pct: Decimal = Decimal("0")
    starting_equity: Decimal = Decimal("0")
    ending_equity: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    gross_loss: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    average_win: Decimal | None = None
    average_loss: Decimal | None = None
    largest_win: Decimal | None = None
    largest_loss: Decimal | None = None
    profit_factor: float | None = None
    expectancy: Decimal | None = None
    expectancy_r: float | None = None
    average_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    max_drawdown_pct: Decimal = Decimal("0")
    max_drawdown_duration_bars: int = 0
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    exposure_pct: float | None = None
    average_bars_held: float | None = None
    consecutive_wins_max: int = 0
    consecutive_losses_max: int = 0
    per_strategy: dict[str, dict[str, Any]] = Field(default_factory=dict)
    per_regime: dict[str, dict[str, Any]] = Field(default_factory=dict)
    insufficient_data: bool = False
    notes: list[str] = Field(default_factory=list)


class BacktestResult(BaseModel):
    backtest_id: str
    label: str | None = None
    created_at: datetime
    symbol: str
    timeframe: str
    data_source: str
    bars: int
    period_start: datetime | None = None
    period_end: datetime | None = None
    strategies: list[str]
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: PerformanceMetrics
    trades: list[BacktestTrade] = Field(default_factory=list)
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    rejected_signals: int = 0
    risk_rejections: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    benchmark: BuyAndHoldBenchmark | None = None
    yield_baseline: YieldBaseline | None = None


class BuyAndHoldBenchmark(BaseModel):
    """What doing nothing would have returned over the identical window.

    The bar every strategy has to clear. A strategy that makes 8% while the asset
    made 40% has not made money in any sense that matters -- it has taken risk,
    paid fees and underperformed sitting still. Reporting P&L without this is how
    a losing idea keeps looking reasonable.

    Costs are charged on both legs, so the comparison is like for like rather
    than a frictionless ideal.
    """

    start_price: Decimal
    end_price: Decimal
    return_pct: Decimal
    net_pnl: Decimal
    max_drawdown_pct: Decimal


class YieldBaseline(BaseModel):
    """What the same money would have earned just sitting in a yield account.

    Cash at 0% is the wrong floor. Idle USDT can be lent on any major venue, so
    the money a strategy ties up has a real opportunity cost. A strategy that
    returns +2% a year is not "profitable" when lending pays 4% for no work,
    no screen time and no execution risk -- it is a loss dressed as a gain.

    This is deliberately NOT called risk-free. CeFi lending carries counterparty
    risk, stablecoins carry depeg risk, and the rate floats. It is a benchmark,
    not a guarantee; `annual_rate` records the assumption so a result can never
    be read without knowing which rate produced it.
    """

    annual_rate: Decimal
    days: Decimal
    return_pct: Decimal
    net_pnl: Decimal


class WalkForwardRequest(BaseModel):
    data: BacktestDataSpec
    train_bars: int = Field(default=500, ge=100)
    validate_bars: int = Field(default=150, ge=30)
    test_bars: int = Field(default=150, ge=30)
    step_bars: int | None = None
    strategies: list[str] | None = None
    starting_balance: Decimal = Field(default=Decimal("10000"), gt=0)
    parameter_grid: dict[str, list[Any]] | None = Field(
        default=None,
        description="Feature/strategy parameters to search on TRAIN, selected on "
        "VALIDATE, and then evaluated once on TEST.",
    )
    selection_metric: Literal[
        "expectancy_r", "profit_factor", "sharpe_ratio", "total_return_pct"
    ] = "expectancy_r"
    min_trades_for_selection: int = Field(default=5, ge=1)
    label: str | None = None


class WalkForwardWindow(BaseModel):
    index: int
    train_start: datetime
    train_end: datetime
    validate_start: datetime
    validate_end: datetime
    test_start: datetime
    test_end: datetime
    selected_parameters: dict[str, Any] = Field(default_factory=dict)
    train_metrics: PerformanceMetrics
    validate_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics
    selection_skipped: bool = False
    note: str | None = None


class WalkForwardResult(BaseModel):
    run_id: str
    label: str | None = None
    created_at: datetime
    symbol: str
    timeframe: str
    data_source: str
    windows: list[WalkForwardWindow] = Field(default_factory=list)
    aggregate_test_metrics: PerformanceMetrics
    in_sample_vs_out_of_sample: dict[str, Any] = Field(default_factory=dict)
    verdict: str = ""
    warnings: list[str] = Field(default_factory=list)
