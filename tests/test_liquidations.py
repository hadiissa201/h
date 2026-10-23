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


# ------------------------- the venue, because futures access is region-gated
def test_bybit_side_is_normalised_to_binance_meaning():
    """Bybit reports the side of the liquidated POSITION; Binance reports the
    side of the closing ORDER. A liquidated long is Bybit 'Buy' and Binance
    'SELL'. Not normalising would invert every return from one venue and the
    sign would look like a finding."""
    from collect import parse_bybit_liquidation
    rows = parse_bybit_liquidation({
        "topic": "allLiquidation.BTCUSDT",
        "ts": 1760000000000,
        "data": [{"s": "BTCUSDT", "S": "Buy", "p": "60000", "v": "2",
                  "T": 1760000000000}]})
    assert len(rows) == 1
    assert rows[0]["side"] == "SELL"
    assert trade_direction(rows[0]["side"]) == "BUY"


def test_a_bybit_short_liquidation_is_forced_buying():
    from collect import parse_bybit_liquidation
    rows = parse_bybit_liquidation({
        "topic": "liquidation.BTCUSDT", "ts": 1760000000000,
        "data": {"s": "BTCUSDT", "S": "Sell", "p": "60000", "v": "1",
                 "T": 1760000000000}})
    assert rows[0]["side"] == "BUY"
    assert trade_direction(rows[0]["side"]) == "SELL"


def test_one_bybit_message_can_carry_several_liquidations():
    from collect import parse_bybit_liquidation
    rows = parse_bybit_liquidation({
        "topic": "allLiquidation.BTCUSDT", "ts": 1760000000000,
        "data": [{"s": "BTCUSDT", "S": "Buy", "p": "60000", "v": "1",
                  "T": 1760000000000},
                 {"s": "BTCUSDT", "S": "Buy", "p": "59900", "v": "3",
                  "T": 1760000000001}]})
    assert len(rows) == 2
    assert sum(r["notional_usd"] for r in rows) == 60000 + 59900 * 3


def test_a_non_liquidation_topic_yields_nothing():
    from collect import parse_bybit_liquidation
    assert parse_bybit_liquidation({"topic": "tickers.BTCUSDT",
                                    "data": [{"s": "BTCUSDT"}]}) == []


def test_both_venues_go_through_one_extractor():
    """So the burst logic never has to know which venue it is reading."""
    from collect import extract_liquidations
    binance = {"data": {"o": {"s": "BTCUSDT", "S": "SELL", "ap": "100",
                              "q": "1", "T": 1760000000000}}}
    bybit = {"topic": "allLiquidation.BTCUSDT", "ts": 1760000000000,
             "data": [{"s": "BTCUSDT", "S": "Buy", "p": "100", "v": "1",
                       "T": 1760000000000}]}
    assert extract_liquidations("binance", binance)[0]["side"] == "SELL"
    assert extract_liquidations("bybit", bybit)[0]["side"] == "SELL"


def test_bybit_is_the_default_venue():
    """Binance futures is region-blocked on the machine this runs on, which an
    earlier run showed by reaching api.binance.com while fapi failed."""
    import inspect

    import collect
    source = inspect.getsource(collect.main)
    assert 'default="bybit"' in source


# ------------------- a message count that cannot tell data from protocol
def test_a_subscription_acknowledgement_is_not_data():
    """Eight hours produced seven messages and zero liquidations. Every one
    was an ack. A total count could not tell that from a calm market."""
    from collect import classify_message
    assert classify_message("bybit", {"op": "subscribe", "success": True}) == "ack"
    assert classify_message("bybit", {"topic": "allLiquidation.BTCUSDT",
                                      "data": []}) == "data"


def test_a_refused_subscription_is_detected_and_quoted():
    """The collector printed "subscribed" after SENDING the request, never
    reading the reply, so a refusal looked like an empty market."""
    from collect import subscription_problem
    assert subscription_problem("bybit", {"op": "subscribe", "success": False,
                                          "ret_msg": "Invalid topic"}) == "Invalid topic"


def test_a_successful_subscription_reports_no_problem():
    from collect import subscription_problem
    assert subscription_problem("bybit", {"op": "subscribe",
                                          "success": True}) is None


