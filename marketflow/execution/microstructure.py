"""Order-book microstructure signal.

Computes, from one snapshot of a binary market's book:
  - imbalance I = (Qb - Qa) / (Qb + Qa)  in [-1, 1]   (Qb,Qa = top-N depth)
  - micro_price = (bb*Qa + ba*Qb) / (Qb + Qa)         (standard microstructure
        fair value: sits closer to the side with MORE opposite-side size, i.e. the
        side the next marketable order is more likely to lift)
  - a directional P(YES) signal = the micro_price (clipped), which is the book's
    OWN estimate of P(YES), independent of any model opinion.

Whether micro_price predicts the binary resolution better than the displayed mid
is an empirical question for a forward calibration study. This module only
COMPUTES the signal and a confidence; it places no order and makes no edge claim
on its own.

Pure functions, no I/O. Book sizes can come from the price websocket's `book`
stream (`marketflow.execution.price_ws`).
"""

from __future__ import annotations

import math
from typing import Any, Optional

EPS = 1e-9


def _f(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _levels(side: Any) -> list[tuple[float, float]]:
    """Normalize a book side to [(price, size), ...]; accepts dicts or pairs."""
    out: list[tuple[float, float]] = []
    for lvl in side or []:
        if isinstance(lvl, dict):
            p, s = _f(lvl.get("price")), _f(lvl.get("size"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            p, s = _f(lvl[0]), _f(lvl[1])
        else:
            continue
        if p is not None and s is not None and s > 0:
            out.append((p, s))
    return out


def microstructure_features(bids: Any, asks: Any, *, depth_levels: int = 3) -> dict:
    """Compute imbalance / micro_price / spread / depth from one book snapshot.

    bids/asks: lists of {price,size} or (price,size). Returns ok=False (no signal)
    when either side is empty — a one-sided book has no microstructure fair value.
    """
    b = sorted(_levels(bids), key=lambda x: -x[0])   # best (highest) bid first
    a = sorted(_levels(asks), key=lambda x: x[0])    # best (lowest) ask first
    if not b or not a:
        return {"ok": False, "reason": "one-sided or empty book; no microstructure signal"}

    best_bid, best_ask = b[0][0], a[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread = max(0.0, best_ask - best_bid)

    qb = sum(s for _, s in b[:depth_levels])
    qa = sum(s for _, s in a[:depth_levels])
    denom = qb + qa
    if denom <= EPS:
        return {"ok": False, "reason": "zero depth; no microstructure signal"}

    imbalance = (qb - qa) / denom
    # standard micro-price: weight each best quote by the OPPOSITE side's size.
    micro_price = (best_bid * qa + best_ask * qb) / denom

    return {
        "ok": True,
        "best_bid": round(best_bid, 6),
        "best_ask": round(best_ask, 6),
        "mid": round(mid, 6),
        "spread": round(spread, 6),
        "bid_depth": round(qb, 4),
        "ask_depth": round(qa, 4),
        "imbalance": round(imbalance, 6),
        "micro_price": round(micro_price, 6),
        "micro_minus_mid": round(micro_price - mid, 6),
        "depth_levels": depth_levels,
    }


def microstructure_signal(bids: Any, asks: Any, *, depth_levels: int = 3,
                          min_total_depth: float = 0.0, max_spread: float = 0.10) -> dict:
    """Turn the book into a directional P(YES) signal + a confidence.

    p_yes_signal = micro_price (the book's size-weighted fair P(YES)). direction is
    YES if micro_price sits above the mid (more size resting to lift the ask),
    NO if below. confidence shrinks with a wide spread and thin depth — a thin /
    wide book carries little microstructure information.
    """
    feat = microstructure_features(bids, asks, depth_levels=depth_levels)
    if not feat.get("ok"):
        return {"ok": False, "reason": feat.get("reason"), "p_yes_signal": None, "direction": "NONE"}

    micro = feat["micro_price"]
    mid = feat["mid"]
    total_depth = feat["bid_depth"] + feat["ask_depth"]
    p_yes = min(1.0 - 1e-6, max(1e-6, micro))

    # confidence in [0,1]: tight spread + real depth => trust the micro-price.
    spread_factor = max(0.0, 1.0 - feat["spread"] / max(EPS, max_spread))
    depth_factor = total_depth / (total_depth + 50.0)   # saturating; 50 shares ~ 0.5
    confidence = round(max(0.0, min(1.0, spread_factor * depth_factor)), 6)

    thin = total_depth < min_total_depth or feat["spread"] > max_spread
    direction = "NONE"
    if not thin and abs(feat["micro_minus_mid"]) > 1e-4:
        direction = "YES" if feat["micro_minus_mid"] > 0 else "NO"

    return {
        "ok": True,
        "p_yes_signal": round(p_yes, 6),    # the book's own P(YES) estimate
        "direction": direction,             # which way the book leans vs its mid
        "confidence": confidence,
        "thin_book": bool(thin),
        **feat,
    }


# --------------------------------------------------------------------------- #
# Selftest: synthetic books, pure.
# --------------------------------------------------------------------------- #

def selftest() -> dict:
    checks: dict[str, bool] = {}

    # Symmetric book => micro_price == mid, imbalance 0, no direction.
    sym = microstructure_signal(
        bids=[{"price": 0.49, "size": 100}], asks=[{"price": 0.51, "size": 100}])
    checks["symmetric_micro_equals_mid"] = abs(sym["micro_minus_mid"]) < 1e-9
    checks["symmetric_zero_imbalance"] = abs(sym["imbalance"]) < 1e-9
    checks["symmetric_no_direction"] = sym["direction"] == "NONE"

    # Heavy BID size => micro_price leans toward the ASK (buyers will lift it) =>
    # micro_price > mid => YES lean, positive imbalance.
    heavy_bid = microstructure_signal(
        bids=[{"price": 0.49, "size": 900}], asks=[{"price": 0.51, "size": 100}])
    checks["heavy_bid_positive_imbalance"] = heavy_bid["imbalance"] > 0
    checks["heavy_bid_micro_above_mid"] = heavy_bid["micro_minus_mid"] > 0
    checks["heavy_bid_yes_direction"] = heavy_bid["direction"] == "YES"

    # Heavy ASK size => micro below mid => NO lean.
    heavy_ask = microstructure_signal(
        bids=[{"price": 0.49, "size": 100}], asks=[{"price": 0.51, "size": 900}])
    checks["heavy_ask_no_direction"] = heavy_ask["direction"] == "NO"
    checks["heavy_ask_micro_below_mid"] = heavy_ask["micro_minus_mid"] < 0

    # One-sided / empty book => no signal.
    checks["one_sided_no_signal"] = microstructure_signal(bids=[], asks=[{"price": 0.5, "size": 10}])["ok"] is False

    # Wide spread => thin_book true, confidence low.
    wide = microstructure_signal(
        bids=[{"price": 0.30, "size": 100}], asks=[{"price": 0.70, "size": 100}], max_spread=0.10)
    checks["wide_spread_flagged_thin"] = wide["thin_book"] is True
    checks["wide_spread_low_confidence"] = wide["confidence"] < 0.2

    # p_yes_signal stays a valid probability.
    checks["p_yes_in_unit_interval"] = 0.0 < heavy_bid["p_yes_signal"] < 1.0

    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "passed": sum(checks.values()), "total": len(checks)}


if __name__ == "__main__":
    import json
    import sys
    r = selftest()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
