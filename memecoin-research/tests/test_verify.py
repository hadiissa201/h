"""Chain-state verification of routing quotes.

The check exists because a Jupiter quote proves a route, not a transfer. These
tests mostly guard the ways the check could corrupt the thing it is checking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from collector import verify
from collector.models import Base, SimulatedExit, Token
from collector.paper import exit_available
from poc.sources import METHOD_QUOTE, METHOD_RPC_SIM
from poc.store import insert_simulated_exit

TS = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
CURVE = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def token(session):
    tok = Token(chain="solana", address="VerifyMint111111111111111111111111111111111",
                detected_ts=TS, detection_source="pumpfun-bonding-curve")
    session.add(tok)
    session.commit()
    return tok


# ------------------------------------------------------- the contamination guard
def test_paper_trading_ignores_rpc_verification_rows(session, token):
    """The check must not become part of what it checks.

    exit_available takes the MOST RECENT attempt as decisive. If a verification
    row counted, a sampled rpc_sim would change which positions close and shift
    the project's headline unsellability rate.
    """
    insert_simulated_exit(session, token_id=token.id, simulated_ts=TS,
                          method=METHOD_QUOTE, notional_usd=100.0, succeeded=True)
    # A later verification row that disagrees.
    insert_simulated_exit(session, token_id=token.id, simulated_ts=TS + timedelta(seconds=5),
                          method=METHOD_RPC_SIM, notional_usd=100.0, succeeded=False,
                          failure_kind="reverted")
    session.commit()

    sellable = exit_available(session, token.id, TS + timedelta(seconds=10), max_age_s=600)
    assert sellable is not None, "a verification row overrode the quote pipeline"
    assert sellable.method == METHOD_QUOTE


def test_an_rpc_row_alone_never_authorises_an_exit(session, token):
    """The converse: a passing verification is not a substitute for a quote."""
    insert_simulated_exit(session, token_id=token.id, simulated_ts=TS,
                          method=METHOD_RPC_SIM, notional_usd=100.0, succeeded=True)
    session.commit()
    assert exit_available(session, token.id, TS + timedelta(seconds=10),
                          max_age_s=600) is None


# ------------------------------------------------------------------- sampling
def test_sampling_is_deterministic_and_respects_the_rate():
    mint = "SomeMint11111111111111111111111111111111111"
    assert verify.should_verify(mint, 0.0) is False
    assert verify.should_verify(mint, 1.0) is True
    first = verify.should_verify(mint, 0.5, "2026-09-27T12")
    assert verify.should_verify(mint, 0.5, "2026-09-27T12") is first


def test_sampling_rate_is_roughly_honoured():
    """Not 'whatever we had capacity for' -- that over-represents quiet hours."""
    mints = [f"Mint{i:039d}" for i in range(4000)]
    hits = sum(verify.should_verify(m, 0.10) for m in mints)
    assert 0.08 < hits / len(mints) < 0.12


def test_a_token_is_re_verified_in_a_later_bucket():
    """Authorities and liquidity change; one check for all time is not enough."""
    mint = "Recheck1111111111111111111111111111111111111"
    buckets = [f"2026-09-27T{h:02d}" for h in range(24)]
    assert len({verify.should_verify(mint, 0.5, b) for b in buckets}) == 2


# --------------------------------------------------------------- holder lookup
class FakeClient:
    """Replays canned RPC responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls.append(json.get("method"))
        return self.responses.pop(0)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = "{}"
        self.content = b"{}"

    def json(self):
        return self._payload


def _largest(*accounts):
    return FakeResponse({"result": {"value": list(accounts)}})


def _owner(pubkey):
    return FakeResponse({"result": {"value": {"data": {"parsed": {"info": {"owner": pubkey}}}}}})


def test_the_bonding_curve_is_not_used_as_a_holder():
    """The biggest holder is the launchpad's own vault. Simulating a sell from
    it tests the launchpad's plumbing, not whether a person can exit."""
    client = FakeClient([
        _largest({"address": "CurveAta", "amount": "900000000"},
                 {"address": "HumanAta", "amount": "100000000"}),
        _owner(CURVE),
        _owner("RealPerson1111111111111111111111111111111111"),
    ])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000)
    assert owner == "RealPerson1111111111111111111111111111111111"
    assert "RealPers" in note


def test_a_holder_too_small_to_sell_our_size_is_skipped():
    client = FakeClient([_largest({"address": "Tiny", "amount": "5"})])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000_000)
    assert owner is None
    assert "no eligible holder" in note