def test_a_refusal_with_no_reason_still_reports_a_problem():
    from collect import subscription_problem
    assert subscription_problem("bybit", {"op": "subscribe",
                                          "success": False}) is not None


def test_only_one_topic_is_subscribed_so_an_invalid_one_cannot_poison_it():
    """Subscribing to both topic names was meant to hedge the rename. Bybit
    can reject a whole batch containing any invalid topic, so the hedge may
    have cost the valid subscription."""
    from collect import VENUES
    assert VENUES["bybit"]["subscribe"] == ["allLiquidation"]


def test_a_pong_is_neither_data_nor_an_ack():
    from collect import classify_message
    assert classify_message("bybit", {"op": "pong"}) == "pong"


# ------------------------- counting events, deliberately without prices
def test_one_burst_counts_once_not_once_per_event():
    """A sliding count would report a single spike fifty times over and fill
    the sample with duplicates of one event."""
    from rate import rolling_cascades
    burst = [(float(i), 100_000.0) for i in range(4)]
    assert rolling_cascades(burst, 250_000.0, 10.0) == 1


def test_a_trickle_outside_the_window_is_not_a_cascade():
    from rate import rolling_cascades
    trickle = [(float(i * 60), 100_000.0) for i in range(10)]
    assert rolling_cascades(trickle, 250_000.0, 10.0) == 0


def test_two_separate_bursts_count_twice():
    from rate import rolling_cascades
    events = ([(float(i), 100_000.0) for i in range(3)]
              + [(100.0 + i, 100_000.0) for i in range(3)])
    assert rolling_cascades(events, 250_000.0, 10.0) == 2


def test_a_lower_threshold_never_finds_fewer_cascades():
    """Monotonic by construction. If it were not, the frequency table would be
    nonsense and a threshold could be picked on an artefact."""
    from rate import rolling_cascades
    events = [(float(i), 30_000.0) for i in range(20)]
    counts = [rolling_cascades(events, t, 10.0)
              for t in (25_000.0, 50_000.0, 100_000.0, 250_000.0)]
    assert counts == sorted(counts, reverse=True)


def test_the_rate_script_cannot_compute_a_return():
    """The separation between choosing a threshold on FREQUENCY and choosing
    it on returns is enforced by the script lacking the capability, not by
    remembering not to. It must never read a price.

    The first version of this test matched bare substrings and failed on the
    word "asks" in a comment. A guard that trips on English prose is a bad
    test even when its intent is right, so it now names the actual
    capabilities: the quotes table and the venues' price columns.
    """
    import inspect

    import rate
    source = inspect.getsource(rate)
    assert "FROM quotes" not in source
    assert "bidPrice" not in source and "askPrice" not in source
    assert "bid1Price" not in source and "ask1Price" not in source
    # The only tables it may read.
    assert source.count("FROM ") == source.count("FROM liquidations") \
        + source.count("FROM cascades") + source.count("FROM sessions")


# ----------------------- the denominator is connected time, not elapsed time
def test_a_long_outage_is_excluded_from_the_rate():
    """The first 21-hour run contained a 17-hour hole where the websocket
    could not reconnect. 176 liquidations over 22 elapsed hours reads as
    8/hour; the truth while connected was 35/hour."""
    from rate import find_gaps
    ts = [0.0, 60.0, 120.0, 120.0 + 17 * 3600, 120.0 + 17 * 3600 + 60]
    gaps = find_gaps(ts)
    assert len(gaps) == 1
    assert abs(sum(b - a for a, b in gaps) - 17 * 3600) < 1.0


def test_a_steady_stream_has_no_gaps():
    from rate import find_gaps
    assert find_gaps([float(i * 60) for i in range(50)]) == []


def test_a_short_quiet_stretch_is_not_called_an_outage():
    """Calling a quiet stretch an outage would inflate the rate instead, which
    is the same error pointing the other way."""
    from rate import find_gaps
    assert find_gaps([0.0, 600.0, 1200.0]) == []


def test_the_outage_threshold_is_far_above_the_observed_quiet_period():
    """At ~35 liquidations/hour, seeing none for 30 minutes is vanishingly
    unlikely, so the cut is safely above normal quiet."""
    from rate import OUTAGE_GAP_S
    assert OUTAGE_GAP_S >= 1_800.0


