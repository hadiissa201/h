# Everything wrong with this bot — an unsparing list (2026-10-04)

Written because the goal has not changed: make it profitable. This is the list
of flaws, traps and holes we have fallen through, including the ones I caused.
Nothing here is softened to make the project look better.

---

## 1. The deepest problem, stated plainly

**We are the exit liquidity.**

Every trade needs someone on the other side. On a pump.fun launch the money
flows from later buyers to the deployer and the first few wallets. We detect a
token *seconds to minutes* after it exists, which puts us structurally in the
"later buyer" group. For us to profit, someone must buy our position at a higher
price — and the people best placed to do that are arriving even later than we
are, into a token that is already decaying.

We never wrote this down until now, and it should have been the first paragraph
of the project. Every strategy we tested implicitly assumed a greater fool
arrives after us. We never checked whether one does.

**What would test it:** measure, per token, how much buy volume arrives AFTER
our entry versus before. If the answer is "almost none", the game is unwinnable
from our position regardless of rules.

## 2. The measurement we never made, and should make first

**The oracle bound.** With perfect foresight — buy the exact bottom, sell the
exact top, but still constrained by our measured sellability — what is the best
possible return across the tokens we tracked?

If a perfect trader cannot make money under our exit constraints, no strategy
can, and everything else is wasted effort. If a perfect trader makes 400%, then
the gap between 400% and our −43% is the part skill could close.

This is cheap (the data exists), decisive, and we never ran it. It should come
before the pre-registered experiment, because it can make that experiment
unnecessary.

## 3. Latency: we are not sniping, we are arriving

| stage | our delay |
|---|---|
| chain → our websocket | ~1s |
| detection → mint resolved | seconds to minutes (6 retries) |
| mint → first DexScreener price | often minutes, sometimes never |
| price → paper entry decision | up to the observation interval |

Traced entries happened at token ages of **50s, 178s, 307s and 1,741s**. A real
sniper bot is in the same block as the launch. We are 2–3 orders of magnitude
behind, competing for the same tokens.

**And a cruel detail:** we only see a price once DexScreener indexes the pool. A
token that pumps and dies inside that window is invisible to us — we may be
systematically blind to the exact events the strategy needs.

## 4. Flaws in what we built

- **We never read the pool directly.** All prices come from DexScreener, which
  lags the chain and omits new pools. Reading the bonding curve or AMM reserves
  on-chain would be faster, more accurate, and would work for tokens no
  aggregator has indexed yet.
- **`liquidity_events` has zero rows.** The table meant to record liquidity
  being added or pulled — literally how a rug happens — was never populated. We
  can see the aftermath of a rug and never the act. This is probably the single
  most valuable signal we are not collecting.
- **We never quote the BUY side.** Entry impact is inferred from a sell quote.
  Buying into a thin pool has its own cost and its own failure mode, and we
  model neither.
- **Observation cadence is far too slow at the start.** 60s intervals in the
  first five minutes, when these tokens live and die in minutes. We are sampling
  a process at a fraction of its own frequency.
- **Paper execution is instantaneous.** The decision price and the fill price
  are the same number. In reality there is a gap, and on a collapsing token the
  gap is the whole loss.
- **No MEV, no priority fees, no failed transactions.** Real entry into a hot
  launch means bidding for position and sometimes losing the race while still
  paying.
- **We enter 7% of what we see** (77 of 1,098). Whatever we have measured, it is
  a measurement of that 7%, not of memecoins.

## 5. Flaws in how we measured

- **Sellability is sampled, not continuous.** "Stuck" partly measures our own
  cadence. A position that was briefly sellable between two checks is recorded
  as never sellable.
- **The abandoned-detection bias is still open.** Raydium LaunchLab 53.4% versus
  pump.fun 17.8%. The tracked sample over-represents whatever resolves easily,
  and we have not closed that gap.
- **Delisted and dead tokens vanish from the exit data.** A token nobody quotes
  any more produces no rows, so the dataset quietly tilts toward survivors.

## 6. My own mistakes, which cost real time

- **Five defects in one script in one morning** — the loss floor reported as a
  return, a verdict contradicting its own table, our HTTP failures printed as
  the market refusing, rate limits eating the sample, and a missing price impact
  recorded as zero. **Every single one resolved an unknown in the flattering
  direction.** That is not coincidence; it is what optimism does to code when
  nobody writes the test.
- **I shipped that script with no tests at all.** The tests came after you found
  the bugs by running it.
- **Three wrong diagnoses in a row** on the work-queue backlog — saturated
  workers, then exhausted rate budget, then a retry herd. Only the diagnostic
  tool settled it, and I built that tool third rather than first.
- **I tested entry filters for a week** when a ten-line expectancy calculation
  would have shown on day one that the win rate is the weakest lever and the
  exit is everything. We had the numbers the whole time.
- **I let you restart the collector on stale code for three days** because I
  said "restart it" instead of "pull, then restart".

## 7. Traps that are easy to fall into again

- **Survivorship in any symbol list.** The Faber test returned +2,271% and
  reversed completely on a different basket. Any future test must pick assets by
  a rule available at the time.
- **Choosing a parameter after seeing the result.** The low-vol window is
  unflattering; shortening the lookback would help; doing so would manufacture
  an edge.
- **Reading a point estimate without its interval.** Two identical distributions
  differed by 9.5pp purely by chance at our sample sizes.
- **Believing a backtest that cannot model the thing that kills you.** Our
  backtest cannot model a one-block rug, MEV, or a failed transaction.

## 8. What we have genuinely never tested

Ordered by how much I would expect from each.

1. **Graduated tokens only.** A pump.fun token that migrates to Raydium has
   survived its riskiest phase and has a real pool. Smaller universe, far better
   liquidity, and the 26% no-route problem may largely disappear. **We have the
   migration events in the data already.**
2. **Copy-trading wallets with measured profit.** The idea from the screenshot
   you sent — rank wallets by realised PnL, follow the ones that actually make
   money. This is the only approach that does not require us to predict price;
   it outsources the prediction to someone with a track record. Needs
   wallet-level PnL, which we do not yet collect.
3. **Survivor momentum.** Tokens still alive after 24 hours, which is already a
   severe filter. Completely untested — our max tracking age for entry is 30
   minutes.
4. **Selling the first pump instead of holding for a multiple.** Hold for
   seconds to minutes, take 10–20%, exit while liquidity is at its peak. The
   opposite of everything we tested.
5. **Being the maker, not the taker.** Every trade we modelled pays spread and
   impact in both directions. Providing liquidity instead of consuming it
   inverts that sign.

## 9. The uncomfortable question worth asking once

The strategies that keep failing all share one assumption: that a rule applied
to public data can predict short-horizon crypto prices well enough to overcome
costs. Thousands of better-funded people are testing the same assumption with
faster infrastructure.

That does not make it impossible. It does mean that **if an edge exists for us,
it is far more likely to come from a structural advantage than from a cleverer
rule** — being faster, seeing something others do not, or being paid for
something other than direction.

Of the five untested ideas above, two are rule-based (3 and 4) and three are
structural (1, 2 and 5). The structural ones deserve the time.

---

## What I would do next, in order

1. **The oracle bound** (section 2). Cheap, decisive, may end the question.
2. **Graduated tokens only** (section 8.1). The data exists.
3. **Start collecting wallet-level PnL** (section 8.2). Nothing can be tested
   there until the data exists, so the collection should start now even if the
   analysis comes later.
4. The pre-registered exit experiment, if 1 and 2 leave it worth running.
