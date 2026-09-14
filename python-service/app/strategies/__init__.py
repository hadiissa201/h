from app.strategies.base import Strategy, StrategyContext, StrategyParams
from app.strategies.breakout import BreakoutParams, BreakoutStrategy
from app.strategies.ema_momentum import EmaMomentumParams, EmaMomentumStrategy
from app.strategies.engine import (
    STRATEGY_REGISTRY,
    EngineConfig,
    EvaluationOutput,
    StrategyEngine,
    build_strategies,
    strategy_catalogue,
)
from app.strategies.mean_reversion import MeanReversionParams, MeanReversionStrategy
from app.strategies.trend_following import TrendFollowingParams, TrendFollowingStrategy
from app.strategies.volatility_breakout import (
    VolatilityBreakoutParams,
    VolatilityBreakoutStrategy,
)

__all__ = [
    "STRATEGY_REGISTRY",
    "BreakoutParams",
    "BreakoutStrategy",
    "EmaMomentumParams",
    "EmaMomentumStrategy",
    "EngineConfig",
    "EvaluationOutput",
    "MeanReversionParams",
    "MeanReversionStrategy",
    "Strategy",
    "StrategyContext",
    "StrategyEngine",
    "StrategyParams",
    "TrendFollowingParams",
    "TrendFollowingStrategy",
    "VolatilityBreakoutParams",
    "VolatilityBreakoutStrategy",
    "build_strategies",
    "strategy_catalogue",
]
