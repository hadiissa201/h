"""Walk-forward testing.

Optimising on all the data and reporting the result is the most common way to
produce a system that works beautifully until it trades. This harness makes that
mistake structurally difficult:

* parameters are searched on **TRAIN** only;
* the winner is picked on **VALIDATE** (unseen during the search);
* it is then evaluated exactly once on **TEST** (unseen during selection);
* windows roll forward, so every test slice is out-of-sample;
* the verdict compares in-sample to out-of-sample and says plainly when the edge
  did not survive.

A ``verdict`` of ``no_out_of_sample_edge`` is a normal, useful outcome.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.backtesting.runner import SYNTHETIC_WARNING, build_config, load_candles
from app.core.errors import ValidationError
from app.core.events import EventType
from app.core.logging import get_logger, log_event
from app.database.repositories import new_id
from app.features import FeatureConfig
from app.models.backtest import (
    BacktestRequest,
    PerformanceMetrics,
    WalkForwardRequest,
    WalkForwardResult,
    WalkForwardWindow,
)
from app.strategies import STRATEGY_REGISTRY, StrategyEngine, build_strategies
from app.strategies.engine import EngineConfig
from app.utils.time import ensure_utc, utcnow

logger = get_logger(__name__)

MAX_COMBINATIONS = 240


@dataclass
class _Slice:
    start: int
    end: int

    def frame(self, candles: pd.DataFrame) -> pd.DataFrame:
        return candles.iloc[self.start : self.end]


def run_walkforward(
    request: WalkForwardRequest,
    services,  # noqa: ANN001 - app.container.Services
) -> WalkForwardResult:
    candles, source = load_candles(request.data, services.market_data)
    symbol = request.data.symbol.upper()
    timeframe = request.data.timeframe
    strategy_names = request.strategies or list(services.settings.enabled_strategies)

    base_request = BacktestRequest(
        data=request.data,
        strategies=strategy_names,
        starting_balance=request.starting_balance,
    )
    symbol_spec = services.market_data.get_symbol_spec(symbol)
    base_config = build_config(base_request, services.settings, {"spec": symbol_spec})
    warmup = base_config.warmup_bars or base_config.feature_config.warmup_bars

    step = request.step_bars or request.test_bars
    window_span = request.train_bars + request.validate_bars + request.test_bars
    required = window_span + warmup
    if len(candles) < required:
        raise ValidationError(
            f"need at least {required} bars for one walk-forward window "
            f"(train {request.train_bars} + validate {request.validate_bars} + "
            f"test {request.test_bars} + warmup {warmup}); got {len(candles)}"
        )

    combinations = _expand_grid(request.parameter_grid)
    warnings: list[str] = []
    if source == "synthetic":
        warnings.insert(0, SYNTHETIC_WARNING)
    if len(combinations) > MAX_COMBINATIONS:
        warnings.append(
            f"parameter grid had {len(combinations)} combinations; truncated to "
            f"{MAX_COMBINATIONS}. A grid this size overfits the validation slice too."
        )
        combinations = combinations[:MAX_COMBINATIONS]
    if len(combinations) > 1:
        warnings.append(
            f"searching {len(combinations)} parameter combinations per window: the "
            "more you search, the more the validation result is itself optimistic"
        )

    windows: list[WalkForwardWindow] = []
    cursor = 0
    while True:
        train = _Slice(cursor, cursor + warmup + request.train_bars)
        validate = _Slice(train.end - warmup, train.end + request.validate_bars)
        test = _Slice(validate.end - warmup, validate.end + request.test_bars)
        if test.end > len(candles):
            break

        best_params: dict[str, Any] = {}
        best_score: float | None = None
        best_train: PerformanceMetrics | None = None
        best_validate: PerformanceMetrics | None = None
        selection_skipped = False
        note: str | None = None

        for parameters in combinations:
            train_metrics = _evaluate(
                candles, train, parameters, base_config, strategy_names, symbol, timeframe, request
            )
            validate_metrics = _evaluate(
                candles, validate, parameters, base_config, strategy_names, symbol, timeframe, request
            )
            if validate_metrics.trades < request.min_trades_for_selection:
                continue
            score = _score(validate_metrics, request.selection_metric)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_score = score
                best_params = parameters
                best_train = train_metrics
                best_validate = validate_metrics

        if best_score is None:
            # Nothing cleared the minimum trade count: fall back to defaults rather
            # than picking the least-bad overfit.
            selection_skipped = True
            best_params = combinations[0] if combinations else {}
            best_train = _evaluate(
                candles, train, best_params, base_config, strategy_names, symbol, timeframe, request
            )
            best_validate = _evaluate(
                candles, validate, best_params, base_config, strategy_names, symbol, timeframe, request
            )
            note = (
                "no parameter set produced "
                f"{request.min_trades_for_selection}+ validation trades; used defaults"
            )

        test_metrics = _evaluate(
            candles, test, best_params, base_config, strategy_names, symbol, timeframe, request
        )

        windows.append(
            WalkForwardWindow(
                index=len(windows),
                train_start=ensure_utc(candles.index[train.start].to_pydatetime()),
                train_end=ensure_utc(candles.index[train.end - 1].to_pydatetime()),
                validate_start=ensure_utc(candles.index[validate.start].to_pydatetime()),
                validate_end=ensure_utc(candles.index[validate.end - 1].to_pydatetime()),
                test_start=ensure_utc(candles.index[test.start].to_pydatetime()),
                test_end=ensure_utc(candles.index[test.end - 1].to_pydatetime()),
                selected_parameters=best_params,
                train_metrics=best_train,
                validate_metrics=best_validate,
                test_metrics=test_metrics,
                selection_skipped=selection_skipped,
                note=note,
            )
        )
        cursor += step

    if not windows:
        raise ValidationError("no complete walk-forward window could be built")

    aggregate = _aggregate_test_metrics(windows, request.starting_balance)
    comparison, verdict = _verdict(windows, aggregate, request.selection_metric)

    run_id = new_id("wf_")
    result = WalkForwardResult(
        run_id=run_id,
        label=request.label,
        created_at=utcnow(),
        symbol=symbol,
        timeframe=timeframe,
        data_source=source,
        windows=windows,
        aggregate_test_metrics=aggregate,
        in_sample_vs_out_of_sample=comparison,
        verdict=verdict,
        warnings=warnings,
    )

    services.performance_repo.save_run(
        {
            "id": run_id,
            "label": request.label,
            "kind": "walkforward",
            "symbol": symbol,
            "timeframe": timeframe,
            "data_source": source,
            "bars": len(candles),
            "period_start": windows[0].train_start,
            "period_end": windows[-1].test_end,
            "strategies": strategy_names,
            "config": {
                "train_bars": request.train_bars,
                "validate_bars": request.validate_bars,
                "test_bars": request.test_bars,
                "step_bars": step,
                "selection_metric": request.selection_metric,
                "combinations": len(combinations),
                "warmup_bars": warmup,
            },
            "metrics": aggregate.model_dump(mode="json"),
            "result": {
                "windows": [window.model_dump(mode="json") for window in windows],
                "in_sample_vs_out_of_sample": comparison,
                "verdict": verdict,
            },
            "warnings": warnings,
        }
    )
    log_event(
        logger,
        EventType.WALKFORWARD_COMPLETED,
        run_id=run_id,
        symbol=symbol,
        windows=len(windows),
        verdict=verdict,
        oos_expectancy_r=aggregate.expectancy_r,
        oos_trades=aggregate.trades,
    )
    return result


# ------------------------------------------------------------------ internals
def _expand_grid(grid: dict[str, list[Any]] | None) -> list[dict[str, Any]]:
    if not grid:
        return [{}]
    keys = sorted(grid)
    for key in keys:
        if not isinstance(grid[key], list) or not grid[key]:
            raise ValidationError(f"parameter grid entry {key!r} must be a non-empty list")
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[key] for key in keys))
    ]


def _split_parameters(
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Split dotted keys into feature overrides and per-strategy overrides.

    ``feature.ema_fast`` -> FeatureConfig; ``trend_following.adx_min`` -> strategy.
    """
    feature_overrides: dict[str, Any] = {}
    strategy_overrides: dict[str, dict[str, Any]] = {}
    for key, value in parameters.items():
        if "." not in key:
            raise ValidationError(
                f"parameter key {key!r} must be 'feature.<field>' or '<strategy>.<param>'"
            )
        prefix, field = key.split(".", 1)
        if prefix == "feature":
            feature_overrides[field] = value
        elif prefix in STRATEGY_REGISTRY:
            strategy_overrides.setdefault(prefix, {})[field] = value
        else:
            raise ValidationError(
                f"unknown parameter prefix {prefix!r}; expected 'feature' or a "
                f"strategy name from {sorted(STRATEGY_REGISTRY)}"
            )
    return feature_overrides, strategy_overrides


