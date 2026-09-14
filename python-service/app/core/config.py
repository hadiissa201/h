"""Central configuration.

Every tunable lives here and is read from the environment. Two rules that the
rest of the codebase depends on:

1. The defaults are always the *safe* ones. A service started with an empty
   environment runs in paper mode with conservative risk limits.
2. Live trading requires three independent switches to agree
   (``TRADING_MODE``, ``ENABLE_LIVE_TRADING``, ``LIVE_TRADING_CONFIRMATION``).
   A single mis-set variable can therefore never move real money.
"""

from __future__ import annotations

import functools
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Lists come from the environment as plain comma-separated strings
# (``TRADING_SYMBOLS=BTC/USDT,ETH/USDT``), which is what .env.example documents and
# what docker-compose passes. Without ``NoDecode`` pydantic-settings tries to JSON
# parse them at the *source* layer, before any validator runs, and the service dies
# on boot with "error parsing value for field". ``NoDecode`` hands the raw string to
# ``_split_csv`` below instead.
CsvList = Annotated[list[str], NoDecode]

TradingMode = Literal["paper", "live"]
LLMProvider = Literal["ollama", "openai", "anthropic", "disabled"]
MarketDataProvider = Literal["ccxt", "synthetic"]

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_REAL_MONEY_IS_AT_RISK"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- service
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(default="json")
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    service_api_key: str = Field(
        default="",
        description="Shared secret n8n sends as X-API-Key. Empty disables auth "
        "(only tolerated outside production).",
    )

    # ------------------------------------------------------------- safety
    trading_mode: TradingMode = Field(default="paper")
    enable_live_trading: bool = Field(default=False)
    live_trading_confirmation: str = Field(default="")

    # ------------------------------------------------------------ database
    database_url: str = Field(
        default="postgresql+psycopg://trader:trader@postgres:5432/trading"
    )
    db_echo: bool = Field(default=False)
    redis_url: str = Field(default="")

    # ------------------------------------------------------------- market
    trading_symbols: CsvList = Field(default=["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    timeframes: CsvList = Field(default=["5m", "15m", "1h", "4h"])
    primary_timeframe: str = Field(default="1h")
    quote_currency: str = Field(default="USDT")

    exchange_id: str = Field(default="binance")
    exchange_api_key: str = Field(default="")
    exchange_api_secret: str = Field(default="")
    exchange_password: str = Field(default="")
    market_data_provider: MarketDataProvider = Field(default="ccxt")
    market_data_timeout_seconds: float = Field(default=15.0)

    # Data-quality gates. Failing any of these means NO TRADE.
    max_data_staleness_seconds: int = Field(default=300)
    min_candles_for_analysis: int = Field(default=120)
    max_candle_gap_ratio: float = Field(default=0.02)

    # ------------------------------------------------------- paper trading
    paper_starting_balance: Decimal = Field(default=Decimal("10000"))
    paper_taker_fee_bps: Decimal = Field(default=Decimal("10"))
    paper_maker_fee_bps: Decimal = Field(default=Decimal("8"))
    paper_slippage_bps: Decimal = Field(default=Decimal("5"))
    paper_spread_bps: Decimal = Field(default=Decimal("4"))
    paper_partial_fill_probability: float = Field(default=0.0)

    # --------------------------------------------------------------- risk
    risk_per_trade: Decimal = Field(default=Decimal("0.005"))
    max_position_pct_equity: Decimal = Field(default=Decimal("0.20"))
    max_portfolio_exposure_pct: Decimal = Field(default=Decimal("0.50"))
    max_open_positions: int = Field(default=3)
    max_positions_per_symbol: int = Field(default=1)
    max_daily_loss_pct: Decimal = Field(default=Decimal("0.02"))
    max_drawdown_pct: Decimal = Field(default=Decimal("0.10"))
    consecutive_loss_limit: int = Field(default=3)
    cooldown_minutes: int = Field(default=120)
    min_confidence: Decimal = Field(default=Decimal("0.60"))
    min_risk_reward: Decimal = Field(default=Decimal("1.5"))
    max_spread_bps: Decimal = Field(default=Decimal("15"))
    min_24h_quote_volume: Decimal = Field(default=Decimal("5000000"))
    require_stop_loss: bool = Field(default=True)
    min_stop_distance_pct: Decimal = Field(default=Decimal("0.003"))
    max_stop_distance_pct: Decimal = Field(default=Decimal("0.15"))
    max_leverage: Decimal = Field(default=Decimal("1"))
    abnormal_price_move_pct: Decimal = Field(default=Decimal("0.10"))
    risk_approval_ttl_seconds: int = Field(default=120)

    # ---------------------------------------------------------------- LLM
    ai_enabled: bool = Field(default=True)
    llm_provider: LLMProvider = Field(default="ollama")
    llm_model: str = Field(default="qwen2.5-coder:7b")
    llm_base_url: str = Field(
        default="",
        description="Override the provider's default endpoint. Left empty each "
        "provider uses its own default, so switching provider cannot accidentally "
        "point one vendor's request at another's URL.",
    )
    llm_temperature: float = Field(default=0.1)
    llm_timeout_seconds: float = Field(default=60.0)
    llm_max_output_tokens: int = Field(default=1024)
    llm_max_calls_per_hour: int = Field(default=60)
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")

    # AI can only ever *veto or confirm* — never widen risk. See risk engine.
    ai_veto_only: bool = Field(default=True)
    ai_required_for_entry: bool = Field(
        default=True,
        description="When the AI layer is unavailable (disabled, rate limited, "
        "erroring), refuse the entry instead of trading on the deterministic "
        "signal alone. Conservative by default.",
    )
    ai_min_minutes_between_calls_per_symbol: int = Field(default=15, ge=0)

    # ---------------------------------------------------------- strategies
    enabled_strategies: CsvList = Field(
        default=[
            "trend_following",
            "ema_momentum",
            "breakout",
            "mean_reversion",
            "volatility_breakout",
        ]
    )

    @field_validator(
        "trading_symbols", "timeframes", "enabled_strategies", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.primary_timeframe not in self.timeframes:
            raise ValueError(
                f"primary_timeframe {self.primary_timeframe!r} must be one of "
                f"timeframes {self.timeframes}"
            )
        if self.risk_per_trade <= 0 or self.risk_per_trade > Decimal("0.05"):
            raise ValueError("risk_per_trade must be in (0, 0.05] — 5% per trade is already reckless")
        if self.max_leverage != Decimal("1"):
            raise ValueError("leverage is not supported: spot only, max_leverage must be 1")
        return self

    # ------------------------------------------------------------ helpers
    @property
    def live_trading_armed(self) -> bool:
        """True only when all three independent switches agree.

        This is the single source of truth for "may we touch real money".
        """
        return (
            self.trading_mode == "live"
            and self.enable_live_trading is True
            and self.live_trading_confirmation == LIVE_CONFIRMATION_PHRASE
        )

    @property
    def effective_mode(self) -> TradingMode:
        """The mode actually used for execution.

        Deliberately fails *closed*: a half-configured live setup stays paper.
        """
        return "live" if self.live_trading_armed else "paper"

    def live_mode_blockers(self) -> list[str]:
        """Human-readable reasons live trading is not armed."""
        blockers: list[str] = []
        if self.trading_mode != "live":
            blockers.append("TRADING_MODE is not 'live'")
        if not self.enable_live_trading:
            blockers.append("ENABLE_LIVE_TRADING is not true")
        if self.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
            blockers.append(
                "LIVE_TRADING_CONFIRMATION does not match the required phrase"
            )
        if not (self.exchange_api_key and self.exchange_api_secret):
            blockers.append("exchange API credentials are not configured")
        return blockers


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper — drops the cached Settings instance."""
    get_settings.cache_clear()
