#!/usr/bin/env python3
"""Fee geometry for event contracts — why the prevailing humped fee schedule is
defective, and a solved alternative curve. Pure algebra, read-only, no data
dependency.

**Why this module exists.** The major event-contract venues charge from the same
family of curves: a hump of the form `r * p * (1 - p)` per share, with a maker
paying zero or a fraction of the taker rate. Two readings of that curve matter, in
different units:

- **per share** (absolute): largest at p = 0.5, zero at both ends;
- **per dollar deployed** (`r * (1 - p)`): largest as p approaches 0, falling
  monotonically as p rises.

The second reading is the one that explains the measured complaint. A near-certain
ticket is the cheapest thing in the market to trade, so a dollar-volume figure can
be manufactured at almost no cost — the structural cause of wash-volume
contamination, and the same reasoning appears at the top of the farm filter. What
this module does *not* claim is that the curve is highest where the market knows
least: that is an interpretation of p = 0.5, not a property of the algebra.

This module turns that defect and its solution into arithmetic anybody can
recompute.

**Two layers, and the line between them is the publication boundary:**
  1. The curve mathematics and the revenue-neutral solve — pure algebra, no data
     dependency, independently reproducible by anyone.
  2. Distribution calibration constants (`CALIBRATION`) — aggregates derived from
     a measured tape. Every solver takes the distribution as a **parameter**, so
     the two layers are not coupled. Run the mathematics against `uniform_dist()`
     or against your own distribution and the **shape** of the conclusion does not
     change (see `--robustness`).

**Core results (reproduced by --table):**
  - Under the prevailing schedule the informed band pays a wildly
    disproportionate share of all fees, while the near-certain band pays almost
    nothing relative to its notional.
  - Under the prevailing schedule the net EV of carrying a near-certain ticket,
    `(1 - p)(1/p - r)`, is **always positive**, so wash volume has no reason to
    stop by itself.
  - A floored alternative `g(p) = r' * p * max(1 - p, m)`, solved for revenue
    neutrality, cuts the tax across `p < 1 - m` and converts `p >= 1 - m` into a
    fixed ROI rate `r' * m`, which turns wash volume EV-negative above
    `p* = 1 / (1 + r' * m)`.
  - The break-even condition is a modest volume response in the middle band,
    which is where the cost fell.

    **Calibration and limits.** Functions accept an explicit distribution;
    `uniform_dist()` supplies a synthetic example. The default table uses
    historical aggregate calibration constants, not the frozen ledger snapshot;
    the original tape is not distributed. Revenue neutrality holds with the
    assumed distribution fixed, so changes in trading behaviour are scenarios
    rather than measured outcomes, and the venue fee schedules are historical
    assumptions rather than a statement of current pricing.
"""
from __future__ import annotations

import argparse
import math

# ── Layer 1: pure algebra, no data dependency ───────────────────────────────

def fee_hump(p: float, r: float) -> float:
    """Prevailing per-share fee: r * p * (1 - p). Rounding conventions differ by
    venue and are deliberately not modelled here; the results do not depend on
    them."""
    return r * p * (1.0 - p)


def fee_floored(p: float, r: float, m: float) -> float:
    """Alternative curve: r * p * max(1 - p, m). At m = 0 it degenerates back to
    the prevailing hump."""
    return r * p * max(1.0 - p, m)


def fee_flat(p: float, c: float) -> float:
    """Control curve: a flat fee on notional, c * p, giving a constant ROI
    threshold of c."""
    return c * p


def roi_threshold(fee_fn, p: float) -> float:
    """Pre-fee alpha required to break even, in ROI terms: per-share fee divided
    by entry price."""
    return fee_fn(p) / p


def wash_breakeven_price(r_new: float, m: float) -> float:
    """The critical price p* above which carrying a near-certain ticket, and
    therefore wash trading, turns EV-negative.

    Net EV = (1 - p)/p - r' * m; setting it to zero gives p* = 1 / (1 + r' * m).
    Under the prevailing hump (m = 0) net EV = (1 - p)(1/p - r) is always
    positive, so no such p* exists and the channel never closes.
    """
    if m <= 0:
        return float("nan")
    return 1.0 / (1.0 + r_new * m)


