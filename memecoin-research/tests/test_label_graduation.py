"""The pool table could not label graduation, so the chain has to.

It was censored in both directions: 88 tokens had a pumpswap pool with no
pump.fun pool recorded, so their curve phase was never seen, and any token
that migrated after the collector stopped watching looked like it never did.
That produced a 1.14% graduation rate on a population whose true rate must be
higher -- and a base rate that wrong makes every entry-signal test
meaningless.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector.models import Base, CurveState, Token
from graduated import chain_labels
from label_graduation import summarise

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def state(token_id, exists=True, complete=False, real=0, virtual=30_000_000_000):
    return CurveState(token_id=token_id, curve_address=f"c{token_id}",
                      account_exists=exists, complete=complete,
                      real_sol_reserves=real, virtual_sol_reserves=virtual,
                      account_bytes=141, checked_ts=NOW, note="ok")


def seeded(states):
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    for i in range(1, len(states) + 1):
        s.add(Token(address=f"t{i}", chain="solana",
                    detection_source="test", detected_ts=NOW))
    s.flush()
    for row in states:
        s.add(row)
    s.commit()
    return s


# ------------------------------------------------------------- the label
def test_the_complete_flag_decides_graduation():
    with seeded([state(1, complete=True), state(2, complete=False)]) as s:
        graduated, eligible, labelled = chain_labels(s)
    assert graduated == {1}
    assert eligible == {1, 2}
    assert labelled == 2


def test_a_token_with_no_curve_is_not_eligible():
    """No account at the derived address means it was never a pump.fun launch,
    so including it would dilute the base rate a signal has to beat."""
    with seeded([state(1, exists=False, complete=None),
                 state(2, complete=True)]) as s:
        graduated, eligible, _ = chain_labels(s)
    assert eligible == {2}
    assert graduated == {2}


def test_no_labels_yet_reports_nothing_rather_than_an_empty_truth():
    """graduated.py must fall back to the pool table and say so, not read an
    unrun pass as "no token graduated"."""
    with seeded([]) as s:
        graduated, eligible, labelled = chain_labels(s)
    assert not graduated and not eligible and labelled == 0


# ------------------------------------------- checking the assumption, not trusting it
def test_the_flag_is_cross_tabulated_against_the_reserves():
    """The labelling rests on migration setting `complete`. That belief had
    never been checked: the earlier run saw 11 curves with zeroed reserves and
    never looked at their flag, because the price helper returned None first."""
    out = summarise([state(1, complete=True, real=0, virtual=0),
                     state(2, complete=False, real=5_000_000_000)])
    assert "complete" in out and "zeroed" in out and "present" in out


def test_disagreement_between_flag_and_reserves_is_reported():
    """A drained but incomplete curve is ordinary selling, not graduation, and
    the two signals parting ways is worth saying out loud."""
    out = summarise([state(1, complete=False, real=0, virtual=0)])
    assert "disagree" in out
    assert "trust the flag" in out


def test_agreement_is_also_stated_rather_than_left_implicit():
    out = summarise([state(1, complete=True, real=0, virtual=0),
                     state(2, complete=False, real=1_000_000_000)])
    assert "agree on every curve" in out


def test_tokens_with_no_curve_are_left_out_of_the_cross_tab():
    """A missing account has no reserves to tabulate, and counting it as a
    False flag would invent a curve that is not there."""
    out = summarise([state(1, exists=False, complete=None),
                     state(2, complete=True, real=0, virtual=0)])
    rows = [line for line in out.splitlines()
            if line.startswith("  True") or line.startswith("  False")]
    assert len(rows) == 1
    assert rows[0].split()[-1] == "1"


# ---------------------------------------- a network failure is not a finding
def test_a_refused_connection_is_not_recorded_as_having_no_curve():
    """The defect the smoke test exposed. Six tokens came back as "not a
    pump.fun launch" when the only fact established was that a port was shut.
    Recording that would shrink the eligible population and bias the base rate
    downward -- in the direction that makes graduation look rarer than it is."""
    from label_graduation import is_definitive
    assert not is_definitive(
        "ConnectError reaching http://127.0.0.1:9 after 3 attempts")
    assert not is_definitive("HTTP 429 from https://rpc: rate limited")
    assert not is_definitive("ReadTimeout: timed out")
    assert not is_definitive("node refused getAccountInfo: -32603")


def test_the_node_saying_the_account_is_absent_is_definitive():
    from label_graduation import is_definitive
    assert is_definitive("account does not exist or is empty")


def test_a_foreign_account_at_the_derived_address_is_definitive():
    """A matching address holding something that is not a BondingCurve is a
    real answer: this is not a pump.fun launch."""
    from label_graduation import is_definitive
    assert is_definitive("discriminator abc is not a BondingCurve (def); the "
                         "layout assumption is wrong")
    assert is_definitive("the byte after five u64s is 7, which is not a bool")


def test_an_unanswered_read_returns_none_so_the_next_run_retries_it():
    import httpx

    from collector.pacing import Pacer
    from label_graduation import label_one
    token = Token(address="So11111111111111111111111111111111111111112",
                  chain="solana", detection_source="test", detected_ts=NOW)
    token.id = 1
    with httpx.Client() as client:
        assert label_one(client, "http://127.0.0.1:9", token, Pacer()) is None


def test_an_undecodable_mint_is_recorded_because_that_is_about_the_mint():
    import httpx

    from collector.pacing import Pacer
    from label_graduation import label_one
    token = Token(address="not-base58-!!!", chain="solana",
                  detection_source="test", detected_ts=NOW)
    token.id = 1
    with httpx.Client() as client:
        row = label_one(client, "http://127.0.0.1:9", token, Pacer())
    assert row is not None
    assert row.account_exists is False


def test_an_empty_cross_tab_is_not_reported_as_agreement():
    """It said "the flag and the reserves agree on every curve" having read
    no curves at all."""
    out = summarise([state(1, exists=False, complete=None)])
    assert "nothing to cross-check" in out
    assert "not agreement" in out