def test_the_collector_records_its_own_sessions():
    """Uptime inferred from gaps was a heuristic that moves the rate 4x when
    wrong. A recorded connect/disconnect is a fact, so the collector writes
    one and rate.py prefers it."""
    from collect import SCHEMA
    assert "CREATE TABLE IF NOT EXISTS sessions" in SCHEMA
    assert "connected_ts" in SCHEMA and "disconnected_ts" in SCHEMA


def test_rate_prefers_the_session_log_over_the_gap_heuristic():
    import inspect

    import rate
    source = inspect.getsource(rate.main)
    assert "FROM sessions" in source
    assert "session log" in source


def test_the_reconnect_backoff_is_jittered():
    """A fixed delay retries in lockstep with whatever broke and hammers a
    resolver that has just failed -- the same thundering-herd problem the
    memecoin scheduler had to fix."""
    import inspect

    import collect
    source = inspect.getsource(collect.run)
    assert "random.random()" in source
    assert "backoff" in source


def test_the_handshake_timeout_is_raised_above_the_default():
    """Eight consecutive 'timed out during opening handshake'. The library
    default of 10s is tight when DNS has just recovered and the route is
    still settling."""
    import inspect

    import collect
    assert "open_timeout=45" in inspect.getsource(collect.run)


# --------------------------- IPv6 that goes nowhere looks like a slow venue
def test_the_stream_forces_ipv4():
    """stream.bybit.com is CloudFront and returns eight AAAA records ahead of
    four A records. On a network with partial IPv6, Python tries those first
    and the symptom is 'timed out during opening handshake' -- which repeated
    eight times in a row while DNS resolution was demonstrably fine."""
    import inspect
    import socket

    import collect
    source = inspect.getsource(collect.run)
    assert "family=socket.AF_INET" in source
    assert collect.socket.AF_INET == socket.AF_INET


def test_the_quote_fetches_force_ipv4_too():
    """A follow-up quote that times out costs a horizon we cannot go back
    for, so the same fix applies to the REST calls."""
    import inspect

    import collect
    assert 'local_address="0.0.0.0"' in inspect.getsource(collect.run)


# ------------------- a sleeping machine is not a network fault
def test_a_suspension_is_distinguished_from_a_network_failure():
    """Both long gaps in the collected data began around 23:00 local: a
    bedtime, not a flaky ISP. WinError 1225 and 121 are what the adapter
    returns while coming back from sleep, and two rounds went into DNS and
    IPv6 before the timestamps were read for what they were."""
    import inspect

    import collect
    source = inspect.getsource(collect.run)
    assert "MACHINE WAS ASLEEP" in source
    assert "SUSPEND_GAP_S" in source


def test_the_suspension_threshold_is_far_above_the_recv_timeout():
    """One iteration awaits recv() with a 30s timeout, so it cannot
    legitimately take five minutes. Anything beyond that means the process
    stopped running rather than the market going quiet."""
    from collect import SUSPEND_GAP_S
    assert SUSPEND_GAP_S >= 300.0
    assert SUSPEND_GAP_S > 30.0 * 5


def test_suspensions_are_summarised_at_the_end_of_a_run():
    import inspect

    import collect
    source = inspect.getsource(collect.run)
    assert "the machine slept, which no" in source


# --------------- clustered cascades are not independent observations
def test_three_cascades_in_one_selloff_are_one_episode():
    """The first live burst: ETH $310k, ETH $297k, BTC $304k inside 140
    seconds, all forced selling. That is one market move counted three times.
    Treating them as three narrows every interval computed from the sample."""
    from rate import independent_episodes
    assert independent_episodes([0.0, 70.0, 140.0]) == 1


def test_cascades_an_hour_apart_are_separate_episodes():
    from rate import independent_episodes
    assert independent_episodes([0.0, 3600.0, 7200.0]) == 3


def test_no_cascades_is_no_episodes():
    from rate import independent_episodes
    assert independent_episodes([]) == 0


def test_a_single_cascade_is_one_episode():
    from rate import independent_episodes
    assert independent_episodes([123.0]) == 1


