# Integrity audit — Phase 1 collector and paper trading

Run on the live database, 2026-09-27, before any strategy work.
Scope: is the data real, and is the P&L arithmetic honest? Not: is any
strategy good. Deliberately no strategy parameter was changed, and no
strategy is called profitable or unprofitable anywhere in this document.

Read this with the headline first: **the accounting is correct, the sample
is not representative, and the two are separate problems.**

---

## A. What is definitely correct

Verified by tracing individual rows end to end, not by reading code.

**Trade arithmetic reconciles exactly.** All four positions on token 415 were
recomputed by hand from stored fields. Example, `wait_5m`:

| Field | Value |
|---|---|
| entry | 10:48:11, token age 309s, price 1.856e-05 |
| stake | $100.00 |
| exit checks during hold | 3, all sellable |
| exit | price 2.896e-06, reason `stop_loss` |
| gross | −$84.40 |
| costs | $1.32 |
| net | −$85.72 |

Gross + costs = net, to the cent, on every position traced. Capital, fees and
slippage are each counted exactly once. No double-charging, no missing leg.

**No look-ahead anywhere.** Zero observations recorded before their token's
detection. Zero exit simulations before detection. A position cannot be priced
at a moment we had not yet seen.

**No duplicate or phantom data.** Zero duplicate tokens. Zero duplicate
observations. Median 64 observations per token — real density, not a handful of
points stretched into a chart.

**Exit failures are correctly separated from our own failures.** 6,503 exit
simulations:

| Verdict | Count | Share |
|---|---|---|
| sellable | 4,322 | 66.5% |
| no route (the finding) | 1,713 | 26.3% |
| unknown — we couldn't ask | 468 | 7.2% |

The 468 unknowns break down as 462 transport errors and 6 malformed requests of
ours. **Not one row is filed as "this token could not be sold" because of a
network or API failure on our side.** That check matters more than any other in
this audit: the 26.3% is the project's central measurement, and a single leaked
timeout would inflate it.

**Downtime is recorded, not silently absorbed.** 71 collection gaps, all closed.
Absence of launches in the data never has to be guessed apart from absence of
the collector.

**No private key exists anywhere in the system.** Exit simulation is
`quote` only — 6,503 quote calls, zero signing, zero submission. There is no
code path that can move funds, because there is no key to move them with.

---

## B. What is currently unreliable

**The sample over-represents one launchpad. This is the most serious finding.**

12,307 detections, 9,854 resolved, 2,446 abandoned (19.9%). But the abandonment
is not random:

| Launchpad | Detections given up |
|---|---|
| meteora-dbc | 4.2% |
| pumpfun-bonding-curve | 17.7% |
| pumpswap-amm | 22.0% |
| **raydium-launchlab** | **51.8%** |

A 48-point spread. Half of all Raydium LaunchLab launches never entered the
dataset. **This is not survivorship bias in the market's favour — it is our
resolver silently choosing which launchpad to study.** Root-caused during this
audit (see C) and fixed, but every token collected before the fix carries the
skew. Any statement about "Solana memecoins" drawn from the current data is
really a statement about pump.fun.

**Costs on 14 closed positions are impossible.** Worst case, position 35
(`early_200`): **$3,505,068.57 of costs on a $100 stake.** Root cause found: a
quote returned a price-impact figure far above 100% and nothing clamped it.
Fixed previously; these 14 rows are pre-fix residue and must be excluded, not
interpreted.

**551 closed positions were opened before entry-leg price impact was charged.**
Their costs are understated — they only paid impact on the way out. The bias
runs in the flattering direction, which is the direction that matters.

**The `no_route` count cannot be fully cleaned retrospectively.** The old code
filed HTTP 400s from our own malformed requests as `no_route`. Post-fix, only 6
such requests occurred in 6,503 calls, which suggests the historical
contamination is small — but it is *invisible* to the audit's check, because
those legacy rows are indistinguishable from genuine no-routes. I cannot put a
number on it. Treat 26.3% as an upper bound for the pre-fix period.

**Liquidity is tested by routing only.** Zero `rpc_sim` rows. We prove a route
exists and what it would pay; we do not prove the transaction would land. A
freeze authority activated after launch, or a transfer hook that rejects sells,
would pass our check and fail in reality. This is a known gap, not a bug.

**18 price jumps above 1000× across 400 tokens.** Plausible for this asset
class, but unverified — I have not confirmed these against an independent source
and will not claim they are real.

---

## C. Bugs found and fixed during this audit

**1. A sale could be authorised by a check taken before the entry.**
Found by tracing, not by reading code. Three of the four traced positions
closed with *zero* exit checks during the hold. `quick_50` on token 415:

