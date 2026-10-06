"""The report must not draw a conclusion wider than the evidence under it.

The first real run of the verifier read three curves, matched zero DexScreener
prices, and still printed "the chain shows more tokens than the aggregator
does, which is the whole point" -- about three tokens holding 0.00 SOL. The
layout was unconfirmed and the coverage gain was unsellable, and neither fact
reached the summary. These tests pin the summary to the data.
"""

from __future__ import annotations

import base64
import struct

from collector.onchain import BONDING_CURVE_DISCRIMINATOR, parse_bonding_curve
from verify_onchain import report


def curve(virtual_sol=30_000_000_000, real_sol=0, complete=0,
          virtual_tokens=1_073_000_000_000_000,
          real_tokens=793_100_000_000_000,
          supply=1_000_000_000_000_000):
    raw = struct.pack("<8s5QB", BONDING_CURVE_DISCRIMINATOR, virtual_tokens,
                      virtual_sol, real_tokens, real_sol, supply, complete)
    return parse_bonding_curve(base64.b64encode(raw).decode())


def run(capsys, curves, agreed=(), disagreed=(), unreadable=(),
        funded=0, empty=0):
    report(list(curves), list(agreed), list(disagreed), list(unreadable),
           funded, empty)
    return capsys.readouterr().out


def test_a_single_curve_is_not_enough_to_confirm_anything(capsys):
    out = run(capsys, [curve()], unreadable=[("a", "account does not exist")],
              empty=1)
    assert "LAYOUT NOT CONFIRMED" in out


def test_the_live_run_sample_now_confirms_the_layout(capsys):
    """The exact shape the user's machine returned: four untraded curves at
    pump.fun's documented launch state plus one that departs from it. The
    layout is confirmed and the odd curve is raised separately."""
    out = run(capsys, [curve(real_sol=0)] * 4
              + [curve(real_sol=0, virtual_sol=426_629_411)],
              funded=0, empty=5)
    assert "LAYOUT CONFIRMED" in out
    assert "ANOMALIES" in out
    assert "426,629,411" in out


def test_an_empty_curve_is_never_counted_as_tradable_coverage(capsys):
    out = run(capsys, [curve(real_sol=0)] * 3, funded=1, empty=9)
    assert "only the chain sees, 1 hold more than" in out
    assert "unsellable" in out


def test_missing_dexscreener_overlap_is_not_a_pass_and_not_a_failure(capsys):
    out = run(capsys, [curve(real_sol=0)] * 3)
    assert "Unavailable" in out
    assert "not a pass either" in out


def test_disagreeing_sources_block_the_verdict_even_when_invariants_hold(capsys):
    """Internal consistency plus an external contradiction is unresolved. The
    old code printed CONFIRMED from the invariants and MISMATCH from
    DexScreener in the same output."""
    rows = [("mint", 1e-7, 9e-7, 9.0, 0.0, False)]
    out = run(capsys, [curve(real_sol=0)] * 3, disagreed=rows)
    assert "LAYOUT NOT CONFIRMED" in out
    assert out.count("LAYOUT CONFIRMED") == 0


def test_a_misaligned_read_is_still_rejected(capsys):
    """The confirmation must not be a rubber stamp: shift the fields and all
    four documented values break at once."""
    out = run(capsys, [curve(real_sol=0, virtual_sol=7, virtual_tokens=9)] * 4)
    assert "LAYOUT NOT CONFIRMED" in out


def test_a_graduated_token_is_reported_as_left_the_curve_not_as_unreadable(capsys):
    """A zeroed curve means the token survived to an AMM. Counting it as
    unreadable threw away the most interesting group in the sample."""
    from verify_onchain import report
    report([curve(real_sol=0)] * 3, [], [], [], 0, 0, 11)
    out = capsys.readouterr().out
    assert "LEFT the curve" in out
    assert "11" in out


def test_reading_nothing_at_all_is_not_confirmation(capsys):
    out = run(capsys, [], unreadable=[("a", "account does not exist")] * 15)
    assert "LAYOUT NOT CONFIRMED" in out


# --------------------------------------------- the database-free layout check
def test_an_undecodable_mint_is_reported_without_touching_the_network(capsys):
    """curve_address derives locally, so a malformed mint must fail before any
    RPC call. If this ever makes a request, the no-network guarantee is gone."""
    from verify_onchain import check_mints
    rc = check_mints(["not-a-valid-base58-mint-!!!"], "http://127.0.0.1:1")
    out = capsys.readouterr().out
    assert rc == 0
    assert "LAYOUT NOT CONFIRMED" in out
    assert "cannot derive from mint" in out


def test_empty_mints_argument_exits_nonzero(monkeypatch, capsys):
    import sys as _sys

    import verify_onchain
    monkeypatch.setattr(_sys, "argv",
                        ["verify_onchain.py", "--mints", " , ",
                         "--rpc", "http://127.0.0.1:1"])
    assert verify_onchain.main() == 1


# ------------------------------------------------------------ which tokens
def seeded_session():
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from collector.models import Base, Observation, Token

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    now = datetime.now(UTC)
    # Oldest token took the most liquidity; newest was never priced at all.
    rows = [("whale", 14, 90_000.0), ("mid", 9, 4_000.0), ("small", 5, 80.0)]
    for name, days, liq in rows:
        t = Token(address=name, chain="solana", detection_source="test",
                  detected_ts=now - timedelta(days=days))
        s.add(t)
        s.flush()
        s.add(Observation(token_id=t.id, observed_ts=now - timedelta(days=days),
                          source="dexscreener", price_usd=1e-7,
                          liquidity_usd=liq))
    for i in range(3):
        s.add(Token(address=f"never-priced-{i}", chain="solana", detection_source="test",
                    detected_ts=now - timedelta(hours=i + 1)))
    s.commit()
    return s


def test_the_traded_half_is_ordered_by_peak_liquidity_not_recency(capsys):
    """Recency picked tokens nobody bought, three runs in a row. The curve
    that took the most SOL is the one most likely to still hold some."""
    from verify_onchain import pick_tokens
    with seeded_session() as s:
        picked = [t.address for t in pick_tokens(s, limit=4, hours=None)]
    assert picked[:2] == ["whale", "mid"]


def test_never_priced_tokens_fill_the_second_half(capsys):
    from verify_onchain import pick_tokens
    with seeded_session() as s:
        picked = [t.address for t in pick_tokens(s, limit=6, hours=None)]
    assert sum(p.startswith("never-priced") for p in picked) == 3


def test_a_recent_window_excludes_the_old_whale(capsys):
    """Proof the default of all-history matters: a 48h window, which is what
    the script used to default to, cannot see any token that ever traded."""
    from verify_onchain import pick_tokens
    with seeded_session() as s:
        picked = [t.address for t in pick_tokens(s, limit=6, hours=48)]
    assert "whale" not in picked
    assert all(p.startswith("never-priced") for p in picked)


def test_graduated_curves_count_toward_the_sample_denominator(capsys):
    """The run printed "5 of 29" after looking at 40 tokens, because the 11
    curves it read and classified as graduated were dropped from the total."""
    from verify_onchain import report
    report([curve(real_sol=0)] * 5, [], [], [("a", "no account")] * 24,
           0, 5, 11)
    out = capsys.readouterr().out
    assert "5 of 40" in out


def test_the_verdict_and_the_seed_check_never_disagree(capsys):
    """Both now read the same predicate. One dust curve must not be a traded
    curve in one section and absent in the other."""
    out = run(capsys, [curve(real_sol=4_000)] + [curve(real_sol=0)] * 3)
    assert "UNTESTED" in out
    assert "only 0 of the" in out
