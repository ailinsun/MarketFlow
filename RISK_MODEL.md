# Risk model

What each number means, how it is computed, and — the part that matters more — what it
does not establish.

## 1. Exposure: what is genuinely at risk

**The claim.** The sum of what you paid for a set of positions is not what those
positions can lose.

**The computation.** Legs are bucketed by event. A bucket whose legs are mutually
exclusive is enumerated exactly: for a group of *n* legs there are at most *n + 1*
settlement states, and each one's portfolio P&L is arithmetic, not simulation.
Exposure is the worst state. A bucket whose legs are not mutually exclusive takes the
bound where every leg loses together, and the output field is named `all_lose_bound`
so no reader mistakes a bound for a prediction.

State probabilities come from each leg's implied probability, with the remainder
assigned to the "outside" state. When a group is held in full, the outside state is
physically impossible and is dropped. When any leg has no price, the whole bucket falls
back to uniform and says so.

**What it establishes.** Inside a mutually exclusive bucket the number is exact, given
the settlement rules. At portfolio level it is the sum of each bucket's worst case,
which assumes every bucket bottoms out simultaneously — an upper bound, far tighter
than the book, and honestly labelled.

**What it does not establish.** Nothing about probability of loss. It is a worst case,
not an expectation. It also trusts the venue's mutual-exclusivity flag; if that flag
is wrong, the enumeration is wrong, which is why `complete_sets` exists as a separate
check on the same structure from a different direction.

## 2. Correlation: three sources, deliberately not merged

Correlation enters portfolio risk from three places, and they are kept apart because
they have different epistemic status. Merging them would make an assumption
indistinguishable from a measurement.

| Source | Status | Default |
|---|---|---|
| Within a mutually exclusive group | **computed** from structure and implied probabilities | not a parameter |
| Several non-exclusive legs on one event | **assumed** | 1.0, the conservative end |
| Across events, same theme | **measured**, and only if a pre-registered test confirms it | 0.0 |

The thematic layer has a falsifiable test attached: residual correlation with a
date-stratified permutation null, a cluster-level bootstrap, and a positive control
that must reproduce the known-negative sign inside exclusive groups before any of the
other numbers are read at all. Residuals are mean-centred first, because a book with a
systematic edge would otherwise show every cluster as positively correlated — that
measures the edge, not correlation.

The standing verdict is that cross-event thematic correlation is statistically
detectable in a whole-market sample and too small to use, and undetectable in a single
book. So the engine uses **zero**. A criterion that says zero means zero; the value of
the test is that it can say so rather than producing a small number that looks like
information.

**Effective independent bets** is `(Σw)² / (w'Rw)`. With all correlations at zero it
degenerates to the inverse Herfindahl index — pure size concentration. Positive
correlation lowers it. The negative correlation inside an exclusive group raises it,
and that is real structural diversification rather than an accounting trick.

## 3. Sizing: three haircuts, one failure mode

A raw model probability is never sized on. In order:

1. **Shrink toward the market.** The market price is an aggregate of everyone else's
   view, and a model that disagrees with it by a lot is more often wrong than early.
2. **Discount by the estimate's own uncertainty.** A probability with a wide standard
   deviation is taken down by it, so confidence must be earned before it is spent.
3. **A fraction of Kelly** on what survives, then per-market and per-cluster caps.

The demonstration makes the effect concrete: a 25-point raw edge becomes 1.81% of
bankroll where full Kelly would ask for 46.2%. A 13-point edge does not survive at all.

**What this is not.** It is not an optimal policy. Full Kelly maximises long-run growth
only if the probability is right; every haircut here is a statement that it is not
known to be. The fractions are choices, documented as choices.

## 4. Budget: capital-relative by construction

No limit in the system is a dollar constant. Every cap is a fraction of a declared
capital base:

