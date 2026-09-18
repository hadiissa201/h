"""API contract tests.

n8n depends on these shapes: if a field disappears or an error stops carrying an
``error_code``, workflows break silently in production rather than loudly here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


# --------------------------------------------------------------------- auth
def test_health_needs_no_key(client):
    client.headers.pop("X-API-Key", None)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/portfolio", "/positions", "/risk/limits", "/performance", "/bot/state", "/orders"],
)
def test_trading_endpoints_require_a_key(client, path):
    client.headers.pop("X-API-Key", None)
    response = client.get(path)
    assert response.status_code == 401
    # Every error uses the same flat envelope so n8n can branch on error_code.
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_a_wrong_key_is_refused(client):
    client.headers["X-API-Key"] = "not-the-key"
    assert client.get("/portfolio").status_code == 401


def test_the_dashboard_refuses_an_unauthenticated_viewer(client):
    client.headers.pop("X-API-Key", None)
    assert client.get("/dashboard/summary").status_code == 401
    assert client.get("/").status_code == 401


def test_the_dashboard_accepts_a_key_in_the_query_string(client):
    """A browser cannot set headers on a navigation."""
    client.headers.pop("X-API-Key", None)
    assert client.get("/?key=test-api-key").status_code == 200
    assert client.get("/dashboard/summary?key=test-api-key").status_code == 200


# ------------------------------------------------------------------- health
def test_health_reports_mode_and_live_blockers(client):
    body = client.get("/health").json()
    assert body["mode"] == "paper"
    assert body["live_trading_armed"] is False
    assert body["bot_status"] == "RUNNING"
    assert len(body["live_mode_blockers"]) >= 3
    names = {component["name"] for component in body["components"]}
    assert "database" in names
    assert "market_data" in names
    assert any(name.startswith("exchange:") for name in names)


# ------------------------------------------------------------------ market
def test_market_snapshot_carries_a_quality_verdict(client):
    body = client.get("/market/BTC%2FUSDT").json()
    assert body["symbol"] == "BTC/USDT"
    assert body["data_ok"] is True
    assert body["last_price"] > 0
    assert "quality" in body and "issues" in body["quality"]


def test_candles_endpoint_returns_ohlcv(client):
    body = client.get("/market/BTC%2FUSDT/candles?limit=50").json()
    assert len(body["candles"]) == 50
    candle = body["candles"][0]
    assert {"timestamp", "open", "high", "low", "close", "volume"} <= set(candle)
    assert candle["high"] >= candle["low"]


def test_collect_validates_and_stores(client):
    body = client.post(
        "/market/collect", json={"symbols": ["BTC/USDT"], "timeframes": ["1h"]}
    ).json()
    assert body["all_data_ok"] is True
    assert body["symbols"][0]["timeframes"]["1h"]["bars"] > 100


def test_features_endpoint_returns_a_warm_feature_row(client):
    body = client.post("/features", json={"symbol": "BTC/USDT", "timeframe": "1h"}).json()
    assert body["is_warm"] is True
    assert body["features"]["close"] > 0
    assert "rsi" in body["features"] and "adx" in body["features"]
    assert body["missing_features"] == []


def test_features_can_be_restricted_to_one_block(client):
    body = client.post(
        "/features", json={"symbol": "BTC/USDT", "timeframe": "1h", "blocks": ["momentum"]}
    ).json()
    assert "rsi" in body["features"]
    assert "adx" not in body["features"]


# ----------------------------------------------------------------- analysis
def test_strategy_evaluate_returns_the_full_decision_context(client):
    body = client.post("/strategy/evaluate", json={"symbol": "BTC/USDT"}).json()
    assert body["symbol"] == "BTC/USDT"
    assert body["data_ok"] is True
    assert body["regime"]["regime"]
    assert isinstance(body["signals"], list)
    assert "should_call_ai" in body
    assert "has_open_position" in body
    assert body["bot_status"] == "RUNNING"


def test_strategies_catalogue_is_served(client):
    strategies = client.get("/strategies").json()["strategies"]
    # Counted against the registry rather than a literal: a hard-coded number
    # here just fails every time a strategy is added, which teaches nothing.
    from app.strategies.engine import STRATEGY_REGISTRY

    assert len(strategies) == len(STRATEGY_REGISTRY)
    assert all(entry["allowed_regimes"] for entry in strategies)
    assert all(entry["description"] for entry in strategies)


def test_regime_endpoint_lists_the_strategies_it_permits(client):
    body = client.post("/regime", json={"symbol": "BTC/USDT"}).json()
    assert body["regime"]["regime"]
    assert isinstance(body["allowed_strategies"], list)


# --------------------------------------------------------------------- risk
def test_risk_check_rejects_a_weak_proposal_with_codes(client):
    price = client.get("/market/BTC%2FUSDT").json()["last_price"]
    body = client.post(
        "/risk/check",
        json={
            "symbol": "BTC/USDT",
            "direction": "BUY",
            "entry": price,
            "stop_loss": price * 0.97,
            "take_profit": price * 1.08,
            "confidence": 0.1,
        },
    ).json()
    assert body["decision"] == "REJECTED"
    assert "LOW_CONFIDENCE" in body["rejection_codes"]
    assert body["approval_id"] is None
    assert body["checks"]


def test_risk_check_approves_and_issues_a_single_use_approval(client):
    price = client.get("/market/BTC%2FUSDT").json()["last_price"]
    body = client.post(
        "/risk/check",
        json={
            "symbol": "BTC/USDT",
            "direction": "BUY",
            "entry": price,
            "stop_loss": price * 0.96,
            "take_profit": price * 1.09,
            "confidence": 0.75,
            "strategy": "trend_following",
            "regime": "range",
        },
    ).json()
    assert body["decision"] == "APPROVED", body["reasons"]
    assert body["approval_id"].startswith("ra_")
    assert body["expires_at"]
    # Money is serialised as a JSON string: JSON numbers are floats, and a float
    # is not a safe container for a quantity or a price.
    assert isinstance(body["sizing"]["quantity"], str)
    assert Decimal(body["sizing"]["quantity"]) > 0


def test_risk_limits_are_published(client):
    body = client.get("/risk/limits").json()
    assert float(body["risk_per_trade"]) > 0
    assert body["require_stop_loss"] is True
    assert body["allow_short"] is False


def test_malformed_risk_payloads_are_rejected_with_field_errors(client):
    response = client.post("/risk/check", json={"symbol": "BTC/USDT"})
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "REQUEST_VALIDATION_ERROR"
    assert body["context"]["errors"]


def test_an_inverted_stop_is_rejected_by_the_schema(client):
    response = client.post(
        "/risk/check",
        json={
            "symbol": "BTC/USDT",
            "direction": "BUY",
            "entry": 100,
            "stop_loss": 110,
            "confidence": 0.8,
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- execution
def test_order_without_approval_returns_a_machine_readable_error(client):
    response = client.post("/paper/order", json={"risk_approval_id": "ra_nope123456"})
    assert response.status_code == 403
    assert response.json()["error_code"] == "RISK_REJECTED"


def test_full_chain_through_the_api(client):
    price = client.get("/market/BTC%2FUSDT").json()["last_price"]
    decision = client.post(
        "/risk/check",
        json={
            "symbol": "BTC/USDT",
            "direction": "BUY",
            "entry": price,
            "stop_loss": price * 0.96,
            "take_profit": price * 1.09,
            "confidence": 0.8,
            "strategy": "trend_following",
            "regime": "range",
        },
    ).json()
    assert decision["decision"] == "APPROVED"

    placed = client.post(
        "/paper/order", json={"risk_approval_id": decision["approval_id"]}
    ).json()
    assert placed["order"]["status"] == "FILLED"
    position_id = placed["position"]["position_id"]

    positions = client.get("/positions").json()
    assert positions["count"] == 1

    portfolio = client.get("/portfolio").json()
    assert portfolio["open_positions"] == 1
    assert float(portfolio["exposure_pct"]) > 0

    closed = client.post(
        f"/positions/{position_id}/close", json={"reason": "MANUAL", "fraction": 1}
    ).json()
    assert closed["position"]["status"] == "CLOSED"
    assert closed["trade"]["trade_id"]

    assert client.get("/positions").json()["count"] == 0
    performance = client.get("/performance").json()
    assert performance["metrics"]["trades"] == 1
    assert "honesty_note" in performance


def test_monitor_endpoint_runs_without_positions(client):
    body = client.post("/positions/monitor").json()
    assert body["checked"] == 0
    assert body["bot_status"] == "RUNNING"
    assert "equity" in body


def test_unknown_position_returns_404(client):
    response = client.get("/positions/pos_missing")
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


# --------------------------------------------------------------- bot state
def test_halt_and_reset_cycle(client):
    halted = client.post(
        "/bot/halt", json={"reason": "MANUAL", "detail": "drill", "source": "test"}
    ).json()
    assert halted["state"]["status"] == "HALTED"
    assert halted["requires_manual_reset"] is True
    assert halted["emergency_event_id"]

    assert client.get("/bot/state").json()["status"] == "HALTED"

    refused = client.post("/bot/reset", json={"confirmation": "yes", "operator": "t"})
    assert refused.status_code == 422

    reset = client.post(
        "/bot/reset", json={"confirmation": "RESET", "operator": "tester", "note": "done"}
    ).json()
    assert reset["state"]["status"] == "RUNNING"
    assert reset["state"]["halt_reason"] is None


def test_resetting_a_running_bot_is_a_conflict(client):
    response = client.post("/bot/reset", json={"confirmation": "RESET", "operator": "t"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "CONFLICT"


def test_live_readiness_lists_unmet_requirements(client):
    body = client.get("/bot/live-readiness").json()
    assert body["ready"] is False
    assert body["blockers"]
    keys = {item["key"] for item in body["items"]}
    assert {"paper_mode_default", "live_switches", "kill_switch_exercised"} <= keys
    assert "Paper results do not transfer" in body["reminder"]


def test_workflow_error_sink_records_and_can_halt(client):
    body = client.post(
        "/workflow/error",
        json={
            "workflow": "05 - Trade Execution",
            "message": "order node failed",
            "severity": "CRITICAL",
            "halt_bot": True,
        },
    ).json()
    assert body["recorded"] is True
    assert body["halted"] is True
    assert client.get("/bot/state").json()["status"] == "HALTED"


# --------------------------------------------------------------- dashboard
def test_dashboard_summary_has_everything_the_page_needs(client):
    body = client.get("/dashboard/summary").json()
    for section in (
        "account",
        "risk",
        "positions",
        "performance",
        "recent_signals",
        "recent_ai_decisions",
        "recent_risk_decisions",
        "recent_trades",
        "equity_curve",
        "ai_evaluation",
        "recent_events",
        "bot",
    ):
        assert section in body, f"dashboard summary is missing '{section}'"
    assert body["mode"] == "paper"
    assert body["live_trading_armed"] is False


def test_dashboard_page_renders(client):
    response = client.get("/?key=test-api-key")
    assert response.status_code == 200
    assert "Trading System Monitor" in response.text
    assert "dashboard/summary" in response.text


def test_openapi_document_is_served(client):
    body = client.get("/openapi.json").json()
    assert "/risk/check" in body["paths"]
    assert "/paper/order" in body["paths"]
    assert "/positions/monitor" in body["paths"]
