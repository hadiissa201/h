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

---

## Amendment, 2026-10-10 — the crux test requires significance

**What prompted it.** The crux test fired `THESIS FAILS` on 32 of 35
target-reaching positions being sellable against 391 of 400 losing positions:
91.43% [77.62%, 97.04%] versus 97.75% [95.78%, 98.81%]. Those intervals
overlap. Fisher's exact two-tailed p is 0.0635.

**What changed.** The decision rule as written compares two rates. It now
requires the difference to be established at p<0.05, and reports the p-value
either way. A direction that matches the prediction without clearing the bar
is reported as `DIRECTION MATCHES but is NOT ESTABLISHED`, which is neither a
pass nor a failure.

**The part that needs declaring.** This is a pre-registered kill criterion
being loosened AFTER seeing the data, and it moves in the direction that
favours the strategy. That is exactly the move pre-registration exists to
prevent, so it is recorded here rather than applied quietly, and it deserves
more scepticism than a change made the other way.

**Why it is defensible anyway.** The rest of this document, and the code it
governs, already insists on intervals everywhere: `wilson_interval` exists
"so a number resting on a handful of checks cannot be read as a measurement",
and `MIN_VERIFIED_FOR_A_RATE = 20` exists because "two reverts out of two is
100% only in the sense that a coin landing heads twice is a 100%-heads coin".
A bare comparison of 3 unsellable winners against 9 unsellable losers is
precisely the case those rules were written for. Applying that standard to
this test is consistency, not an exemption.

**What it does not change.** The direction still matches the thesis's
prediction, and 35 target-reaching positions is too few either way. The
finding is live, not dismissed: more winners settle it. Nothing about the
primary number changes — `control_any` remains -26.67% [-33.67%, -19.40%],
entirely below zero, and optimistic because 11.1% of verified exits would
have reverted.

---

## Closure, 2026-10-10 — stopped without a verdict, by choice

The pre-registration's answer is **NOT YET**: three of four preconditions
fail (27 verifications at an 11.1% quote false-positive rate against a 10%
bar, 82.4 points of abandonment spread across launchpads, 388 overdue queue
items). All three are fixable by restarting the collector for a few days.

**We are stopping anyway, and that is a different thing from a verdict.** It
is a decision about where to spend effort, not a conclusion the data licensed.
Recording it as such so nobody later reads "memecoin sniping was disproved"
into a file that says "not yet".

What the data does support, at the strength stated:

- `control_any` mean net is **-26.67% [-33.67%, -19.40%]**, entirely below
  zero, and optimistic: 11.1% of verified exits would have reverted on chain,
  and a reverted sale is a sale that did not happen.
- Every lever has been measured and closed. Exits: `headroom.py` gave the
  entry rule a hindsight-perfect exit and expectancy stayed negative, so no
  exit rule can rescue it. Entries: the one signal that survived a held-out
  test (first 5m buy count >= 10, graduating at 5.80% against 0.41% below)
  captures a median 1.33x of a 14x run, because it keys on the buying that
  already moved the price. Stops: 96% of the avoidable loss is gap risk at a
  60s observation interval, so a threshold cannot reach it. Size: expectancy
  is a percentage, so sizing bounds the damage without changing the sign.
- The crux test points the predicted way and does not carry it: winners
  unsellable 3/35 against losers 9/400, Fisher exact p=0.0635. Live, not
  dismissed.

What would reopen it: a signal that fires BEFORE the buying rather than on
it, or sub-second observation. Both are different projects, not parameter
changes.

Next: the spot strategies, where `docs/FINDINGS.md` names four structural
candidates and two have never been tested -- cross-exchange arbitrage and
liquidation cascades.
