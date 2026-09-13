> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.

> **Republished September 2026.** This is the July 26, 2026 report as written, with the August 4 calibration banner that was added after publication and a correction note at the end. The direction of every conclusion survives the corrections; some magnitudes do not, and the text says where. Method, instruments and the frozen aggregate snapshots are open at https://github.com/ailinsun/MarketFlow.

# Big Money Isn't a Signal: 109,000 Whale Prints on Polymarket, Audited

> ⚠️ **2026-08-04 calibration — three instrument defects found after publication.**
> The direction of every conclusion below survives; some magnitudes do not.
> 1. **Fee rate was 0.07, the real one is 0.05** (per-market `feeSchedule.rate`; 448/466 markets
>    read 0.05). Every after-fee number here is therefore **too negative by `0.02·(1−p)` per dollar**
>    — roughly 0.4–1.0 pp for the entry prices in this sample. Copy-trade returns stay negative,
>    the mid-price bands move most.
> 2. **The wallet sample was ranked by notional.** On the same tape, the top-5,000 wallets by
>    dollar volume are **53.2% volume-farming wallets**; ranked by trade count instead, **0.1%**.
>    The 300 profiled wallets here are the largest **by dollars**, so an unknown share of the
>    "whale" behaviour described is farm behaviour.
> 3. **`endDate` was used as settlement time.** It is the planned event time, not the resolution
>    time; `closedTime` is the real one.
>
> Fixes and evidence: the decontamination report published alongside this one in the repository.

**What we did:** we run a public-API collector that has been recording every Polymarket trade
above $2,000 since late June 2026, plus a rolling snapshot of holder concentration on the markets
those trades touch. This report takes 20 days of that feed — **109,460 whale prints, $880.8M in
notional, 11,636 markets, 10,535 distinct wallets** — profiles the 300 largest wallets by
reconstructing their full trade ledgers from public endpoints, and then asks the only question that
matters to anyone watching a whale-alert bot:

> If you had copied those trades, what would have happened to your money?

Every number below is reproducible from the artifacts listed in referenced section. Where the data refuses to
support a clean answer, we say so instead of rounding it into one.

---

## 1. Executive summary

1. **We found the copiers on-chain, and all of them lost money.** Pulling every trade on the 500
   whale-densest markets (1.94M trades) and matching them against 48,338 whale buy prints:
   **182,355 buys landed within 60 seconds of a whale**, at a **median ticket of $10** and a median
   lag of **35 seconds**. Every follower cohort finished negative after fees — **−6.6%** at one
   minute, **−14.1%** at one hour. We have not seen this measured anywhere public, though we make
   no claim to have searched exhaustively; the raw material for it is entirely public.

2. **The cost of copying is delay, not slippage.** Followers paid the whale's price plus **half a
   cent** — the fill is not the problem. But against a price-matched control group of buyers who
   weren't in any whale window, one-minute followers do **+1.6 points better**, five-minute
   followers **−5.7**, and one-hour followers **−12.2**. Reacting instantly is survivable. Seeing
   an alert, thinking about it, and buying ten minutes later is what costs money.

3. **Copying is negative even with a time machine.** Give a copier the whale's own fill price with
   zero latency and infinite depth, across all **76,069 resolved whale buy prints**: they win
   **69.5% of their bets** and still finish at **−3.11% ± 0.63%** per dollar after fees. Market-
   clustered: −1.22% ± 0.99%. High hit rate, negative expectancy — the odds do the killing.

4. **The average "whale" wins about half its events. Public leaderboards show ~86%.**
   The gap is one mechanism: losers are never closed. Counting a position whose price has collapsed
   below $0.02 as the loss it already is, the 280 profiled wallets show a **median true win rate of
   47.3%** against a **median displayed win rate of 90.9%**. **84% of wallets are flattered by
   this**, and 102 of 278 are flattered by 50 percentage points or more. On the subsample where we
   can see complete trade history (n=56), the numbers are **55.6% true vs 74.1% displayed** — an
   independent replication of PANews's January 2026 audit (53.8% vs 73.6%) on a different, larger,
   more recent sample.

5. **Only 2 of the 300 biggest wallets look like actual forecasters.** By behaviour, the money is
   **35% structural arbitrage, 23% near-certainty yield farming, 18% position management, 17%
   directional gamblers** — and **0.7% pure predictors**, who account for **0.5% of the notional**.
   When you get a whale alert, the prior is overwhelmingly that you are watching an arb leg or a
   yield trade with no directional content at all.

