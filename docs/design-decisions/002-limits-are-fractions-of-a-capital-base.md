# 002 — Limits are fractions of a declared capital base

## Decision

No risk limit anywhere in the system is a dollar constant. Every cap, fuse and ceiling
is a fraction of a declared base: `MARKETFLOW_CAPITAL_BASE_USD` for the owner path,
`MARKETFLOW_MANDATE_CAPITAL_USD` for delegated mandates,
`MARKETFLOW_FLEET_CAPITAL_USD` for fleet-wide guards.

## Why

A hard-coded dollar cap is a statement about one particular balance sheet. It makes the
system unusable by anyone else without editing source, it goes stale silently as the
book changes, and it discloses the size of the book that wrote it.

With ratios, the same code governs a $100k book and a $500M mandate, and the ceilings
remain ceilings at every size because they are ratios too.

## What this rules out

- **Tests that assert dollar amounts.** Every test asserts a relationship, so the suite
  keeps meaning something after a deployment changes its base.
- **Defaults presented as recommendations.** The shipped fractions (20% deployed,
  0.5% per trade, 2% drawdown; tiers at 50/2/10% and 100/5/20%) are illustrative. They
  are not derived from any regulatory framework or industry survey, and the
  documentation says so everywhere they appear.

## Enforcement

`tests/test_scale_invariance.py` measures every limit at four bases spanning a
5,000-fold range and asserts the ratios are identical to floating-point tolerance, that
no limit is insensitive to the base, and that the ordering per-trade < drawdown < total
holds at each scale.

## What would make this wrong

A venue or mandate with a genuine absolute limit — a hard per-order notional cap, or a
regulatory threshold in dollars. Those exist and would have to be expressed as an
additional absolute clamp on top of the ratios, not by reverting to constants.