def _evaluate(
    candles: pd.DataFrame,
    window: _Slice,
    parameters: dict[str, Any],
    base_config: BacktestConfig,
    strategy_names: list[str],
    symbol: str,
    timeframe: str,
    request: WalkForwardRequest,
) -> PerformanceMetrics:
    feature_overrides, strategy_overrides = _split_parameters(parameters)
    feature_config = (
        FeatureConfig(**{**base_config.feature_config.model_dump(), **feature_overrides})
        if feature_overrides
        else base_config.feature_config
    )
    config = BacktestConfig(
        starting_balance=request.starting_balance,
        cost_model=base_config.cost_model,
        feature_config=feature_config,
        limits=base_config.limits,
        spec=base_config.spec,
        warmup_bars=base_config.warmup_bars,
        allow_short=base_config.allow_short,
        # A per-window drawdown halt would end a slice early and contaminate the
        # comparison between windows; the daily loss limit still applies.
        enforce_drawdown_halt=False,
    )
    engine = BacktestEngine(
        config,
        StrategyEngine(
            build_strategies(strategy_names, strategy_overrides),
            EngineConfig(allow_short=base_config.allow_short),
        ),
    )
    output = engine.run(window.frame(candles), symbol, timeframe)
    return output.metrics


def _score(metrics: PerformanceMetrics, metric: str) -> float | None:
    value = getattr(metrics, metric, None)
    if value is None:
        return None
    return float(value)


