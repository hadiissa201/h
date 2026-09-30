# What the data will decide, written before the data exists

Pre-registered 2026-09-30, at the start of the first clean collection run. The
point of writing it now is that I cannot then adjust the criteria to fit whatever
comes back. Any later change to this file must be dated and must say what
prompted it.

The question, in the form it was originally asked: **detect memecoins early, sell
at +200% to +1000% before the whales do — does that make money?**

---

## The primary number

Mean net return per position for `control_any`, with a 95% confidence interval,
on positions opened after 2026-09-30.

`control_any` buys anything it can prove is sellable, with no filter. It is the
base rate. If it is negative, every filtered strategy has to beat it just to
reach zero, and a filter that beats a losing baseline is still losing.

Three outcomes, decided in advance:

| Interval | Verdict | What follows |
|---|---|---|
| Entirely below 0 | Unprofitable | Stop. Do not fund. |
| Straddles 0 | Unproven | Keep collecting; no money either way |
| Entirely above 0 | Candidate | Go to the second test below |

## The second test, only if a strategy clears the first

A positive mean is not a tradable edge. It has to survive three things this
project does not yet model:

1. **Priority fees and MEV.** Entering a launch means competing with bots that
   pay to be ahead. Not measured here at all.
2. **Real latency.** Paper entries happen at an observed price. A live entry
   happens after detection, routing, signing and confirmation.
3. **Size.** Returns at $100 do not survive at $10,000 in these pools; the
   feasibility gate exists because most of these pools cannot absorb even $100.

So a positive result means "worth testing further", never "worth funding".

## The test that may matter more than the return

**Of positions that reached the profit target, what fraction could actually be
sold?**

This is the crux of the original idea. Selling before the whales requires being
able to sell at all. The pre-fix data already showed positions up 6.02x, 4.34x
and 3.03x that could not be exited, alongside one down to 0.03x that also could
not be exited.

If winners are systematically less exitable than losers, the strategy is
unworkable no matter what the average says, because the average is computed over
exits that were available and the unavailable ones are exactly the wins. I will
report exit availability split by outcome, not pooled.

Decision rule: **if positions at or above the profit target are sellable less
often than positions at a loss, the thesis fails** regardless of mean return.

## Preconditions — if these are not met, no verdict is issued

1. At least 100 closed positions per strategy compared.
2. At least 20 chain-state verifications returning a verdict, with a
   quote false-positive rate below 10%. Above that, paper exits are not
   trustworthy and the returns are overstated by an unknown amount.
3. Raydium LaunchLab abandonment within 15 points of pump.fun's. At 53.5%
   versus 18.7% the sample is not representative of Solana launches.
4. Zero overdue queue items, so nothing was dropped.

Failing a precondition means the answer is "not yet", and saying "not yet" is a
result, not a delay.

## What this will not answer

- Whether some untested strategy works. Absence of an edge in eight rule sets is
  not proof that none exists.
- Whether it works for anyone with faster infrastructure.
- Anything about tokens outside the four launchpads we watch.

## The prior, stated honestly

Pre-fix data put every strategy between −21% and −23%, with every 95% interval
entirely below zero. Seven bugs have been fixed since, and **every one of them
pushed results in the pessimistic direction** — they removed optimism rather
than adding profit. The base rate is also structurally against the trade: fees
and price impact are paid on both legs, and roughly a quarter of exit attempts
find no route.

The most likely outcome is therefore "unprofitable", and I would rather have
written that down beforehand than present it as a discovery afterwards.

Being wrong about this prior would be good news. It just is not the way to bet.
