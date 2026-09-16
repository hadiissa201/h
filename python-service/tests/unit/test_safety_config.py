"""Configuration safety.

The single most important property of this system: it cannot move real money
unless three independent switches agree *and* credentials exist. These tests
enumerate the near-misses.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import LIVE_CONFIRMATION_PHRASE, Settings

PHRASE = LIVE_CONFIRMATION_PHRASE
ROOT = Path(__file__).resolve().parents[3]  # repo root, above python-service/


def make(**overrides) -> Settings:
    payload = {
        "trading_mode": "paper",
        "enable_live_trading": False,
        "live_trading_confirmation": "",
        "exchange_api_key": "",
        "exchange_api_secret": "",
    }
    payload.update(overrides)
    return Settings(**payload)


def test_defaults_are_paper_mode():
    settings = make()
    assert settings.trading_mode == "paper"
    assert settings.enable_live_trading is False
    assert settings.effective_mode == "paper"
    assert not settings.live_trading_armed


def test_all_three_switches_plus_credentials_are_required():
    armed = make(
        trading_mode="live",
        enable_live_trading=True,
        live_trading_confirmation=PHRASE,
        exchange_api_key="key",
        exchange_api_secret="secret",
    )
    assert armed.live_trading_armed
    assert armed.effective_mode == "live"


@pytest.mark.parametrize(
    "missing",
    [
        {"trading_mode": "paper"},
        {"enable_live_trading": False},
        {"live_trading_confirmation": ""},
        {"live_trading_confirmation": "yes please"},
        {"live_trading_confirmation": PHRASE.lower()},
    ],
)
def test_any_missing_switch_keeps_the_system_in_paper(missing):
    payload = {
        "trading_mode": "live",
        "enable_live_trading": True,
        "live_trading_confirmation": PHRASE,
        "exchange_api_key": "key",
        "exchange_api_secret": "secret",
    }
    payload.update(missing)
    settings = Settings(**payload)
    assert not settings.live_trading_armed
    assert settings.effective_mode == "paper"


def test_adding_exchange_keys_alone_does_not_enable_live_trading():
    """The scenario the docs promise is safe: someone pastes in real keys."""
    settings = make(exchange_api_key="real-key", exchange_api_secret="real-secret")
    assert settings.effective_mode == "paper"
    assert not settings.live_trading_armed


def test_trading_mode_live_alone_does_not_arm():
    settings = make(trading_mode="live")
    assert settings.effective_mode == "paper"
    assert "ENABLE_LIVE_TRADING is not true" in settings.live_mode_blockers()


def test_blockers_explain_every_missing_piece():
    blockers = make().live_mode_blockers()
    assert len(blockers) == 4
    assert any("TRADING_MODE" in item for item in blockers)
    assert any("ENABLE_LIVE_TRADING" in item for item in blockers)
    assert any("LIVE_TRADING_CONFIRMATION" in item for item in blockers)
    assert any("credentials" in item for item in blockers)


def test_armed_configuration_reports_no_blockers():
    settings = make(
        trading_mode="live",
        enable_live_trading=True,
        live_trading_confirmation=PHRASE,
        exchange_api_key="k",
        exchange_api_secret="s",
    )
    assert settings.live_mode_blockers() == []


# ------------------------------------------------------------------- limits
def test_reckless_risk_per_trade_is_refused_at_startup():
    with pytest.raises(ValueError, match="risk_per_trade"):
        make(risk_per_trade=Decimal("0.25"))


def test_zero_risk_per_trade_is_refused():
    with pytest.raises(ValueError):
        make(risk_per_trade=Decimal("0"))


def test_leverage_cannot_be_enabled():
    with pytest.raises(ValueError, match="spot only"):
        make(max_leverage=Decimal("3"))


def test_primary_timeframe_must_be_one_we_collect():
    with pytest.raises(ValueError, match="primary_timeframe"):
        make(timeframes=["5m", "15m"], primary_timeframe="1h")


def test_csv_lists_are_parsed_from_environment_strings():
    settings = make(
        trading_symbols="BTC/USDT, ETH/USDT",
        timeframes="5m,1h",
        primary_timeframe="1h",
        enabled_strategies="breakout, trend_following",
    )
    assert settings.trading_symbols == ["BTC/USDT", "ETH/USDT"]
    assert settings.timeframes == ["5m", "1h"]
    assert settings.enabled_strategies == ["breakout", "trend_following"]


def test_shipped_defaults_are_conservative():
    """Asserted against the field defaults, not a constructed instance.

    The environment can (and in tests does) override these, so reading them from
    the model is the only way to check what someone gets with an empty ``.env``.
    """
    defaults = {name: field.default for name, field in Settings.model_fields.items()}

    assert defaults["trading_mode"] == "paper"
    assert defaults["enable_live_trading"] is False
    assert defaults["live_trading_confirmation"] == ""
    assert defaults["exchange_api_key"] == ""

    assert defaults["risk_per_trade"] <= Decimal("0.01")
    assert defaults["max_daily_loss_pct"] <= Decimal("0.05")
    assert defaults["max_drawdown_pct"] <= Decimal("0.20")
    assert defaults["require_stop_loss"] is True
    assert defaults["max_open_positions"] <= 5
    assert defaults["max_portfolio_exposure_pct"] <= Decimal("1")
    assert defaults["max_leverage"] == Decimal("1")
    assert defaults["min_risk_reward"] >= Decimal("1")

    # The AI is a veto, and its absence must not silently open the gate.
    assert defaults["ai_required_for_entry"] is True
    assert defaults["ai_veto_only"] is True

    # Costs are never modelled as free.
    assert defaults["paper_taker_fee_bps"] > 0
    assert defaults["paper_slippage_bps"] > 0
    assert defaults["paper_spread_bps"] > 0


def test_exchange_factory_returns_paper_unless_fully_armed(session, market_data):
    from app.database.repositories import ExecutionRepository
    from app.execution.factory import build_exchange
    from app.execution.paper_exchange import PaperExchangeAdapter

    repo = ExecutionRepository(session, mode="paper")
    half_armed = make(trading_mode="live", enable_live_trading=True)
    adapter = build_exchange(repo, market_data, half_armed)
    assert isinstance(adapter, PaperExchangeAdapter)
    assert adapter.mode.value == "paper"


# --------------------------------------------------------------------- env parsing
# .env.example documents list settings as comma-separated strings. Settings() built
# from kwargs (as every other test here does) never exercises the environment source,
# so these tests load them the way docker-compose and a real .env actually do.


def test_csv_list_settings_load_from_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # do not pick up a developer's real .env
    monkeypatch.setenv("TRADING_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT")
    monkeypatch.setenv("TIMEFRAMES", "5m,15m,1h,4h")
    monkeypatch.setenv("ENABLED_STRATEGIES", "trend_following,breakout")

    settings = Settings()

    assert settings.trading_symbols == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert settings.timeframes == ["5m", "15m", "1h", "4h"]
    assert settings.enabled_strategies == ["trend_following", "breakout"]


def test_csv_list_settings_tolerate_spacing_and_blanks(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADING_SYMBOLS", " BTC/USDT , ETH/USDT ,, ")

    assert Settings().trading_symbols == ["BTC/USDT", "ETH/USDT"]


def test_env_file_from_env_example_template_loads(monkeypatch, tmp_path):
    """The shipped template must actually boot the service.

    Regression: list fields were plain ``list[str]``, so pydantic-settings tried to
    JSON-decode ``TRADING_SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT`` in the settings source
    and raised before any validator ran. The service died on startup with the
    documented configuration.
    """
    template = ROOT / ".env.example"
    assert template.exists(), "the configuration template must ship with the repo"

    env_file = tmp_path / ".env"
    env_file.write_text(template.read_text(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for key in ("TRADING_SYMBOLS", "TIMEFRAMES", "ENABLED_STRATEGIES"):
        monkeypatch.delenv(key, raising=False)

    settings = Settings()

    assert settings.trading_symbols == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert settings.timeframes == ["5m", "15m", "1h", "4h"]
    assert settings.primary_timeframe in settings.timeframes
    # ...and the template must still be disarmed.
    assert settings.effective_mode == "paper"
    assert not settings.live_trading_armed


def test_env_file_is_found_regardless_of_working_directory(monkeypatch, tmp_path):
    """Settings must not depend on where you happened to `cd` before starting.

    Regression: ``env_file=".env"`` resolved against the working directory, so the
    repo-root .env the README tells you to create was silently ignored when the
    service was started from python-service/ -- and it starts from there. The
    service came up on defaults with nothing reporting that it had.
    """
    from app.core.config import ENV_FILES

    repo_root_env, service_root_env = ENV_FILES
    assert repo_root_env.is_absolute() and service_root_env.is_absolute()
    assert repo_root_env == ROOT / ".env"
    assert service_root_env == ROOT / "python-service" / ".env"
    # The service-local file is read last so it can override the repo-root one.
    assert ENV_FILES.index(repo_root_env) < ENV_FILES.index(service_root_env)

    # Changing directory must not change which files are consulted.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SERVICE_API_KEY=should-be-ignored\n", encoding="utf-8")
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)

    assert Settings().service_api_key != "should-be-ignored"