def revenue_per_notional(dist, fee_fn) -> float:
    """Fee revenue per $1 of notional = E_w[g(p)/p], with w the notional weight."""
    return sum(w * fee_fn(p) / p for p, w in dist)


def solve_revenue_neutral(dist, fee_family, lo: float, hi: float, target: float,
                          iters: int = 200) -> float:
    """Bisect for the revenue-neutral parameter over a one-parameter curve family.
    fee_family(p, x) returns the per-share fee."""
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if revenue_per_notional(dist, lambda p: fee_family(p, mid)) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def uniform_dist(n: int = 300) -> list[tuple[float, float]]:
    """A uniform distribution for demonstration: the default when running the
    mathematics on its own, carrying no measured constant."""
    return [((i + 0.5) / n, 1.0 / n) for i in range(n)]


# ── Layer 2: distribution calibration, derived from a measured tape ─────────

CALIBRATION = {
    # Aggregates measured over a tape. Replace them with your own to recalibrate.
    "rate": 0.05,                 # the prevailing per-market rate in the sample
    "total_fee_usd": 5_524_178.0, # total taker fees over the sample period
    "flat_equivalent": 0.011898,  # revenue-neutral flat rate on notional
    # Notional weight in three bands: p<0.60 / 0.60-0.90 / p>=0.90
    "bands": ((0.001, 0.60, 0.280), (0.60, 0.90, 0.265), (0.90, 0.999, 0.455)),
}


def implied_notional() -> float:
    """Total fees divided by the flat rate gives implied taker notional, which
    cross-checks against an independently computed figure."""
    return CALIBRATION["total_fee_usd"] / CALIBRATION["flat_equivalent"]


def implied_mean_price() -> float:
    """Solve r * E_w[1-p] = flat for the notional-weighted mean price E_w[p]. A
    hard constraint implied by two known quantities."""
    return 1.0 - CALIBRATION["flat_equivalent"] / CALIBRATION["rate"]


def _band_points(lo: float, hi: float, n: int) -> list[float]:
    return [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]


def calibrated_dist(tilts=(1.0, 1.0, 1.0), n_per_band: int = 300):
    """Build a distribution from band weights; tilts control the linear slope
    inside each band (>0 leans toward higher prices)."""
    out = []
    for (lo, hi, w), t in zip(CALIBRATION["bands"], tilts):
        xs = _band_points(lo, hi, n_per_band)
        raw = [max(1.0 + t * (((x - lo) / (hi - lo)) - 0.5) * 2.0, 1e-9) for x in xs]
        s = sum(raw)
        out += [(x, w * v / s) for x, v in zip(xs, raw)]
    return out


def fit_dist(direction=(1.0, 1.0, 1.0), n_per_band: int = 300, iters: int = 200):
    """Scale along a given tilt direction so that E_w[p] hits the measured
    constraint. Returns (dist, scale).

    Four constraints — three band weights plus E_w[p] — are jointly solvable, so
    the distribution is not a free parameter. Different `direction` values give
    different within-band shapes that satisfy the same constraints, which is what
    the robustness check varies.
    """
    target = implied_mean_price()
    lo, hi = 0.0, 60.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        d = calibrated_dist(tuple(mid * x for x in direction), n_per_band)
        if sum(w * p for p, w in d) < target:
            lo = mid
        else:
            hi = mid
    s = 0.5 * (lo + hi)
    return calibrated_dist(tuple(s * x for x in direction), n_per_band), s


# ── Reports ─────────────────────────────────────────────────────────────────

SEGMENTS = ((0.0, 0.60, "p<0.60 informed band"), (0.60, 0.80, "0.60-0.80"),
            (0.80, 0.90, "0.80-0.90"), (0.90, 0.98, "0.90-0.98 carry band"),
            (0.98, 1.0, "p>=0.98 wash band"))


