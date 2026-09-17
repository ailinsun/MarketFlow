# MarketFlow

**A measurement of why event-contract markets are hard to trade, and the execution
and risk-control system built on that answer.**

The measurement came first. Across 439 settled Polymarket markets and 1,338,933
taker BUY fills — $419.4M of buy notional — the flow-accounting identity closes like
this:

| Party | Net share of taker BUY notional |
|---|---:|
| Buying crowd, after modelled fees | −1.345% |
| Counterparties, after modelled fees | −0.143% |
| Venue, modelled taker fees | +1.488% |

The venue's take is an order of magnitude larger than the edge either side of the
trade keeps. Prediction is not the scarce resource in this market; **friction
control is**. So MarketFlow is not a forecasting engine. It is the machinery that
decides how much may be at risk, refuses trades whose cost exceeds their edge, keeps
the accounting honest about what is actually exposed, and cannot move money without
passing an authority boundary.

Both halves are here: the instruments and frozen snapshots that produced the
measurement, and the system built on it.

```sh
git clone https://github.com/ailinsun/MarketFlow && cd MarketFlow
make demo      # end-to-end on synthetic data, no credentials, no network
make verify    # every gate and self-test, offline
```

Python 3.9+. The core has no third-party dependencies at all.

---

## What the demonstration shows

`make demo` runs the real modules over a synthetic seven-position portfolio. Three of
its outputs are the argument for the whole design.

**1. The book overstates what is at risk.** Four NO legs on a five-way race show
$9,100 of cost. At most one of them can lose, because exactly one leg of a mutually
exclusive group settles YES. Enumerating every settlement state exactly:

```
leg_0_yes     p= 12.0%   P&L  $-1,600.00
leg_1_yes     p= 10.0%   P&L  $-1,600.00
leg_2_yes     p=  8.0%   P&L  $-1,600.00
leg_3_yes     p=  6.0%   P&L  $-1,600.00
outside_yes   p= 64.0%   P&L    $+900.00
```

True exposure is $1,600, not $9,100. Adding leg costs together is not a conservative
approximation of that; it is wrong, and it is wrong by a factor that grows with the
number of legs. Reported across the whole portfolio: $13,620 of cost, $6,120 of
genuine event exposure, 55% of the book absorbed by structure the position already
had.

**2. A visible edge mostly is not one.** The sizer never acts on a raw model
probability. It shrinks it toward the market price, takes it down again by its own
standard deviation, applies a quarter of Kelly to what survives, then caps per market
and per cluster. A 13-point raw edge does not survive that. A 25-point edge does, and
buys 1.81% of the bankroll where full Kelly would ask for 46.2%.

**3. Some opportunities cannot be bought at all.** A multi-candidate election market
priced off mid quotes showed a 3.70% underround. Of its 34 legs, 27 were unquoted
placeholders whose mid read as zero; the seven real legs carried $1 to $237 of depth.
The set could not be completed at any size. `marketflow.risk.complete_sets` refuses
that shape by construction, and the self-test pins the refusal.

## The layers

```
feeds      read-only market and chain data, rotation, tape collection
  ↓
risk       risk budget, position sizing, event exposure and correlation,
           complete-set integrity, settlement resolution, money-path watchdog
  ↓
execution  order construction, market-quality gates, fill modelling,
           the trading daemon, multi-tenant portfolios
  ↓
guardian   wallet authority, arm state, structural traps, unitized share ledger
  ↓
monitor    settlement guards, service watchdogs, operator alerts
```

Each layer is reachable only through the one below it, and the boundaries are
enforced rather than documented: `marketflow.risk.exposure` fails its own self-test if
it ever imports anything from the execution stack. [ARCHITECTURE.md](ARCHITECTURE.md)
has the data and control flow; [RISK_MODEL.md](RISK_MODEL.md) defines every number and
says what it does not mean; [docs/EVOLUTION.md](docs/EVOLUTION.md) explains how the
design arrived here, which is the fastest way to understand why several of these
components exist at all; and [docs/design-decisions/](docs/design-decisions/) records
the choices that are hard to reverse.

## Safety properties

These are invariants with tests behind them, not intentions.

- **Dry run is the default and live requires arming.** A live request without armed
  state resolves to dry run rather than erroring — refusing is the safe direction.
- **Caps are fractions of a declared capital base**, never dollar constants. Set
  `MARKETFLOW_CAPITAL_BASE_USD` and every gate moves with it. Ceilings are fractions
  too, so they stay ceilings at any scale. The shipped defaults are illustrative.
- **Root authority requires a quorum of at least two signers.** A single-signature
  root configuration is refused, and a self-test asserts the refusal.
- **Pluggable seams fail closed.** Entitlement and intelligence routing load from
  environment-named modules; unconfigured means nothing is permitted, not everything.
