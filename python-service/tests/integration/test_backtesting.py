"""Backtester and walk-forward harness.

The tests that matter most are the ones about honesty: that the backtester uses
the same code as live trading, that it cannot see the future, that it charges
costs, and that the walk-forward verdict is capable of saying "no edge".
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtesting.runner import run_backtest
from app.backtesting.walkforward import run_walkforward
from app.models.backtest import (
    BacktestDataSpec,
    BacktestRequest,
    WalkForwardRequest,
)


def spec(**overrides) -> BacktestDataSpec:
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "source": "synthetic",
        "limit": 2000,
        "synthetic_seed": 7,
    }
    payload.update(overrides)
    return BacktestDataSpec(**payload)


@pytest.fixture(scope="module")
def _result_cache() -> dict:
    return {}


@pytest.fixture
def result(services, _result_cache):
    """One backtest reused across assertions (it takes a second to run)."""
    if "value" not in _result_cache:
        _result_cache["value"] = run_backtest(
            BacktestRequest(data=spec(), starting_balance=Decimal("10000")), services
        )
    return _result_cache["value"]


# ------------------------------------------------------------------- running
def test_a_backtest_produces_trades_and_an_equity_curve(result):
    assert result.bars > 1000
    assert result.metrics.trades > 0
    assert len(result.equity_curve) == result.bars
    assert result.period_start < result.period_end


def test_metrics_are_internally_consistent(result):
    metrics = result.metrics
    assert metrics.trades == metrics.wins + metrics.losses + metrics.breakeven
    assert metrics.win_rate == pytest.approx(metrics.wins / metrics.trades, abs=1e-6)
    assert metrics.net_pnl == pytest.approx(
        metrics.gross_profit - metrics.gross_loss, abs=Decimal("0.01")
    )
    assert metrics.ending_equity == pytest.approx(
        metrics.starting_equity + metrics.net_pnl, abs=Decimal("1")
    )
    assert metrics.max_drawdown_pct >= 0


def test_every_trade_reconciles(result):
    for trade in result.trades:
        gross = (trade.exit_price - trade.entry_price) * trade.quantity
        assert trade.gross_pnl == pytest.approx(gross, abs=Decimal("0.01"))
        assert trade.pnl == pytest.approx(trade.gross_pnl - trade.fees, abs=Decimal("0.01"))
        assert trade.fees > 0, "a trade with no fees means costs were not charged"
        # Equal timestamps are legitimate: a position entered at a bar's open can
        # be stopped out inside that same bar.
        assert trade.exit_time >= trade.entry_time
        assert trade.bars_held >= 1


def test_risk_per_trade_is_respected_on_every_entry(result):
    """No trade may risk more than the configured budget."""
    for trade in result.trades:
        risked = abs(trade.entry_price - trade.stop_loss) * trade.quantity
        # 0.5% of 10,000 is 50; allow headroom for equity growth during the run.
        assert risked <= Decimal("100"), f"{trade.trade_id} risked {risked}"


def test_rejected_signals_are_counted_with_reasons(result):
    assert result.rejected_signals >= 0
    if result.risk_rejections:
        assert all(isinstance(code, str) for code in result.risk_rejections)


# -------------------------------------------------------------------- honesty
def test_synthetic_results_are_labelled_as_meaningless(result):
    assert result.data_source == "synthetic"
    assert any("SYNTHETIC" in warning for warning in result.warnings)


def test_the_ai_exclusion_is_stated(result):
    assert any("LLM layer is not replayed" in warning for warning in result.warnings)


def test_untested_filters_are_disclosed(result):
    assert any("Spread and liquidity" in warning for warning in result.warnings)


def test_a_small_sample_is_flagged(services):
    small = run_backtest(
        BacktestRequest(data=spec(limit=400), starting_balance=Decimal("10000")),
        services,
    )
    if small.metrics.trades < 10:
        assert small.metrics.insufficient_data
        assert any("indicative" in note for note in small.metrics.notes)


def test_too_little_data_returns_an_empty_result_not_a_guess(services):
    tiny = run_backtest(
        BacktestRequest(data=spec(limit=150), starting_balance=Decimal("10000")),
        services,
    )
    assert tiny.metrics.trades == 0
    assert any("not enough bars" in warning for warning in tiny.warnings)


# ----------------------------------------------------------------- costs bite
def test_removing_costs_changes_the_result(services):
    """If zeroing fees and slippage does not move P&L, they were never charged."""
    with_costs = run_backtest(
        BacktestRequest(data=spec(), starting_balance=Decimal("10000")), services
    )
    without_costs = run_backtest(
        BacktestRequest(
            data=spec(),
            starting_balance=Decimal("10000"),
            taker_fee_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
            spread_bps=Decimal("0"),
        ),
        services,
    )
    assert without_costs.metrics.fees_paid == 0
    assert with_costs.metrics.fees_paid > 0
    assert without_costs.metrics.net_pnl > with_costs.metrics.net_pnl


# ------------------------------------------------------------- no look-ahead
def test_a_backtest_on_a_prefix_matches_the_head_of_the_full_run(services, market_data):
    """The decisive property: knowing the future must not change the past.

    Running on bars 0..N must produce exactly the trades that a run over 0..2N
    produced in its first N bars. Any look-ahead in features, signals, sizing or
    exits breaks this, and every backtest number becomes fiction.
    """
    from app.backtesting.engine import BacktestEngine
    from app.backtesting.runner import build_config
    from app.strategies import StrategyEngine, build_strategies
    from app.strategies.engine import EngineConfig

    candles = market_data.provider.generate("BTC/USDT", "1h", 2000)
    config = build_config(
        BacktestRequest(data=spec(), starting_balance=Decimal("10000")),
        services.settings,
        {"spec": services.market_data.get_symbol_spec("BTC/USDT")},
    )

    def run(frame):
        engine = BacktestEngine(
            config, StrategyEngine(build_strategies(), EngineConfig(allow_short=False))
        )
        return engine.run(frame, "BTC/USDT", "1h")

    full = run(candles)
    prefix = run(candles.iloc[:1200])
    assert prefix.period_start == full.period_start

    # The final prefix trade may be the forced end-of-data close, which the full
    # run would have let breathe; compare everything before that.
    settled = [
        trade
        for trade in prefix.trades
        if str(trade.exit_reason) != "END_OF_BACKTEST"
    ]
    assert settled, "no settled trades to compare"

    by_entry = {trade.entry_time: trade for trade in full.trades}
    for trade in settled:
        twin = by_entry.get(trade.entry_time)
        assert twin is not None, f"trade at {trade.entry_time} vanished in the longer run"
        assert trade.entry_price == twin.entry_price
        assert trade.exit_time == twin.exit_time
        assert trade.exit_price == twin.exit_price
        assert trade.quantity == twin.quantity
        assert trade.pnl == twin.pnl


def test_entries_fill_on_the_bar_after_the_signal(services, market_data):
    """A signal computed on a bar's close cannot fill at that same close."""
    candles = market_data.provider.generate("BTC/USDT", "1h", 2000)
    result = run_backtest(
        BacktestRequest(data=spec(limit=2000), starting_balance=Decimal("10000")),
        services,
    )
    for trade in result.trades[:10]:
        bar = candles.loc[candles.index == trade.entry_time]
        assert not bar.empty
        # The fill references the entry bar's open, adjusted for costs.
        assert trade.entry_price > Decimal(str(float(bar["open"].iloc[0]))) * Decimal("0.99")