def report(m_values=(0.10, 0.15, 0.20, 0.25, 0.35, 0.50)) -> None:
    r = CALIBRATION["rate"]
    dist, _ = fit_dist()
    target = revenue_per_notional(dist, lambda p: fee_hump(p, r))

    print(f"implied taker notional  ${implied_notional()/1e6:,.1f}M")
    print(f"notional-weighted mean  E_w[p] = {implied_mean_price():.4f}")
    print(f"prevailing effective rate  {target*100:.4f}% per $1 notional\n")

    print("Burden distribution under the prevailing schedule")
    print("-" * 68)
    for lo, hi, label in SEGMENTS:
        nom = sum(w for p, w in dist if lo <= p < hi)
        fee = sum(w * fee_hump(p, r) / p for p, w in dist if lo <= p < hi)
        if nom <= 0:
            continue
        print(f"  {label:22s} notional {nom*100:5.1f}%   fees {fee/target*100:5.1f}%"
              f"   effective rate {fee/nom*100:.4f}%")

    print("\nRevenue-neutral solve for g(p) = r' * p * max(1 - p, m)")
    print("-" * 68)
    print(f"{'m':>6} {'r_new':>8} {'tax below':>11} {'ROI floor':>15} {'p*':>9}"
          f" {'cost per $1 vol':>16}")
    print(f"{'current':>6} {r:>8.4f} {'-':>11} {'-':>15} {'none':>9}"
          f" {fee_hump(0.998, r)/0.998*100:>15.4f}%")
    for m in m_values:
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        print(f"{m:>6.2f} {rp:>8.4f} {(rp/r-1)*100:>10.1f}% {rp*m*100:>14.4f}%"
              f" {wash_breakeven_price(rp, m):>9.5f} {rp*m*100:>15.4f}%")

    for m in (0.20, 0.35):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        print(f"\nDynamic impact (m={m}, r_new={rp:.4f}) - wash band exits by retention rate")
        print("-" * 68)
        for keep in (1.0, 0.5, 0.2, 0.0):
            rev = sum(w * (keep if p >= 0.98 else 1.0) * fee_floored(p, rp, m) / p
                      for p, w in dist)
            print(f"  wash retained {keep*100:5.0f}%   venue revenue {(rev/target-1)*100:+6.1f}%")
        lost = sum(w * fee_floored(p, rp, m) / p for p, w in dist if p >= 0.98)
        mid = sum(w * fee_floored(p, rp, m) / p for p, w in dist if p < 0.80)
        need = lost / mid
        print(f"  if wash fully exits: middle band needs +{need*100:.1f}% volume "
              f"while its unit cost falls {(1-rp/r)*100:.1f}%"
              f"  -> required elasticity {need/(1-rp/r):.2f}")


def robustness() -> None:
    """Check the stability of r' across four within-band shapes, all satisfying
    the same measured constraints."""
    r = CALIBRATION["rate"]
    print("Robustness: revenue-neutral r' across within-band shapes")
    print("-" * 68)
    for name, direction in (("uniform tilt", (1, 1, 1)), ("low-mid weighted", (1, 1, 0.001)),
                            ("mid-high weighted", (0.001, 1, 1)), ("high weighted", (0.2, 0.5, 1))):
        dist, _ = fit_dist(direction)
        ep = sum(w * p for p, w in dist)
        target = revenue_per_notional(dist, lambda p: fee_hump(p, r))
        row = []
        for m in (0.20, 0.35):
            rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
            row.append(f"m={m}: r´={rp:.4f}")
        hit = "OK" if abs(ep - implied_mean_price()) < 1e-3 else "constraint missed"
        print(f"  {name:12s} E_w[p]={ep:.4f} [{hit}]   " + "   ".join(row))


