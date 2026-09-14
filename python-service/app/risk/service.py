"""Risk service: the DB-backed wrapper around the deterministic engine.

Responsibilities:
  * resolve account state and market microstructure *itself* (never from the caller)
  * run the engine
  * persist every verdict, approved or not
  * issue and consume single-use approvals
  * run the safety monitor that trips the kill switch
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.errors import MarketDataError
from app.core.events import EventType, HaltReason
from app.core.logging import get_logger, log_event
from app.core.numeric import ZERO, to_decimal
from app.data.service import MarketDataService
from app.database.repositories import (
    BotStateRepository,
    EventRepository,
    ExecutionRepository,
    RiskRepository,
    new_id,
)
from app.models.enums import BotStatus, MarketRegime, TradingModeEnum
from app.models.risk import RiskDecision, RiskLimitsView, RiskProposal
from app.models.system import EmergencyEvent
from app.portfolio.service import PortfolioService
from app.risk.approval import proposal_fingerprint, verify_approval
from app.risk.engine import MarketConditions, RiskEngine, limits_from_settings
from app.utils.time import utcnow

logger = get_logger(__name__)


class RiskService:
    def __init__(
        self,
        risk_repo: RiskRepository,
        portfolio: PortfolioService,
        market_data: MarketDataService,
        bot_repo: BotStateRepository,
        event_repo: EventRepository,
        execution_repo: ExecutionRepository,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = risk_repo
        self.portfolio = portfolio
        self.market_data = market_data
        self.bot = bot_repo
        self.events = event_repo
        self.execution = execution_repo
        self.limits: RiskLimitsView = limits_from_settings(self.settings)
        self.engine = RiskEngine(self.limits)
        self.mode = self.settings.effective_mode

    # ------------------------------------------------------------------ check
    def check(self, proposal: RiskProposal, *, persist: bool = True) -> RiskDecision:
        account = self.portfolio.account_risk_state()
        conditions = self._market_conditions(proposal)
        spec = self.market_data.get_symbol_spec(proposal.symbol)
        exposure_value = self.portfolio.positions_value()

        decision = self.engine.evaluate(
            proposal,
            account,
            spec,
            conditions,
            current_exposure_value=exposure_value,
            mode=TradingModeEnum(self.mode),
        )

        if decision.approved and decision.sizing is not None:
            decision.approval_id = new_id("ra_")
            decision.expires_at = utcnow() + timedelta(
                seconds=self.settings.risk_approval_ttl_seconds
            )
            decision.proposal_fingerprint = proposal_fingerprint(
                symbol=decision.symbol,
                direction=str(decision.direction),
                entry=decision.entry,
                stop_loss=decision.stop_loss,
                quantity=decision.sizing.quantity,
                mode=self.mode,
            )

        if persist:
            self._persist(proposal, decision)

        log_event(
            logger,
            EventType.RISK_APPROVED if decision.approved else EventType.RISK_REJECTED,
            symbol=decision.symbol,
            strategy=proposal.strategy,
            decision=str(decision.decision),
            direction=str(decision.direction),
            confidence=proposal.confidence,
            price=float(decision.entry),
            stop_loss=float(decision.stop_loss),
            take_profit=float(decision.take_profit) if decision.take_profit else None,
            quantity=float(decision.quantity),
            risk=float(decision.sizing.effective_risk_pct) if decision.sizing else None,
            risk_amount=float(decision.sizing.effective_risk_amount)
            if decision.sizing
            else None,
            equity=float(account.equity),
            rejection_codes=decision.rejection_codes,
            reason="; ".join(decision.reasons) or None,
            approval_id=decision.approval_id,
            ai_decision_id=proposal.ai_decision_id,
            mode=self.mode,
        )
        self.events.record(
            EventType.RISK_APPROVED if decision.approved else EventType.RISK_REJECTED,
            message="; ".join(decision.reasons) or "approved",
            symbol=decision.symbol,
            strategy=proposal.strategy,
            level="INFO" if decision.approved else "WARNING",
            context={
                "rejection_codes": decision.rejection_codes,
                "quantity": float(decision.quantity),
                "approval_id": decision.approval_id,
                "confidence": proposal.confidence,
                "ai_decision_id": proposal.ai_decision_id,
            },
        )
        return decision

    def _market_conditions(self, proposal: RiskProposal) -> MarketConditions:
        spread = proposal.spread_bps
        volume = proposal.quote_volume_24h
        data_ok = proposal.data_quality_ok
        try:
            resolved = self.market_data.market_conditions(proposal.symbol)
            # Server-resolved values always win over anything supplied by a caller.
            if resolved.get("spread_bps") is not None:
                spread = resolved["spread_bps"]
            if resolved.get("quote_volume_24h") is not None:
                volume = resolved["quote_volume_24h"]
        except MarketDataError as exc:
            data_ok = False
            log_event(
                logger,
                EventType.MARKET_DATA_REJECTED,
                symbol=proposal.symbol,
                reason=f"risk check could not resolve market conditions: {exc.detail}",
                level=30,
            )
        return MarketConditions(
            spread_bps=spread,
            quote_volume_24h=volume,
            last_bar_move_pct=proposal.last_bar_move_pct,
            data_quality_ok=data_ok,
            regime=proposal.regime or MarketRegime.UNKNOWN,
            regime_abnormal=proposal.regime_abnormal,
        )

    def _persist(self, proposal: RiskProposal, decision: RiskDecision) -> None:
        sizing = decision.sizing
        self.repo.save_decision(
            {
                "id": new_id("rd_"),
                "created_at": decision.evaluated_at,
                "symbol": decision.symbol,
                "timeframe": proposal.timeframe,
                "direction": str(decision.direction),
                "decision": str(decision.decision),
                "strategy": proposal.strategy,
                "regime": str(proposal.regime),
                "entry": decision.entry,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "quantity": sizing.quantity if sizing else None,
                "notional": sizing.notional if sizing else None,
                "risk_amount": sizing.effective_risk_amount if sizing else None,
                "risk_pct": sizing.effective_risk_pct if sizing else None,
                "risk_reward": float(proposal.risk_reward)
                if proposal.risk_reward is not None
                else None,
                "equity": decision.account.equity if decision.account else None,
                "confidence": proposal.confidence,
                "checks": [check.model_dump(mode="json") for check in decision.checks],
                "rejection_codes": decision.rejection_codes,
                "reasons": decision.reasons,
                "warnings": decision.warnings,
                "account_state": decision.account.model_dump(mode="json")
                if decision.account
                else {},
                "sizing": sizing.model_dump(mode="json") if sizing else {},
                "mode": self.mode,
                "ai_decision_id": proposal.ai_decision_id,
                "candidate_id": proposal.candidate_id,
                "approval_id": decision.approval_id,
                "proposal_fingerprint": decision.proposal_fingerprint,
                "expires_at": decision.expires_at,
            }
        )

    # -------------------------------------------------------------- approvals
    def validate_approval(
        self, approval_id: str, *, symbol: str, side: str, quantity: Decimal
    ):
        record = self.repo.get_by_approval(approval_id, for_update=True)
        check = verify_approval(
            record, symbol=symbol, side=side, quantity=quantity, mode=self.mode
        )
        if not check.valid:
            log_event(
                logger,
                EventType.RISK_APPROVAL_INVALID,
                symbol=symbol,
                approval_id=approval_id,
                reason=check.reason,
                level=30,
            )
            self.events.record(
                EventType.RISK_APPROVAL_INVALID,
                message=check.reason,
                symbol=symbol,
                level="WARNING",
                context={"approval_id": approval_id},
            )
        return record, check

    def consume_approval(self, approval_id: str, order_id: str) -> None:
        self.repo.consume_approval(approval_id, order_id)
        log_event(
            logger,
            EventType.RISK_APPROVAL_CONSUMED,
            approval_id=approval_id,
            order_id=order_id,
        )

    # ----------------------------------------------------------- safety monitor
    def run_safety_checks(
        self, *, stale_symbols: list[str] | None = None, auto_halt: bool = True
    ) -> list[EmergencyEvent]:
        """Evaluate every automatic kill-switch trigger.

        Called by the position-monitoring and emergency workflows, and after each
        closed trade. Returns the triggers that fired; halts the bot when
        ``auto_halt`` is set.
        """
        triggered: list[EmergencyEvent] = []
        snapshot = self.portfolio.snapshot()
        state = self.bot.get()

        def fire(reason: HaltReason, detail: str) -> None:
            record = self.events.record_emergency(
                reason=str(reason),
                detail=detail,
                source="risk_service",
                equity=snapshot.equity,
                drawdown_pct=snapshot.drawdown_pct,
                daily_pnl_pct=snapshot.daily_pnl_pct,
            )
            triggered.append(
                EmergencyEvent(
                    event_id=record.id,
                    triggered_at=record.triggered_at,
                    reason=str(reason),
                    detail=detail,
                    source="risk_service",
                    equity=snapshot.equity,
                    drawdown_pct=snapshot.drawdown_pct,
                    daily_pnl_pct=snapshot.daily_pnl_pct,
                )
            )
            log_event(
                logger,
                EventType.KILL_SWITCH_TRIGGERED,
                reason=str(reason),
                detail=detail,
                equity=float(snapshot.equity),
                drawdown_pct=float(snapshot.drawdown_pct),
                daily_pnl_pct=float(snapshot.daily_pnl_pct),
                level=40,
            )
            if auto_halt:
                self.bot.halt(str(reason), detail, "risk_service")

        if snapshot.daily_pnl_pct <= -to_decimal(self.settings.max_daily_loss_pct):
            fire(
                HaltReason.MAX_DAILY_LOSS,
                f"daily P&L {snapshot.daily_pnl_pct:.4%} hit the "
                f"-{self.settings.max_daily_loss_pct:.4%} limit",
            )
        if snapshot.drawdown_pct >= to_decimal(self.settings.max_drawdown_pct):
            fire(
                HaltReason.MAX_DRAWDOWN,
                f"drawdown {snapshot.drawdown_pct:.4%} hit the "
                f"{self.settings.max_drawdown_pct:.4%} limit",
            )
        if stale_symbols:
            fire(
                HaltReason.MARKET_DATA_STALE,
                f"stale or invalid market data for: {', '.join(sorted(stale_symbols))}",
            )
        failures = self.execution.count_failed_orders_since(
            utcnow() - timedelta(minutes=30)
        )
        if failures >= 3:
            fire(
                HaltReason.REPEATED_ORDER_FAILURES,
                f"{failures} rejected orders in the last 30 minutes",
            )

        if triggered and state.status == BotStatus.RUNNING.value:
            log_event(
                logger,
                EventType.BOT_HALTED,
                reason=triggered[0].reason,
                detail=triggered[0].detail,
                level=40,
            )
        return triggered

    # -------------------------------------------------------------- utilities
    def limits_view(self) -> RiskLimitsView:
        return self.limits

    def exposure_headroom(self) -> Decimal:
        snapshot = self.portfolio.snapshot()
        budget = snapshot.equity * to_decimal(self.settings.max_portfolio_exposure_pct)
        return max(ZERO, budget - snapshot.positions_value)
