# Limitations

The things a careful reader should know before trusting anything here. Nothing in this
file is hedging for its own sake; each item changes how a specific number or component
should be read.

## Scale and operating history

**This system has been run against a live venue at small scale by its author.** It has
not been operated at institutional size. The capital-relative design means the same
code paths govern any size, and tests assert that the limits scale — but *tested to
scale* and *operated at scale* are different claims, and only the first is made here.

Active development has ended. The verification suite runs in CI on every push, so
"it still works" is checkable; "it is maintained" is not claimed.

## What the measurements establish, and what they do not

**The zero-sum ledger** covers 439 settled markets and 1,338,933 taker BUY fills
between 2025-12-11 and 2026-08-13, selected from the active core by large-print
activity.

- It is **not a census** of the venue. The sample is selected, and selected on activity.
- The three rows are **flow-accounting rows under a stated fee model**, not observed
  participant profit and loss. Nobody's account was read.
- Fills in the same market share an outcome, so per-print observations are correlated;
  naive standard errors on this sample would be too small.
- Resolved-only analysis under-represents long-dated and unsettled markets.
- Capped trade histories retain recent fills and omit older activity.

**The fee-geometry result** — a 25.9% reduction in mid-range fees at constant revenue —
is a model calculation with the distribution held fixed. It is not an observed reform
outcome, and it says nothing about how behaviour would change if the schedule changed.
The venue schedules used are historical assumptions rather than a statement of current
pricing.

**The repeated-size signature** describes behaviour. It does **not** establish identity,
common control, intent, or anything about future performance. The 500-fold difference
in contamination between ranking conventions is a fact about the two conventions on
that sample, not a universal law.

**The settlement rate** (7.12% of settled disputes changed the proposal) is a
historical conditional rate. It is not a forecast for an individual contract.

**Cross-venue spreads** were measured in a single snapshot, top of book, with no depth
sweep. The matching is a token-similarity heuristic; it does not verify that two
contracts settle on the same event, which is the largest hidden risk in that trade and
why every reported pair carries both sides' rules text for a human to read.

## Reproducibility

The original per-fill tapes **were not retained and are not distributed.** Market
metadata can be rebuilt from the public API; a tape cannot. The offline checks verify
the frozen aggregates' internal identities and the methods, and they cannot reproduce
every table in every original report. Where a report depends on an input that no longer
exists, its index says so.

Redistribution rights for upstream venue material are not ours to grant. Code is
Apache-2.0 and the original compilation is CC BY 4.0; venue facts and rules retain
their own terms.

## Known gaps in the system

- **Depth is not swept on the general exposure path.** Exposure assumes exit near the
  mark. That assumption fails precisely in the conditions where exposure matters.
- **Settlement-timing correlation is not modelled.** Buckets are treated as
  independent at portfolio level; many contracts settling the same day are not.
- **The multi-tenant layer is a dry-run skeleton.** It proves the decision loop
  isolates tenants; it has not run production money across tenants.
- **One self-test is skipped** without the optional signing install, because it pins
  the venue SDK's contract surface and needs the SDK present.
- **The venue SDK is pinned to a commit**, not a release, because the project does not
  publish to PyPI. Verify the commit before trusting it with a key.
- **`fill_model` and `microstructure` are first cuts.** They measure execution quality
  rather than gate it, and their calibration is from one venue over one window.
- **No formal verification anywhere.** The safety properties are enforced by tests.
  Tests demonstrate the presence of behaviour, never its absence under all inputs.

## Things deliberately not in this repository

Signal generation and edge discovery are not here. That is a scope decision, not an
oversight: this repository is the risk and execution half, and it is complete as that.
A reader looking for "what to trade" will not find it, and none of the machinery here
depends on it — every entry path takes a probability as an *input* from a source the
deployment supplies.

## If you are considering running this with real money

Read [SECURITY.md](SECURITY.md) first, then:

1. Run `make verify` and read what it actually checked.
2. Run the demonstration and reconcile every number in it against
   [RISK_MODEL.md](RISK_MODEL.md).
3. Set your own capital base and mandate tier. The defaults are illustrative.
4. Keep dry run on until you have watched a full cycle, including a settlement.
5. Verify the pinned SDK commit yourself.

No warranty. Trading event contracts can lose money. Availability and legality vary by
jurisdiction.
