"""Behaviour when things break.

Every case here asserts the system **fails closed**: a broken dependency, a
confused model or a disorderly market results in no trade, not a guess.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.implementations import DisabledProvider, OllamaProvider
from app.core.errors import (
    InsufficientBalanceError,
    LLMError,
    MarketDataError,
    OrderRejectedError,
)
from app.data.providers.base import MarketDataProvider
from app.data.service import MarketDataService
from app.models.enums import OrderType, Side
from app.models.trading import OrderRequest


# ---------------------------------------------------------------- market data
class BrokenProvider(MarketDataProvider):
    name = "broken"

    def fetch_ohlcv(self, symbol, timeframe, limit=300, since=None):
        raise MarketDataError(f"exchange unreachable for {symbol}")

    def fetch_ticker(self, symbol):
        raise MarketDataError("exchange unreachable")


class StaleProvider(MarketDataProvider):
    """Serves a week-old snapshot, as a wedged feed would."""

    name = "stale"

    def __init__(self, good: MarketDataService) -> None:
        self._good = good

    def fetch_ohlcv(self, symbol, timeframe, limit=300, since=None):
        frame = self._good.get_candles(symbol, timeframe, limit=limit)
        frame = frame.copy()
        frame.index = frame.index - timedelta(days=7)
        return frame

    def fetch_ticker(self, symbol):
        return self._good.get_ticker(symbol)


def test_an_unreachable_exchange_stops_analysis(session, settings, market_data):
    from app.container import build_services

    services = build_services(
        session, settings, market_data=MarketDataService(provider=BrokenProvider())
    )
    result = services.analysis.analyse("BTC/USDT", "1h")
    assert not result.data_ok
    assert result.candidate is None
    assert "unreachable" in result.skip_reason


def test_stale_data_produces_no_trade(session, settings, market_data):
    from app.container import build_services

    services = build_services(
        session, settings, market_data=MarketDataService(provider=StaleProvider(market_data))
    )
    result = services.analysis.analyse("BTC/USDT", "1h")
    assert not result.data_ok
    assert result.candidate is None
    assert "STALE_DATA" in str(result.data_quality) or "stale" in result.skip_reason.lower()


def test_stale_data_is_a_kill_switch_trigger(services):
    triggered = services.risk.run_safety_checks(stale_symbols=["BTC/USDT"])
    assert any(event.reason == "MARKET_DATA_STALE" for event in triggered)
    assert services.bot_repo.get().status == "HALTED"


def test_a_market_data_failure_during_a_risk_check_blocks_the_trade(
    session, settings, proposal_factory
):
    from app.container import build_services

    services = build_services(
        session, settings, market_data=MarketDataService(provider=BrokenProvider())
    )
    decision = services.risk.check(proposal_factory(spread_bps=None, quote_volume_24h=None))
    assert not decision.approved
    assert {"BAD_MARKET_DATA", "SPREAD_UNKNOWN", "LIQUIDITY_UNKNOWN"} & set(
        decision.rejection_codes
    )


def test_extreme_volatility_makes_the_market_untradeable(services, market_data):
    """A 30% candle is not an opportunity, it is a reason to stand aside."""
    frame = market_data.provider.generate("BTC/USDT", "1h", 400).copy()
    position = frame.columns.get_loc("close")
    frame.iloc[-1, position] = float(frame["close"].iloc[-2]) * Decimal("1.30").__float__()
    frame.iloc[-1, frame.columns.get_loc("high")] = float(frame["close"].iloc[-1]) * 1.01

    features = services.features.compute(frame, "BTC/USDT", "1h")
    assessment = services.regime.classify(features)
    assert assessment.is_abnormal
    output = services.strategies.evaluate(features, assessment)
    assert output.candidate is None
    assert output.signals == []


# ------------------------------------------------------------------------ AI
class TimeoutProvider(LLMProvider):
    name = "timeout"

    def __init__(self) -> None:
        super().__init__(model="test", timeout_seconds=1.0)

    def complete_json(self, **_):
        raise LLMError("request timed out after 60s")


class GarbageProvider(LLMProvider):
    name = "garbage"

    def __init__(self, text: str) -> None:
        super().__init__(model="test", timeout_seconds=1.0)
        self.text = text

    def complete_json(self, **_):
        return LLMResponse(
            text=self.text, provider=self.name, model="test", latency_ms=5
        )


@pytest.fixture
def ai_context(services):
    """A real AI context built from a synthetic candidate."""
    from datetime import UTC, datetime

    from app.models.signals import RegimeAssessment, TradeCandidate

    price = services.market_data.get_ticker("BTC/USDT").last
    regime = RegimeAssessment(
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=datetime.now(UTC),
        regime="range",
        trend_state="flat",
        volatility_state="normal",
        confidence=0.6,
    )
    candidate = TradeCandidate(
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=datetime.now(UTC),
        direction="BUY",
        price=price,
        entry=price,
        stop_loss=price * Decimal("0.96"),
        take_profit=price * Decimal("1.09"),
        confidence=0.7,
        regime=regime,
        aligned_strategies=["trend_following"],
    )
    context = services.ai.build_context(
        candidate, {"1h": {}}, services.risk.limits_view()
    )
    return candidate, context


def test_an_llm_timeout_becomes_hold(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = TimeoutProvider()
    evaluation = services.ai.evaluate(context)
    assert evaluation.decision.decision == "HOLD"
    assert evaluation.fallback_used
    assert not evaluation.parse_ok
    proposal, notes = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal is None


@pytest.mark.parametrize(
    "garbage",
    [
        "I would buy here, looks strong!",
        '{"decision": "BUY"',
        '{"decision":"MOON","confidence":2,"reason":"x"}',
        "",
        "<html>502 Bad Gateway</html>",
    ],
)
def test_garbage_from_the_model_becomes_hold(services, ai_context, garbage):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(garbage)
    evaluation = services.ai.evaluate(context)
    assert evaluation.decision.decision == "HOLD"
    proposal, _ = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal is None


def test_a_model_flipping_the_direction_is_refused(services, ai_context):
    """The model is a reviewer. It cannot turn a long into a short."""
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(
        '{"decision":"SELL","confidence":0.95,"reason":"I disagree"}'
    )
    evaluation = services.ai.evaluate(context)
    proposal, notes = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal is None
    assert any("against the deterministic candidate" in note for note in notes)
    assert evaluation.violations


def test_a_model_cannot_raise_confidence_above_the_deterministic_view(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(
        '{"decision":"BUY","confidence":0.99,"reason":"very confident"}'
    )
    evaluation = services.ai.evaluate(context)
    proposal, notes = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal is not None
    assert proposal.confidence == pytest.approx(candidate.confidence)
    assert any("capped" in note for note in notes)


def test_a_model_can_lower_confidence(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(
        '{"decision":"BUY","confidence":0.62,"reason":"ok but thin"}'
    )
    evaluation = services.ai.evaluate(context)
    proposal, _ = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal.confidence == pytest.approx(0.62)


def test_the_proposal_levels_always_come_from_the_quant_layer(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(
        '{"decision":"BUY","confidence":0.7,"reason":"widen the stop please"}'
    )
    evaluation = services.ai.evaluate(context)
    proposal, _ = services.ai.apply_to_candidate(candidate, evaluation)
    assert proposal.entry == candidate.entry
    assert proposal.stop_loss == candidate.stop_loss
    assert proposal.take_profit == candidate.take_profit


def test_rate_limiting_skips_the_model_and_holds(services, ai_context, settings):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider(
        '{"decision":"BUY","confidence":0.8,"reason":"fine"}'
    )
    first = services.ai.evaluate(context)
    assert first.decision.decision == "BUY"
    services.session.flush()

    # The per-symbol cooldown now applies.
    second = services.ai.evaluate(context)
    assert second.decision.decision == "HOLD"
    assert "cooldown" in second.decision.reason


def test_a_disabled_provider_holds_without_calling_anything(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = DisabledProvider()
    evaluation = services.ai.evaluate(context)
    assert evaluation.decision.decision == "HOLD"
    assert not evaluation.ai_enabled


def test_every_ai_decision_is_persisted_even_when_it_fails(services, ai_context):
    candidate, context = ai_context
    services.ai._provider = GarbageProvider("not json at all")
    evaluation = services.ai.evaluate(context)
    services.session.flush()
    stored = services.ai_repo.get(evaluation.decision_id)
    assert stored is not None
    assert stored.decision == "HOLD"
    assert stored.parse_ok is False
    assert stored.raw_response == "not json at all"
    assert stored.context  # the exact inputs are recoverable


def test_ollama_health_check_reports_an_unreachable_host():
    provider = OllamaProvider("test-model", base_url="http://127.0.0.1:9", timeout_seconds=0.2)
    healthy, detail = provider.health_check()
    assert not healthy
    assert "unreachable" in detail


def test_ollama_wraps_transport_errors(monkeypatch):
    provider = OllamaProvider("m", base_url="http://localhost:1", timeout_seconds=0.1)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "post", boom)
    with pytest.raises(LLMError, match="ollama request failed"):
        provider.complete_json(system_prompt="s", user_prompt="u")


# ------------------------------------------------------------------ exchange
def test_an_order_larger_than_the_balance_is_refused(services):
    with pytest.raises(InsufficientBalanceError):
        services.exchange.create_order(
            OrderRequest(
                symbol="BTC/USDT",
                side=Side.BUY,
                type=OrderType.MARKET,
                quantity=Decimal("1000"),
                client_order_id="too-big",
            )
        )


def test_selling_something_we_do_not_hold_is_refused(services):
    with pytest.raises(InsufficientBalanceError):
        services.exchange.create_order(
            OrderRequest(
                symbol="ETH/USDT",
                side=Side.SELL,
                type=OrderType.MARKET,
                quantity=Decimal("5"),
                client_order_id="naked-short",
            )
        )


def test_a_dust_order_is_refused_by_the_minimum_notional(services):
    with pytest.raises(OrderRejectedError, match="minimum"):
        services.exchange.create_order(
            OrderRequest(
                symbol="BTC/USDT",
                side=Side.BUY,
                type=OrderType.MARKET,
                quantity=Decimal("0.00001"),
                client_order_id="dust",
            )
        )


def test_rejected_orders_are_recorded_so_repeats_can_trip_the_kill_switch(services):
    for index in range(3):
        with pytest.raises(OrderRejectedError):
            services.exchange.create_order(
                OrderRequest(
                    symbol="BTC/USDT",
                    side=Side.BUY,
                    type=OrderType.MARKET,
                    quantity=Decimal("0.00001"),
                    client_order_id=f"dust-{index}",
                )
            )
    services.session.flush()
    from app.utils.time import utcnow

    failures = services.execution_repo.count_failed_orders_since(
        utcnow() - timedelta(minutes=5)
    )
    assert failures >= 3

    triggered = services.risk.run_safety_checks()
    assert any(event.reason == "REPEATED_ORDER_FAILURES" for event in triggered)
    assert services.bot_repo.get().status == "HALTED"


def test_a_duplicate_client_order_id_does_not_create_a_second_order(services):
    request = OrderRequest(
        symbol="BTC/USDT",
        side=Side.BUY,
        type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        client_order_id="only-once",
    )
    first = services.exchange.create_order(request)
    second = services.exchange.create_order(request)
    assert first.order_id == second.order_id
    assert len(services.execution_repo.recent_orders(10)) == 1


def test_the_live_adapter_refuses_to_trade_while_disarmed(settings):
    from app.core.errors import LiveTradingBlockedError
    from app.execution.live_exchange import LiveExchangeAdapter

    adapter = LiveExchangeAdapter(settings)
    with pytest.raises(LiveTradingBlockedError):
        adapter.create_order(
            OrderRequest(
                symbol="BTC/USDT",
                side=Side.BUY,
                type=OrderType.MARKET,
                quantity=Decimal("1"),
            )
        )
    healthy, detail = adapter.health_check()
    assert not healthy
    assert "not armed" in detail


# ------------------------------------------------------------------ database
def test_an_unreachable_database_is_reported_not_raised(settings):
    """``check_database`` is what /health uses; it must never raise."""
    from app.database.session import check_database

    broken = settings.model_copy(
        update={"database_url": "postgresql+psycopg://nobody@127.0.0.1:1/nothing"}
    )
    import app.database.session as session_module

    saved_engine, saved_factory = session_module._engine, session_module._session_factory
    session_module._engine = None
    session_module._session_factory = None
    try:
        healthy, detail = check_database(broken)
        assert healthy is False
        assert detail  # carries the reason for the operator
    finally:
        session_module._engine, session_module._session_factory = saved_engine, saved_factory


def test_a_healthy_database_reports_ok(db_engine, settings):
    from app.database.session import check_database

    healthy, detail = check_database(settings)
    assert healthy is True
    assert detail == "ok"


def test_the_service_starts_even_if_bootstrap_fails(client):
    """A database problem must surface on /health, not crash-loop the container."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in ("ok", "degraded")
