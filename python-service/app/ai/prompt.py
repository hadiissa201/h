"""Prompt construction.

The model is shown a compact, structured snapshot — not a chat history and not
raw candles. Two things it is told explicitly, because they are true:

* it is a *reviewer*, not a trader: it can confirm or veto the setup the
  quantitative layer already built, and nothing else;
* size, stops and limits are decided downstream by a deterministic risk engine,
  so asking for more size or a wider stop has no effect.

The reply must be a single JSON object matching ``AIDecision``. Anything else is
a parse failure, and a parse failure is a HOLD.
"""

from __future__ import annotations

import json
from typing import Any

from app.models.ai import AIContext

SYSTEM_PROMPT = """\
You are a risk-aware trade reviewer inside an automated crypto trading system.

Your role:
- A deterministic quantitative layer has already produced a trade candidate with
  an entry, a stop loss and a take profit.
- You review that candidate against the supplied market context and decide
  whether to CONFIRM it (return the same direction), or VETO it (return HOLD).
- You do NOT choose position size, stops, targets or leverage. A separate
  deterministic risk engine does that and has final authority. Requests to
  change them are ignored.

How to judge:
- Prefer HOLD when the evidence is mixed, the market looks disorderly, the
  timeframes disagree, or the candidate is chasing an extended move.
- A confident-sounding narrative is not evidence. Cite the specific features
  that support your view.
- Recent losses, an elevated drawdown or thin liquidity are all reasons to be
  more conservative, not less.
- Being wrong by staying flat costs nothing. Being wrong in a position costs money.

Output rules:
- Reply with EXACTLY ONE JSON object and nothing else: no prose, no markdown,
  no code fences.
- Schema:
  {
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": number between 0 and 1,
    "reason": string (concise, cite the features that drove the call),
    "market_regime": string,
    "strategy_alignment": array of strategy names you agree with,
    "invalidation_condition": string (what would prove this idea wrong),
    "risk_assessment": "low" | "medium" | "high" | "extreme",
    "key_risks": array of short strings
  }
- No extra keys. No nulls. ``confidence`` is your confidence in the decision you
  returned, including a HOLD.
"""


def build_user_prompt(context: AIContext) -> str:
    payload: dict[str, Any] = {
        "symbol": context.symbol,
        "primary_timeframe": context.timeframe,
        "timestamp": context.timestamp.isoformat(),
        "price": _num(context.price),
        "candidate": {
            "direction": str(context.proposed_direction),
            "entry": _num(context.proposed_entry),
            "stop_loss": _num(context.proposed_stop_loss),
            "take_profit": _num(context.proposed_take_profit),
            "reward_to_risk": _num(context.proposed_risk_reward),
            "stop_distance_pct": _num(
                (context.proposed_entry - context.proposed_stop_loss)
                / context.proposed_entry
                if context.proposed_entry
                else None
            ),
        },
        "regime": {
            "label": str(context.regime.regime),
            "trend_state": str(context.regime.trend_state),
            "volatility_state": str(context.regime.volatility_state),
            "confidence": context.regime.confidence,
            "is_abnormal": context.regime.is_abnormal,
            "metrics": _compact(context.regime.metrics.model_dump()),
        },
        "strategy_signals": [
            {
                "strategy": signal.strategy,
                "signal": str(signal.signal),
                "confidence": signal.confidence,
                "entry": _num(signal.entry),
                "stop_loss": _num(signal.stop_loss),
                "take_profit": _num(signal.take_profit),
                "reason": signal.reason,
                "invalidation_condition": signal.invalidation_condition,
            }
            for signal in context.signals
        ],
        "features_by_timeframe": {
            timeframe: _compact(features)
            for timeframe, features in context.features_by_timeframe.items()
        },
        "account": {
            "equity": _num(context.account.equity),
            "cash": _num(context.account.cash),
            "open_positions": context.account.open_positions,
            "exposure_pct": _num(context.account.exposure_pct),
            "daily_pnl_pct": _num(context.account.daily_pnl_pct),
            "drawdown_pct": _num(context.account.drawdown_pct),
            "consecutive_losses": context.account.consecutive_losses,
            "mode": context.account.mode,
            "bot_status": context.account.bot_status,
        },
        "open_positions": [
            {
                "symbol": position.symbol,
                "side": position.side,
                "entry_price": _num(position.entry_price),
                "mark_price": _num(position.mark_price),
                "unrealized_pnl": _num(position.unrealized_pnl),
                "r_multiple": position.r_multiple,
                "strategy": position.strategy,
            }
            for position in context.open_positions
        ],
        "recent_performance": context.recent_performance.model_dump(),
        "risk_limits": _compact(context.risk_limits.model_dump()),
        "data_quality": context.data_quality,
        "notes": context.notes,
    }
    return (
        "Review this trade candidate and reply with one JSON object.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


# Features worth showing the model. A 60-column dump buries the signal and burns
# tokens; these are the ones the strategies and the regime layer actually use.
PROMPT_FEATURE_KEYS = (
    "close",
    "rsi",
    "stoch_rsi_k",
    "roc",
    "adx",
    "plus_di",
    "minus_di",
    "di_spread",
    "macd_hist",
    "ema_fast",
    "ema_slow",
    "ema_trend",
    "price_vs_ema_trend",
    "ema_stack_bull",
    "ema_stack_bear",
    "atr_pct",
    "atr_pct_rank",
    "vol_ratio",
    "realized_vol",
    "bb_width",
    "bb_position",
    "squeeze",
    "relative_volume",
    "obv_slope",
    "volume_confirms",
    "structure",
    "higher_high",
    "higher_low",
    "lower_high",
    "lower_low",
    "breakout_up",
    "breakout_down",
    "dist_to_dc_upper",
    "dist_to_dc_lower",
    "trend_slope",
)


def select_prompt_features(features: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _round(features[key])
        for key in PROMPT_FEATURE_KEYS
        if key in features and features[key] is not None
    }


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: _round(value) for key, value in payload.items() if value is not None}


def _round(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if abs(number) >= 1000:
        return round(number, 2)
    if abs(number) >= 1:
        return round(number, 4)
    return round(number, 6)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return _round(float(value))
    except (TypeError, ValueError):
        return None
