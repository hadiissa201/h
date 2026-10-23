# Liquidation cascades: what the data will decide, written before it exists

Pre-registered 2026-10-12, before the collector has recorded a single row.
The point of writing it now is the same as it was for the memecoin plan: I
cannot then adjust the criteria to fit whatever comes back. Any later change
must be dated and must say what prompted it.

## Why this one has a reason to work

`docs/FINDINGS.md` requires any new idea to arrive with a structural reason
someone is willing to lose money to you, because eight indicator strategies
without one measured -0.255 R gross. This has one.

A liquidated position is closed by the exchange at whatever price is
available. The seller is not choosing to sell; the seller has no choice. That
is involuntary supply, and involuntary supply overshoots, because the people
who would normally absorb it are not standing by in the size the cascade
needs. If the overshoot is real, price recovers part of it once the forced
selling stops.

**This is NOT the mean-reversion strategy already tested and rejected.** That
one conditioned on price alone — a stretched fall, a band touch — and
`short_term_reversal` with maker entries made BTC marginally worse (-0.345 to
-0.373 R). The difference here is conditioning on *observed forced selling*.
A 3% fall because sellers chose to sell is a different event from a 3% fall
because leveraged longs were closed out, and only the second carries the
structural argument. If the test cannot distinguish them it has not tested
anything.

## The cost hurdle, before any measurement

Binance USD-M futures, lowest fee tier, taker both ways:

| Component | Cost |
|---|---|
| taker fee in | 0.050% |
| taker fee out | 0.050% |
| **fees round trip** | **0.100%** |
| spread crossed, entry | measured, not assumed |
| spread crossed, exit | measured, not assumed |

The spread is deliberately left to measurement. A cascade is exactly when
liquidity is worst, so assuming a normal spread would be the single most
flattering error available — the same shape as using a memecoin's quoted
price while the payout came from real reserves. The collector records best
bid and ask at entry and at each follow-up, so the hurdle is computed from
what was actually quoted.

**A reversion must clear fees plus both spreads to be worth anything.** At a
typical 1bp BTC futures spread that is roughly 0.12%; during a cascade it
will be worse, and how much worse is a finding in itself.

## The entry rule, fixed now

1. A **cascade** is at least `MIN_CASCADE_USD` of liquidations in the same
   direction within `CASCADE_WINDOW_S`. Both constants are set in the
   collector before collection starts and are not to be tuned afterwards.
2. Entry is at the **first quote timestamped after the cascade was received by
   our own process**, never at the liquidation price itself. We learn of a
   liquidation after it happened, and the memecoin result turned on exactly
   this: a signal that keys on the move it is reacting to captures a median
   1.33x of a 14x run. Our own latency is recorded so this cannot be fudged.
3. Direction: long liquidations are forced SELLING, so the candidate trade is
   to BUY. Short liquidations are forced buying, so the candidate is to SELL.
4. Exit at a fixed horizon, evaluated at several: 30s, 60s, 300s, 900s. All
   horizons are reported. Picking the best one afterwards and presenting it
   alone is the fitting this document exists to prevent.

## The primary number

Mean net return per cascade trade, after fees and both measured spreads, with
a bootstrap 95% interval, at **each** horizon. Reported alongside the same
figure for a **control**: entries at randomly chosen times in the same period,
same horizon, same costs.

The control is the point. Crypto drifts; a window that happened to rise makes
every long look clever. If cascade entries do not beat random entries, there
is no cascade effect, only market direction.

| Interval, cascade minus control | Verdict | What follows |
|---|---|---|
| Entirely above 0 | Candidate | Go to the second test |
| Straddles 0 | Unproven | Keep collecting, no money either way |
| Entirely below 0 | Dead | Stop |

## The second test, only if the first is cleared

1. **Does it survive out of sample?** Threshold and horizon fixed on the first
   half by date, measured on the second.
2. **Does the size work?** Reversion measured at a notional the book can
   absorb during a cascade, not at top of book.
3. **Is it the cascade or the fall?** Compare cascade entries against entries
   on equally large price falls with NO liquidation burst. If both revert the
   same, the liquidation data adds nothing and this is the rejected
   mean-reversion strategy wearing a new hat.

## Preconditions — no verdict without these

1. At least **100 cascades** at the chosen threshold.
2. At least **20 distinct days**, so one unusual session cannot carry it.
3. Follow-up quotes present for at least 90% of cascades at the horizon being
   reported. Missing follow-ups are not zero returns.
4. Our own median latency from exchange event time to receipt under 2s,
   recorded per event. Above that, the entry price is not one we could have
   reached.

5. At least **100 control entries**. Added 2026-10-23, after the first run
   of the analyser.

Failing a precondition means "not yet", which is a result rather than a delay.

## Amendment, 2026-10-23: a size requirement for the control

The original four preconditions set sizes for the cascade arm and none for
the control, although the control is what the primary number is measured
against. The first analyser run exposed that: the control sampler had been
live for forty minutes, so a difference was computed and an interval printed
against roughly three control entries, and the table displayed `n` for
cascades only. At 60s it read `+0.086% [+0.032%, +0.142%]`, entirely above
zero and indistinguishable in presentation from a real result.

This is recorded here because the plan requires any change to be dated and
explained. It differs from the memecoin crux amendment in direction: that one
loosened a kill criterion after seeing data, in the direction favouring the
strategy. This one adds a hurdle the plan omitted. It can only make a
candidate harder to declare, never easier, and the number was fixed at the
cascade arm's own 100 rather than chosen from what the data would clear.

The control sampler itself was missing entirely until 2026-10-22 and is not
an amendment but a defect: `docs/LIQUIDATION_PLAN.md` always required it,
`follow_up` was simply never called for anything but cascades. The first 33
hours of collection therefore have cascades and no control, and cannot be
repaired, since no price history is stored. Any verdict must come from data
collected after the control exists.

## What this will not answer

- Whether it works for someone co-located with lower fees.
- Whether a maker entry during a cascade would fill.
- Anything about symbols outside those collected.
- Whether the effect persists after it is traded.
