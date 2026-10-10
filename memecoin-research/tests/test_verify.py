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
    """getAccountInfo on a TOKEN account: names the wallet that owns it."""
    return FakeResponse({"result": {"value": {"data": {"parsed": {"info": {"owner": pubkey}}}}}})


def _wallet(lamports=50_000_000):
    """getAccountInfo on that wallet: a plain funded account, able to pay fees."""
    return FakeResponse({"result": {"value": {"owner": verify.SYSTEM_PROGRAM,
                                              "lamports": lamports, "data": {}}}})


def _pda(program="LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"):
    """A program-derived address. Holds tokens, cannot pay fees."""
    return FakeResponse({"result": {"value": {"owner": program,
                                              "lamports": 2_039_280, "data": {}}}})


def test_the_bonding_curve_is_not_used_as_a_holder():
    """The biggest holder is the launchpad's own vault. Simulating a sell from
    it tests the launchpad's plumbing, not whether a person can exit."""
    client = FakeClient([
        _largest({"address": "CurveAta", "amount": "900000000"},
                 {"address": "HumanAta", "amount": "100000000"}),
        _owner(CURVE),
        _owner("RealPerson1111111111111111111111111111111111"),
        _wallet(),
    ])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000)
    assert owner == "RealPerson1111111111111111111111111111111111"
    assert "RealPers" in note


def test_a_holder_too_small_to_sell_our_size_is_skipped():
    client = FakeClient([_largest({"address": "Tiny", "amount": "5"})])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000_000)
    assert owner is None
    assert "no holder" in note


def test_a_pda_is_never_chosen_as_the_fee_payer():
    """The bug that produced four meaningless 'would revert' rows.

    A pool vault's owner is a PDA derived from the launchpad program, not the
    program id, so an id blocklist misses it. A PDA cannot pay fees: Solana
    rejects with InvalidAccountForFee before the token is involved at all, and
    that revert was being recorded as though the coin could not be sold.
    """
    client = FakeClient([
        _largest({"address": "VaultAta", "amount": "900000000"}),
        _owner("PoolPda11111111111111111111111111111111111"),
        _pda(),
    ])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000)
    assert owner is None
    assert "cannot pay fees" in note


def test_a_broke_wallet_is_not_chosen_either():
    """Holds the token but has no SOL, so the simulation would revert on fees."""
    client = FakeClient([
        _largest({"address": "BrokeAta", "amount": "900000000"}),
        _owner("BrokeWallet1111111111111111111111111111111"),
        _wallet(lamports=1_000),
    ])
    owner, note = verify.find_holder(client, "http://rpc", "mint", 1_000)
    assert owner is None
    assert "lamports" in note


def test_a_pda_holder_is_skipped_for_a_funded_wallet_behind_it():
    """The vault is biggest; the real holder further down is still usable."""
    client = FakeClient([
        _largest({"address": "VaultAta", "amount": "900000000"},
                 {"address": "HumanAta", "amount": "100000000"}),
        _owner("PoolPda11111111111111111111111111111111111"),
        _pda(),
        _owner("FundedHuman11111111111111111111111111111111"),
        _wallet(),
    ])
    owner, _ = verify.find_holder(client, "http://rpc", "mint", 1_000)
    assert owner == "FundedHuman11111111111111111111111111111111"


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
        _wallet(),
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


# ------------------------------- our fault must never be stored as the market's
def test_a_fee_payer_revert_is_classified_as_ours():
    """The first four live verifications were all InvalidAccountForFee: the
    fee payer was a PDA, so Solana rejected the transaction before the token
    was involved. Stored as False, that reads as "this token cannot be sold"."""
    from collector.verify import classify_failure
    whose, _ = classify_failure("Transaction failed: InvalidAccountForFee")
    assert whose == "OURS"


def test_a_holder_without_the_tokens_is_ours():
    from collector.verify import classify_failure
    assert classify_failure("Error: insufficient funds")[0] == "OURS"


def test_a_frozen_account_is_a_real_finding():
    from collector.verify import classify_failure
    assert classify_failure("Account is frozen")[0] == "REAL"


def test_slippage_is_a_real_finding_not_our_fault():
    """The route moved beyond tolerance before landing. That is the market."""
    from collector.verify import classify_failure
    assert classify_failure("SlippageToleranceExceeded")[0] == "REAL"


def test_an_unrecognised_reason_is_unknown_and_counts_against_the_strategy():
    """Calling an unknown reason ours would lower the false-positive rate
    every time a new error string appeared, which is the direction that
    quietly rescues the strategy."""
    from collector.verify import classify_failure
    whose, _ = classify_failure("Program log: 0xdeadbeef something new")
    assert whose == "UNKNOWN"


def test_the_classifier_is_in_the_collector_not_only_in_an_analysis_script():
    """It used to live in why_reverted.py, so the collector wrote rows the
    analysis then had to repair. repair_verifications.py exists because of
    that split. Shared now for the same reason exit_trigger is shared."""
    import collector.verify as cv
    assert hasattr(cv, "classify_failure")
    assert "classify_failure" in cv.__all__
    from why_reverted import classify
    assert classify is cv.classify_failure


def test_our_method_has_its_own_failure_kind():
    from collector.verify import FAILURE_OUR_METHOD
    assert FAILURE_OUR_METHOD == "our_method"