def selftest() -> int:
    r = CALIBRATION["rate"]
    fails = []

    # 1) implied notional agrees with the independent figure to within 2%
    if abs(implied_notional() / 464e6 - 1) > 0.02:
        fails.append(f"implied notional {implied_notional():.0f} is more than 2% "
                     f"off the independent figure")

    # 2) the E_w[p] identity
    if abs(r * (1 - implied_mean_price()) - CALIBRATION["flat_equivalent"]) > 1e-9:
        fails.append("E_w[p] is inconsistent with the flat rate")

    # 3) under the prevailing hump, wash net EV is always positive (no p* exists)
    for p in (0.90, 0.99, 0.999):
        if (1 - p) / p - fee_hump(p, r) / p <= 0:
            fails.append(f"carry net EV at p={p} should be positive under the "
                         f"prevailing schedule")

    # 4) revenue-neutral accuracy, r' < r, and p* inside (0.98, 1)
    dist, _ = fit_dist()
    target = revenue_per_notional(dist, lambda p: fee_hump(p, r))
    for m in (0.10, 0.20, 0.35, 0.50):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        got = revenue_per_notional(dist, lambda p: fee_floored(p, rp, m))
        if abs(got / target - 1) > 1e-6:
            fails.append(f"m={m} revenue-neutrality error {abs(got/target-1):.2e}")
        if not rp < r:
            fails.append(f"m={m}: r'={rp:.4f} should be strictly below r={r}")
        ps = wash_breakeven_price(rp, m)
        if not 0.98 < ps < 1.0:
            fails.append(f"m={m}: p*={ps:.5f} is not inside (0.98, 1)")

    # 5) the band decomposition sums to total revenue. It must aggregate fee/p,
    #    matching report(), not fee.
    seg_sum = sum(w * fee_hump(p, r) / p for p, w in dist)
    if abs(seg_sum / target - 1) > 1e-9:
        fails.append(f"band aggregation is wrong: sum={seg_sum:.6f} vs target={target:.6f}")
    by_seg = sum(sum(w * fee_hump(p, r) / p for p, w in dist if lo <= p < hi)
                 for lo, hi, _ in SEGMENTS)
    if abs(by_seg / target - 1) > 1e-6:
        fails.append(f"SEGMENTS does not cover the domain: sum={by_seg:.6f} vs target={target:.6f}")

    # 6) at revenue neutrality, venue revenue is unchanged when wash volume stays
    for m in (0.20, 0.35):
        rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
        rev = revenue_per_notional(dist, lambda p: fee_floored(p, rp, m))
        if abs(rev / target - 1) > 1e-6:
            fails.append(f"m={m}: revenue should be neutral at unchanged volume, "
                         f"got {(rev/target-1)*100:+.2f}%")

    # 7) the curve is continuous at p = 1 - m
    for m in (0.20, 0.35):
        eps = 1e-9
        lhs, rhs = fee_floored(1 - m - eps, r, m), fee_floored(1 - m + eps, r, m)
        if abs(lhs - rhs) > 1e-9:
            fails.append(f"m={m}: discontinuous at p = 1 - m")

    # 8) below 1 - m the tax falls proportionally: the shape is unchanged
    m, = (0.20,)
    rp = solve_revenue_neutral(dist, lambda p, x: fee_floored(p, x, m), 1e-4, 0.6, target)
    ratios = [fee_floored(p, rp, m) / fee_hump(p, r) for p in (0.05, 0.2, 0.4, 0.6, 0.79)]
    if max(ratios) - min(ratios) > 1e-9:
        fails.append("below 1 - m the tax should fall proportionally")

    # 9) the pure-algebra layer does not depend on calibration constants
    ud = uniform_dist(50)
    t = revenue_per_notional(ud, lambda p: fee_hump(p, 0.07))
    if not 0 < t < 0.07:
        fails.append("effective rate on the uniform distribution is out of range")

    for f in fails:
        print(f"FAIL  {f}")
    print(f"selftest: {'PASS' if not fails else str(len(fails)) + ' FAIL'} (9 assertion groups)")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--robustness", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if args.robustness:
        robustness()
    else:
        report()
