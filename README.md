# MarketFlow

![MarketFlow — execution and risk control for event-contract markets, backed by
reproducible market-microstructure research. The card carries the published sample:
439 settled markets, 1,338,933 taker BUY fills, $419.4M of buy notional, and the
three ledger rows.](assets/marketflow-cover.jpg)

**Execution and risk control for event-contract markets, backed by reproducible
market-microstructure research.**

The research came first. Across a selected sample of 439 settled Polymarket markets
and 1,338,933 taker BUY fills ($419.4M of buy notional), the buying side showed
+0.143% aggregate gross selection edge before modelled taker fees. Modelled fees were
1.488% of buy notional — 10.4 times that gross edge — leaving the taker side at
−1.345% net in this sample:

| Share of taker BUY notional | |
|---|---:|
| Taker side, gross selection edge before fees | +0.143% |
| Modelled taker fees | 1.488% |
| Taker side, net of modelled fees | −1.345% |
| Counterparties (makers, no taker fee) | −0.143% |

The last three rows sum to zero. The fee row is the modelled taker-fee burden computed
from each market's own fee schedule, not the venue's retained revenue: where a venue
passes part of its taker fees to makers as rebates, that transfer is not modelled.

This does not show that forecasting is unimportant. It shows that, in this sample, a
modest aggregate predictive edge was overwhelmed by execution friction. MarketFlow is
built around that constraint: exposure accounting, capital-relative sizing,
executable-market checks, settlement gates, and explicit authority boundaries.

Both halves are here: the instruments and frozen snapshots behind the measurements,
and the system built on them.

```sh
git clone https://github.com/ailinsun/MarketFlow && cd MarketFlow
make demo      # end-to-end on synthetic data, no credentials, no network
make verify    # every gate and self-test, offline
```

Python 3.9+. The core has no third-party dependencies at all; the signing layer needs
3.10+ (see `requirements-signing.txt`).

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

**2. The sizer never acts on a raw model probability.** It shrinks the probability
toward the market price, takes it down again by its own standard deviation, applies a
quarter of Kelly to what survives, then caps per market and per cluster. In the
demonstration a 13-point raw edge does not survive that. A 25-point edge does, and
buys 1.81% of the bankroll where full Kelly would ask for 46.2%.

**3. Some opportunities cannot be bought at all.** A multi-candidate election market
priced off mid quotes showed a 3.70% underround. Of its 34 legs, 27 were unquoted
placeholders whose mid read as zero; the seven real legs carried $1 to $237 of depth.
The set could not be completed at any size. `marketflow.risk.complete_sets` refuses
that shape by construction, and the self-test pins the refusal.

## The system

```
feeds      read-only market data, large prints, tape rotation
risk       risk budget, position sizing, event exposure and correlation,
           complete-set integrity, settlement resolution, money-path watchdog
execution  market-quality gates, order construction and fuses, fill modelling,
           the exit-first execution daemon, a multi-tenant decision loop
guardian   delegated mandates: user-root authority, arm state, risk budget,
           structural traps, enclave signing
monitor    settlement guards, heartbeat watchdog, operator alerts
mcp        a read-only data plane (MCP and REST) over the same modules
```

Data flows from feeds through risk into execution; guardian wraps execution for
delegated mandates; monitor watches all of it. The boundary that matters most is
asserted rather than documented: the read-only exposure engine,
`marketflow.risk.exposure`, parses its own syntax tree and fails its self-test if it
ever imports anything from the execution stack.
[ARCHITECTURE.md](ARCHITECTURE.md) has the data and control flow;
[RISK_MODEL.md](RISK_MODEL.md) defines every number and says what it does not mean;
[docs/EVOLUTION.md](docs/EVOLUTION.md) explains how the design arrived here; and
[docs/design-decisions/](docs/design-decisions/) records the choices that are hard to
reverse.

## Safety properties

These are invariants with tests behind them, not intentions.

- **Dry run is the default and live requires arming.** A live request without armed
  state resolves to dry run rather than erroring — refusing is the safe direction.
- **Caps are fractions of a declared capital base**, never dollar constants. Set
  `MARKETFLOW_CAPITAL_BASE_USD` and every gate moves with it. Ceilings are fractions
  too, so they stay ceilings at any scale. The shipped defaults are illustrative.
- **An unknown fee refuses a live taker order.** When a market's fee rate cannot be
  read, a dry run may still price the order with an estimate, but a live taker entry
  is refused rather than placed at a guessed cost.
- **A delegated mandate has exactly one way to sign.** The trading key stays in the
  account holder's enclave; the service holds only the credentials of two side-bound
  agents, never a wallet or root key. Every arm and every signature re-validates a
  fresh read-only proof that the account holder's root quorum — at least two signers —
  controls the account, and every live entry needs a risk-budget reservation.