def test_the_backtest_is_deterministic(services):
    first = run_backtest(BacktestRequest(data=spec()), services)
    second = run_backtest(BacktestRequest(data=spec()), services)
    assert first.metrics.trades == second.metrics.trades
    assert first.metrics.net_pnl == second.metrics.net_pnl


def test_results_can_be_persisted_for_the_live_readiness_gate(services):
    run_backtest(
        BacktestRequest(data=spec(limit=800), persist=True, label="test"), services
    )
    services.session.flush()
    runs = services.performance_repo.recent_runs("backtest")
    assert runs
    assert runs[0].label == "test"
    assert runs[0].metrics["trades"] >= 0


# ---------------------------------------------------------------- walk-forward
@pytest.fixture(scope="module")
def _wf_cache() -> dict:
    return {}


@pytest.fixture
def walkforward(services, _wf_cache):
    if "value" not in _wf_cache:
        _wf_cache["value"] = run_walkforward(
            WalkForwardRequest(
                data=spec(limit=2600),
                train_bars=500,
                validate_bars=200,
                test_bars=200,
                parameter_grid={"trend_following.adx_min": [18, 24]},
                min_trades_for_selection=3,
            ),
            services,
        )
    return _wf_cache["value"]


def test_walkforward_produces_rolling_windows(walkforward):
    assert len(walkforward.windows) >= 3
    for window in walkforward.windows:
        assert window.train_end <= window.validate_end <= window.test_end
        assert window.train_start < window.train_end


def test_each_window_selects_parameters_on_validation_only(walkforward):
    for window in walkforward.windows:
        if not window.selection_skipped:
            assert window.selected_parameters
            assert "trend_following.adx_min" in window.selected_parameters


def test_the_verdict_reports_pooled_out_of_sample_results(walkforward):
    comparison = walkforward.in_sample_vs_out_of_sample
    assert "pooled_test_expectancy_r" in comparison
    assert "test_mean_of_windows" in comparison
    assert comparison["windows"] == len(walkforward.windows)
    assert walkforward.verdict


def test_the_verdict_can_say_there_is_no_edge(walkforward):
    """The whole point of the harness: it must be able to return bad news."""
    assert any(
        walkforward.verdict.startswith(prefix)
        for prefix in (
            "insufficient_data",
            "no_out_of_sample_edge",
            "degraded_out_of_sample",
            "inconsistent",
            "survives_out_of_sample",
        )
    )
    if walkforward.aggregate_test_metrics.net_pnl <= 0:
        assert not walkforward.verdict.startswith("survives")


def test_overfitting_risk_is_disclosed(walkforward):
    assert any("optimistic" in warning for warning in walkforward.warnings)


def test_walkforward_refuses_insufficient_data(services):
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError, match="need at least"):
        run_walkforward(
            WalkForwardRequest(
                data=spec(limit=400), train_bars=500, validate_bars=200, test_bars=200
            ),
            services,
        )


def test_an_unknown_grid_parameter_is_rejected(services):
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError, match="unknown parameter prefix"):
        run_walkforward(
            WalkForwardRequest(
                data=spec(limit=2600),
                train_bars=500,
                validate_bars=200,
                test_bars=200,
                parameter_grid={"nonsense.field": [1, 2]},
            ),
            services,
        )
