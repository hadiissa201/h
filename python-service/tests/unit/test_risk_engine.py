"""Risk engine rules.

Each test breaks exactly one thing and asserts the engine rejects for that
reason. The point is not coverage for its own sake: every one of these rules is
the only thing standing between a bad input and a loss.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.models.enums import BotStatus, MarketRegime, SignalDirection, TradingModeEnum
from app.models.risk import AccountRiskState, RiskProposal
from app.models.trading import SymbolSpec
from app.risk.engine import MarketConditions, RiskEngine, limits_from_settings
from app.utils.time import utcnow

SPEC = SymbolSpec(
    symbol="BTC/USDT",
    base="BTC",
    quote="USDT",
    quantity_step=Decimal("0.00001"),
    min_notional=Decimal("10"),
)


@pytest.fixture
def limits(settings):
    return limits_from_settings(settings)


@pytest.fixture
def engine(limits):
    return RiskEngine(limits)


@pytest.fixture
def account():
    def _make(**overrides) -> AccountRiskState:
        payload = {
            "timestamp": utcnow(),
            "mode": TradingModeEnum.PAPER,
            "bot_status": BotStatus.RUNNING,
            "equity": Decimal("10000"),
            "cash": Decimal("10000"),
            "starting_equity": Decimal("10000"),
            "peak_equity": Decimal("10000"),
            "open_positions": 0,
            "open_symbols": [],
            "exposure_pct": Decimal("0"),
            "daily_pnl_pct": Decimal("0"),
            "drawdown_pct": Decimal("0"),
            "consecutive_losses": 0,
        }
        payload.update(overrides)
        return AccountRiskState(**payload)

    return _make


@pytest.fixture
def conditions():
    def _make(**overrides) -> MarketConditions:
        payload = {
            "spread_bps": Decimal("5"),
            "quote_volume_24h": Decimal("50000000"),
            "data_quality_ok": True,
            "regime": MarketRegime.RANGE,
            "regime_abnormal": False,
        }
        payload.update(overrides)
        return MarketConditions(**payload)

    return _make


@pytest.fixture
def evaluate(engine, account, conditions, proposal_factory):
    def _run(proposal_kwargs=None, account_kwargs=None, condition_kwargs=None):
        return engine.evaluate(
            proposal_factory(**(proposal_kwargs or {})),
            account(**(account_kwargs or {})),
            SPEC,
            conditions(**(condition_kwargs or {})),
        )

    return _run


def codes(decision) -> set[str]:
    return set(decision.rejection_codes)


# ------------------------------------------------------------------- approval
def test_a_sound_proposal_is_approved_and_sized(evaluate):
    decision = evaluate()
    assert decision.approved, decision.reasons
    assert decision.quantity > 0
    assert decision.sizing.effective_risk_pct <= Decimal("0.005")


def test_every_check_is_reported_even_when_passing(evaluate):
    decision = evaluate()
    assert len(decision.checks) >= 15
    assert all(check.name for check in decision.checks)
    assert all(check.passed for check in decision.checks)


def test_approval_is_not_issued_by_the_pure_engine(evaluate):
    """Approval ids are minted by the service layer, which persists them."""
    decision = evaluate()
    assert decision.approval_id is None


# --------------------------------------------------------------- system state
def test_halted_bot_blocks_everything(evaluate):
    decision = evaluate(account_kwargs={"bot_status": BotStatus.HALTED})
    assert not decision.approved
    assert "BOT_NOT_RUNNING" in codes(decision)


def test_active_cooldown_blocks_entry(evaluate):
    decision = evaluate(
        account_kwargs={
            "cooldown_until": utcnow() + timedelta(minutes=30),
            "consecutive_losses": 3,
        }
    )
    assert "COOLDOWN_ACTIVE" in codes(decision)


def test_expired_cooldown_does_not_block(evaluate):
    decision = evaluate(
        account_kwargs={"cooldown_until": utcnow() - timedelta(minutes=1)}
    )
    assert decision.approved, decision.reasons


# --------------------------------------------------------------------- data
def test_bad_market_data_blocks_entry(evaluate):
    assert "BAD_MARKET_DATA" in codes(evaluate(condition_kwargs={"data_quality_ok": False}))


def test_abnormal_market_blocks_entry(evaluate):
    assert "ABNORMAL_MARKET" in codes(evaluate(condition_kwargs={"regime_abnormal": True}))


def test_unknown_regime_blocks_entry(evaluate):
    decision = evaluate(
        proposal_kwargs={"regime": MarketRegime.UNKNOWN},
        condition_kwargs={"regime": MarketRegime.UNKNOWN},
    )
    assert "REGIME_UNKNOWN" in codes(decision)


def test_unknown_spread_is_a_rejection_not_a_warning(evaluate):
    """Fail closed: trading blind on microstructure is not allowed."""
    decision = evaluate(
        proposal_kwargs={"spread_bps": None}, condition_kwargs={"spread_bps": None}
    )
    assert "SPREAD_UNKNOWN" in codes(decision)


def test_unknown_liquidity_is_a_rejection(evaluate):
    decision = evaluate(
        proposal_kwargs={"quote_volume_24h": None},
        condition_kwargs={"quote_volume_24h": None},
    )
    assert "LIQUIDITY_UNKNOWN" in codes(decision)


# ------------------------------------------------------------------ direction
def test_short_entries_are_refused_in_spot_mode(evaluate):
    decision = evaluate(
        proposal_kwargs={
            "direction": SignalDirection.SELL,
            "entry": "100",
            "stop_loss": Decimal("103"),
            "take_profit": Decimal("92"),
        }
    )
    assert "SHORT_NOT_ALLOWED" in codes(decision)


def test_inactive_symbol_is_refused(engine, account, conditions, proposal_factory):
    inactive = SymbolSpec(symbol="X/USDT", base="X", quote="USDT", active=False)
    decision = engine.evaluate(proposal_factory(), account(), inactive, conditions())
    assert "SYMBOL_INACTIVE" in codes(decision)


# ----------------------------------------------------------------- stop loss
def test_stop_too_tight_is_refused(evaluate):
    decision = evaluate(
        proposal_kwargs={"entry": "100", "stop_loss": Decimal("99.9")}
    )
    assert "STOP_TOO_TIGHT" in codes(decision)


def test_stop_too_wide_is_refused(evaluate):
    decision = evaluate(
        proposal_kwargs={
            "entry": "100",
            "stop_loss": Decimal("70"),
            "take_profit": Decimal("200"),
        }
    )
    assert "STOP_TOO_WIDE" in codes(decision)


def test_a_proposal_without_a_stop_cannot_even_be_built(proposal_factory):
    """The schema refuses it before the engine ever sees it."""
    with pytest.raises(ValueError):
        RiskProposal(
            symbol="BTC/USDT",
            direction=SignalDirection.BUY,
            entry=Decimal("100"),
            stop_loss=Decimal("101"),  # above entry for a long
            confidence=0.9,
        )


# ------------------------------------------------------------ idea quality
def test_low_confidence_is_refused(evaluate):
    assert "LOW_CONFIDENCE" in codes(evaluate(proposal_kwargs={"confidence": 0.4}))


def test_missing_take_profit_is_refused(evaluate):
    assert "NO_TAKE_PROFIT" in codes(evaluate(proposal_kwargs={"take_profit": None}))


def test_poor_reward_to_risk_is_refused(evaluate):
    decision = evaluate(
        proposal_kwargs={
            "entry": "100",
            "stop_loss": Decimal("96"),
            "take_profit": Decimal("103"),  # 0.75 R
        }
    )
    assert "POOR_RISK_REWARD" in codes(decision)


# --------------------------------------------------------------- portfolio
def test_max_open_positions_is_enforced(evaluate):
    decision = evaluate(
        account_kwargs={"open_positions": 3, "open_symbols": ["A/USDT", "B/USDT", "C/USDT"]}
    )
    assert "MAX_OPEN_POSITIONS" in codes(decision)


def test_a_second_position_in_the_same_symbol_is_refused(evaluate):
    decision = evaluate(
        account_kwargs={"open_positions": 1, "open_symbols": ["BTC/USDT"]}
    )
    assert "SYMBOL_POSITION_EXISTS" in codes(decision)


def test_daily_loss_limit_is_enforced(evaluate):
    decision = evaluate(account_kwargs={"daily_pnl_pct": Decimal("-0.02")})
    assert "MAX_DAILY_LOSS" in codes(decision)


def test_just_inside_the_daily_loss_limit_still_trades(evaluate):
    decision = evaluate(account_kwargs={"daily_pnl_pct": Decimal("-0.0199")})
    assert decision.approved, decision.reasons


def test_max_drawdown_is_enforced(evaluate):
    decision = evaluate(
        account_kwargs={"drawdown_pct": Decimal("0.10"), "peak_equity": Decimal("11200")}
    )
    assert "MAX_DRAWDOWN" in codes(decision)


def test_insufficient_cash_is_refused(evaluate):
    decision = evaluate(
        account_kwargs={"equity": Decimal("10000"), "cash": Decimal("5")}
    )
    assert not decision.approved
    assert {"SIZING_FAILED", "INSUFFICIENT_CASH"} & codes(decision)


def test_zero_equity_is_refused(evaluate):
    decision = evaluate(
        account_kwargs={"equity": Decimal("0"), "cash": Decimal("0")}
    )
    assert "NO_EQUITY" in codes(decision)


# ------------------------------------------------------------ microstructure
def test_wide_spread_is_refused(evaluate):
    assert "SPREAD_TOO_WIDE" in codes(evaluate(condition_kwargs={"spread_bps": Decimal("40")}))


def test_thin_liquidity_is_refused(evaluate, settings):
    decision = evaluate(condition_kwargs={"quote_volume_24h": Decimal("10")})
    assert "INSUFFICIENT_LIQUIDITY" in codes(decision)


def test_abnormal_last_bar_move_is_refused(evaluate):
    decision = evaluate(proposal_kwargs={"last_bar_move_pct": Decimal("0.25")})
    assert "ABNORMAL_PRICE_MOVE" in codes(decision)


# --------------------------------------------------------- trust boundaries
def test_caller_supplied_spread_cannot_override_a_worse_server_value(
    engine, account, conditions, proposal_factory
):
    """A caller claiming a tight spread must not beat the resolved one."""
    decision = engine.evaluate(
        proposal_factory(spread_bps=Decimal("1")),
        account(),
        SPEC,
        conditions(spread_bps=Decimal("40")),
    )
    assert "SPREAD_TOO_WIDE" in codes(decision)


def test_multiple_breaches_are_all_reported(evaluate):
    decision = evaluate(
        proposal_kwargs={"confidence": 0.1},
        account_kwargs={"bot_status": BotStatus.HALTED, "open_positions": 5},
    )
    assert {"BOT_NOT_RUNNING", "LOW_CONFIDENCE", "MAX_OPEN_POSITIONS"} <= codes(decision)
    assert len(decision.reasons) >= 3


def test_rejection_reasons_are_human_readable(evaluate):
    decision = evaluate(proposal_kwargs={"confidence": 0.2})
    assert any("confidence" in reason for reason in decision.reasons)
    failed = [check for check in decision.checks if not check.passed]
    assert failed[0].value is not None and failed[0].limit is not None


def test_engine_is_deterministic(evaluate):
    first = evaluate()
    second = evaluate()
    assert first.decision == second.decision
    assert first.quantity == second.quantity
    assert [c.name for c in first.checks] == [c.name for c in second.checks]