- **Unset means closed.** No entry source is allow-listed, and the live, entry and
  user-root gates are closed, until a deployment opens them deliberately.
- **Self-tests may not touch the network.** The test runner installs an audit hook
  that records a socket attempt even when the code under test catches the exception,
  so a fail-soft fallback cannot hide a live dependency.

[SECURITY.md](SECURITY.md) covers the trust boundaries and what to check before
connecting a real account.

## What was measured

Every claim below links to the data or the arithmetic behind it.

- **Zero-sum ledger.** Window 2025-12-11 to 2026-08-13, 439 settled markets selected
  by large-print activity. The rows above are flow-accounting rows under the stated
  fee model, not observed participant profit and loss.
  [Snapshot and sampling method](data/zero_sum_ledger/zero_sum_ledger.json).
- **Fee geometry.** The prevailing schedule charges `rate * p * (1 - p)` per share —
  largest at p = 0.5 in absolute terms, and `rate * (1 - p)` per dollar deployed. Under
  a fixed calibrated distribution, a floored alternative lowers fees across p < 0.8
  by 25.9% at constant revenue — a model calculation, not an observed reform.
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
- **Cross-venue spreads.** In one matched top-of-book snapshot, Polymarket/Kalshi pairs
  differed by at most about a cent against a combined taker-fee wall of two and a half
  to three cents. That snapshot is not distributed; the
  [scanner](instruments/cross_venue_spread.py) is.

[Data inventory, provenance and citation](data/README.md)

## Repository

**The current system**

| Path | What is in it |
|---|---|
| [`marketflow/`](marketflow/) | the system: feeds, risk, execution, guardian, monitor, mcp |
| [`examples/`](examples/) | the runnable demonstration and its synthetic fixtures |
| [`tests/`](tests/) | unit tests; module self-tests live beside their modules |
| [`checks/`](checks/) | the publication gate and the offline test runner |
| [`docs/`](docs/) | quickstart, configuration, design decisions, design history |

**Reproducible evidence**

| Path | What is in it |
|---|---|
| [`data/`](data/) | frozen aggregate snapshots with embedded citation blocks, checked by `make verify` |
| [`instruments/`](instruments/) | the measurement methods, each runnable alone |

**Historical archive**

| Path | What is in it |
|---|---|
| [`reports/`](reports/) | dated working reports, mostly in Chinese; most rest on inputs that were not retained, and [the index](reports/README.md) says which |
| [`governance/`](governance/) | the research methodology charter |

## Integrating a live venue

`make verify` needs nothing installed. Signing orders does:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-signing.txt
cp .env.example .env        # placeholders only; nothing here is a secret
```

This repository is not a deployment guide. It contains the components a live
integration needs — the order module and its fuses, the exit-first daemon, the
delegated-mandate service, the gates and the monitors — and it documents every
setting: [docs/quickstart.md](docs/quickstart.md) and
[docs/configuration.md](docs/configuration.md), including what each setting does when
unset. Supplying the signal source, provisioning accounts, supervising the processes
and connecting a venue account are the deployer's work, and
[SECURITY.md](SECURITY.md) lists what to verify first.

## Status and limits

Feature development has ended; releases since then correct and simplify. The
verification suite runs in CI on every push, so "it still works" is a checkable claim
rather than a hope.

The measurements come from public venue data. The execution and signing paths are
exercised by offline self-tests and synthetic fixtures; the system has not been
operated with production capital, and nothing here should be read as a claim that it
has. [LIMITATIONS.md](LIMITATIONS.md) is the full list: what the measurements do not
establish, where the risk model is an assumption rather than a result, and which parts
have never been exercised in production.

- No warranty. Trading event contracts can lose money. Availability and legality vary
  by jurisdiction.
- Historical raw tapes were not retained and are not distributed; aggregate checks
  cannot reproduce every original table.
- Repeated trading patterns do not establish identity, control, intent or future
  performance.

## Licence and citation

Code: [Apache-2.0](LICENSE). Data, reports and research documentation:
[CC BY 4.0](LICENSE-DATA). Venue material obtained through an API retains its own
terms. Citation metadata is in [CITATION.cff](CITATION.cff). Cite
[DOI 10.5281/zenodo.22733781](https://doi.org/10.5281/zenodo.22733781), which always
resolves to the most recent archived version; each version's own DOI is listed on
that record. The accompanying paper is
[on SSRN](https://papers.ssrn.com/abstract=7453738).

Corrections and method questions are welcome through
[the issue tracker](https://github.com/ailinsun/MarketFlow/issues); see
[contribution guidance](.github/CONTRIBUTING.md).
