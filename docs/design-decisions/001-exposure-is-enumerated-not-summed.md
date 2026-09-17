# 001 — Exposure is enumerated, not summed

## Decision

For a bucket of mutually exclusive legs, portfolio exposure is computed by enumerating
every settlement state and taking the worst. Summing leg costs is used only where
exclusivity cannot be established, and the result is labelled `all_lose_bound` so no
reader mistakes a bound for a measurement.

## Why

Summing costs is not a conservative approximation of the enumerated answer. It is a
different number, and it is wrong by a factor that grows with the number of legs.
Holding four NO legs on a five-way race, exactly one leg settles YES, so at most one
NO can lose: $9,100 of cost carries $1,600 of exposure.

A risk system that overstates exposure by 5.7x is not "being careful". It sizes down
positions that were never risky, leaves the operator unable to distinguish a
structurally hedged book from a concentrated one, and trains them to ignore the number.

## What this rules out

- **Simulation.** There is nothing to simulate; for *n* legs there are at most *n + 1*
  states and each one's P&L is arithmetic.
- **Correlation assumptions inside an exclusive group.** The correlation is implied by
  the structure, so it is computed rather than assumed, and it is not a parameter.

## What it depends on

The venue's mutual-exclusivity flag. If that flag is wrong, the enumeration is wrong.
This is why `risk.complete_sets` checks the same structure from a different direction —
by looking for real offers on every leg — rather than trusting the flag twice.

## What would make this wrong

A venue where the legs of a flagged group are not in fact exclusive, or where more than
one can settle YES. The enumeration would then understate exposure, which is the
dangerous direction. Nothing here detects that; it is a stated assumption about the
venue's contract semantics.