def test_episodes_collapse_across_symbols_not_within_one():
    """ETH and BTC liquidating together is the same market move. Collapsing
    per symbol would keep the inflation this exists to remove."""
    import inspect

    import rate
    assert "across all symbols" in inspect.getsource(rate.independent_episodes)


# ------------------ a gap is data missing at a particular time of day
def test_a_nightly_outage_shows_as_blind_hours():
    """The collection machine has intermittent mains power, so outages recur
    at whichever hours the supply fails. Liquidations follow the trading
    sessions, so being blind at the same hours every day is a bias with a
    direction rather than a smaller sample."""
    import time as _time

    from rate import coverage_by_hour

    day = _time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
    # Connected all day except 02:00-05:00, on two consecutive days.
    intervals = []
    for d in range(2):
        base = day + d * 86400
        intervals.append((base, base + 2 * 3600))
        intervals.append((base + 5 * 3600, base + 24 * 3600))
    coverage = coverage_by_hour(intervals, (day, day + 2 * 86400))
    assert coverage[3] < 0.1
    assert coverage[12] > 0.9


def test_a_quiet_market_is_not_mistaken_for_an_outage():
    """The first version inferred coverage from gaps between liquidations, so
    an hour with no liquidations read as blind. A quiet market is not an
    outage, and overstating a bias is still getting it wrong."""
    import time as _time

    from rate import coverage_by_hour
    day = _time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
    # Connected the whole time, regardless of how few liquidations arrived.
    coverage = coverage_by_hour([(day, day + 86400)], (day, day + 86400))
    assert min(coverage.values()) > 0.9


def test_full_coverage_reports_no_blind_hours():
    import time as _time

    from rate import coverage_by_hour
    day = _time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
    coverage = coverage_by_hour([(day, day + 86400)], (day, day + 86400))
    assert coverage
    assert min(coverage.values()) > 0.9


def test_no_sessions_gives_no_coverage_rather_than_a_crash():
    from rate import coverage_by_hour
    assert coverage_by_hour([], (0.0, 100.0)) == {}
    assert coverage_by_hour([(0.0, 10.0)], (100.0, 100.0)) == {}


def test_short_silence_is_not_called_a_fault():
    """Two empty minutes happen ~1 run in 30 at the measured rate.

    The guard this replaces asserted "subscription problem" at the first
    heartbeat with no data, which is 2 minutes.
    """
    import collect
    assert not collect.silence_is_suspicious(120.0)
    assert not collect.silence_is_suspicious(600.0)


def test_long_silence_is_called_a_fault():
    """Hours of nothing is not a calm market at any plausible rate."""
    import collect
    assert collect.silence_is_suspicious(3600.0)
    assert collect.silence_is_suspicious(8 * 3600.0)


def test_silence_threshold_matches_the_stated_odds():
    """The cutoff is where P(zero) crosses the alarm probability."""
    import math

    import collect
    cutoff = -math.log(collect.SILENCE_ALARM_P) / collect.QUIET_RATE_PER_HOUR
    assert not collect.silence_is_suspicious(cutoff * 3600.0 * 0.99)
    assert collect.silence_is_suspicious(cutoff * 3600.0 * 1.01)


def test_missing_ack_is_a_fault_immediately():
    """No ack means nothing was ever subscribed; waiting proves nothing."""
    import collect
    lines = " ".join(collect.silence_verdict(5.0, acked=False))
    assert "NO SUBSCRIPTION ACK" in lines


def test_acked_short_silence_says_it_is_not_evidence():
    import collect
    lines = " ".join(collect.silence_verdict(120.0, acked=True))
    assert "not yet evidence" in lines
    assert "NO MARKET DATA" not in lines


def test_acked_long_silence_overrides_the_ack():
    """The 8-hour failure acked too; a confirmed ack must not buy silence."""
    import collect
    lines = " ".join(collect.silence_verdict(8 * 3600.0, acked=True))
    assert "NO MARKET DATA" in lines
    assert "despite the ack" in lines


