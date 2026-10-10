
## The early-buys entry signal: real, confirmed out of sample, and not tradable

A rule was found, held up on data it had never seen, and still loses money.
Recording it in that order because the first two steps are the ones that
usually get mistaken for the third.

**The signal is real.** Tokens whose first 5-minute buy count is at least 10
graduate from the pump.fun curve at 5.80% [3.35, 9.86], against 0.41% [0.11,
1.49] below the threshold. A fourteen-fold lift. The threshold was fitted on
tokens detected in the earlier half of the sample and measured on the later
half, split by time so a period's own conditions could not leak across; the
intervals do not overlap in either half.

**Graduation is labelled from the chain, not inferred.** 1,416 of 1,764
collected tokens have a pump.fun bonding curve; 38 of those have completed.
2.68% [1.96, 3.66]. The curve's own `complete` flag agreed with zeroed
reserves on all 1,416 curves, with no exceptions. An earlier figure of 1.14%
came from the pool table and was censored: 88 tokens had a pumpswap pool with
no pump.fun pool recorded, so their curve phase was never seen at all.

**The rule captures almost none of the move.** Graduation carries a curve from
roughly $5k to $69k, about 14x, and at a 5.80% win rate against the measured
-77% median realised loss, break-even needs +1,251% per winner. Measured on
the 34 graduates the rule would actually have bought, entering at the price
the rule pays and exiting at the best price available afterwards with full
hindsight:

| | chart prices | verified sellable |
|---|---|---|
| median multiple from entry | 1.46x | 1.33x |
| best multiple | 217x | 142x |
| median net | +45.0% | +29.4% |
| mean net | +868.9% | +190.3% |
| cleared +1,251% | 2 of 34 | 1 of 34 |

**Expectancy per trade: -22% using the mean chart winner, -61% using the mean
sellable winner.** Both negative, and the exit was chosen with hindsight, so
no exit rule can beat them. By the time ten buys have landed in five minutes
the move has happened: the median entry captures 1.33x of a 14x run.

Three cautions on the numbers above, all of which cut against reading this as
a near miss:

- The mean is carried by one 217x outcome among 34. A strategy whose edge is
  one trade has not been measured.
- 5m buys, 5m volume and market cap at 300s are one signal, not three. They
  all measure "this token is already moving", and the three separations
  graduated.py reported are the same finding counted three times.
- Market cap at 300s is close to tautological as a predictor, since
  graduation IS crossing a market-cap threshold.

**What would change the conclusion.** Not a better exit; that avenue is
closed by construction. Only a higher win rate or a smaller loss per loser.
The loss side is where the room is: -77% median realised against a -50%
configured stop means the stop does not execute, and stop_10 already measured
-37.3% against stop_50's -43.5% with no whipsaw cost. The entry side needs a
signal that fires BEFORE the buying, not one that keys on it.

Two methodological errors found and corrected while producing this, both
noted because the pattern matters more than the instances: the replay printed
a mean net of -538% from treating a percentage as a fraction, and the
expectancy first used the median winner rather than the mean, which
understated a power-law payoff and so was unfair to the rule.
