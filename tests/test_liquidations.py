"""The sign, the burst boundary, and our own latency.

docs/LIQUIDATION_PLAN.md was written before collection started. These tests
pin the three things that would silently invert or inflate the result, each a
shape this project has already been caught by.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "liquidations"))

from collect import (
    CASCADE_WINDOW_S,
    HORIZONS_S,
    MIN_CASCADE_USD,
    Burst,
    is_cascade,
    parse_force_order,
    trade_direction,
    update_burst,
)


def liq(side="SELL", notional=100_000.0, ts=1000.0, symbol="BTCUSDT"):
    return {"symbol": symbol, "side": side, "price": 50_000.0,
            "quantity": notional / 50_000.0, "notional_usd": notional,
            "event_ts": ts, "received_ts": ts + 0.3}


# ------------------------------------------------------------- the sign
def test_a_liquidated_long_is_forced_selling_so_the_trade_is_to_buy():
    """Binance reports the side of the LIQUIDATION order. Getting this
    backwards would invert every return in the study, and the sign would look
    like a finding."""
    assert trade_direction("SELL") == "BUY"


def test_a_liquidated_short_is_forced_buying_so_the_trade_is_to_sell():
    assert trade_direction("BUY") == "SELL"


def test_the_direction_is_case_insensitive():
    assert trade_direction("sell") == "BUY"


# -------------------------------------------------- what counts as a cascade
def test_liquidations_in_the_window_accumulate():
    burst = None
    for i in range(3):
        burst = update_burst(burst, liq(notional=100_000.0, ts=1000.0 + i),
                             CASCADE_WINDOW_S)
    assert burst.events == 3
    assert burst.total_usd == 300_000.0
    assert is_cascade(burst, MIN_CASCADE_USD)


def test_a_trickle_outside_the_window_restarts_rather_than_summing():
    """Without the restart, a slow drip over an hour eventually sums past the
    threshold and gets recorded as a cascade, filling the sample with
    non-events."""
    burst = update_burst(None, liq(notional=200_000.0, ts=1000.0),
                         CASCADE_WINDOW_S)
    burst = update_burst(burst, liq(notional=200_000.0, ts=5000.0),
                         CASCADE_WINDOW_S)
    assert burst.events == 1
    assert burst.total_usd == 200_000.0
    assert not is_cascade(burst, MIN_CASCADE_USD)


def test_a_direction_change_restarts_the_burst():
    """Forced selling and forced buying are opposite events. Summing them
    would manufacture a cascade out of two-sided noise."""
    burst = update_burst(None, liq(side="SELL", notional=200_000.0),
                         CASCADE_WINDOW_S)
    burst = update_burst(burst, liq(side="BUY", notional=200_000.0),
                         CASCADE_WINDOW_S)
    assert burst.side == "BUY"
    assert burst.total_usd == 200_000.0


def test_one_large_liquidation_is_a_cascade_on_its_own():
    burst = update_burst(None, liq(notional=400_000.0), CASCADE_WINDOW_S)
    assert is_cascade(burst, MIN_CASCADE_USD)


def test_below_the_threshold_is_not_a_cascade():
    burst = update_burst(None, liq(notional=10_000.0), CASCADE_WINDOW_S)
    assert not is_cascade(burst, MIN_CASCADE_USD)


# ------------------------------------------------ a malformed message is cheap
def test_a_real_force_order_message_parses():
    payload = {"data": {"o": {"s": "BTCUSDT", "S": "SELL", "ap": "61234.5",
                              "q": "0.85", "T": 1760000000000}}}
    row = parse_force_order(payload)
    assert row is not None
    assert row["symbol"] == "BTCUSDT"
    assert abs(row["notional_usd"] - 61234.5 * 0.85) < 1e-6
    assert row["event_ts"] == 1760000000.0


def test_our_receipt_time_is_recorded_separately_from_the_exchange_clock():
    """We learn of a liquidation after it happened. The memecoin result turned
    on exactly this, so entry prices must be checkable against when the event
    reached US."""
    payload = {"data": {"o": {"s": "BTCUSDT", "S": "SELL", "ap": "100",
                              "q": "1", "T": 1760000000000}}}
    row = parse_force_order(payload)
    assert row["received_ts"] >= time.time() - 5
    assert row["received_ts"] != row["event_ts"]


def test_a_malformed_message_costs_one_event_not_the_run():
    assert parse_force_order({"data": {"o": {}}}) is None
    assert parse_force_order({}) is None
    assert parse_force_order({"data": {"o": {"s": "X", "S": "SELL",
                                             "ap": "0", "q": "1",
                                             "T": 1}}}) is None


def test_a_zero_quantity_is_refused():
    payload = {"data": {"o": {"s": "BTCUSDT", "S": "SELL", "ap": "100",
                              "q": "0", "T": 1760000000000}}}
    assert parse_force_order(payload) is None


# ------------------------------------------------- the pre-registered numbers
def test_the_constants_are_the_pre_registered_ones():
    """Tuning these after seeing the data is the fitting the plan exists to
    prevent, so they are asserted rather than left to drift."""
    assert MIN_CASCADE_USD == 250_000.0
    assert CASCADE_WINDOW_S == 10.0


def test_every_horizon_is_collected_so_none_can_be_chosen_afterwards():
    assert HORIZONS_S == (30, 60, 300, 900)