| Limit | Default fraction | At the default $1M base |
|---|---:|---:|
| Total deployment | 20% | $200,000 |
| Per trade | 0.5% | $5,000 |
| Drawdown fuse, per budget epoch | 2% | $20,000 |
| Operator ceiling, per trade | 5% | $50,000 |

Mandate tiers layer on top: `standard` allows half the mandate deployed with 2% per
trade and a 10% drawdown fuse; `professional` allows the full mandate, 5% and 20%.

**These defaults are illustrative.** They are not an industry standard, not a
recommendation, and not derived from any regulatory framework. They exist so the
system has coherent behaviour out of the box and so the tests can assert scale
invariance. A deployment sets its own from its own mandate, and the ceilings are
fractions too, so they remain ceilings at any size.

**Tested property:** the tests assert relationships, not dollar amounts. Change the
capital base and every assertion still holds, which is what makes the claim "this
scales" checkable rather than aspirational.

## 5. Fees and the probability band

The band gate has two ends, and each comes from a different kind of reason. Keeping
them apart is the point of this section: one is arithmetic, the other is a measured
behavioural fact, and a third — the high end — is a risk-shape choice.

**What the fee schedule does (arithmetic).** The prevailing schedule charges
`rate * p * (1 - p)` per share, which in the units that matter for a decision is
`rate * (1 - p)` per dollar deployed:

- In *absolute* terms the fee is largest at p = 0.5 and goes to zero at both ends.
- In *relative* terms — fee per dollar staked — it is largest as p approaches 0 and
  falls monotonically as p rises. Near-certain tickets are the cheapest in the market
  to trade, which is why a dollar-volume ranking can be manufactured almost for free
  (section 2 above, and the farm filter).

`instruments/fee_geometry.py` recomputes both curves in one command. Nothing in this
section claims the fee curve alone explains the price bands used below.

**The low end (measured).** In the frozen ledger, takers buying below 10 cents lost
about half their notional *before* fees (−54.6% for 0–5 cents, −47.8% for 5–10 cents),
and turns roughly flat only above 20 cents. That is the favourite-longshot bias — cheap
contracts are overpriced — not a fee effect: the fee in that band was under 5% of
notional. The band's low end encodes that measurement, and it is why the ledger's fee
column must never be read as the explanation for the loss.

**The high end (a risk-shape choice, not a finding).** Above 0.85 the most a position
can gain is 15 cents per dollar staked while a single adverse resolution loses the
stake, so a small probability error erases the trade. This is a default, not an edge
result — in the same ledger, takers buying between 0.80 and 0.98 were *positive* gross
— which is exactly why it is configurable rather than fixed.

**What the system does not claim.** Not that the band is derived from fee geometry. Not
that a low price is dangerous *because of fees*. The band is a hard filter; the
after-cost edge test beside it is advisory and belongs to the sizer.

## 6. Settlement: a risk, not a formality

7.12% of settled disputes changed the proposal. That is a historical conditional rate
over the observed window — not a forecast for any individual contract, and not a
probability that a given proposal will be disputed.

The system treats settlement as a gate rather than an afterthought: `risk.resolution`
refuses contracts that do not settle on an objective fact, and `monitor.settlement_guard`
reads the oracle's proposal state directly on chain rather than trusting a metadata
field that lags it.

## 7. What the risk model does not cover

- **Venue or counterparty failure.** If the venue halts, freezes withdrawals, or
  resolves against its own published rules, nothing here helps.
- **Liquidity beyond top of book** in most paths. `complete_sets` sweeps real depth;
  the general exposure path does not, and assumes the position can be exited at a price
  near the mark, which is the assumption that fails exactly when it matters.
- **Correlated settlement timing.** Many contracts settling the same day is modelled
  as bucket independence, which it is not.
- **Regime change in fee schedules.** The calibration constants are historical, and
  venues change fees.
- **Model risk in the probability itself.** The haircuts assume the estimate is
  noisy. They do not save a probability that is biased.

[LIMITATIONS.md](LIMITATIONS.md) has the rest, including what the published
measurements do and do not establish.
