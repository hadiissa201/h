"""The size probe's accounting rules.

Written after five defects in this one script reached a live run: the loss floor
reported as a return, a verdict contradicting its own table, our HTTP failures
printed as the market refusing, rate limits silently eating the sample, and a
missing price impact recorded as zero impact.

Every one of them resolved an unknown in the flattering direction. These tests
pin the rules that keep that from happening again, because the script had none.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from size_probe import Pacer, Quote, amount_for, summarise


def output(quotes: list[Quote]) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        summarise(quotes)
    # Collapse wrapping so an assertion about a sentence does not depend on
    # where the line happened to break.
    return " ".join(buffer.getvalue().split())


# ------------------------------------------------- missing is not zero
def test_a_routed_quote_with_no_impact_is_excluded_from_the_median():
    """The defect that made a two-day-old token look free to exit.

    Jupiter sometimes omits priceImpactPct. Treating that as 0% turns 'we do
    not know' into 'it costs nothing', which is the most flattering possible
    reading of silence.
    """
    quotes = [Quote(1, "A", 100.0, 0.50, True), Quote(2, "B", 100.0, 0.50, True),
              Quote(3, "C", 100.0, None, True, "no impact reported")]
    text = output(quotes)
    assert "50.0%" in text, "a known impact vanished from the median"
    # " 0.0%" with the leading space, so this does not match inside "50.0%".
    assert " 0.0%" not in text, "a missing impact was counted as zero"


def test_quotes_without_impact_are_counted_in_their_own_column():
    text = output([Quote(1, "A", 100.0, None, True, "no impact reported")])
    assert "no impact" in text


# ------------------------------------- our failures are never the market's
def test_an_unanswered_quote_is_not_counted_as_a_routing_failure():
    """A 429 is a fact about us. The route percentage must not absorb it."""
    quotes = [Quote(1, "A", 100.0, 0.05, True)]
    quotes += [Quote(i, "X", 100.0, None, None, "HTTP 429") for i in range(2, 10)]
    text = output(quotes)
    # One of one ANSWERED quote routed, so 100% -- not 1 of 9.
    assert "100%" in text, "unanswered quotes were put in the denominator"


def test_an_explicit_refusal_does_count_against_the_route_rate():
    quotes = [Quote(1, "A", 100.0, 0.05, True),
              Quote(2, "B", 100.0, None, False, "no_route")]
    assert "50%" in output(quotes)


# -------------------------------------------------------- verdict discipline
def test_no_verdict_on_a_handful_of_pairs():
    """The first run issued a verdict on two paired tokens."""
    quotes = []
    for i in range(5):
        quotes += [Quote(i, "A", 100.0, 0.03, True), Quote(i, "A", 5.0, 0.01, True)]
    text = output(quotes)
    assert "NO VERDICT" in text
    assert "VERDICT: size" not in text


def test_a_cheap_exit_is_not_reported_as_an_impossible_one():
    """The verdict fired on the DIFFERENCE alone, so a 1.8-point gap between
    two tiny impacts printed 'these pools cannot be exited at any size' over a
    table showing 2.8%."""
    quotes = []
    for i in range(25):
        quotes += [Quote(i, "A", 100.0, 0.028, True), Quote(i, "A", 5.0, 0.014, True)]
    text = output(quotes)
    assert "cannot be exited" not in text
    assert "cheap" in text


def test_a_genuinely_impossible_exit_is_reported_as_one():
    quotes = []
    for i in range(25):
        quotes += [Quote(i, "A", 100.0, 0.96, True), Quote(i, "A", 5.0, 0.95, True)]
    text = output(quotes)
    assert "cannot be exited at any size" in text


def test_a_size_that_genuinely_helps_is_reported_as_helping():
    quotes = []
    for i in range(25):
        quotes += [Quote(i, "A", 100.0, 0.90, True), Quote(i, "A", 5.0, 0.10, True)]
    text = output(quotes)
    assert "size matters" in text


# ------------------------------------------------------------------- sizing
def test_the_order_amount_scales_with_the_notional():
    big = amount_for(100.0, 2e-5, 6)
    small = amount_for(5.0, 2e-5, 6)
    assert big // small == 20


def test_an_unusable_price_yields_no_amount():
    assert amount_for(100.0, 0.0, 6) is None
    assert amount_for(100.0, -1.0, 6) is None


def test_an_amount_outside_the_exchange_limits_is_refused():
    """A clamped amount would quote a different trade than the one asked for,
    and the answer would not be about this position size."""
    assert amount_for(100.0, 1e-30, 6) is None      # astronomically large
    assert amount_for(1e-9, 1e6, 6) is None         # below the minimum


def test_decimals_are_respected():
    assert amount_for(100.0, 1e-5, 9) == amount_for(100.0, 1e-5, 6) * 1000


# -------------------------------------------------------------------- pacing
def test_the_pacer_slows_on_a_rate_limit_and_recovers_on_success():
    pacer = Pacer(delay=1.0, floor=0.8, ceiling=8.0)
    start = pacer.delay
    pacer.delay = min(pacer.ceiling, pacer.delay * 1.6)   # as saw_429 does
    assert pacer.delay > start
    for _ in range(200):
        pacer.saw_success()
    assert pacer.delay == pacer.floor, "never returned to full speed"


def test_the_pacer_never_outruns_its_floor():
    pacer = Pacer(delay=5.0, floor=0.8)
    for _ in range(1000):
        pacer.saw_success()
    assert pacer.delay >= pacer.floor