def _aggregate_test_metrics(
    windows: list[WalkForwardWindow], starting_balance
) -> PerformanceMetrics:
    """Pool the out-of-sample slices into one summary.

    Trade counts and P&L add up; ratios are recomputed from the pooled totals
    rather than averaged, because averaging ratios across windows flatters them.
    """
    total = PerformanceMetrics(starting_equity=starting_balance)
    for window in windows:
        metrics = window.test_metrics
        total.trades += metrics.trades
        total.wins += metrics.wins
        total.losses += metrics.losses
        total.breakeven += metrics.breakeven
        total.gross_profit += metrics.gross_profit
        total.gross_loss += metrics.gross_loss
        total.net_pnl += metrics.net_pnl
        total.fees_paid += metrics.fees_paid
        total.max_drawdown_pct = max(total.max_drawdown_pct, metrics.max_drawdown_pct)
        total.consecutive_losses_max = max(
            total.consecutive_losses_max, metrics.consecutive_losses_max
        )
    if total.trades:
        total.win_rate = round(total.wins / total.trades, 6)
        total.expectancy = total.net_pnl / total.trades
        if total.gross_loss > 0:
            total.profit_factor = round(float(total.gross_profit / total.gross_loss), 6)
        r_values = [
            window.test_metrics.expectancy_r
            for window in windows
            if window.test_metrics.expectancy_r is not None
            and window.test_metrics.trades > 0
        ]
        weights = [
            window.test_metrics.trades
            for window in windows
            if window.test_metrics.expectancy_r is not None
            and window.test_metrics.trades > 0
        ]
        if r_values and sum(weights):
            total.expectancy_r = round(
                sum(value * weight for value, weight in zip(r_values, weights, strict=True))
                / sum(weights),
                6,
            )
            total.average_r = total.expectancy_r
    total.ending_equity = starting_balance + total.net_pnl
    total.total_return_pct = (
        total.net_pnl / starting_balance if starting_balance else total.total_return_pct
    )
    if total.trades < 10:
        total.insufficient_data = True
        total.notes.append(
            f"pooled out-of-sample sample is {total.trades} trades: not enough to "
            "conclude anything"
        )
    return total


