# Memecoin exit experiment — pre-registered 2026-10-04

Written before the data exists, so the criteria cannot be adjusted to fit
whatever comes back. Any later change to this file must be dated and must say
what prompted it.

Everything below rests on measurements already in `docs/FINDINGS.md`. Nothing
here is a new theory about which coins go up.

---

## The one-line hypothesis

The bot does not lose because it picks bad coins. It loses because **winners are
capped at +200% while losers run to −77%**, and both halves of that are caused
by the same thing: by the time it acts, there is often nothing left to sell
into. Trading far smaller should buy back enough exit availability to make the
existing 16% win rate sufficient.

## Why that is the hypothesis and not something else

Decomposing the measured loss reproduces it almost exactly, which means the
mechanism is understood rather than guessed:

| input | measured | source |
|---|---|---|
| win rate | 16% (11 of 69) | replay, `control_any` |
| a win pays | +200% | the take-profit cap |
| a loss costs | −77% median | 227 closed stop-loss positions |
| costs | ~10% round trip | fees plus impact both legs |

Expectancy −42.8% against an observed −43.5%.

Changing one input at a time:

| change | expectancy |
|---|---|
| nothing | −42.8% |
| **double the win rate to 32%** | **+1.6%** |
| stop truly holds at −50% | −20.1% |
| **stop truly holds at −15%** | **+9.3%** |
| **wins pay +500%** | **+5.0%** |

Doubling the win rate is the **weakest** lever, which is consistent with every
filter tested having failed. The break-evens say the same thing:

```
wins at +200%  ->  need 31% win rate   (have 16%)   fails
wins at +500%  ->  need 15% win rate   (have 16%)   marginal
wins at +900%  ->  need  9% win rate   (have 16%)   works
```

**The existing win rate is already sufficient if winners are allowed to run.**

## The change being tested

One coherent change, not three independent knobs:

1. **Position size $10**, not $100. The size probe measured route availability of
   47% at $100 against 78% at $5, and `order_too_big_for_pool` was the single
   largest entry rejection at 28,770 observations.
2. **Stop at −10%, −15% or −20%.** All three run as separate arms. The measured
   gain from −50% to −10% was +6.2pp, so the direction is established and the
   level is not.
3. **No take-profit.** A six-hour time stop ends the position instead.

Entry rules are `control_any` — unfiltered — because every filter tested lost to
it. This experiment is about exits and nothing else.

## Arms

Identical but for the stated difference, so any gap is attributable.

| arm | size | stop | take-profit | purpose |
|---|---|---|---|---|
| `ctl_100_tp` | $100 | −50% | +200% | today's bot, the control |
| `ctl_10_tp` | $10 | −50% | +200% | isolates SIZE alone |
| `run_10_s10` | $10 | −10% | none | the hypothesis |
| `run_10_s15` | $10 | −15% | none | the hypothesis |
| `run_10_s20` | $10 | −20% | none | the hypothesis |
| `run_100_s15` | $100 | −15% | none | isolates the stop alone |

`ctl_10_tp` versus `ctl_100_tp` measures size. `run_100_s15` versus
`run_10_s15` measures size again under the new exit. Both comparisons must agree
or the size claim is not established.

## Preconditions — no verdict without all four

1. **≥150 closed positions per arm.** Below that, two identical distributions
   differ by 9.5pp from chance alone, as the sniper test demonstrated.
2. **≥20 chain-state verifications** with a quote false-positive rate under 10%.
3. **Raydium abandonment within 15 points of pump.fun's.** Currently 53.4% vs
   17.8%; the sample is not representative until that closes.
4. **Zero overdue work items** at the end of the window.

Failing any of these means "not yet", which is a result rather than a delay.

## Decision rules, fixed now

**Primary:** mean net return per position for the best `run_*` arm, with a 95%
interval, paired against `ctl_100_tp` on the same tokens.

| outcome | verdict |
|---|---|
| interval entirely above 0 AND above the lending rate | **candidate** |
| interval entirely above `ctl_100_tp` but below 0 | loses less; still a loss |
| interval spans 0 | **unproven** — no further tuning |
| interval entirely below 0 | **dead** |

**The kill criterion, and it is the important one.** The whole case rests on a
fat tail. If across the full window **no position in any `run_*` arm exceeds
+300%**, the premise is false and the experiment is over regardless of the mean.
A strategy that needs a 5x winner and never sees one is not unlucky, it is
wrong.

**Secondary, and necessary:** the realised stop exit must land near where the
stop is set. `stop_reality.py` must show the `run_10_s15` median above x0.70. If
tight stops still execute at −77%, size did not buy exit availability and the
mechanism is refuted even if the mean happens to look acceptable.

## What I expect to go wrong

Stated in advance so a null result is not reinterpreted afterwards.

- **The tail may not exist in reach.** Removing the cap already tested worse
  (−60% vs −43%) because that window held no runners. If the +500% outcomes only
  happen to people entering in the first block, our seconds-late detection never
  touches them.
- **$10 may be below the floor where fees matter.** Solana gas is small but the
  ~1% round trip is proportionally identical, and a $10 position that must
  survive several hours is exposed for longer.
- **Smaller size cannot fix a pool with nothing in it.** The probe found impacts
  that are bimodal — a few percent, or 86–100%. On the second group no size
  helps.

## What I will not do

- Not tune the stop after seeing results. The three levels run together and are
  reported together; picking the winner afterwards is how the Faber +2,271%
  was manufactured.
- Not add entry filters mid-experiment. This tests exits.
- Not extend the window to reach a nicer number. The window ends when the
  preconditions are met.
- Not drop the control arms. Without `ctl_100_tp` on the same tokens there is
  nothing to attribute a difference to.

## Cost of running it

Roughly two weeks of collection at a 0.10 sampling rate. Trading at $10 should
admit substantially more tokens, since the pool-size gate was the largest
rejection, so this may arrive sooner.

Zero money at risk. Paper only, no wallet, no keys.