- **Self-tests may not touch the network.** The test runner installs an audit hook
  that records a socket attempt even when the code under test catches the exception,
  so a fail-soft fallback cannot hide a live dependency.
- **The read-only analysis layer cannot act.** It writes to one namespace, imports no
  execution module, and proves both from its own abstract syntax tree.

[SECURITY.md](SECURITY.md) covers the trust boundaries and what to check before
connecting a real wallet.

## What was measured

Every claim below links to the data or the arithmetic behind it.

- **Zero-sum ledger.** Window 2025-12-11 to 2026-08-13, 439 settled markets. The three
  rows above sum to zero. They are flow-accounting rows under the stated fee model,
  not observed participant profit and loss.
  [Snapshot and sampling method](data/zero_sum_ledger/zero_sum_ledger.json).
- **Fee geometry.** The prevailing schedule charges `rate * p * (1 - p)` per share,
  which peaks exactly where the market knows least and vanishes where it already
  knows. It taxes information and exempts noise. A floored alternative reduces
  mid-range fees by 25.9% at constant revenue under a fixed calibrated distribution —
  a model calculation, not an observed reform.
  [Arithmetic](instruments/fee_geometry.py), recomputable in one command.
- **Ranking conventions decide what you see.** Of the top 5,000 wallets by dollar
  volume, 46.2% match the repeated-size signature. Rank the same 144,532 wallets by
  trade count times market breadth and none of the top 5,000 do, against a 5.9%
  baseline across the whole population. Same wallets, same window, only the sort key
  changes. [Rank audit](data/farm_signature/rank_audit_2026-08-13.json). The signature
  describes behaviour, not identity, control or intent.
- **Settlement is not free.** Across 955 settled disputed markets, 7.12% saw the final
  answer differ from the proposed one (Wilson 95% CI 5.7%–8.9%). A historical
  conditional rate, not a forecast for any individual contract.
  [Register](data/settlement_quality/settlement_quality.json).
- **Cross-venue spreads are smaller than the cost of capturing them.** Matched
  Polymarket/Kalshi pairs differ by at most about a cent against a combined fee wall
  of two and a half to three cents. [Scanner](instruments/cross_venue_spread.py).

[Full report index](reports/README.md) ·
[data inventory, provenance and citation](data/README.md)

## Repository

| Path | What is in it |
|---|---|
| [`marketflow/`](marketflow/) | the system: feeds, risk, execution, guardian, monitor |
| [`instruments/`](instruments/) | research and measurement tools, each runnable alone |
| [`data/`](data/) | frozen aggregate snapshots with embedded citation blocks |
| [`reports/`](reports/) | dated working reports, in the language they were written in |
| [`examples/`](examples/) | the runnable demonstration and its synthetic fixtures |
| [`tests/`](tests/) | unit tests; module self-tests live beside their modules |
| [`checks/`](checks/) | the publication gate and the offline test runner |
| [`governance/`](governance/) | the research methodology charter |

## Running it for real

`make verify` needs nothing installed. Signing orders does:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-signing.txt
cp .env.example .env        # placeholders only; nothing here is a secret
```

[docs/quickstart.md](docs/quickstart.md) walks through it.
[docs/configuration.md](docs/configuration.md) documents every environment variable,
which of them are hard gates, and what happens when each is unset. Connecting a live
venue account is the one step this repository will not do for you, and
[SECURITY.md](SECURITY.md) explains what to verify first.

## Status and limits

Active development has ended. This is a finished artifact, preserved and documented
so it can be read, run, audited and reused; the verification suite runs in CI on every
push, so "it still works" is a checkable claim rather than a hope.

It has been run against a live venue at small scale by its author. It has not been
operated at institutional size, and nothing here should be read as a claim that it
has. [LIMITATIONS.md](LIMITATIONS.md) is the full list: what the measurements do not
establish, where the risk model is an assumption rather than a result, and which
parts have never been exercised in production.

- No warranty. Trading event contracts can lose money. Availability and legality vary
  by jurisdiction.
- Historical raw tapes were not retained and are not distributed; aggregate checks
  cannot reproduce every original table.
- Repeated trading patterns do not establish identity, control, intent or future
  performance.

## Licence and citation

Code: [Apache-2.0](LICENSE). Data, reports and research documentation:
[CC BY 4.0](LICENSE-DATA). Venue material obtained through an API retains its own
terms. Citation metadata is in [CITATION.cff](CITATION.cff); the archived research
release is [DOI 10.5281/zenodo.22734802](https://doi.org/10.5281/zenodo.22734802), and
the accompanying paper is [on SSRN](https://papers.ssrn.com/abstract=7453738).

Corrections and method questions are welcome through
[the issue tracker](https://github.com/ailinsun/MarketFlow/issues); see
[contribution guidance](.github/CONTRIBUTING.md).