def test_control_trades_are_never_stored_as_liquidations():
    """publicTrade proves the data path; it is not liquidation data."""
    import collect

    trade = {"topic": "publicTrade.BTCUSDT",
             "data": [{"S": "Sell", "p": "60000", "v": "1.5",
                       "T": 1700000000000, "s": "BTCUSDT"}]}
    assert collect.extract_liquidations("bybit", trade) == []
    assert collect.classify_message("bybit", trade) == "control"


def test_liquidation_topic_still_classifies_as_data():
    import collect

    liq = {"topic": "allLiquidation.BTCUSDT", "data": []}
    assert collect.classify_message("bybit", liq) == "data"


def test_flowing_control_makes_silence_a_market_fact():
    """The whole point: trades arriving proves nothing is broken."""
    import collect

    lines = " ".join(collect.silence_verdict(3600.0, acked=True,
                                             control_msgs=9000))
    assert "data path demonstrably works" in lines
    assert "not a fault" in lines


def test_dead_control_after_a_long_silence_is_still_a_fault():
    import collect

    lines = " ".join(collect.silence_verdict(3600.0, acked=True,
                                             control_msgs=0))
    assert "broken despite the ack" in lines


def test_control_sampler_writes_rows_with_no_cascade_id():
    """The control arm is what separates a cascade effect from drift.

    It went unimplemented while the schema documented it, so the column
    existed and nothing ever filled it. Quotes cannot be backfilled: no
    price history is stored, so an hour without this is an hour the control
    arm does not exist for.
    """
    import asyncio
    import sqlite3

    import collect

    conn = sqlite3.connect(":memory:")
    conn.executescript(collect.SCHEMA)

    async def fake_fetch(client, venue, symbol):
        return {"bid": 100.0, "ask": 100.1, "quoted_ts": 1.0}

    real_fetch, real_horizons = collect.fetch_quote, collect.HORIZONS_S
    collect.fetch_quote = fake_fetch
    collect.HORIZONS_S = (0.01,)
    try:
        asyncio.run(collect.control_sampler(
            conn, None, "bybit", {"BTCUSDT"},
            deadline=time.time() + 0.3, per_hour=200_000.0))
    finally:
        collect.fetch_quote = real_fetch
        collect.HORIZONS_S = real_horizons

    rows = conn.execute(
        "SELECT count(*) FROM quotes WHERE cascade_id IS NULL").fetchone()[0]
    assert rows > 0, "control sampler wrote nothing"


def test_control_sampler_does_not_block_on_each_follow_up():
    """follow_up sleeps through every horizon, up to 900s.

    Awaiting it would cap the control rate near 2.4/hour whatever per_hour
    said, and would have looked exactly like the sampler working.
    """
    import asyncio
    import sqlite3

    import collect

    conn = sqlite3.connect(":memory:")
    conn.executescript(collect.SCHEMA)
    started = []

    async def slow_fetch(client, venue, symbol):
        started.append(time.time())
        await asyncio.sleep(0.2)
        return {"bid": 100.0, "ask": 100.1, "quoted_ts": 1.0}

    real_fetch, real_horizons = collect.fetch_quote, collect.HORIZONS_S
    collect.fetch_quote = slow_fetch
    collect.HORIZONS_S = (5.0,)   # longer than the sampler window
    try:
        asyncio.run(collect.control_sampler(
            conn, None, "bybit", {"BTCUSDT"},
            deadline=time.time() + 0.3, per_hour=100_000.0))
    finally:
        collect.fetch_quote = real_fetch
        collect.HORIZONS_S = real_horizons

    assert len(started) > 1, (
        "only one entry began; the sampler blocked on the first follow_up")


def test_control_sampler_stops_at_the_deadline():
    import asyncio
    import sqlite3

    import collect

    conn = sqlite3.connect(":memory:")
    conn.executescript(collect.SCHEMA)
    started = time.time()
    asyncio.run(collect.control_sampler(
        conn, None, "bybit", {"BTCUSDT"},
        deadline=started - 1.0, per_hour=6.0))
    assert time.time() - started < 1.0


def test_control_rate_exceeds_the_measured_cascade_rate():
    """The control must not be the binding constraint on the comparison."""
    import collect

    measured_cascades_per_hour = 20.0 / 24.0
    assert collect.CONTROL_PER_HOUR > measured_cascades_per_hour * 3


