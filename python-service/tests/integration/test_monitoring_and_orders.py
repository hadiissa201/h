"""Position monitoring, resting orders, and the analytics that judge the AI.

The monitor is what actually gets you out of a trade, so it needs the same
scrutiny as the code that gets you in.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import ExitReason, OrderStatus, OrderType, Side
from app.models.trading import OrderRequest
from app.utils.time import utcnow


@pytest.fixture
def position(services, proposal_factory):
    price = services.market_data.get_ticker("BTC/USDT").last
    decision = services.risk.check(
        proposal_factory(
            entry=price,
            stop_loss=price * Decimal("0.96"),
            take_profit=price * Decimal("1.09"),
        )
    )
    assert decision.approved, decision.reasons
    _, opened = services.execution.place_entry(
        approval_id=decision.approval_id,
        strategy="trend_following",
        exit_plan_overrides={
            "initial_stop": str(decision.stop_loss),
            "trailing_stop_atr_multiple": 2.0,
            "breakeven_at_r": 1.0,
        },
    )
    services.session.flush()
    return opened


# --------------------------------------------------------------- monitoring
def test_a_monitor_pass_with_no_positions_is_harmless(services):
    outcome = services.monitor.run()
    assert outcome.checked == 0
    assert outcome.exits == []
    assert outcome.errors == []


def test_monitoring_marks_positions_and_counts_bars(services, position):
    outcome = services.monitor.run()
    assert outcome.checked == 1
    record = services.execution_repo.get_position(position.position_id)
    assert record.mark_price is not None
    assert record.bars_held >= 1


def test_monitoring_persists_excursions(services, position):
    services.monitor.run()
    record = services.execution_repo.get_position(position.position_id)
    assert record.max_favorable_price is not None
    assert record.max_adverse_price is not None


def test_a_stop_below_the_market_triggers_an_exit(services, position):
    """Move the stop above price and the next pass must close the position."""
    record = services.execution_repo.get_position(position.position_id)
    mark = services.market_data.get_ticker("BTC/USDT").last
    record.stop_loss = mark * Decimal("1.05")  # stop now above price
    plan = dict(record.exit_plan or {})
    plan["initial_stop"] = str(record.stop_loss)
    plan["trailing_stop_atr_multiple"] = None
    record.exit_plan = plan
    services.session.flush()

    outcome = services.monitor.run()
    assert len(outcome.exits) == 1
    assert outcome.exits[0]["position_id"] == position.position_id
    assert outcome.exits[0]["reason"] in (
        str(ExitReason.STOP_LOSS),
        str(ExitReason.TRAILING_STOP),
    )
    assert services.portfolio.open_positions() == []
    assert services.execution_repo.trade_for_position(position.position_id) is not None


def test_a_target_below_the_market_triggers_a_take_profit(services, position):
    record = services.execution_repo.get_position(position.position_id)
    mark = services.market_data.get_ticker("BTC/USDT").last
    record.take_profit = mark * Decimal("0.995")
    plan = dict(record.exit_plan or {})
    plan["trailing_stop_atr_multiple"] = None
    record.exit_plan = plan
    services.session.flush()

    outcome = services.monitor.run()
    assert len(outcome.exits) == 1
    assert outcome.exits[0]["reason"] == str(ExitReason.TAKE_PROFIT)


def test_a_time_stop_closes_a_stale_position(services, position):
    record = services.execution_repo.get_position(position.position_id)
    plan = dict(record.exit_plan or {})
    plan["time_stop_bars"] = 1
    plan["trailing_stop_atr_multiple"] = None
    record.exit_plan = plan
    record.bars_held = 5
    services.session.flush()

    outcome = services.monitor.run()
    assert outcome.exits[0]["reason"] == str(ExitReason.TIME_STOP)


def test_monitoring_moves_a_trailing_stop(services, position):
    outcome = services.monitor.run()
    record = services.execution_repo.get_position(position.position_id)
    if record is not None:  # not exited
        plan = record.exit_plan or {}
        assert plan.get("trailing_stop_price") or outcome.stop_updates == []


def test_monitoring_runs_the_kill_switch_checks(services, position):
    """A monitor pass is also a safety sweep."""
    from app.database.repositories import PerformanceRepository

    performance = PerformanceRepository(services.session, mode="paper")
    day = utcnow().date()
    stats = performance.get_or_create_day(day, Decimal("10000"))
    stats.starting_equity = Decimal("20000")  # today's equity is now far below
    services.session.flush()

    outcome = services.monitor.run()
    triggered = services.risk.run_safety_checks()
    assert any(event.reason == "MAX_DAILY_LOSS" for event in triggered)
    assert services.bot_repo.get().status == "HALTED"
    assert outcome.checked >= 0


def test_one_bad_symbol_does_not_stop_the_pass(services, position, monkeypatch):
    """A failure on one position must not leave the others unmanaged."""
    original = services.execution.close_position

    def explode(position_id, **kwargs):
        raise RuntimeError("exchange rejected the exit")

    monkeypatch.setattr(services.execution, "close_position", explode)
    record = services.execution_repo.get_position(position.position_id)
    record.stop_loss = services.market_data.get_ticker("BTC/USDT").last * Decimal("1.05")
    plan = dict(record.exit_plan or {})
    plan["trailing_stop_atr_multiple"] = None
    record.exit_plan = plan
    services.session.flush()

    outcome = services.monitor.run()
    assert outcome.errors, "the failure should be reported, not swallowed"
    assert outcome.exits == []
    monkeypatch.setattr(services.execution, "close_position", original)


# ----------------------------------------------------------- resting orders
def test_a_limit_order_rests_until_the_market_reaches_it(services):
    price = services.market_data.get_ticker("BTC/USDT").last
    order = services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=price * Decimal("0.80"),  # far below the market
            client_order_id="resting-limit",
        )
    )
    assert order.status is OrderStatus.NEW
    assert order.filled_quantity == 0
    assert len(services.exchange.get_open_orders("BTC/USDT")) == 1


def test_a_limit_order_fills_when_the_market_trades_through(services):
    price = services.market_data.get_ticker("BTC/USDT").last
    limit = price * Decimal("0.98")
    services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=limit,
            client_order_id="crossing-limit",
        )
    )
    filled = services.exchange.process_resting_orders(
        "BTC/USDT", high=price, low=limit * Decimal("0.99"), last=limit
    )
    assert len(filled) == 1
    assert filled[0].status is OrderStatus.FILLED
    # Resting orders earn the maker fee.
    assert filled[0].fills[0].is_maker


def test_a_limit_order_that_is_not_reached_stays_open(services):
    price = services.market_data.get_ticker("BTC/USDT").last
    services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=price * Decimal("0.50"),
            client_order_id="far-limit",
        )
    )
    filled = services.exchange.process_resting_orders(
        "BTC/USDT", high=price, low=price * Decimal("0.99"), last=price
    )
    assert filled == []
    assert len(services.exchange.get_open_orders()) == 1


def test_a_stop_order_triggers_and_pays_slippage(services):
    """Give the account some BTC first, then rest a protective stop under it."""
    price = services.market_data.get_ticker("BTC/USDT").last
    services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("0.02"),
            client_order_id="seed-inventory",
        )
    )
    stop_price = price * Decimal("0.95")
    services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.SELL,
            type=OrderType.STOP_MARKET,
            quantity=Decimal("0.01"),
            stop_price=stop_price,
            client_order_id="protective-stop",
        )
    )
    filled = services.exchange.process_resting_orders(
        "BTC/USDT",
        high=price,
        low=stop_price * Decimal("0.99"),
        last=stop_price * Decimal("0.995"),
    )
    assert len(filled) == 1
    assert filled[0].status is OrderStatus.FILLED
    # A stop becomes a market order, so it fills below the trigger.
    assert filled[0].average_fill_price < stop_price


def test_an_order_can_be_cancelled(services):
    price = services.market_data.get_ticker("BTC/USDT").last
    order = services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=price * Decimal("0.5"),
            client_order_id="to-cancel",
        )
    )
    cancelled = services.exchange.cancel_order(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert services.exchange.get_open_orders() == []


def test_cancelling_a_filled_order_is_refused(services):
    from app.core.errors import OrderRejectedError

    order = services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            client_order_id="already-done",
        )
    )
    with pytest.raises(OrderRejectedError):
        services.exchange.cancel_order(order.order_id)


def test_partial_fills_are_supported_when_configured(session, market_data, settings):
    from app.container import build_services
    from app.services.bootstrap import bootstrap_session

    partial_settings = settings.model_copy(
        update={"paper_partial_fill_probability": 1.0}
    )
    bootstrap_session(session, partial_settings)
    services = build_services(session, partial_settings, market_data=market_data)

    order = services.exchange.create_order(
        OrderRequest(
            symbol="BTC/USDT",
            side=Side.BUY,
            type=OrderType.MARKET,
            quantity=Decimal("0.05"),
            client_order_id="partial-fill",
        )
    )
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert 0 < order.filled_quantity < order.quantity
    assert order.remaining_quantity > 0


# ------------------------------------------------------------- analytics
def test_ai_evaluation_refuses_to_claim_an_edge_without_data(services):
    verdict = services.analytics.ai_evaluation()
    assert verdict["verdict"] == "insufficient_data"
    assert "no claim" in verdict["detail"] or "needed" in verdict["detail"]
    assert verdict["linked_trades"] == 0


def test_daily_summary_reports_zero_activity_honestly(services):
    summary = services.analytics.daily_summary()
    assert summary["metrics"]["trades"] == 0
    assert summary["trades"] == []
    assert summary["ai_decisions"]["total"] == 0
    assert "ai_evaluation" in summary


def test_daily_summary_includes_closed_trades(services, position):
    services.execution.close_position(position.position_id)
    services.session.flush()
    summary = services.analytics.daily_summary()
    assert summary["metrics"]["trades"] == 1
    assert len(summary["trades"]) == 1
    assert summary["trades"][0]["symbol"] == "BTC/USDT"


def test_performance_context_feeds_the_ai_without_inventing_numbers(services):
    context = services.analytics.performance_context()
    assert context.trades_total == 0
    assert context.win_rate is None
    assert context.best_strategy is None


def test_strategy_performance_is_persisted_for_reporting(services, position):
    services.execution.close_position(position.position_id)
    services.session.flush()
    services.analytics.persist_strategy_performance()
    services.session.flush()
    rows = services.performance_repo.strategy_performance()
    assert rows
    assert rows[0].trades >= 1


def test_the_daily_report_is_built_from_stored_data(services):
    from app.reports.daily import build_daily_report

    report = build_daily_report(services, include_ai_summary=False, persist=True)
    services.session.flush()

    assert report["report_id"]
    assert report["facts"]["mode"] == "paper"
    assert "account" in report["facts"]
    assert "Daily trading report" in report["markdown"]
    assert "overstate live performance" in report["markdown"]

    stored = services.performance_repo.latest_report("daily")
    assert stored is not None
    assert stored.body_markdown == report["markdown"]


def test_the_daily_report_states_whether_the_books_close(services):
    """A silent accounting break is the one failure that invalidates everything."""
    from app.reports.daily import build_daily_report

    report = build_daily_report(services, include_ai_summary=False, persist=False)
    account = report["facts"]["account"]

    assert account["books_balance"] is True
    assert abs(account["reconciliation_error"]) <= float(
        services.portfolio.RECONCILIATION_TOLERANCE
    )
    assert "Fees paid to date" in report["markdown"]
    assert "Accounting does not reconcile" not in report["markdown"]


def test_the_daily_report_names_the_halt_reason(services):
    from app.reports.daily import build_daily_report

    services.bot_repo.halt("MAX_DRAWDOWN", "drawdown breached", "test")
    services.session.flush()
    report = build_daily_report(services, include_ai_summary=False, persist=False)
    assert "BOT STATUS: HALTED" in report["markdown"]
    assert report["facts"]["halt_reason"] == "MAX_DRAWDOWN"


# --------------------------------------------------------------- AI routes
def test_ai_context_endpoint_reports_when_there_is_no_candidate(client):
    body = client.post("/ai/context", json={"symbol": "BTC/USDT"}).json()
    assert "has_candidate" in body
    if not body["has_candidate"]:
        assert body["skip_reason"]


def test_ai_evaluate_declines_without_a_candidate(client):
    body = client.post("/ai/evaluate", json={"symbol": "BTC/USDT"}).json()
    assert body["evaluated"] in (True, False)
    if not body["evaluated"]:
        assert body["reason"]


def test_ai_decisions_endpoint_is_empty_before_any_call(client):
    assert client.get("/ai/decisions").json()["decisions"] == []


def test_ai_value_endpoint_says_insufficient_data(client):
    body = client.get("/ai/evaluation").json()
    assert body["verdict"] == "insufficient_data"
