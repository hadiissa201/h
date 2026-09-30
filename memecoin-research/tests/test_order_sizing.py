"""Sizing the sell order we ask Jupiter about.

Found by tracing a real position: a token entered at 4.643e-05, price rose 34%,
and the audit reported 100% price impact on a $20,162 pool with a $100 stake --
arithmetically impossible. The order was being sized off the price at DETECTION,
so for a token that had moved we were quoting a completely different trade.

The cost error was the visible symptom. The serious one was silent: a quote for
a multi-billion-dollar sale has no route, and "no route" is recorded as "this
token cannot be sold" -- the project's central measurement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.models import Base, Observation, Token, TokenStatus
from collector.workers import _MAX_QUOTE_AMOUNT, _amount_for_notional

TS = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DECIMALS = 6


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_token(session, decimals=DECIMALS) -> Token:
    tok = Token(chain="solana", address="SizeMint11111111111111111111111111111111111",
                detected_ts=TS, detection_source="pumpfun-bonding-curve",
                decimals=decimals)
    session.add(tok)
    session.commit()
    return tok


def observe(session, token, at, price):
    session.add(Observation(token_id=token.id, observed_ts=at, price_usd=price,
                            source="dexscreener"))
    session.commit()


def test_the_order_is_sized_at_the_current_price_not_the_first(session):
    """The bug. A token up 1000x since detection was quoted 1000x too large."""
    token = make_token(session)
    status = TokenStatus(token_id=token.id, first_price_usd=1e-08)
    session.add(status)
    observe(session, token, TS + timedelta(minutes=5), 1e-05)   # up 1000x
    session.commit()

    amount, trustworthy = _amount_for_notional(session, token, 100.0)

    # $100 at 1e-05 is 10,000,000 tokens, not 10,000,000,000.
    assert amount == int((100.0 / 1e-05) * 10**DECIMALS)
    assert trustworthy is True
    off_by = amount / int((100.0 / 1e-08) * 10**DECIMALS)
    assert off_by == pytest.approx(0.001), "still sizing off the detection price"


def test_a_dollar_notional_stays_a_dollar_notional(session):
    """The sanity check the 100%-impact trace should have failed."""
    token = make_token(session)
    observe(session, token, TS, 4.643e-05)
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    # Converting back through the price must return the dollar size asked for.
    dollars = (amount / 10**DECIMALS) * 4.643e-05
    assert dollars == pytest.approx(100.0, rel=1e-6)
    assert trustworthy is True


def test_the_newest_observation_wins(session):
    token = make_token(session)
    observe(session, token, TS, 1e-05)
    observe(session, token, TS + timedelta(minutes=1), 2e-05)
    observe(session, token, TS + timedelta(minutes=2), 5e-06)
    amount, _ = _amount_for_notional(session, token, 100.0)
    assert amount == int((100.0 / 5e-06) * 10**DECIMALS)


def test_an_unpriced_token_is_flagged_as_not_a_real_size(session):
    """No price means we cannot ask about $100, and must not pretend we did."""
    token = make_token(session)
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    assert amount == 10**DECIMALS          # one whole token: asks 'any route?'
    assert trustworthy is False


def test_the_peak_price_is_never_used_to_size_an_order(session):
    """A peak is not an estimate of the current price. Using it would quote a
    far-too-small order on a token that has since collapsed, understating
    impact in the flattering direction."""
    token = make_token(session)
    session.add(TokenStatus(token_id=token.id, first_price_usd=None,
                            peak_price_usd=1.0))
    session.commit()
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    assert trustworthy is False
    assert amount == 10**DECIMALS


def test_first_price_is_used_only_when_there_is_no_observation(session):
    token = make_token(session)
    session.add(TokenStatus(token_id=token.id, first_price_usd=2e-05))
    session.commit()
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    assert amount == int((100.0 / 2e-05) * 10**DECIMALS)
    assert trustworthy is True


def test_a_clamped_amount_is_not_a_trustworthy_size(session):
    """A price so small the honest amount exceeds Jupiter's maximum. The quote
    that comes back is about a different trade, so it cannot count as evidence
    that this token is unsellable. A rugged token really does trade here."""
    token = make_token(session)
    observe(session, token, TS, 1e-15)
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    assert amount == _MAX_QUOTE_AMOUNT
    assert trustworthy is False


def test_token_decimals_are_respected(session):
    """A 9-decimal token sized with 6 decimals is off by 1000x."""
    token = make_token(session, decimals=9)
    observe(session, token, TS, 1e-05)
    amount, _ = _amount_for_notional(session, token, 100.0)
    assert amount == int((100.0 / 1e-05) * 10**9)


def test_a_price_too_small_to_store_is_treated_as_no_price(session):
    """Price is NUMERIC(40, 20), so anything under 1e-20 stores as zero. A zero
    price must not be divided by, and must not be reported as a real size."""
    token = make_token(session)
    observe(session, token, TS, 1e-30)
    amount, trustworthy = _amount_for_notional(session, token, 100.0)
    assert trustworthy is False
    assert amount == 10**DECIMALS