def test_the_write_path_turns_our_fault_into_none_not_false():
    """succeeded=None means "we could not ask", which is exactly what a
    transaction rejected before the token was involved amounts to."""
    import inspect

    from collector.verify import verify_exit
    source = inspect.getsource(verify_exit)
    assert 'whose == "OURS"' in source
    assert "succeeded = None" in source


# ------------------------------- an opaque code is not an unknown cause
def test_jupiter_slippage_code_is_a_real_market_finding():
    """6001 is SlippageToleranceExceeded (0x1771): the out amount fell below
    the minimum before landing. That is the market moving, which is exactly
    what this check exists to detect."""
    from collector.verify import classify_failure
    whose, explanation = classify_failure('{"InstructionError": [3, {"Custom": 6001}]}')
    assert whose == "REAL"
    assert "Slippage" in explanation


def test_jupiter_invalid_token_account_code_is_our_fault():
    """6025 is InvalidTokenAccount: an account in the transaction was
    uninitialised or not the expected one. That is how WE built it. 21 of the
    first 28 reverts were this, counted against the market."""
    from collector.verify import classify_failure
    whose, explanation = classify_failure('{"InstructionError": [1, {"Custom": 6025}]}')
    assert whose == "OURS"
    assert "PROVISIONAL" in explanation


def test_the_provisional_mapping_says_so_in_the_explanation():
    """6025 was read from a search summary of Jupiter's error table, not the
    table itself, because this container cannot resolve those hosts. A
    provisional basis has to travel with the classification."""
    from collector.verify import JUPITER_ERROR_CODES
    assert "PROVISIONAL" in JUPITER_ERROR_CODES[6025][1]


def test_an_unlisted_custom_code_stays_unknown_and_says_what_would_resolve_it():
    from collector.verify import classify_failure
    whose, explanation = classify_failure('{"InstructionError": [1, {"Custom": 4242}]}')
    assert whose == "UNKNOWN"
    assert "logs" in explanation


def test_the_code_is_extracted_from_the_instruction_error_shape():
    from collector.verify import custom_error_code
    assert custom_error_code('{"InstructionError": [2, {"Custom": 6001}]}') == 6001
    assert custom_error_code('"InvalidAccountForFee"') is None


def test_a_named_error_in_the_reason_wins_over_the_code():
    """Once the logs are captured the name is present, and a name is better
    evidence than a number from a table we could not load."""
    from collector.verify import classify_failure
    reason = ('{"InstructionError": [1, {"Custom": 6025}]} :: AnchorError '
              'occurred. Error Code: SlippageToleranceExceeded.')
    assert classify_failure(reason)[0] == "REAL"


# ------------------------------------------- the logs were being thrown away
def test_the_named_error_is_kept_from_the_simulation_logs():
    """The point: the err object says 6025, the logs say InvalidTokenAccount.
    Marker lines come FIRST so the name leads, ahead of the tail context."""
    from poc.sources import error_lines_from_logs
    out = error_lines_from_logs([
        "Program log: Instruction: Route",
        "Program log: AnchorError occurred. Error Code: InvalidTokenAccount. "
        "Error Number: 6025."], tail=0)
    assert "InvalidTokenAccount" in out
    assert "Instruction: Route" not in out


def test_the_name_still_leads_when_the_tail_is_included():
    from poc.sources import error_lines_from_logs
    out = error_lines_from_logs([
        "Program log: Instruction: Route",
        "Program log: Error Code: InvalidTokenAccount.",
        "Program JUP failed: custom program error: 0x1789"])
    assert out.index("InvalidTokenAccount") < out.index("Instruction: Route")


def test_duplicate_log_lines_are_not_repeated():
    from poc.sources import error_lines_from_logs
    line = "Program log: Error Code: Foo."
    assert error_lines_from_logs([line, line]).count("Error Code: Foo") == 1


def test_no_logs_yields_no_reason_rather_than_a_crash():
    from poc.sources import error_lines_from_logs
    assert error_lines_from_logs(None) == ""
    assert error_lines_from_logs([]) == ""


def test_the_log_tail_is_kept_so_a_revert_can_be_diagnosed_not_just_classified():
    """Marker lines classify a revert. Diagnosing it needs the failing
    instruction and the program that raised it, which are in the tail. 21
    reverts were InvalidTokenAccount with the cause still unknown."""
    from poc.sources import error_lines_from_logs
    out = error_lines_from_logs([
        "Program log: Instruction: Route",
        "Program ABC invoke [2]",
        "Program ABC consumed 12345 compute units",
        "Program ABC failed: custom program error: 0x1789"])
    assert "Program ABC invoke [2]" in out
    assert "0x1789" in out


def test_marker_lines_are_not_duplicated_by_the_tail():
    from poc.sources import error_lines_from_logs
    line = "Program log: Error Code: InvalidTokenAccount."
    out = error_lines_from_logs(["a", "b", line])
    assert out.count("InvalidTokenAccount") == 1


def test_a_zero_tail_really_means_no_tail():
    """logs[-0:] is logs[0:] -- the whole list. Without a guard, asking for no
    tail returns everything, which is the opposite of the request."""
    from poc.sources import error_lines_from_logs
    out = error_lines_from_logs(["noise one", "noise two"], tail=0)
    assert out == ""
