"""Backtest orchestration: load data, assemble config, run, persist.

Data sources are labelled in the result. ``synthetic`` output is explicitly
flagged as meaningless for edge evaluation — it exercises the machinery, nothing
more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.backtesting.engine import BacktestConfig, BacktestEngine, BacktestOutput
from app.core.errors import ValidationError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.core.numeric import to_decimal
from app.database.repositories import new_id
from app.execution.fill_model import CostModel
from app.features import FeatureConfig, validate_ohlcv_frame
from app.models.backtest import BacktestDataSpec, BacktestRequest, BacktestResult
from app.risk.engine import limits_from_settings
from app.strategies import StrategyEngine, build_strategies
from app.strategies.engine import EngineConfig
from app.utils.time import utcnow

logger = get_logger(__name__)

SYNTHETIC_WARNING = (
    "Data source is SYNTHETIC: these numbers describe the generator, not a market. "
    "They are valid only as a check that the pipeline runs."
)


def load_candles(spec: BacktestDataSpec, market_data) -> tuple[pd.DataFrame, str]:  # noqa: ANN001
    """Load candles for a backtest and report the effective source."""
    if spec.source == "csv":
        if not spec.csv_path:
            raise ValidationError("csv source requires csv_path")
        path = Path(spec.csv_path)
        if not path.exists():
            raise ValidationError(f"csv file not found: {path}")
        frame = pd.read_csv(path)
        columns = {column.lower(): column for column in frame.columns}
        timestamp_column = columns.get("timestamp") or columns.get("time") or columns.get("date")
        if timestamp_column is None:
            raise ValidationError("csv must contain a timestamp/time/date column")
        frame = frame.rename(columns={timestamp_column: "timestamp"})
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.set_index("timestamp")
        frame.columns = [column.lower() for column in frame.columns]
        return validate_ohlcv_frame(frame), f"csv:{path.name}"

    if spec.source == "synthetic":
        from app.data.providers.synthetic import SyntheticMarketDataProvider

        provider = SyntheticMarketDataProvider(
            seed=spec.synthetic_seed if spec.synthetic_seed is not None else 7
        )
        frame = provider.generate(spec.symbol, spec.timeframe, spec.limit)
        return validate_ohlcv_frame(frame), "synthetic"

    frame = market_data.get_candles(
        spec.symbol, spec.timeframe, limit=spec.limit, use_cache=False
    )
    frame = validate_ohlcv_frame(frame)
    if spec.start:
        frame = frame.loc[frame.index >= pd.Timestamp(spec.start)]
    if spec.end:
        frame = frame.loc[frame.index <= pd.Timestamp(spec.end)]
    return frame, f"exchange:{market_data.provider.name}"


def build_config(
    request: BacktestRequest,
    settings,  # noqa: ANN001
    spec_overrides: dict[str, Any] | None = None,
) -> BacktestConfig:
    feature_config = (
        FeatureConfig(**request.feature_config) if request.feature_config else FeatureConfig()
    )
    limits = limits_from_settings(settings, allow_short=request.allow_short)
    if request.risk_per_trade is not None:
        limits = limits.model_copy(update={"risk_per_trade": request.risk_per_trade})
    # Microstructure checks are disabled in backtests (no historical spread/volume
    # feed), so those limits are documented as untested rather than silently passed.
    cost_model = CostModel(
        taker_fee_bps=to_decimal(
            request.taker_fee_bps
            if request.taker_fee_bps is not None
            else settings.paper_taker_fee_bps
        ),
        maker_fee_bps=to_decimal(settings.paper_maker_fee_bps),
        slippage_bps=to_decimal(
            request.slippage_bps
            if request.slippage_bps is not None
            else settings.paper_slippage_bps
        ),
        spread_bps=to_decimal(
            request.spread_bps
            if request.spread_bps is not None
            else settings.paper_spread_bps
        ),
    )
    return BacktestConfig(
        starting_balance=request.starting_balance,
        cost_model=cost_model,
        feature_config=feature_config,
        limits=limits,
        warmup_bars=request.warmup_bars,
        allow_short=request.allow_short,
        **(spec_overrides or {}),
    )


def run_backtest(
    request: BacktestRequest,
    services,  # noqa: ANN001 - app.container.Services
    *,
    strategy_overrides: dict[str, dict[str, Any]] | None = None,
) -> BacktestResult:
    frame, source = load_candles(request.data, services.market_data)
    symbol_spec = services.market_data.get_symbol_spec(request.data.symbol)
    config = build_config(request, services.settings, {"spec": symbol_spec})

    strategy_names = request.strategies or list(services.settings.enabled_strategies)
    engine = BacktestEngine(
        config,
        StrategyEngine(
            build_strategies(strategy_names, strategy_overrides),
            EngineConfig(allow_short=request.allow_short),
        ),
    )
    output: BacktestOutput = engine.run(frame, request.data.symbol.upper(), request.data.timeframe)

    warnings = list(output.warnings)
    if source == "synthetic":
        warnings.insert(0, SYNTHETIC_WARNING)
    if output.metrics.insufficient_data:
        warnings.append(
            "Too few trades for statistical confidence; do not tune on this result."
        )
    warnings.append(
        "The LLM layer is not replayed in backtests: this measures the "
        "deterministic strategy + risk system only."
    )
    warnings.append(
        "Spread and liquidity risk filters are not evaluated in backtests (no "
        "historical order-book data), so live rejections will be more frequent."
    )

    backtest_id = new_id("bt_")
    result = BacktestResult(
        backtest_id=backtest_id,
        label=request.label,
        created_at=utcnow(),
        symbol=request.data.symbol.upper(),
        timeframe=request.data.timeframe,
        data_source=source,
        bars=output.bars_tested,
        period_start=output.period_start,
        period_end=output.period_end,
        strategies=strategy_names,
        config={
            "starting_balance": float(request.starting_balance),
            "risk_per_trade": float(config.limits.risk_per_trade),
            "taker_fee_bps": float(config.cost_model.taker_fee_bps),
            "slippage_bps": float(config.cost_model.slippage_bps),
            "spread_bps": float(config.cost_model.spread_bps),
            "allow_short": request.allow_short,
            "warmup_bars": config.warmup_bars or config.feature_config.warmup_bars,
            "feature_config": config.feature_config.model_dump(),
            "strategy_overrides": strategy_overrides or {},
            "halted": output.halted,
            "halt_reason": output.halt_reason,
        },
        metrics=output.metrics,
        trades=output.trades,
        equity_curve=output.equity_curve,
        rejected_signals=output.rejected_signals,
        risk_rejections=output.risk_rejections,
        warnings=warnings,
    )

    if request.persist:
        services.performance_repo.save_run(
            {
                "id": backtest_id,
                "label": request.label,
                "kind": "backtest",
                "symbol": result.symbol,
                "timeframe": result.timeframe,
                "data_source": source,
                "bars": result.bars,
                "period_start": result.period_start,
                "period_end": result.period_end,
                "strategies": strategy_names,
                "config": result.config,
                "metrics": result.metrics.model_dump(mode="json"),
                # Equity curves can be thousands of points; store the trades and a
                # sampled curve so the row stays a sane size.
                "result": {
                    "trades": [trade.model_dump(mode="json") for trade in result.trades],
                    "equity_curve": [
                        point.model_dump(mode="json")
                        for point in result.equity_curve[:: max(1, len(result.equity_curve) // 500)]
                    ],
                },
                "warnings": warnings,
            }
        )

    log_event(
        logger,
        EventType.BACKTEST_COMPLETED,
        backtest_id=backtest_id,
        symbol=result.symbol,
        timeframe=result.timeframe,
        source=source,
        bars=result.bars,
        trades=result.metrics.trades,
        net_pnl=float(result.metrics.net_pnl),
        total_return_pct=float(result.metrics.total_return_pct),
        max_drawdown_pct=float(result.metrics.max_drawdown_pct),
        expectancy_r=result.metrics.expectancy_r,
        insufficient_data=result.metrics.insufficient_data,
    )
    return result
