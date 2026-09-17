#!/usr/bin/env python3
"""Polymarket market-quality gate — longshot / probability-band hard filters
plus configurable resolution / edge advisory policy for autonomous entries.

Single source of truth for "should MarketFlow be allowed to BUY into this market at
all?", independent of the capital fuses (caps / kill / arm-state, which live in
polymarket_execution). Pure functions: no network, no SDK, no secrets — so the
chat enqueue tool, the daemon entry path, and any packet builder all share the
exact same thresholds.

Why this exists: an autonomous entry can pass every capital fuse ($1 cap, FOK,
idempotency, arm-state) and still be a terrible bet — a sub-$0.15 longshot
(retail loses >60% on <15c contracts; the South Africa @0.08 loss was exactly
this), an out-of-band price, or a position with no real edge over the executable
price. Longshot / probability-band rejects happen before any sign/POST.
Resolution and edge/model-probability are advisory by default and become hard
only when owner config asks for that. The capital fuses still apply on top.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "polymarket-market-gate-v0.1"
PRICE_EXPRESSION_SCHEMA_VERSION = "polymarket-price-expression-gate-v0.1"

# --- Cost of expressing a view at a price: the venue's taker fee -------------
# fee = rate * p * (1 - p) per share; a maker (post-only limit) pays zero. That
# gives two quantities in different units:
#   absolute (probability points): fee_per_share = rate * p * (1 - p)
#       -> same unit as edge = p_model - px
#   relative (per $1 deployed):    fee_per_share / px = rate * (1 - px)
#       -> same unit as ROI = (p_model - px) / px
# The sign of net edge is identical either way (for px > 0 they differ by a
# positive divisor), so a gate can use either. Both are audited, because the
# decision "at which price should this alpha be expressed" reads the relative
# figure while the refusal computes the absolute one. Mixing the units is wrong by
# an order of magnitude as px approaches zero.
FEE_RATE_FALLBACK = 0.05      # fallback when per-market feeSchedule.rate is unreadable;
                              # the overwhelming majority of markets use this rate
BUY_PRICE_FLOOR = 0.10        # Default no-go band. Measured over a full tape, this band
                              # is a little over 1% of deployed capital and lost more
                              # than the entire sample's net loss; excluding it leaves
                              # the remaining 98.6% break-even after fees. A gate, not
                              # a continuous penalty: below 2c the bias disappears, so
                              # "cheaper is worse" would be false. The damage is in the
                              # 2-5 cent band specifically.

PRICE_EXPRESSION_REJECT_CODES = (
    "BAD_ENTRY_PRICE",
    "BUY_PRICE_FLOOR_REJECT",
    "NEGATIVE_EDGE_AFTER_FEE",
)

# --- thresholds: single source of truth (cited in the Rail E scorecard) ------
# All bands are on the market-implied probability of the side being BOUGHT,
# which equals that side's executable ask price.
LONGSHOT_REJECT_LOW = 0.15    # below: deep longshot (retail structurally loses on <15c; the SA @0.08 loss)
LONGSHOT_REJECT_HIGH = 0.85   # above: near-certain side priced >85c — the OTHER side is the deep longshot
# Inner band collapsed onto the longshot guard. The
# old [0.30,0.70] "only near-coinflips" band was a hand-picked aesthetic constant
# that rejected every favorite/underdog and ~90% of sports markets before any bet.
# Policy now: any non-deep-longshot, clean-resolving market is entry-eligible.
# Edge is NOT pre-judged at the gate; it is managed dynamically AFTER entry
# (in-play / exit). A caller may still pass a tighter auto_band explicitly.
AUTO_BAND_LOW = 0.15
AUTO_BAND_HIGH = 0.85
DEFAULT_MIN_EDGE_AFTER_COST = 0.04   # only a hard reject when edge_advisory=False; informational otherwise
DEFAULT_COST_BUFFER = 0.02           # conservative fee + spread + slippage reserve

REJECT_CODES = (
    "BAD_MARKET_PRICE",
    "LONGSHOT_REJECT",
    "PROBABILITY_BAND_REJECT",
    "RESOLUTION_NOT_CONFIRMED_CLEAN",
    "NO_MODEL_PROBABILITY",
    "LOW_EDGE_AFTER_COST",
)


def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def classify_market_band(
    price: Any,
    *,
    longshot_low: float = LONGSHOT_REJECT_LOW,
    longshot_high: float = LONGSHOT_REJECT_HIGH,
    auto_band_low: float = AUTO_BAND_LOW,
    auto_band_high: float = AUTO_BAND_HIGH,
) -> str:
    """Coarse band of the BUY-side market price: 'BAD' | 'LONGSHOT' | 'BAND' | 'AUTO'."""
    p = _to_float(price)
    if p is None or not (0.0 < p < 1.0):
        return "BAD"
    if p < longshot_low or p > longshot_high:
        return "LONGSHOT"
    if p < auto_band_low or p > auto_band_high:
        return "BAND"
    return "AUTO"


def market_quality_gate(
    *,
    entry_ask_price: Any,
    model_probability: Any = None,
    resolution_confirmed_clean: Any = None,
    require_model_probability: bool = True,
    require_resolution: bool = True,
    min_edge_after_cost: float = DEFAULT_MIN_EDGE_AFTER_COST,
    cost_buffer: float = DEFAULT_COST_BUFFER,
    edge_advisory: bool = True,
    longshot_low: float = LONGSHOT_REJECT_LOW,
    longshot_high: float = LONGSHOT_REJECT_HIGH,
    auto_band_low: float = AUTO_BAND_LOW,
    auto_band_high: float = AUTO_BAND_HIGH,
) -> dict[str, Any]:
    """Market-quality verdict for a BUY entry.

    APPROVED requires the configured market-implied price band and clean
    resolution confirmation to pass. Callers that only need coarse band
    classification must explicitly set require_resolution=False.

    The edge/model-probability terms are the private-signal layer. With the
    default `edge_advisory=True`, a missing model probability or sub-threshold
    edge is recorded but not a hard gate. Either way this gate never authorizes
    execution by itself.
    """
    reject: list[str] = []
    p = _to_float(entry_ask_price)
    band = classify_market_band(
        p,
        longshot_low=longshot_low,
        longshot_high=longshot_high,
        auto_band_low=auto_band_low,
        auto_band_high=auto_band_high,
    )
    if band == "BAD":
        reject.append("BAD_MARKET_PRICE")
    elif band == "LONGSHOT":
        reject.append("LONGSHOT_REJECT")
    elif band == "BAND":
        reject.append("PROBABILITY_BAND_REJECT")

    if require_resolution and resolution_confirmed_clean is not True:
        reject.append("RESOLUTION_NOT_CONFIRMED_CLEAN")

    edge = None
    mp = _to_float(model_probability)
    if mp is None:
        if require_model_probability and not edge_advisory:
            reject.append("NO_MODEL_PROBABILITY")
    elif p is not None and 0.0 < p < 1.0:
        edge = mp - p - max(0.0, cost_buffer)
        if edge < min_edge_after_cost and not edge_advisory:
            reject.append("LOW_EDGE_AFTER_COST")

    return {
        "schema_version": SCHEMA_VERSION,
        "authorization": "APPROVED" if not reject else "REJECTED",
        "reject_reason_codes": reject,
        "band": band,
        "edge_advisory": edge_advisory,
        "market_implied_probability": round(p, 6) if p is not None else None,
        "model_probability": round(mp, 6) if mp is not None else None,
        "edge_after_cost": round(edge, 6) if edge is not None else None,
        "min_edge_after_cost": min_edge_after_cost,
        "cost_buffer": cost_buffer,
        "thresholds": {
            "longshot": [longshot_low, longshot_high],
            "auto_band": [auto_band_low, auto_band_high],
        },
    }


def taker_fee_per_share(price: Any, rate: Any) -> float | None:
    """Taker fee = `rate * p * (1 - p)` per share. A maker (post-only limit) pays
    zero; which side pays is the caller's call based on execution mode, and this
    function computes the taker leg only.

    Unit: probability points per share, the same unit as `edge = p_model - px`, so
    the two can be subtracted directly. At rate = 0.05 this is the familiar
    "5 * p(1-p) probability points" threshold.
    """
    p = _to_float(price)
    r = _to_float(rate)
    if p is None or r is None or not (0.0 <= p <= 1.0) or r < 0:
        return None
    return r * p * (1.0 - p)


def fee_rate_effective(price: Any, rate: Any) -> float | None:
    """Relative cost per $1 deployed = `taker_fee_per_share / px` = `rate * (1 - px)`.

    This is the threshold that decides at which price a given alpha should be
    expressed: at px = 0.05 it takes 4.75% pre-fee ROI to break even, at px = 0.99
    only 0.05% — a factor of 55 between the ends. Unit: ROI, the same unit as
    `(p_model - px) / px`. **It must not be subtracted from an absolute edge**; as
    px approaches zero that is wrong by an order of magnitude.
    """
    p = _to_float(price)
    r = _to_float(rate)
    if p is None or r is None or not (0.0 < p <= 1.0) or r < 0:
        return None
    return r * (1.0 - p)


def price_expression_gate(
    *,
    entry_price: Any,
    model_probability: Any = None,
    fee_rate: Any = None,
    maker: bool = True,
    fee_rate_source: str = "unknown",
    price_floor: float = BUY_PRICE_FLOOR,
    floor_override_reason: Any = None,
    enforce: bool = True,
) -> dict[str, Any]:
    """Price-expression gate — does a BUY at *this* price still have positive
    expectation after fees?

    Two tests:
      1. `px < price_floor` -> refuse.
      2. `edge_after_fee = (p_model - px) - fee_per_share <= 0` -> refuse.
         `fee_per_share` follows the execution mode: a maker (post-only) pays 0, a
         taker pays `rate * px * (1 - px)`. With no model probability this test is
         skipped — the order layer often has exactly that view, because it never
         sees the signal.

    `enforce=False` is shadow mode: everything is computed and reported, but
    authorization stays APPROVED and the verdicts land in `would_reject_codes`.
    Use it to quantify the impact before switching a gate on.

    `fee_rate` must be the per-market `feeSchedule.rate`. When it is None the
    fallback is used and `fee_rate_source` is marked as such — a per-market rate is
    required, and a hardcoded constant is not an acceptable substitute.
    """
    reject: list[str] = []
    px = _to_float(entry_price)
    if px is None or not (0.0 < px < 1.0):
        reject.append("BAD_ENTRY_PRICE")

    rate = _to_float(fee_rate)
    if rate is None or rate < 0:
        rate = FEE_RATE_FALLBACK
        fee_rate_source = "fallback"

    override = str(floor_override_reason).strip() if floor_override_reason is not None else ""
    floor = max(0.0, _to_float(price_floor) if _to_float(price_floor) is not None else BUY_PRICE_FLOOR)
    if px is not None and 0.0 < px < 1.0 and px < floor and not override:
        reject.append("BUY_PRICE_FLOOR_REJECT")

    fee_abs = taker_fee_per_share(px, rate)
    fee_rel = fee_rate_effective(px, rate)
    paid_abs = 0.0 if maker else fee_abs
    paid_rel = 0.0 if maker else fee_rel

    edge = edge_roi = edge_after_fee = edge_after_fee_roi = None
    mp = _to_float(model_probability)
    if mp is not None and px is not None and 0.0 < px < 1.0:
        edge = mp - px
        edge_roi = edge / px
        edge_after_fee = edge - (paid_abs or 0.0)
        edge_after_fee_roi = edge_roi - (paid_rel or 0.0)
        if edge_after_fee <= 0.0:
            reject.append("NEGATIVE_EDGE_AFTER_FEE")

    def _r(v: float | None, nd: int = 8) -> float | None:
        return round(v, nd) if v is not None else None

    return {
        "schema_version": PRICE_EXPRESSION_SCHEMA_VERSION,
        "authorization": "APPROVED" if (not reject or not enforce) else "REJECTED",
        "enforced": bool(enforce),
        "reject_reason_codes": reject if enforce else [],
        "would_reject_codes": reject,
        "entry_price": _r(px),
        "fee_rate": _r(rate),
        "fee_rate_source": fee_rate_source,
        "maker": bool(maker),
        "fee_per_share": _r(paid_abs),
        "fee_rate_effective": _r(paid_rel),
        "taker_fee_per_share_if_crossed": _r(fee_abs),
        "taker_fee_rate_effective_if_crossed": _r(fee_rel),
        "model_probability": _r(mp, 6),
        "edge": _r(edge),
        "edge_roi": _r(edge_roi),
        "edge_after_fee": _r(edge_after_fee),
        "edge_after_fee_roi": _r(edge_after_fee_roi),
        "price_floor": _r(floor, 6),
        "floor_override_reason": override or None,
    }


def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    r = market_quality_gate(entry_ask_price=0.08, model_probability=0.9, resolution_confirmed_clean=True)
    checks["longshot_low_rejected"] = "LONGSHOT_REJECT" in r["reject_reason_codes"] and r["authorization"] == "REJECTED"
    r = market_quality_gate(entry_ask_price=0.92, model_probability=0.99, resolution_confirmed_clean=True)
    checks["longshot_high_rejected"] = "LONGSHOT_REJECT" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.22, model_probability=0.9, resolution_confirmed_clean=True)
    checks["underdog_admitted"] = "PROBABILITY_BAND_REJECT" not in r["reject_reason_codes"] and r["authorization"] == "APPROVED"
    r = market_quality_gate(entry_ask_price=0.78, model_probability=0.99, resolution_confirmed_clean=True)
    checks["favorite_admitted"] = "PROBABILITY_BAND_REJECT" not in r["reject_reason_codes"] and r["authorization"] == "APPROVED"
    r = market_quality_gate(entry_ask_price=0.22, model_probability=0.9, resolution_confirmed_clean=True, auto_band_low=0.30, auto_band_high=0.70)
    checks["explicit_tight_band_still_rejects"] = "PROBABILITY_BAND_REJECT" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.0, model_probability=0.5, resolution_confirmed_clean=True)
    checks["bad_price_zero_rejected"] = "BAD_MARKET_PRICE" in r["reject_reason_codes"]
    r = market_quality_gate(entry_ask_price=1.5, model_probability=0.5, resolution_confirmed_clean=True)
    checks["bad_price_high_rejected"] = "BAD_MARKET_PRICE" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.8, resolution_confirmed_clean=None)
    checks["unset_resolution_default_rejected"] = "RESOLUTION_NOT_CONFIRMED_CLEAN" in r["reject_reason_codes"]
    r = market_quality_gate(
        entry_ask_price=0.5,
        model_probability=0.8,
        resolution_confirmed_clean=None,
        require_resolution=False,
    )
    checks["unset_resolution_explicit_band_only_allowed"] = "RESOLUTION_NOT_CONFIRMED_CLEAN" not in r["reject_reason_codes"]
    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.8, resolution_confirmed_clean=False, require_resolution=True)
    checks["false_resolution_rejected_when_required"] = "RESOLUTION_NOT_CONFIRMED_CLEAN" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.5, model_probability=None, resolution_confirmed_clean=True)
    checks["no_model_probability_default_informational"] = "NO_MODEL_PROBABILITY" not in r["reject_reason_codes"]
    r = market_quality_gate(entry_ask_price=0.5, model_probability=None, resolution_confirmed_clean=True, edge_advisory=False)
    checks["no_model_probability_strict_rejected"] = "NO_MODEL_PROBABILITY" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.52, resolution_confirmed_clean=True)
    checks["low_edge_default_informational"] = "LOW_EDGE_AFTER_COST" not in r["reject_reason_codes"]
    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.52, resolution_confirmed_clean=True, edge_advisory=False)
    checks["low_edge_strict_rejected"] = "LOW_EDGE_AFTER_COST" in r["reject_reason_codes"]

    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.62, resolution_confirmed_clean=True)
    checks["clean_entry_approved"] = r["authorization"] == "APPROVED" and not r["reject_reason_codes"]
    checks["clean_entry_edge_value"] = abs((r["edge_after_cost"] or 0.0) - (0.62 - 0.5 - 0.02)) < 1e-9

    checks["band_auto"] = classify_market_band(0.5) == "AUTO"
    checks["band_longshot"] = classify_market_band(0.08) == "LONGSHOT"
    checks["band_25_now_auto"] = classify_market_band(0.25) == "AUTO"
    checks["band_tier_via_explicit"] = classify_market_band(0.25, auto_band_low=0.30) == "BAND"
    checks["band_bad"] = classify_market_band(1.2) == "BAD"
    # boundary semantics (symmetric): 0.15 / 0.85 are the longshot/AUTO edges; everything between is AUTO.
    checks["boundary_low_auto"] = classify_market_band(0.15) == "AUTO"
    checks["boundary_high_auto"] = classify_market_band(0.85) == "AUTO"
    checks["just_below_low_longshot"] = classify_market_band(0.149) == "LONGSHOT"
    checks["just_above_high_longshot"] = classify_market_band(0.851) == "LONGSHOT"

    # enqueue-style coarse use: band-only, no model probability / resolution required.
    r = market_quality_gate(entry_ask_price=0.5, require_model_probability=False, require_resolution=False)
    checks["band_only_auto_pass"] = r["authorization"] == "APPROVED"
    r = market_quality_gate(entry_ask_price=0.08, require_model_probability=False, require_resolution=False)
    checks["band_only_longshot_reject"] = "LONGSHOT_REJECT" in r["reject_reason_codes"]

    # edge_advisory (autonomous / owner-directed entry policy): the engine-edge
    # terms become informational, objective filters still bind.
    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.52, resolution_confirmed_clean=True, edge_advisory=True)
    checks["advisory_low_edge_not_rejected"] = r["authorization"] == "APPROVED" and "LOW_EDGE_AFTER_COST" not in r["reject_reason_codes"]
    checks["advisory_reports_edge_value"] = abs((r["edge_after_cost"] or 0.0) - (0.52 - 0.5 - 0.02)) < 1e-9
    r = market_quality_gate(entry_ask_price=0.5, model_probability=None, resolution_confirmed_clean=True, edge_advisory=True)
    checks["advisory_no_model_not_rejected"] = r["authorization"] == "APPROVED" and "NO_MODEL_PROBABILITY" not in r["reject_reason_codes"]
    r = market_quality_gate(entry_ask_price=0.08, model_probability=0.9, resolution_confirmed_clean=True, edge_advisory=True)
    checks["advisory_keeps_longshot_filter"] = "LONGSHOT_REJECT" in r["reject_reason_codes"] and r["authorization"] == "REJECTED"
    r = market_quality_gate(entry_ask_price=0.5, model_probability=0.62, resolution_confirmed_clean=False, edge_advisory=True)
    checks["advisory_resolution_still_rejected"] = "RESOLUTION_NOT_CONFIRMED_CLEAN" in r["reject_reason_codes"]
    r = market_quality_gate(
        entry_ask_price=0.18,
        model_probability=0.9,
        resolution_confirmed_clean=True,
        longshot_low=0.20,
        auto_band_low=0.25,
    )
    checks["configurable_longshot_threshold"] = "LONGSHOT_REJECT" in r["reject_reason_codes"]

    # --- price-expression gate -------------------------------------------------
    # Analytic anchors: the relative threshold is rate * (1 - p) at five points;
    # the absolute one is rate * p * (1 - p), i.e. the "5 * p(1-p) probability
    # points" figure at rate = 0.05.
    rate = 0.05
    for p_pt in (0.02, 0.10, 0.50, 0.95, 0.99):
        checks[f"fee_rate_effective_p{p_pt}"] = abs(fee_rate_effective(p_pt, rate) - rate * (1 - p_pt)) < 1e-12
        checks[f"fee_per_share_p{p_pt}"] = abs(taker_fee_per_share(p_pt, rate) - rate * p_pt * (1 - p_pt)) < 1e-12
        g = price_expression_gate(entry_price=p_pt, model_probability=None, fee_rate=rate, maker=False)
        checks[f"gate_fee_matches_analytic_p{p_pt}"] = (
            abs(g["fee_rate_effective"] - rate * (1 - p_pt)) < 1e-8
            and abs(g["fee_per_share"] - rate * p_pt * (1 - p_pt)) < 1e-8
        )
    # two published figures for the probability-point threshold
    checks["spec_threshold_p50_is_1_25pp"] = abs(taker_fee_per_share(0.50, 0.05) - 0.0125) < 1e-12
    checks["spec_threshold_p95_is_0_238pp"] = abs(taker_fee_per_share(0.95, 0.05) - 0.002375) < 1e-12
    # The hump and the 55x spread: per-share fee peaks at p = 0.5 while the
    # relative cost falls monotonically.
    checks["fee_per_share_peaks_at_half"] = (
        taker_fee_per_share(0.5, rate) > taker_fee_per_share(0.05, rate)
        and taker_fee_per_share(0.5, rate) > taker_fee_per_share(0.95, rate)
    )
    checks["fee_rate_effective_monotone_down"] = (
        fee_rate_effective(0.02, rate) > fee_rate_effective(0.5, rate) > fee_rate_effective(0.99, rate)
    )

    # The units must not be mixed: near px = 0 the absolute threshold is an order
    # of magnitude below the relative one. px = 0.05 is the CHEAPEST place to
    # express a view, not the most expensive, and using the relative figure as an
    # absolute threshold would refuse it by mistake.
    checks["units_not_interchangeable_at_low_px"] = (
        taker_fee_per_share(0.05, rate) < fee_rate_effective(0.05, rate) / 10
    )

    # a maker pays zero, a taker pays the real fee
    g_mk = price_expression_gate(entry_price=0.50, model_probability=0.52, fee_rate=rate, maker=True)
    g_tk = price_expression_gate(entry_price=0.50, model_probability=0.52, fee_rate=rate, maker=False)
    checks["maker_pays_zero_fee"] = g_mk["fee_per_share"] == 0.0 and g_mk["edge_after_fee"] == g_mk["edge"]
    checks["taker_pays_real_fee"] = abs(g_tk["fee_per_share"] - 0.0125) < 1e-9
    checks["taker_fee_reported_even_for_maker"] = abs(g_mk["taker_fee_per_share_if_crossed"] - 0.0125) < 1e-9

    # Refusal test: one probability point of alpha is negative for a taker at
    # p = 0.5 and positive at p = 0.95.
    g = price_expression_gate(entry_price=0.50, model_probability=0.51, fee_rate=rate, maker=False)
    checks["one_point_alpha_at_half_rejected"] = "NEGATIVE_EDGE_AFTER_FEE" in g["reject_reason_codes"]
    g = price_expression_gate(entry_price=0.95, model_probability=0.96, fee_rate=rate, maker=False)
    checks["one_point_alpha_at_95_approved"] = g["authorization"] == "APPROVED" and g["edge_after_fee"] > 0
    checks["one_point_alpha_at_95_roi_positive"] = g["edge_after_fee_roi"] > 0.0

    # with no model probability only the price floor applies
    g = price_expression_gate(entry_price=0.50, model_probability=None, fee_rate=rate, maker=False)
    checks["no_model_prob_skips_edge_check"] = (
        g["authorization"] == "APPROVED" and g["edge_after_fee"] is None
    )

    # the sub-10c no-go band, and its explicit override
    g = price_expression_gate(entry_price=0.06, model_probability=0.9, fee_rate=rate)
    checks["floor_rejects_sub_10c"] = "BUY_PRICE_FLOOR_REJECT" in g["reject_reason_codes"]
    g = price_expression_gate(entry_price=0.10, model_probability=0.9, fee_rate=rate)
    checks["floor_boundary_10c_allowed"] = "BUY_PRICE_FLOOR_REJECT" not in g["reject_reason_codes"]
    g = price_expression_gate(entry_price=0.06, model_probability=0.9, fee_rate=rate,
                              floor_override_reason="owner: negRisk hedge leg")
    checks["floor_override_admits_with_reason"] = (
        g["authorization"] == "APPROVED" and g["floor_override_reason"] == "owner: negRisk hedge leg"
    )
    # Not a continuous "cheaper is worse" penalty: 0.01 and 0.09 get the same
    # verdict because it is one gate, and below 2c the relative cost is barely
    # worse than at 5c. The damage in the 2-5 cent band is behavioural, not a fee
    # effect.
    g_low = price_expression_gate(entry_price=0.01, model_probability=0.9, fee_rate=rate)
    g_hi = price_expression_gate(entry_price=0.09, model_probability=0.9, fee_rate=rate)
    checks["floor_is_a_gate_not_a_ramp"] = (
        g_low["reject_reason_codes"] == g_hi["reject_reason_codes"] == ["BUY_PRICE_FLOOR_REJECT"]
    )

    # shadow mode: verdicts computed, authorization unchanged
    g = price_expression_gate(entry_price=0.06, model_probability=0.0615, fee_rate=rate,
                              maker=False, enforce=False)
    checks["shadow_never_rejects"] = g["authorization"] == "APPROVED" and g["reject_reason_codes"] == []
    checks["shadow_still_reports_verdict"] = set(g["would_reject_codes"]) == {
        "BUY_PRICE_FLOOR_REJECT", "NEGATIVE_EDGE_AFTER_FEE"}
    checks["shadow_flag_recorded"] = g["enforced"] is False

    # the per-market rate must be used; when absent, fall back and label the source
    g = price_expression_gate(entry_price=0.50, fee_rate=0.03, maker=False)
    checks["per_market_rate_used"] = abs(g["fee_per_share"] - 0.03 * 0.25) < 1e-9
    g = price_expression_gate(entry_price=0.50, fee_rate=None, maker=False)
    checks["missing_rate_falls_back_and_labels"] = (
        g["fee_rate"] == FEE_RATE_FALLBACK and g["fee_rate_source"] == "fallback"
    )
    g = price_expression_gate(entry_price=0.50, fee_rate=0.0, maker=False, fee_rate_source="gamma")
    checks["zero_fee_market_is_not_missing"] = (
        g["fee_rate"] == 0.0 and g["fee_rate_source"] == "gamma" and g["fee_per_share"] == 0.0
    )
    g = price_expression_gate(entry_price=1.0, model_probability=0.5, fee_rate=rate)
    checks["bad_entry_price_rejected"] = "BAD_ENTRY_PRICE" in g["reject_reason_codes"]

    ok = all(checks.values())
    return {
        "schema_version": "polymarket-market-gate-selftest-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "PASS": ok,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Polymarket market-quality gate (longshot/band/resolution/min-edge)")
    ap.add_argument("--selftest", action="store_true", help="Run offline selftest.")
    ap.add_argument("--ask", type=float, help="BUY-side ask price to evaluate.")
    ap.add_argument("--model-prob", type=float, default=None, help="Independent model probability for the bought side.")
    ap.add_argument("--resolution-clean", action="store_true", help="Mark resolution as confirmed clean.")
    args = ap.parse_args(argv)
    if args.selftest:
        rep = selftest()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["PASS"] else 1
    if args.ask is not None:
        rep = market_quality_gate(
            entry_ask_price=args.ask,
            model_probability=args.model_prob,
            resolution_confirmed_clean=args.resolution_clean or None,
        )
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