```
entry   10:48:11   price 5.952e-05
exit checks while held: 0
exit    10:48:31   price 2.267e-05   reason stop_loss
```

Twenty seconds. The sale was authorised by the same liquidity verdict that let
the position *enter* — one taken before a 62% collapse. **A liquidity check from
before the crash is not evidence you could sell after it**, and this is exactly
the optimism the project exists to avoid. A sale now requires a verdict strictly
later than the entry. Positions held back by the new rule are attributed to our
sampling rate (`stale`), never to the market (`no_route`), so the 26.3% headline
does not absorb our own slowness.

**2. Mint resolution favoured one launchpad.** The resolver read the new mint
only from `postTokenBalances`, which lists mints a token account held a balance
of after the transaction. pump.fun mints its whole supply to the bonding curve
in the creation transaction, so its mints appeared. Raydium LaunchLab does not,
so they did not — and the detection was thrown away. That is the mechanism
behind the 51.8% vs 17.7% gap in B. The resolver now also reads the mint
directly off `initializeMint` / `initializeMint2`, including inner instructions,
where launchpads actually create it.

Neither bug was cosmetic: the first inflated returns, the second decided which
tokens we were allowed to learn from.

---

## D. Tests added

8 new tests, 196 passing, linter clean.

| Guards against | Test |
|---|---|
| Selling on a pre-entry verdict | `test_sale_needs_a_verdict_from_during_the_hold` |
| Over-correcting into a deadlock | `test_sale_proceeds_on_a_verdict_from_inside_the_hold` |
| pump.fun-shaped transactions | `test_pumpfun_shape_resolves_from_balances` |
| LaunchLab-shaped transactions | `test_launchlab_shape_resolves_from_the_initialize_instruction` |
| Top-level mint creation | `test_top_level_initialize_mint_is_read_too` |
| Counting wrapped SOL as a launch | `test_wrapped_sol_is_never_mistaken_for_a_launch` |
| Inventing a mint from a create-like log | `test_a_transaction_that_created_nothing_yields_nothing` |
| Double-counting a mint found twice | `test_both_routes_agree_rather_than_duplicating` |

The audit script itself was validated against 5 deliberately planted defects
before being trusted on real data.

---

## E. Can paper P&L be trusted right now?

**Arithmetically yes. As evidence about memecoin trading, no — not yet.**

Every number reconciles. Nothing is fabricated. But three things stand between
the current dataset and a conclusion:

1. Positions opened before this audit's first fix could close on a stale
   liquidity verdict, which flatters exits.
2. 551 positions underpaid costs; 14 have impossible costs.
3. The sample itself is skewed towards pump.fun by our own resolver.

(1) and (2) bias results optimistically. (3) means the population under study
is not the population claimed. **Data collected from commit `0f9c5b2` onward is
trustworthy. Everything before it is diagnostic only.**

Per the terms of this audit, no strategy is declared profitable or unprofitable
here. The honest statement is narrower and, I think, more useful: *the dataset
is not yet capable of supporting either verdict.*

---

## F. What must be fixed before any strategy evaluation

Ordered by how much each one would invalidate a conclusion.

**1. Accumulate a clean post-fix sample.** Non-negotiable. Both fixes change
which positions open, when they close, and which launchpads are represented, so
pre-fix and post-fix rows cannot be pooled. This costs calendar time, not
engineering.

**2. Confirm the launchpad bias actually closed.** The fix is tested against
transaction fixtures; it is not yet proven against live LaunchLab launches. Re-run
the per-launchpad abandonment table in a few hours. If LaunchLab has not fallen
from 51.8% towards pump.fun's 17.7%, there is a second cause I have not found.

**3. Wire in RPC simulation.** `simulate_sell_rpc` exists and has never been
called. Until it is, "sellable" means "routable", and the specific failure this
project exists to detect — a token you can quote but cannot exit — is outside
what we measure.

**4. Quarantine the contaminated rows explicitly**, rather than relying on the
analysis script's `--since` filter to remember. 14 impossible-cost positions and
551 understated-cost positions should be flagged in the database itself.

**5. `clean_deployer` still has 0 positions.** It needs deployers with prior
launch history, which at the current sampling rate is days away. It remains the
only untested hypothesis; the others are resolved or dead.

---

## What the audit did not find

No fabricated data. No look-ahead. No double-counted capital. No missing fees.
No key material. No path by which a strategy could bypass the sellability
check. The defects found were optimism and sampling bias — which is the
expected failure mode for this kind of system, and the reason for auditing
before optimising rather than after.