def test_control_quotes_are_groupable_into_series():
    """Overlapping control entries must be separable.

    All-NULL cascade_id could not group them: a 900s series is still running
    when the next starts, so their rows interleave.
    """
    import asyncio
    import sqlite3

    import collect

    conn = sqlite3.connect(":memory:")
    conn.executescript(collect.SCHEMA)

    async def fake_fetch(client, venue, symbol):
        return {"bid": 100.0, "ask": 100.1, "quoted_ts": time.time()}

    real_fetch, real_horizons = collect.fetch_quote, collect.HORIZONS_S
    collect.fetch_quote = fake_fetch
    collect.HORIZONS_S = (0.02,)
    try:
        asyncio.run(collect.control_sampler(
            conn, None, "bybit", {"BTCUSDT"},
            deadline=time.time() + 0.3, per_hour=200_000.0))
    finally:
        collect.fetch_quote = real_fetch
        collect.HORIZONS_S = real_horizons

    series = conn.execute(
        "SELECT count(DISTINCT control_id) FROM quotes "
        "WHERE control_id IS NOT NULL").fetchone()[0]
    assert series > 1, "control quotes cannot be told apart"
    orphans = conn.execute(
        "SELECT count(*) FROM quotes WHERE cascade_id IS NULL "
        "AND control_id IS NULL").fetchone()[0]
    assert orphans == 0, "a quote belongs to neither arm"


def test_migrate_adds_control_id_to_an_older_database():
    """CREATE TABLE IF NOT EXISTS never alters an existing table.

    Without the migration the collector runs happily against the old shape,
    writing control rows that look fine and cannot be grouped.
    """
    import sqlite3

    import collect

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cascade_id INTEGER,
            symbol TEXT NOT NULL,
            offset_s REAL NOT NULL,
            bid REAL, ask REAL, quoted_ts REAL NOT NULL);
    """)
    conn.execute("INSERT INTO quotes (cascade_id, symbol, offset_s, bid, ask,"
                 " quoted_ts) VALUES (1,'BTCUSDT',0,1.0,1.1,1.0)")
    conn.commit()

    added = collect.migrate(conn)
    assert "quotes.control_id" in added
    # Depth too: top of book prices an infinitesimal trade, and the plan's
    # second test needs a notional the book can absorb.
    assert "quotes.bids" in added and "quotes.asks" in added
    cols = {r[1] for r in conn.execute("PRAGMA table_info(quotes)")}
    assert {"control_id", "bids", "asks"} <= cols
    kept = conn.execute("SELECT count(*) FROM quotes").fetchone()[0]
    assert kept == 1, "migration lost existing rows"
    assert collect.migrate(conn) == [], "migration is not idempotent"


def test_fetch_quote_records_depth_not_just_the_touch():
    """Top of book prices a trade of zero size.

    Cross-exchange arbitrage looked alive at a 1.62bp top-of-book spread and
    died against a 36bp hurdle once the book was walked for real size. Depth
    cannot be reconstructed once the moment passes.
    """
    import asyncio
    import json

    import collect

    class FakeResponse:
        @staticmethod
        def json():
            return {"result": {
                "b": [["100.0", "1.5"], ["99.9", "10.0"]],
                "a": [["100.1", "2.0"], ["100.2", "8.0"]]}}

    class FakeClient:
        async def get(self, url, params=None, timeout=None):
            return FakeResponse()

    quote = asyncio.run(collect.fetch_quote(FakeClient(), "bybit", "BTCUSDT"))
    assert quote["bid"] == 100.0 and quote["ask"] == 100.1
    assert json.loads(quote["bids"])[1] == [99.9, 10.0]
    assert json.loads(quote["asks"])[1] == [100.2, 8.0]


def test_fetch_quote_rejects_a_crossed_book():
    """ask below bid is impossible and would invert every spread."""
    import asyncio

    import collect

    class FakeResponse:
        @staticmethod
        def json():
            return {"result": {"b": [["100.5", "1.0"]], "a": [["100.0", "1.0"]]}}

    class FakeClient:
        async def get(self, url, params=None, timeout=None):
            return FakeResponse()

    assert asyncio.run(
        collect.fetch_quote(FakeClient(), "bybit", "BTCUSDT")) is None
