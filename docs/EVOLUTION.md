# How MarketFlow arrived at this shape

This is the intellectual history, not the chronology. Each section is a thing that was
believed, the measurement that contradicted it, and the part of the system that exists
because of it. It is included because the architecture is otherwise hard to justify:
several components look like over-engineering until you know what they were built
after.

## 1. From "predict better" to "pay less"

**The starting belief.** Prediction markets reward better forecasts. Build a
forecasting engine, price the difference against the market, take the difference.

**What the measurement said.** In a selected sample of 439 settled markets and 1.34M
taker BUY fills, the buying side showed **+0.143% aggregate gross selection edge**
before fees. Modelled taker fees on the same notional were **1.488%**, about **10.4×**
that gross edge, leaving the taker side at **−1.345%** net. Whatever aggregate
selection edge the sample contains is smaller than the modelled friction by a wide
margin.

**What changed.** The target moved from the probability to the cost of expressing it.
The reframing does not rest on the sample being universal; it rests on the fact that
the friction a system controls is larger than the edge it is likely to find. Fee
geometry, the band gate, the markout instrument and the execution funnel all date from
that reframing.

**What it does not mean.** Not that forecasting is worthless or that no edge exists —
this sample cannot show that, and the sample is selected. Not that the 1.488% is venue
revenue: fees where a venue rebates part of them to makers are a transfer, and only the
fee burden as computed from each market's own schedule is modelled here. What it means
is narrower: *in this sample, a modest aggregate selection edge was overwhelmed by
modelled execution friction*, so the engineering went where the constraint appeared to
be.

## 2. A ranking key is a measurement decision

**The starting belief.** To find sophisticated participants, rank wallets by dollar
volume. Size indicates conviction.

**What the measurement said.** Of the top 5,000 wallets by dollar volume, 46.2% match
the repeated-size signature. Rank the same 144,532 wallets by trade count times market
breadth and none of the top 5,000 do — against a 5.9% baseline across the whole
population. Same wallets, same window, two reasonable-sounding ranking keys, and a
leaderboard that is almost half one thing or none of it depending on which you pick.

The cause is structural, not accidental: the fee curve `rate * p * (1 - p)` goes to
zero at both ends of the probability range, so buying near-certain tickets is nearly
free. A dollar figure can therefore be manufactured at almost no cost. Count times
breadth times survival cannot — every dimension requires real repeated behaviour over
time.

**What changed.** Ranking moved to count times breadth with lifetime and breadth
floors, and every downstream consumer of "activity" got an excluding variant. The
instrument's own notes record the extreme case it was built after — a market where 820
of 893 apparent traders matched the pattern, leaving 71 people — measured on a
per-fill tape that was not retained and is not distributed, so unlike the rank audit
above that figure cannot be recomputed here.

**What it does not mean.** The signature describes behaviour. It does not establish
identity, common control or intent, and the code says so everywhere it is used.

## 3. Mid prices are not prices

**The starting belief.** For a mutually exclusive group, if the mid prices sum to less
than one, the complete set is underpriced and the difference is free.

**What happened.** A multi-candidate election market showed a 3.70% underround on mids.
Checking real asks: 27 of its 34 legs were unquoted placeholders whose mid read as
zero. The seven real legs carried $1 to $237 of depth. The set could not be bought at
any meaningful size. The "opportunity" was an artefact of treating an absent quote as
a price of zero.

**What changed.** `marketflow/risk/complete_sets.py` exists solely to refuse that
shape: a set that is not demonstrably closed and not demonstrably buyable at a real
offer produces no candidate at all, and the executable size reported is the thinnest
leg's depth rather than the notional.

**The general lesson**, which recurs throughout: *an absent value is not a zero.* The
same failure appears as a missing fee rate read as free, a missing arm state read as
armed, and an unavailable filter read as "nothing to filter". All three now fail in the
tightening direction.

## 4. Counting legs is not conservative

**The starting belief.** Adding up what you paid for each position is a safe
overestimate of what you can lose.

**What is actually true.** For a mutually exclusive group it is not an overestimate of
anything meaningful — it is simply wrong, and wrong by a factor that grows with the
number of legs. Holding four NO legs on a five-way race, at most one can lose. Four
legs at $2,275 of cost each is $9,100 on the book and $1,600 at risk.

**What changed.** Exposure is computed by enumerating settlement states exactly rather
than by summing costs, and a bucket that cannot be enumerated takes an explicit
`all_lose_bound` that is labelled as a bound. A negative correlation concentration
became a reportable result rather than a suspected bug: exclusivity is structural
diversification.

## 5. The tools were measuring the tool, not the world

**The starting belief.** If a statistical test reports significance on the ledger, the
effect is there.

**What went wrong, twice.**

*Mean contamination.* A book with a systematic edge has a positive mean residual, so
the expected product of any two residuals carries that mean squared — and every
cluster looks positively correlated. That measures the edge, not correlation.
Residuals are now mean-centred before any correlation is estimated.

*Resampling bias.* A cluster-level bootstrap that keeps original labels draws the same
cluster twice and puts two identical copies of the residuals into one cluster,
manufacturing perfect self-correlation. Its fingerprint is a point estimate falling
outside its own confidence interval — which is exactly what appeared. Clusters are now
relabelled on every draw.

**What changed.** Every correlation claim now carries a **positive control**: a
relationship whose sign is known from structure must reproduce before any measured
number is read at all. If the control fails, every verdict in that run is
`INSTRUMENT_UNVALIDATED` rather than merely negative. The result is that the standing
NULL on cross-event thematic correlation is a statement about the world rather than
about the tool — which is the only thing that makes a NULL worth reporting.

The general form is a repeated lesson: **it is easier to measure your own apparatus
than the market.** `instruments/n_eff_estimator.py` belongs to the same family — it
answers how much independent evidence a correlated series actually contains, before a
test assumes each observation is one.

## 6. Fail-soft hides what it fails at

**The starting belief.** A price lookup that cannot reach the venue should fall back
quietly. A quote should never crash a tick.

**What went wrong.** Two such reads were still live during the offline self-tests. They
did not fail the tests, because they were fail-soft: the network call happened, the
exception was caught, the fallback ran, the test passed. The suite looked offline and
was not, and would have started failing the day the venue changed an endpoint.

**What changed.** Both reads go through injectable seams, the test runner installs an
audit hook that records a socket attempt *even when it is caught*, and the daemon's
self-test now asserts that it opened no connection. The property being defended is not
"the code handles failure" — it already did — but "the test proves what it claims to".

## 7. Dollar limits are a statement about one book

**The starting belief.** Set the caps to what the account can afford.

**The problem.** Hard-coded dollar caps encode one particular balance. They make the
system unusable by anyone else without editing the source, and they leak the size of
the book that wrote them.

**What changed.** Every limit became a fraction of a declared capital base, with
fractional ceilings so ceilings stay ceilings at any size, and mandate tiers for
delegated capital. `tests/test_scale_invariance.py` measures the limits at bases from
$100k to $500M and asserts every ratio is identical — which is what turns
"capital-relative" from a description into a checkable property.

## 8. Where the line was drawn

This repository is the risk and execution half. Signal generation and edge discovery
are deliberately not here.

That is not modesty about the other half; it is the same conclusion as section 1
applied to scope. If friction is the binding constraint, then the reusable, verifiable,
generally useful part of the work is the machinery that controls friction and exposure
— and it takes a probability as an *input*, from whatever source a deployment supplies.
Nothing in this repository depends on any particular way of forming that view.