def _verdict(
    windows: list[WalkForwardWindow],
    pooled: PerformanceMetrics,
    metric: str,
) -> tuple[dict[str, Any], str]:
    """Judge the run on the *pooled* out-of-sample result.

    Averaging per-window ratios flatters a strategy: a window with one lucky
    trade counts as much as a window with twenty. The verdict therefore keys off
    trade-weighted pooled numbers, and the window mean is reported beside them.
    """
    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 6) if values else None

    train_scores = [
        score
        for window in windows
        if (score := _score(window.train_metrics, metric)) is not None
    ]
    validate_scores = [
        score
        for window in windows
        if (score := _score(window.validate_metrics, metric)) is not None
    ]
    test_scores = [
        score
        for window in windows
        if (score := _score(window.test_metrics, metric)) is not None
    ]
    train_mean = mean(train_scores)
    test_mean = mean(test_scores)
    positive_windows = sum(1 for window in windows if window.test_metrics.net_pnl > 0)
    total_test_trades = sum(window.test_metrics.trades for window in windows)

    degradation = None
    if train_mean not in (None, 0) and test_mean is not None:
        degradation = round(test_mean / train_mean, 4) if train_mean != 0 else None

    pooled_expectancy = pooled.expectancy_r
    pooled_net = float(pooled.net_pnl)
    skipped_selection = sum(1 for window in windows if window.selection_skipped)

    comparison = {
        "metric": metric,
        "windows": len(windows),
        "train_mean": train_mean,
        "validate_mean": mean(validate_scores),
        "test_mean_of_windows": test_mean,
        "pooled_test_expectancy_r": pooled_expectancy,
        "pooled_test_net_pnl": pooled_net,
        "test_over_train_ratio": degradation,
        "profitable_test_windows": positive_windows,
        "total_test_trades": total_test_trades,
        "windows_with_selection_skipped": skipped_selection,
    }

    caveats: list[str] = []
    if skipped_selection:
        caveats.append(
            f"parameter selection was skipped in {skipped_selection} of {len(windows)} "
            "windows (too few validation trades), so those windows used defaults"
        )
    if 20 <= total_test_trades < 50:
        caveats.append(
            f"{total_test_trades} pooled out-of-sample trades is a small sample; "
            "confidence intervals around these numbers are wide"
        )

    if total_test_trades < 20:
        verdict = (
            f"insufficient_data: only {total_test_trades} out-of-sample trades across "
            f"{len(windows)} windows — no conclusion is justified"
        )
    elif pooled_net <= 0 or (pooled_expectancy is not None and pooled_expectancy <= 0):
        verdict = (
            f"no_out_of_sample_edge: pooled out-of-sample expectancy is "
            f"{pooled_expectancy}R on {total_test_trades} trades "
            f"(net {pooled_net:+.2f}). The parameters did not survive outside the "
            f"optimisation window, even though the per-window mean {metric} was "
            f"{test_mean}."
        )
    elif train_mean and pooled_expectancy is not None and pooled_expectancy < 0.5 * train_mean:
        verdict = (
            f"degraded_out_of_sample: pooled out-of-sample expectancy "
            f"{pooled_expectancy}R versus {train_mean} in sample. Most of the "
            "apparent edge was fitted to the training data."
        )
    elif positive_windows <= len(windows) / 2:
        verdict = (
            f"inconsistent: pooled out-of-sample expectancy is {pooled_expectancy}R but "
            f"only {positive_windows} of {len(windows)} windows were profitable — the "
            "result depends on the period."
        )
    else:
        verdict = (
            f"survives_out_of_sample: pooled expectancy {pooled_expectancy}R over "
            f"{total_test_trades} out-of-sample trades, profitable in "
            f"{positive_windows} of {len(windows)} windows. Still not proof of a live "
            "edge: costs, latency and regime change are all worse in production."
        )
    if caveats:
        verdict += " Caveats: " + "; ".join(caveats) + "."
    return comparison, verdict
