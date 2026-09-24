"""Collector configuration.

Every number here that constrains the design was MEASURED by `python -m probe`
on 2026-09-24, not assumed. The measurements are recorded next to the setting
they justify, so a future change has to argue with evidence rather than taste.

No secret has a default. A missing key fails loudly at startup instead of
silently falling back to a worse endpoint -- the public RPC was measured
dropping ~30% of launch messages while reporting no error at all.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SERVICE_ROOT.parent
ENV_FILES = (_REPO_ROOT / ".env", _SERVICE_ROOT / ".env")

class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore",
        env_prefix="MEMECOIN_",
    )

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg://localhost/memecoin_research"

    # ----------------------------------------------------------- data source
    # Helius, not the public endpoint. Measured over comparable 300s windows:
    # public RPC ~29.6 creations/min, Helius ~41.2/min. The public node drops
    # messages under load and reports nothing -- it looks like a quiet market.
    helius_api_key: str = ""
    helius_rpc_template: str = "https://mainnet.helius-rpc.com/?api-key={key}"
    helius_ws_template: str = "wss://mainnet.helius-rpc.com/?api-key={key}"
    fallback_rpc: str = "https://api.mainnet-beta.solana.com"

    dexscreener_base: str = "https://api.dexscreener.com"
    jupiter_quote_url: str = "https://lite-api.jup.ag/swap/v1/quote"
    jupiter_swap_url: str = "https://lite-api.jup.ag/swap/v1/swap"

    # ---------------------------------------------------------- rate limits
    # MEASURED CEILINGS. DexScreener held 297/297 over 60s at 4.9 req/s with no
    # 429 (~300/min). Jupiter returned 429 after 121 requests in 24.9s, so its
    # real budget is ~120/min (~2 req/s) -- the burst test suggested 6.9 req/s
    # and was wrong. Defaults sit deliberately BELOW both: the collector runs
    # for weeks, and a throttled collector loses the launches it was throttled
    # during, which is data we can never get back.
    dexscreener_rps: float = Field(default=4.0, gt=0)
    jupiter_rps: float = Field(default=1.5, gt=0)
    helius_rps: float = Field(default=8.0, gt=0)

    # ------------------------------------------------------------- sampling
    # Not a preference -- an arithmetic consequence. At the cadence below each
    # token costs ~217 DexScreener calls over 7 days; 4 req/s is 345,600/day,
    # so ~1,500 tokens/day is the ceiling. Full collection of ~30k launches/day
    # would need ~20x the budget. Sampling is deterministic on the mint address
    # so it is reproducible, unbiased, and recorded per token: Phase 2 can
    # weight correctly, which "keep whatever we could keep up with" never allows.
    sample_rate: float = Field(default=0.05, gt=0, le=1.0)
    max_tracked_tokens: int = Field(default=40_000, gt=0)

    # -------------------------------------------------------------- cadence
    # Seconds between observations, by token age. Dense early because the
    # question -- did this reach +200%, and could you have sold -- is decided
    # in the first minutes.
    obs_interval_0_5m: float = 10.0
    obs_interval_5_60m: float = 60.0
    obs_interval_1_6h: float = 300.0
    obs_interval_6_24h: float = 1800.0
    obs_interval_1_7d: float = 14400.0

    exit_interval_0_5m: float = 60.0
    exit_interval_5_60m: float = 300.0
    exit_interval_1_6h: float = 1800.0
    exit_interval_6_24h: float = 7200.0
    exit_interval_1_7d: float = 43200.0

    holder_interval_0_60m: float = 600.0
    holder_interval_after: float = 21600.0

    retire_after_days: float = 7.0

    # ------------------------------------------------------ exit simulation
    # One notional every cycle; the full depth curve only at milestones.
    # Three sizes per cycle triples the cost against the TIGHTEST budget we
    # have, for information that barely moves minute to minute.
    exit_notional_usd: float = 100.0
    # Kept as a string and parsed in a property: pydantic-settings JSON-decodes
    # list fields at the source layer, so "100,500,1000" from a .env file fails
    # before any validator runs. The same trap cost us a boot failure on the
    # trading service's TRADING_SYMBOLS.
    exit_depth_curve_usd_csv: str = "100,500,1000"
    exit_slippage_bps: int = 300
    # RPC simulation is the strong test -- it catches frozen accounts and
    # transfer hooks a routing quote cannot -- but costs two calls. Milestones
    # only.
    rpc_simulation_at_milestones_only: bool = True

    # ------------------------------------------------------------- liveness
    # A token is dormant, never deleted. Dead tokens are the rows nobody else
    # has and the entire reason this dataset is worth building.
    dormant_liquidity_usd: float = 100.0
    dormant_after_consecutive: int = 5

    # ----------------------------------------------------------- operations
    http_timeout_s: float = 20.0
    reconnect_base_delay_s: float = 1.0
    reconnect_max_delay_s: float = 60.0
    status_port: int = 8787
    raw_payload_retention_days: int = 30
    store_raw_payloads: bool = True

    @property
    def exit_depth_curve_usd(self) -> list[float]:
        return [float(x) for x in self.exit_depth_curve_usd_csv.split(",") if x.strip()]

    @property
    def rpc_url(self) -> str:
        if not self.helius_api_key:
            return self.fallback_rpc
        return self.helius_rpc_template.format(key=self.helius_api_key)

    @property
    def ws_url(self) -> str:
        if not self.helius_api_key:
            return self.fallback_rpc.replace("https://", "wss://")
        return self.helius_ws_template.format(key=self.helius_api_key)

    @property
    def using_fallback(self) -> bool:
        return not self.helius_api_key


def load_settings() -> CollectorSettings:
    return CollectorSettings()