def test_no_holder_is_recorded_as_unknown_never_as_unsellable(session, token):
    """The critical one. 'We found nobody to simulate as' is a fact about us.

    Recording it as False would turn our own inability to run a check into
    evidence that the token cannot be sold -- the exact error this project
    spent an audit removing from the quote pipeline.
    """
    class Settings:
        rpc_url = "http://rpc"
        jupiter_swap_url = "http://swap"
        http_timeout_s = 5.0

    client = FakeClient([_largest()])
    detail = verify.verify_exit(session, client, None, Settings(), token,
                                {"outAmount": "1"}, 100.0, 1_000)
    session.commit()

    row = session.query(SimulatedExit).filter_by(method=METHOD_RPC_SIM).one()
    assert row.succeeded is None, "an unrunnable check became a market verdict"
    assert row.failure_kind == verify.FAILURE_NO_HOLDER
    assert "skipped" in detail


# ------------------------------------------------------------------- reporting
def test_an_unmeasured_rate_is_none_not_zero(session):
    """Reporting 0% wrong when nothing was checked would be a false all-clear."""
    counts = verify.disagreement_rate(session)
    assert counts["quote_false_positive_rate"] is None
    assert "routing only" in verify.describe(counts)


def test_the_disagreement_rate_counts_only_answered_checks(session, token):
    for i, succeeded in enumerate((True, True, True, False, None)):
        insert_simulated_exit(session, token_id=token.id,
                              simulated_ts=TS + timedelta(minutes=i),
                              method=METHOD_RPC_SIM, notional_usd=100.0,
                              succeeded=succeeded)
    session.commit()
    counts = verify.disagreement_rate(session)
    assert counts["verified"] == 4          # the unknown is excluded
    assert counts["would_revert"] == 1
    assert counts["quote_false_positive_rate"] == pytest.approx(0.25)
    assert "25%" in verify.describe(counts)


def test_the_holder_actually_reaches_the_swap_build(session, token):
    """Guards a bug that would have made the whole check meaningless.

    simulate_sell_rpc defaulted to a zero-balance address. If the holder we
    looked up does not arrive in the swap build, every simulation reverts for
    insufficient funds -- and we would have recorded a pile of false
    'unsellable' verdicts that looked like a real finding.
    """
    sent = {}

    class RecordingClient(FakeClient):
        def post(self, url, json=None, timeout=None):  # noqa: A002
            if "swap" in url:
                sent.update(json)
                return FakeResponse({"swapTransaction": "BASE64TX"})
            return super().post(url, json=json, timeout=timeout)

    class Settings:
        rpc_url = "http://rpc"
        jupiter_swap_url = "http://swap"
        http_timeout_s = 5.0

    holder = "RealHolder111111111111111111111111111111111"
    client = RecordingClient([
        _largest({"address": "HumanAta", "amount": "9999999999"}),
        _owner(holder),
        FakeResponse({"result": {"value": {"err": None}}}),   # simulateTransaction
    ])
    verify.verify_exit(session, client, None, Settings(), token,
                       {"outAmount": "1"}, 100.0, 1_000)
    session.commit()

    assert sent.get("userPublicKey") == holder, \
        "the swap was built for the wrong account; every sim would revert"
    row = session.query(SimulatedExit).filter_by(method=METHOD_RPC_SIM).one()
    assert row.succeeded is True


def test_a_handful_of_checks_is_not_reported_as_a_rate(session, token):
    """The audit's own bug, caught on real output.

    The first live run verified two quotes, both reverted, and the audit
    announced '100.0% of quotes that said sellable would NOT have landed'.
    Two data points cannot support that, and at n=2 a systematic fault in our
    method -- simulating as a holder who could not have sold anyway -- is
    indistinguishable from a discovery about the market.
    """
    for i in range(2):
        insert_simulated_exit(session, token_id=token.id,
                              simulated_ts=TS + timedelta(minutes=i),
                              method=METHOD_RPC_SIM, notional_usd=100.0,
                              succeeded=False)
    session.commit()

    text = verify.describe(verify.disagreement_rate(session))
    assert "TOO FEW" in text
    assert "95% CI" in text, "a small-sample rate must carry its interval"


def test_a_large_enough_sample_is_reported_as_a_rate(session, token):
    for i in range(verify.MIN_VERIFIED_FOR_A_RATE):
        insert_simulated_exit(session, token_id=token.id,
                              simulated_ts=TS + timedelta(minutes=i),
                              method=METHOD_RPC_SIM, notional_usd=100.0,
                              succeeded=i % 4 != 0)
    session.commit()
    text = verify.describe(verify.disagreement_rate(session))
    assert "TOO FEW" not in text
    assert "95% CI" in text


def test_the_interval_widens_as_the_sample_shrinks():
    """Guards the direction of the statistic, not its exact value."""
    wide_low, wide_high = verify.wilson_interval(1, 2)
    tight_low, tight_high = verify.wilson_interval(50, 100)
    assert (wide_high - wide_low) > (tight_high - tight_low)
    # Two-for-two is not proof of certainty.
    low, _ = verify.wilson_interval(2, 2)
    assert low < 0.5


def test_no_trials_claims_nothing():
    assert verify.wilson_interval(0, 0) == (0.0, 1.0)
