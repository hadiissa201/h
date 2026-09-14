"""Deterministic risk engine.

The final authority on whether a trade may happen. Properties that make that
claim meaningful:

* **Deterministic.** Same proposal + same account state + same limits => same
  verdict. No model, no randomness, no network call.
* **Independent of the AI.** The LLM's opinion arrives as two fields (a decision
  and a confidence) and can only ever *lose* the trade a check. It cannot raise
  size, widen a stop, or skip a limit.
* **Fail closed.** Unknown spread, unknown liquidity, missing data quality flag,
  unknown regime — all rejections, not warnings.
* **Explicit.** Every check is reported with its value and its limit, so a
  rejection can be audited without re-running anything.

``evaluate`` is pure: account state and market conditions are inputs. The
DB-backed wrapper lives in ``app.risk.service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.numeric import ZERO, to_decimal
from app.models.enums import (
    BotStatus,
    MarketRegime,
    RiskDecisionType,
    SignalDirection,
    TradingModeEnum,
)
from app.models.risk import (
    AccountRiskState,
    PositionSizing,
    RiskCheck,
    RiskDecision,
    RiskLimitsView,
    RiskProposal,
)
from app.models.trading import SymbolSpec
from app.risk.sizing import calculate_position_size
from app.utils.time import utcnow


@dataclass(frozen=True)
class MarketConditions:
    """Microstructure facts resolved server-side, never taken on trust."""

    spread_bps: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    last_bar_move_pct: Decimal | None = None
    data_quality_ok: bool = True
    regime: MarketRegime = MarketRegime.UNKNOWN
    regime_abnormal: bool = False


class RiskEngine:
    def __init__(self, limits: RiskLimitsView, *, require_microstructure: bool = True) -> None:
        self.limits = limits
        self.require_microstructure = require_microstructure

    # ------------------------------------------------------------------ main
    def evaluate(
        self,
        proposal: RiskProposal,
        account: AccountRiskState,
        spec: SymbolSpec,
        conditions: MarketConditions,
        *,
        current_exposure_value: Decimal | None = None,
        mode: TradingModeEnum = TradingModeEnum.PAPER,
    ) -> RiskDecision:
        limits = self.limits
        checks: list[RiskCheck] = []
        rejections: list[str] = []
        reasons: list[str] = []
        warnings: list[str] = []

        def add(
            name: str,
            passed: bool,
            detail: str,
            *,
            value: object = None,
            limit: object = None,
            code: str | None = None,
            blocking: bool = True,
        ) -> None:
            checks.append(
                RiskCheck(
                    name=name,
                    passed=passed,
                    detail=detail,
                    value=value,  # type: ignore[arg-type]
                    limit=limit,  # type: ignore[arg-type]
                    blocking=blocking,
                )
            )
            if not passed:
                if blocking:
                    rejections.append(code or name.upper())
                    reasons.append(detail)
                else:
                    warnings.append(detail)

        # --- 1. system state ------------------------------------------------
        add(
            "bot_running",
            account.bot_status is BotStatus.RUNNING,
            f"bot status is {account.bot_status}"
            + (f" ({account.halt_reason})" if account.halt_reason else ""),
            value=str(account.bot_status),
            limit=str(BotStatus.RUNNING),
            code="BOT_NOT_RUNNING",
        )
        now = utcnow()
        cooldown_active = bool(account.cooldown_until and account.cooldown_until > now)
        add(
            "cooldown",
            not cooldown_active,
            (
                f"cooldown active until {account.cooldown_until.isoformat()} after "
                f"{account.consecutive_losses} consecutive losses"
                if cooldown_active
                else "no cooldown active"
            ),
            value=account.consecutive_losses,
            limit=limits.consecutive_loss_limit,
            code="COOLDOWN_ACTIVE",
        )

        # --- 2. data integrity ---------------------------------------------
        data_ok = conditions.data_quality_ok and proposal.data_quality_ok
        add(
            "market_data_quality",
            data_ok,
            "market data failed validation — no trade" if not data_ok else "market data valid",
            code="BAD_MARKET_DATA",
        )
        abnormal = conditions.regime_abnormal or proposal.regime_abnormal
        add(
            "market_conditions_normal",
            not abnormal,
            "abnormal market conditions" if abnormal else "market conditions normal",
            code="ABNORMAL_MARKET",
        )
        add(
            "regime_known",
            conditions.regime is not MarketRegime.UNKNOWN
            or proposal.regime is not MarketRegime.UNKNOWN,
            "market regime could not be determined",
            value=str(conditions.regime or proposal.regime),
            code="REGIME_UNKNOWN",
        )

        # --- 3. direction / instrument -------------------------------------
        is_short = proposal.direction is SignalDirection.SELL
        add(
            "direction_allowed",
            not is_short or limits.allow_short,
            "short selling is disabled (spot, long only)"
            if is_short
            else f"{proposal.direction} allowed",
            value=str(proposal.direction),
            code="SHORT_NOT_ALLOWED",
        )
        add(
            "symbol_active",
            spec.active,
            f"symbol {spec.symbol} is not active for trading",
            code="SYMBOL_INACTIVE",
        )

        # --- 4. stop loss ---------------------------------------------------
        stop_distance_pct = (
            proposal.stop_distance / proposal.entry if proposal.entry > ZERO else ZERO
        )
        add(
            "stop_loss_present",
            proposal.stop_loss > ZERO or not limits.require_stop_loss,
            "a stop loss is mandatory",
            code="STOP_LOSS_REQUIRED",
        )
        add(
            "stop_distance_min",
            stop_distance_pct >= limits.min_stop_distance_pct,
            f"stop {stop_distance_pct:.4%} from entry is tighter than the "
            f"{limits.min_stop_distance_pct:.4%} minimum (noise stop)",
            value=stop_distance_pct,
            limit=limits.min_stop_distance_pct,
            code="STOP_TOO_TIGHT",
        )
        add(
            "stop_distance_max",
            stop_distance_pct <= limits.max_stop_distance_pct,
            f"stop {stop_distance_pct:.4%} from entry exceeds the "
            f"{limits.max_stop_distance_pct:.4%} maximum",
            value=stop_distance_pct,
            limit=limits.max_stop_distance_pct,
            code="STOP_TOO_WIDE",
        )

        # --- 5. quality of the idea ----------------------------------------
        confidence = to_decimal(proposal.confidence)
        add(
            "min_confidence",
            confidence >= limits.min_confidence,
            f"confidence {confidence} below minimum {limits.min_confidence}",
            value=confidence,
            limit=limits.min_confidence,
            code="LOW_CONFIDENCE",
        )
        risk_reward = proposal.risk_reward
        if risk_reward is None:
            add(
                "min_risk_reward",
                False,
                "no take profit supplied, so reward/risk cannot be verified",
                limit=limits.min_risk_reward,
                code="NO_TAKE_PROFIT",
            )
        else:
            add(
                "min_risk_reward",
                risk_reward >= limits.min_risk_reward,
                f"reward/risk {risk_reward:.2f} below minimum {limits.min_risk_reward}",
                value=risk_reward,
                limit=limits.min_risk_reward,
                code="POOR_RISK_REWARD",
            )

        # --- 6. portfolio limits -------------------------------------------
        add(
            "max_open_positions",
            account.open_positions < limits.max_open_positions,
            f"{account.open_positions} open positions at the limit of "
            f"{limits.max_open_positions}",
            value=account.open_positions,
            limit=limits.max_open_positions,
            code="MAX_OPEN_POSITIONS",
        )
        same_symbol = sum(1 for symbol in account.open_symbols if symbol == proposal.symbol)
        add(
            "max_positions_per_symbol",
            same_symbol < limits.max_positions_per_symbol,
            f"{same_symbol} open position(s) already in {proposal.symbol}",
            value=same_symbol,
            limit=limits.max_positions_per_symbol,
            code="SYMBOL_POSITION_EXISTS",
        )
        add(
            "max_daily_loss",
            account.daily_pnl_pct > -limits.max_daily_loss_pct,
            f"daily P&L {account.daily_pnl_pct:.4%} at or beyond the "
            f"-{limits.max_daily_loss_pct:.4%} daily loss limit",
            value=account.daily_pnl_pct,
            limit=-limits.max_daily_loss_pct,
            code="MAX_DAILY_LOSS",
        )
        add(
            "max_drawdown",
            account.drawdown_pct < limits.max_drawdown_pct,
            f"drawdown {account.drawdown_pct:.4%} at or beyond the "
            f"{limits.max_drawdown_pct:.4%} limit",
            value=account.drawdown_pct,
            limit=limits.max_drawdown_pct,
            code="MAX_DRAWDOWN",
        )
        add(
            "equity_positive",
            account.equity > ZERO,
            f"equity {account.equity} is not positive",
            value=account.equity,
            code="NO_EQUITY",
        )

        # --- 7. microstructure ---------------------------------------------
        spread = conditions.spread_bps if conditions.spread_bps is not None else proposal.spread_bps
        if spread is None:
            add(
                "max_spread",
                not self.require_microstructure,
                "spread is unknown — refusing to trade blind",
                limit=limits.max_spread_bps,
                code="SPREAD_UNKNOWN",
            )
        else:
            add(
                "max_spread",
                spread <= limits.max_spread_bps,
                f"spread {spread:.2f} bps exceeds {limits.max_spread_bps} bps",
                value=spread,
                limit=limits.max_spread_bps,
                code="SPREAD_TOO_WIDE",
            )
        volume = (
            conditions.quote_volume_24h
            if conditions.quote_volume_24h is not None
            else proposal.quote_volume_24h
        )
        if volume is None:
            add(
                "min_liquidity",
                not self.require_microstructure,
                "24h quote volume unknown — refusing to trade blind",
                limit=limits.min_24h_quote_volume,
                code="LIQUIDITY_UNKNOWN",
            )
        else:
            add(
                "min_liquidity",
                volume >= limits.min_24h_quote_volume,
                f"24h quote volume {volume:.0f} below minimum "
                f"{limits.min_24h_quote_volume:.0f}",
                value=volume,
                limit=limits.min_24h_quote_volume,
                code="INSUFFICIENT_LIQUIDITY",
            )
        move = (
            conditions.last_bar_move_pct
            if conditions.last_bar_move_pct is not None
            else proposal.last_bar_move_pct
        )
        if move is not None:
            add(
                "price_move_normal",
                abs(move) <= limits.abnormal_price_move_pct,
                f"last bar moved {move:.2%}, beyond the "
                f"{limits.abnormal_price_move_pct:.2%} abnormal-move threshold",
                value=abs(move),
                limit=limits.abnormal_price_move_pct,
                code="ABNORMAL_PRICE_MOVE",
            )

        # --- 8. sizing ------------------------------------------------------
        exposure_value = (
            current_exposure_value
            if current_exposure_value is not None
            else account.exposure_pct * account.equity
        )
        sizing: PositionSizing = calculate_position_size(
            equity=account.equity,
            cash=account.cash,
            entry=proposal.entry,
            stop_loss=proposal.stop_loss,
            risk_per_trade=limits.risk_per_trade,
            max_position_pct_equity=limits.max_position_pct_equity,
            max_portfolio_exposure_pct=limits.max_portfolio_exposure_pct,
            current_exposure_value=exposure_value,
            spec=spec,
        )
        add(
            "position_sizing",
            sizing.is_viable,
            sizing.rejected_reason or (
                f"size {sizing.quantity} ({sizing.notional_pct_equity:.2%} of equity), "
                f"risking {sizing.effective_risk_pct:.4%}"
            ),
            value=sizing.quantity,
            code="SIZING_FAILED",
        )
        if sizing.is_viable:
            add(
                "risk_within_budget",
                sizing.effective_risk_pct <= limits.risk_per_trade * Decimal("1.0001"),
                f"effective risk {sizing.effective_risk_pct:.6%} exceeds budget "
                f"{limits.risk_per_trade:.6%}",
                value=sizing.effective_risk_pct,
                limit=limits.risk_per_trade,
                code="RISK_BUDGET_EXCEEDED",
            )
            required_cash = sizing.notional * (Decimal("1") + Decimal("0.002"))
            add(
                "sufficient_cash",
                account.cash >= required_cash or is_short,
                f"cash {account.cash:.2f} below required {required_cash:.2f}",
                value=account.cash,
                limit=required_cash,
                code="INSUFFICIENT_CASH",
            )
            if sizing.capped_by:
                warnings.append(
                    "size reduced by: " + ", ".join(sizing.capped_by)
                )

        approved = not rejections
        return RiskDecision(
            decision=RiskDecisionType.APPROVED if approved else RiskDecisionType.REJECTED,
            evaluated_at=now,
            symbol=proposal.symbol,
            direction=proposal.direction,
            entry=proposal.entry,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            sizing=sizing,
            checks=checks,
            rejection_codes=sorted(set(rejections)),
            reasons=reasons,
            warnings=warnings,
            account=account,
            mode=mode,
        )


def limits_from_settings(settings, *, allow_short: bool = False) -> RiskLimitsView:  # noqa: ANN001
    return RiskLimitsView(
        risk_per_trade=to_decimal(settings.risk_per_trade),
        max_position_pct_equity=to_decimal(settings.max_position_pct_equity),
        max_portfolio_exposure_pct=to_decimal(settings.max_portfolio_exposure_pct),
        max_open_positions=settings.max_open_positions,
        max_positions_per_symbol=settings.max_positions_per_symbol,
        max_daily_loss_pct=to_decimal(settings.max_daily_loss_pct),
        max_drawdown_pct=to_decimal(settings.max_drawdown_pct),
        consecutive_loss_limit=settings.consecutive_loss_limit,
        cooldown_minutes=settings.cooldown_minutes,
        min_confidence=to_decimal(settings.min_confidence),
        min_risk_reward=to_decimal(settings.min_risk_reward),
        max_spread_bps=to_decimal(settings.max_spread_bps),
        min_24h_quote_volume=to_decimal(settings.min_24h_quote_volume),
        require_stop_loss=settings.require_stop_loss,
        min_stop_distance_pct=to_decimal(settings.min_stop_distance_pct),
        max_stop_distance_pct=to_decimal(settings.max_stop_distance_pct),
        abnormal_price_move_pct=to_decimal(settings.abnormal_price_move_pct),
        allow_short=allow_short,
    )