6. **Where the losses concentrate is exactly where retail looks.** Following buys priced 0.20–0.50
   returned **−9.05% ± 2.05%**; below 0.20, **−15.07%** (wide error bars). Following buys above
   0.90 — the boring yield trades nobody screenshots — returned **+0.22% ± 0.21%**. The trades that
   look like an opportunity are the ones that cost money.
   *(Qualified 2026-08-05: that +0.22% is per print. Clustered by market — the same correction referenced section
   already applies to the aggregate — the estimate halves and its interval covers zero, so the band
   is a wash rather than a yield. See [referenced section](#45-where-the-idealised-version-fails-hardest).)*

7. **The mirror trade is not free money either.** Taking the other side of every whale buy returns
   **−8.61% ± 4.26%**. Both sides of the fee-and-spread structure lose. "Fade the whales" is not
   the fix for "follow the whales."

8. **We tested this with our own money-shaped experiment and lost.** Our own smart-money paper
   tracker — real prices, real signals, real settlement, simulated capital — went **−20.3% over 55
   settled positions** and we shut down the live bridge feeding it. Details and the full ledger in
   referenced section, because a report about copy-trading traps that hides the author's own is worthless.

---

## 2. Method

### 2.1 Where the data comes from

| Source | What | Scale in this report |
|---|---|---|
| `data-api.polymarket.com/trades?filterType=CASH` | Every trade ≥ $2,000, polled every 300s with cross-cycle dedup | 109,460 prints · $880.8M · 11,636 markets · 10,535 wallets · 2026-07-06 02:45 → 07-26 16:57 UTC (20.6 days) |
| `data-api.polymarket.com/positions` + `/activity` | Full per-wallet ledger rebuild for the 300 largest wallets by notional (60.9% of feed notional) | 357,797 trades · 74,451 win/loss-adjudicated events |
| `data-api.polymarket.com/holders` | Rolling top-20 holder snapshots on markets with whale activity | 52,019 snapshots → 12,988 unique (market, outcome) tickets |
| `data-api.polymarket.com/trades?market=…` | Full tape — *every* trade, not just large ones, on the 500 whale-densest markets | 1,944,449 trades · 1,000 outcome tokens · 68.7% of whale notional · 155,584 distinct addresses |
| `gamma-api.polymarket.com/markets?closed=true` | On-chain settlement prices | 9,854 resolved markets |

Everything is public and read-only. No account data, no order flow of ours, no paid feeds.

### 2.2 True win rate vs displayed win rate

This is the single most important definition in the report.

A Polymarket position that goes to zero does not have to be closed. It can simply be left in the
wallet forever. If you compute a wallet's record only from *realised* P&L — which is what a
leaderboard does — those positions never enter the denominator, and the record shows only the
trades that were closed, which are overwhelmingly the winners.

- **Displayed win rate** — events with realised P&L only. This is the flattering number.
- **True win rate** — same events, plus every position currently priced **≤ $0.02** counted as the
  loss it already is, and every position priced **≥ $0.98** counted as the win it already is.
- **Zombie gap** = displayed − true. This is not noise; it is a behavioural fingerprint. A wallet
  that never leaves dead positions lying around is a wallet that closes its books.

Event-level adjudication uses on-chain resolution where available (Gamma `closed=true`), redemption
records, and current price for positions that are decided but unredeemed. Anything still genuinely
open counts in neither numerator nor denominator.

### 2.3 Behavioural classification

Wallets are typed by rules anchored to the profiles PANews documented, not fitted to P&L — the
thresholds were fixed before this sample was collected and were not tuned for these results:

| Type | Rule | What its trades mean |
|---|---|---|
| `pure_predictor` | true WR ≥ 55%, hedging < 10%, ≤ 5 trades/day | The only type whose direction carries information |
| `structural_arb` | ≥ 25% of its markets traded on multiple sides | Direction is one leg of an arb — no view |
| `position_manager` | win/loss size ratio ≥ 2 | Makes money on sizing, not on being right |
| `farmer` | ≥ 50% of buy notional above $0.90 | Collecting near-certainty yield — no view |
| `belief_gambler` | the rest, true WR near coin-flip | Directional, and structurally on the losing side |
| `insufficient` | < 8 adjudicated events | We decline to judge |

Hedging detection covers both same-market two-sided buying and the "buy every line in one event"
pattern across the separate condition IDs of a multi-outcome event.

### 2.4 The copy-trade simulation

For every whale **buy** print whose outcome has since resolved, we compute the return of buying that
outcome **at the whale's own fill price**:

```
gross per $1 = (settlement − price) / price
fee  per $1  = 0.07 · (1 − price)        # taker-only, rate·p·(1−p) per share
net  per $1  = gross − fee
```

Three deliberate choices, all of which flatter copying:

- **Zero latency, zero slippage, unlimited depth.** A real copier sees the print after the fact and
  pays worse. This is an upper bound on copy-trade performance, not an estimate of it.
- **Fees only, no spread.** Crossing the book costs more than the fee alone.
- **Unresolved markets are dropped, never assumed.** 86.1% of buy prints are adjudicable; the rest
  are still open and simply absent.

We report equal-weighted (each decision counts once — the investor's view), notional-weighted (the
money's view), and market-clustered (one market counts once — the conservative statistical view,
since prints in the same market share a single outcome and are not independent draws).

---

## 3. What the big wallets actually are

300 wallets, ranked by cumulative notional in the feed, covering **60.9%** of all whale-print
notional. Medians within each type:

| Type | n | Share of wallets | Share of notional | Events | True WR | Displayed WR | Zombie gap | Hedge | Farming | Win/loss ratio | Trades/day |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `structural_arb` | 105 | 35.0% | 46.7% | 44,033 | 25% | 91% | +55pp | 54% | 7% | 1.56 | 205 |
| `farmer` | 68 | 22.7% | 14.8% | 9,797 | 83% | 94% | +2pp | 37% | 91% | 1.18 | 92 |
| `position_manager` | 54 | 18.0% | 14.8% | 10,862 | 53% | 86% | +29pp | 8% | 0% | 4.25 | 41 |
| `belief_gambler` | 51 | 17.0% | 15.6% | 9,490 | 42% | 97% | +45pp | 9% | 0% | 1.10 | 97 |
| `insufficient` | 20 | 6.7% | 7.6% | 59 | — | — | — | — | — | — | — |
| `pure_predictor` | **2** | **0.7%** | **0.5%** | 210 | 67% | 76% | +10pp | 4% | 0% | 9.48 | 1.6 |

Three things worth sitting with:

**The largest single block of whale money has a 25% true win rate — and that's fine.** Structural
arbitrageurs are supposed to lose most of their individual legs; they are buying both sides and
harvesting the spread. Their "buy" prints are not opinions. Nearly half of all whale notional in
this sample is this. An alert bot cannot tell the difference, so half of what it shows you is noise
by construction.

**Farmers have the best true win rate in the dataset (83%) and the least useful signal.** They buy
at $0.90+ and collect the last few cents. Copying them at their price earns roughly the same few
cents minus fees — see referenced section — and no amount of hit rate turns that into an edge.

**Pure predictors exist but are rare and quiet.** Two wallets out of 300, trading **1.6 times a day**
at an average ticket of **$7,579**, with a win/loss size ratio of 9.5. Their combined footprint is
0.5% of whale notional. If a wallet is printing 200 times a day, that alone rules it out of this
category.

### 3.1 Leaving dead positions lying around predicts being bad at this

Split the profiled wallets by whether they carry any zombie gap at all:

| Group | n | Mean true win rate | Median adjudicated events |
|---|---|---|---|
| Zero zombie gap (books kept clean) | 28 | **69.4%** | 32 |
| Any zombie gap > 1pp | 225 | **43.1%** | 127 |

A 26-point spread in true win rate, from a variable you can compute for free from the positions
endpoint without knowing anything about the trader. The caveat is real and we state it plainly: the
clean-book group has far fewer adjudicated events per wallet (median 32 vs 127), so part of that
spread is small-sample luck rather than skill. It replicates a pattern we saw in an earlier scan on
a different sample, which is why we report it — but it is an observation, not a validated selection
rule.

---

## 4. The people who actually copied these trades

Everything above this point is inference about whales. This section is about the people watching
them — reconstructed from the chain, not modelled.

### 4.1 How you can see a copier

Whale-alert users are invisible in aggregate statistics, but they are not invisible on-chain. If a
$40,000 buy prints on an outcome at 14:02:10 and 180 addresses buy the *same* outcome in the next
sixty seconds with a median ticket of $10, those addresses are, at minimum, *acting immediately
after* the whale. So we pulled **every trade — not just large ones — on the 500 markets where whale
activity is densest**: 1,944,449 trades across 1,000 outcome tokens, covering 68.7% of all whale
notional in the feed. Then, for each of the **48,338 whale buy prints** landing inside that tape, we
collected every other address that bought the same outcome within 1, 5, 15 and 60 minutes.

Two things this method is not:

- **It is not proof of causation.** Someone buying 40 seconds after a whale may be reacting to the
  whale, or to the same goal / injury / headline the whale reacted to. We can measure what happened
  to them; we cannot prove why they clicked.
- **It is not usable without a control group.** "Copiers lose money" means nothing if *everyone*
  buying in these markets loses money. So every number below is compared against a control: buyers
  of the same outcomes who were **not** inside any whale window, with whale addresses themselves
  excluded from both groups.

### 4.2 Who they are

| Window after whale print | Addresses' buys | Median lag | Median ticket | Price vs the whale | Hit rate |
|---|---|---|---|---|---|
| 60s | 182,355 | 35s | **$10** | +0.0050 | 65.3% |
| 5 min | 359,858 | 207s | $10 | +0.0061 | 58.1% |
| 15 min | 507,428 | 682s | $10 | +0.0021 | 53.5% |
| 60 min | 652,882 | 2,711s | $10 | +0.0042 | 51.3% |

**The median follower ticket is $10.** These are not funds mirroring each other; this is retail,
clicking within about half a minute of a print, in size that would not cover the gas on most chains.

The most surprising number here is the smallest: followers pay only **half a cent** more than the
whale did, and only about half of them pay worse at all. The intuition that copiers get destroyed by
slippage is **wrong** — at this size the book absorbs them. Whatever it costs to copy, it is not
being spent on the fill.

### 4.3 What happened to their money

Comparing raw averages between followers and controls would be invalid: whale windows sit on
*expensive* outcomes (mean entry 0.653 for one-minute followers vs 0.426 for controls) and returns
depend heavily on entry price. So both groups are bucketed into 29 price bands (0.02 wide below 0.30,
0.05 above) and the control is re-weighted to the followers' price mix. After that alignment, the
largest within-bucket price difference between the two groups is **0.005** — the comparison is clean.

| Window | Followers (equal-weight) | Control, same price mix | Gap | Gap (notional-weighted) |
|---|---|---|---|---|
| 60s | −6.56% | −8.17% | **+1.61%** | +7.93% |
| 5 min | −11.08% | −5.39% | **−5.68%** | +3.46% |
| 15 min | −13.39% | −3.20% | **−10.19%** | +0.68% |
| 60 min | −14.13% | −1.98% | **−12.15%** | −0.31% |

Read the equal-weighted column first — with a median ticket of $10, an equally-weighted decision is
what a retail follower actually experiences.

**Every follower cohort loses money in absolute terms: −6.6% to −14.1% after fees.** That is the
headline finding of this report and it is not close.

But the *comparison* is more interesting than the level, and it does not say what a copy-trading
sceptic would expect:

- **Followers who act within 60 seconds do no worse than comparable buyers** (+1.61%, i.e. slightly
  better). The immediate reaction is not the mistake.
- **The penalty grows monotonically with delay.** Five minutes: −5.7 points. Fifteen: −10.2. An
  hour: −12.2. The people who see an alert, think about it, and buy ten minutes later are the ones
  paying.
- **Notional-weighted, the gap mostly disappears** (+7.9% to −0.3%). Large tickets inside whale
  windows do fine; small ones do badly. The damage is concentrated in exactly the retail-sized
  clicks that make up the median.

The honest summary: **copying is not a fast way to lose money because whales are wrong or because
fills are bad — it is a slow one, driven by acting late and small in markets where all retail buyers
are already losing** (the control group loses 2–8% too, depending on what it buys).

### 4.4 The same test, idealised

The follower analysis above covers 500 markets. Widening to **every whale buy print in the feed**
and asking a different question — *what if you could fill instantly at the whale's own price, with
no delay and no slippage at all?* — gives the theoretical ceiling of copy-trading:

**76,069 resolved whale buy prints, $634M of notional, filled at the whale's own price:**

| View | n | Hit rate | Gross | Net of fees | 2 SE |
|---|---|---|---|---|---|
| Per print (equal weight) | 76,069 | 69.5% | −0.98% | **−3.11%** | ±0.63% |
| Per print (notional weight) | 76,069 | 69.5% | — | **−0.96%** | — |
| Per market (clustered) | 9,459 markets | — | — | **−1.22%** | ±0.99% |
| Per market, mid-priced only (0.20–0.80) | 4,468 markets | — | — | **−3.23%** | ±2.13% |
| First print per (wallet, outcome) only | 46,823 | 68.3% | — | **−3.31%** | ±0.83% |

The gross number is the interesting one: **−0.98%**. Whale prints are, on average, priced *almost*
fairly. There is no large systematic mispricing to harvest by following them — and once you pay to
participate, the small negative becomes a reliable one. Copying does not fail because whales are
wrong. It fails because they are roughly right, and being roughly right is not worth the toll.

### 4.5 Where the idealised version fails hardest

| Entry price | n | Hit rate | Net of fees | 2 SE |
|---|---|---|---|---|
| < 0.20 | 1,889 | 13.1% | **−15.07%** | ±12.12% |
| 0.20–0.50 | 15,598 | 37.2% | **−9.05%** | ±2.05% |
| 0.50–0.80 | 27,475 | 63.1% | **−2.61%** | ±0.94% |
| 0.80–0.90 | 7,133 | 85.9% | −0.06% | ±0.97% |
| ≥ 0.90 | 23,970 | 97.5% | **+0.22%** | ±0.21% |

The monotonicity is the finding. The cheaper and more exciting the ticket, the worse copying does.

**Correction, 2026-08-05 — the top row needs the same clustering we applied in referenced section.** The table
above stands as published: we re-ran it and the arithmetic reproduces. What it lacks is a
qualifier. The table is per print, and prints in the same market share one outcome, so they are not
independent observations — which is exactly why referenced section reports a market-clustered view alongside the
per-print one (−3.11% → −1.22%). We had not applied that to the price bands.

Re-running the same code on a rebuilt, larger sample (104,781 resolved prints across 17,488
markets — the feed has kept collecting since publication, so the per-print column below differs
from the table above by sample, not by method):

| Entry price | Per print | 2 SE | Clustered by market | 2 SE |
|---|---|---|---|---|
| < 0.20 | −17.13% | ±10.74% | −12.12% | ±23.79% |
| 0.20–0.50 | −6.62% | ±1.76% | −6.72% | ±3.54% |
| 0.50–0.80 | −1.60% | ±0.79% | −0.47% | ±1.76% |
| 0.80–0.90 | +1.00% | ±0.81% | **+2.03%** | ±1.38% |
| ≥ 0.90 | +0.29% | ±0.18% | **+0.13%** | **±0.27%** |

Clustered, the ≥ 0.90 estimate halves and its interval covers zero. **That band is not a slice that
tests positive — it is the one slice that does not test clearly negative.** The distinction matters:
the first would support a yield claim, and the data does not. The monotonicity survives clustering
and is in fact sharper; the band that now looks positive on both views is 0.80–0.90.

We tested whether ≥ 0.90 works as a mechanical rule you could write down in advance — buy the
first time a market crosses 0.90, hold to settlement, every resolved market — and it loses
**−1.00% per trade**, with the empirical failure rate (3.82%) running 0.75 points above what the
prices imply (3.06%). Details and the pre-registered gate:
the layer-C verdict published in the repository.

| Print size | n | Net of fees | 2 SE |
|---|---|---|---|
| $2–5k | 46,499 | −2.69% | ±0.81% |
| $5–20k | 24,610 | **−4.54%** | ±1.08% |
| $20–100k | 4,499 | +0.06% | ±2.60% |
| $100k+ | 457 | +0.55% | ±7.53% |

Bigger prints are not worse to follow — if anything the reverse — but the two largest buckets have
error bars that swallow their point estimates. **We cannot tell you that following $100k+ prints
works. n=457 and ±7.53% means we cannot tell you anything about that bucket.**

### 4.6 Sorting by wallet type does not rescue it

This is the part of the report we most wanted to come out differently. Our own thesis going in was
that size carries no information but *behaviour type* does. On this test, it doesn't:

| Wallet type of the printer | n | Hit rate | Net of fees (EW) | Net of fees (notional-weighted) |
|---|---|---|---|---|
| `structural_arb` | 28,515 | 70.7% | −1.60% | −2.45% |
| `farmer` | 4,603 | 90.0% | −2.37% | −2.06% |
| `position_manager` | 4,244 | 53.7% | −3.48% | +2.15% |
| `belief_gambler` | 3,703 | 57.5% | −4.11% | +2.18% |
| `pure_predictor` | **33** | 51.5% | −21.74% | −13.83% |
| unprofiled (outside the top 300) | 34,413 | 69.3% | −4.53% | −6.01% |

Every type is negative equal-weighted. Two types flip positive when weighted by notional, driven by
a handful of large tickets — that is variance, not an edge.

**On pure predictors specifically, we decline to answer.** Two qualifying wallets produced 33
adjudicable prints in this window with a standard error of ±29.5%. That is not a result in either
direction. The honest statement is: *the wallets whose behaviour suggests genuine forecasting are so
rare and so inactive that 20 days of data cannot evaluate copying them.* Anyone claiming otherwise
from a sample this size is guessing.

### 4.7 Fading them doesn't work either

Taking the opposite side of every whale buy — buying the complementary outcome at (1 − p) — returns
**−8.61% ± 4.26%** after fees. The structure is not zero-sum for participants: both directions pay
the toll, and the mirror of a fairly-priced trade is a fairly-priced trade minus another fee.

---

## 5. Concentration: who is actually holding these positions

12,988 unique (market, outcome) tickets snapshotted across the window, 11,500 of them since
resolved.

### 5.1 A data trap worth knowing about

**4.8% of the tickets we sampled show a "largest holder" that is not a trader at all.** One address
appears as the top holder on 620 tickets across 1,086 markets, holding identical enormous balances
across every outcome of the same event — including every 2024 presidential candidate simultaneously,
at an initial value of $0. Its activity log contains no trades of any kind, only protocol yield
entries, and it has never once appeared in 111,000 whale prints.

This is protocol plumbing: depositing collateral into the conditional-token contract mints one share
of *every* outcome, and the resulting balances sit in a vault address that the public `/holders`
endpoint reports alongside real traders. Any tool that reads that endpoint and announces "one wallet
holds 99% of this market" without filtering it is reporting on a smart contract.

We detect these two ways — identical balances across the outcomes of one market, and "appears
constantly as top holder but has literally zero trades" — and exclude them below. Removing them
moves the headline concentration numbers by 1–2 points, which is small, but the extreme tail is
where it matters: tickets showing a ≥99% top holder drop from 2.1% to **1.4%** of the sample.

### 5.2 Concentration is real, and it is not a signal

After excluding protocol addresses (12,368 tickets):

- Median top-holder share: **35.1%** of the top-20 holdings
- **29.8%** of tickets have a single holder above 50%
- **6.0%** above 90%, **1.4%** above 99%
- Median top-5 share: **77.7%**

Note the denominator carefully: `/holders` returns the top 20 addresses, so these are shares *within
the top 20*, not of the entire market. They describe how lopsided the visible top of the book is, not
what fraction of all outstanding shares one person owns.

Does buying the outcome that a dominant holder has cornered work? We bucket every resolved ticket by
its **first** concentration snapshot — deliberately not the last, because near resolution the losing
side clears out and the winning side stays, which manufactures a correlation that has nothing to do
with foresight — and compare the realised settlement rate to the volume-weighted price whales paid:

| Top-holder share (first snapshot) | n | Settled "yes" | Whale VWAP | Bias | 2 SE |
|---|---|---|---|---|---|
| < 30% | 1,459 | 54.8% | 0.651 | **−10.3%** | ±2.6% |
| 30–50% | 1,698 | 56.8% | 0.651 | **−8.3%** | ±2.4% |
| 50–70% | 1,068 | 59.3% | 0.665 | **−7.2%** | ±3.0% |
| 70–90% | 816 | 57.2% | 0.646 | −7.4% | ±3.5% |
| ≥ 90% | 533 | 61.9% | 0.650 | −3.1% | ±4.2% |

Every bucket is negative: across all concentration levels, the outcome whales were buying settled
*less* often than the price they paid implied. Concentration does not flip the sign. There is a mild
gradient — the most concentrated bucket is least bad — but at ±4.2% it is not something to trade on,
and it is equally consistent with concentrated markets simply being closer to decided.

For the extreme cases specifically (top holder ≥95%, 441 resolved tickets): they settle "yes" 58.3%
of the time at an average whale price of 0.706. Cornering a market does not make the corner right.

---

## 6. What happened when we tried this ourselves

We are not observers here. We built a copy-trading tracker, ran it forward on live signals, and it
lost money.

**Design:** simulated capital, everything else real. Entry price is the whale's actual fill VWAP
*plus* one-sided cost — we never assume a perfect fill. Signals require ≥$5,000 net flow from ≥3
independent wallets, must be under 30 minutes old, and near-certainty farming prices are excluded.
Settlement is on-chain; unresolvable positions stay pending rather than being guessed.

**Result, 2026-07-13 → 07-23, 55 settled positions:**

| | |
|---|---|
| Staked | $1,994.93 |
| P&L | **−$404.87** |
| Return | **−20.3%** |
| Hit rate | 58.2% (32/55) |
| Signals evaluated → positions opened | 6,313,091 → 71 |

The same shape as the market-wide result: won more often than not, lost money anyway.

**The part that hurt most:** the stronger the consensus, the worse the outcome.

| Signal conviction | n | Return | Hit rate |
|---|---|---|---|
| < 0.30 | 2 | +29.3% | 100% |
| 0.30–0.60 | 17 | +1.2% | 65% |
| 0.60–0.90 | 20 | −14.2% | 60% |
| **≥ 0.90** | **16** | **−48.0%** | **44%** |

| Independent wallets on the signal | n | Return |
|---|---|---|
| 3–5 | 15 | −8.6% |
| 6–15 | 22 | +2.5% |
| 16–50 | 14 | −43.7% |
| **50+** | **4** | **−57.8%** |

Every filter we would intuitively have tightened — more wallets, more one-sided, higher conviction —
selected *harder* for losses. The worst single position was a World Cup semi-final line with 69
independent whale wallets behind it, one-sided, maximum conviction: −$118.92 on a $118.92 stake.

We had pre-registered a kill rule before this experiment started (kill at n≥30 if returns after
costs were ≤0). We executed it: the bridge that mirrored these signals toward live orders was
switched off on 2026-07-24 and has not been switched back on. The paper track keeps running because
we would rather keep collecting evidence than stop looking.

Sample caveats, stated rather than buried: 55 positions is small, 50 of them are sports, and the
window is a World Cup fortnight. This is a consistent story alongside the 76,069-print market-wide
result, not independent confirmation of it.

---

## 7. What this means if you trade

Stated as plainly as we can, with the confidence each one actually has:

1. **A whale-alert notification, on its own, is close to worthless — and slightly worse than
   worthless after fees.** (Strong: 76,069 prints, negative on every weighting.)
2. **Be most suspicious of the alerts that look most attractive.** Big buy, mid-range price, lots of
   room to run — that's the −9% bucket. (Strong: n=15,598, ±2.05%.)
3. **Ignore win-rate leaderboards entirely unless they tell you how they treat open losing
   positions.** A wallet showing 90%+ is usually showing you its closed trades only. Check the
   positions endpoint for how much dead weight it is carrying. (Strong: 84% of wallets flattered,
   median 90.9% displayed vs 47.3% true.)
4. **Frequency is a fast filter.** Wallets whose behaviour is consistent with genuine forecasting
   trade about **1.6 times a day**. Hundreds of prints a day means arbitrage, market making, or
   farming — whatever it is, it isn't a forecast. (Strong as a description of who's who; unproven as
   a trading rule.)
5. **"Fade the whales" is not the answer to "follow the whales."** Both sides are negative after
   costs. (Strong: −8.61%, though with wide error bars.)
6. **Concentration tells you a market is lopsided, not which way it will go.** And check whether the
   "whale" holding 99% is a smart contract. (Strong.)
7. **We do not know whether following genuine forecasters works.** Two candidate wallets, 33 prints,
   ±29.5%. Anyone who tells you they've proven this either way on public data from a window this
   short is overreaching. (Explicitly unresolved.)

---

## 8. Limitations

We would rather you discount this correctly than trust it wrongly.

- **20 days, one season.** 2026-07-06 to 07-27. Sports and esports are **92% of the $634M we
  analysed in referenced section** ($515.5M + $68.4M) because the window sits on a World Cup. Politics is 91 prints
  and $0.6M. **Nothing here should be extrapolated to an election cycle**, which is exactly when
  whale-following gets most attention.
- **Resolved-only.** 86.1% of buy prints are adjudicable; long-dated markets are structurally
  underrepresented because they haven't settled yet. If whale skill lives in year-out contracts,
  this design cannot see it.
- **Trade history is windowed.** Per-wallet reconstruction pulls the most recent ~1,500 activity
  records. For 224 of 300 wallets that window is full, meaning older redemptions are cut off while
  their still-open dead positions remain fully visible. **This biases true win rates downward.**
  It is why we report the complete-history subsample (n=56: 55.6% true / 74.1% displayed) separately
  and treat it as the better estimate; the full-sample figure (47.3% median true) is a floor, not a
  point estimate.
- **"Follower" means "bought right after", not "copied".** referenced section–4.3 identify people by timing, not
  intent. Someone buying 40 seconds after a whale may have been reacting to the same headline. The
  control-group design removes the "everyone in these markets loses" explanation; it does not
  establish that the whale print *caused* the follower's click.
- **Equal-weighted and notional-weighted follower results point different ways.** Equal-weighted,
  the one-hour cohort trails its control by 12.2 points; notional-weighted, by 0.3. Small tickets
  drive the damage. We lead with equal weighting because the median follower ticket is $10, but a
  reader who cares about aggregate dollars should read the other column.
- **The tape covers 500 markets, not the whole exchange** (68.7% of whale notional, 1,000 outcome
  tokens). 159 of those markets hit our 6,000-trade fetch cap, so on the very busiest markets we
  hold the most recent trades rather than all of them.
- **Idealised copy returns are an upper bound.** Zero latency, zero slippage, infinite depth, fees
  but no spread. Real copying is worse than every number in referenced section–4.7 — as referenced section shows directly.
- **Fee model is a ceiling.** We use rate·p·(1−p) at rate = 0.07, taker-only. Actual costs vary.
- **Correlated observations.** Prints in one market share one outcome. We report market-clustered
  standard errors for the headline; the per-print error bars are optimistic by construction.
- **Concentration is top-20-relative**, sampled only on markets with whale activity, and only for
  markets our collector saw. It is not a census of Polymarket.
- **We cannot see intent.** A wallet buying both sides is classified as an arb leg. It might be a
  forecaster changing their mind. Behaviour is observable; motive isn't.
- **Survivorship in the wallet sample.** We profiled the 300 largest wallets *by activity in this
  window*. Whales who blew up before 2026-07-06 or who trade below $2,000 per clip are invisible
  here.

---

## 9. Reproducing this

All figures come from five artifacts, each stamped with its own generation time (the underlying feed
is live and grows, so row counts differ slightly between artifacts):

| Artifact | Contains |
|---|---|
| `profiles_scan.json` | 300 wallet profiles: true/displayed win rate, zombie gap, hedge ratio, farming share, frequency, win/loss ratio, plus per-wallet fetch-completeness flags |
| `followcheck.json` | Copy-trade returns: overall, market-clustered, by wallet type, by entry price, by print size, by market class, counter-side |
| `concentration_dataset.json` | 12,988 tickets: first/last concentration snapshots, settlement, whale VWAP, protocol-address flags |
| `follower_analysis.json` | Follower reconstruction: per-window cohorts, price-bucketed control comparison, lag/ticket/slippage distributions |
| `own_following_audit.json` | Our own paper track: full ledger stats by conviction, wallet count, market class, best/worst positions |

**Republication note (September 2026):** the five JSON artefacts from the July 26 run were not retained when the working directory was cleaned in August, so the exact tables above cannot be regenerated from stored files. Selected offline methods and aggregate checks are published in this repository. The full collection and analysis pipeline is not distributed, so this release does not reproduce the original study end to end.

---

*Compiled by [Ailin Sun](https://www.linkedin.com/in/ailinsun/). Corrections welcome — if you can show one of these numbers
is wrong, we want to know.*


---

## Correction (August 13, 2026; published with this republication)

Two things in the July 26 report needed fixing, so they were fixed and the numbers re-run. (1) Fees: the report applied a flat 7% taker rate; per-market fee schedules (measured: 5% on 572 markets, 4% on 22, zero on 1) are now read per trade. Every after-fee number in the report is therefore too negative by `0.02·(1−p)` per dollar, roughly 0.4 to 1.0 points at the entry prices in the sample; copy-trade returns stay negative. (2) Holding windows: positions were closed at each market's listed `endDate`, which carries far-future placeholders and early settlements; they are now closed at the actual settlement time. Under the corrected windows, 248 of 298 recomputable wallets show smaller exposure and rankings move a median of 25 places, while eligibility is unchanged for all 300 wallets and the top of the list remains real traders: the correction moves rank ordering, not who qualifies. The anonymized numerical comparison is in the repository; the original exposure instruments are not distributed.
