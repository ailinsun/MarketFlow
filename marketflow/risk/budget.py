#!/usr/bin/env python3
"""Dynamic position budget — size a trade from real event exposure, and let the
number of concurrent positions be a **result** rather than a parameter.

## 0. Why this module exists

The common shape is `per_trade = min(arm_cap, max(exchange_min, cash *
EQUITY_FRACTION))`, where EQUITY_FRACTION is a hardcoded constant, often picked in
a hurry to stop one trade dominating a small account. That shape has three
problems:

1. **Nobody designed the concurrency.** `1 / EQUITY_FRACTION` becomes the de facto
   cap on simultaneous positions as an arithmetic side effect, not as a risk
   structure anyone chose. Worse, using that side effect to test "does more
   capital help" gives a circular answer: it measures the height of a wall you
   built yourself. When a gate blocks the objective, rebuild the gate; do not
   measure it as if it were a law of nature.

2. **Book exposure systematically overstates risk.** Selling several NO legs
   across a mutually exclusive ladder can lose **at most one** of them, so
   counting notional treats one risk as N. On the same set of positions the
   correlation-adjusted effective count can exceed the position count outright:
   negative correlation makes effective diversification higher than the number of
   legs.

3. **The ratio itself moves.** Structural offset changes with the portfolio, day
   to day. A hardcoded constant cannot track it.

## 1. Structure

    per_trade  = min(arm_cap, kelly_stake, risk budget remaining, cash)
    concurrency = whatever that produces — a mutually exclusive leg adds ~0
                  marginal true risk and therefore consumes no concurrency

Each constraint has a defined meaning. None of them is a magic number:

| Constraint | Source | Meaning |
|---|---|---|
| `arm_cap` | arm-state | hard fuse; this module reads it and never writes it |
| `kelly_stake` | the caller's Kelly implementation | optimal fraction from edge and variance |
| risk budget | `MAX_PORTFOLIO_TRUE_RISK * bankroll - true exposure in use` | maximum true risk at portfolio level |

`MAX_PORTFOLIO_TRUE_RISK` is **not a newly chosen risk level**; it is calibrated
to the equivalent of the previous behaviour (section 2). Switching changes **how
risk is allocated** — a mutually exclusive position no longer consumes budget it
does not use — without changing the level. Changing the level is a money-surface
decision, and this module never makes it automatically.

## 2. The risk target is not a constant; it is a function of two observables

**What a fixed constant gets wrong.** Deriving `MAX_PORTFOLIO_TRUE_RISK` from,
say, a Monte Carlo drawdown percentile sounds principled, but if the drawdown
target itself was invented then the rigour only makes the invention harder to
spot. A recommendation should rest on this deployment's own state and objective,
not on remembered priors.

**A useful external comparison.** Mature venues hang every risk parameter on an
observable: order caps on a leverage tier, margin on leverage, open-interest caps
on liquidity. None of them is a percentage pulled from the air. That methodology
transfers. Their framework does not: it is built around leverage and liquidation,
whereas a binary event contract is unleveraged with no liquidation and a maximum
loss equal to the premium. Copying it directly would also copy the wrong
constraint — such venues size against depth, while here depth is a poor predictor
of fills and queue position is the binding one.

### 2.1 base <= arm caps

```
base = max_drawdown_usd / max_total_deploy_usd
```

Example: an arm-state carrying `max_drawdown_usd = 100` and
`max_total_deploy_usd = 200` gives **base = 50%**. That is the deployment's own
stated drawdown tolerance; read it rather than inventing a second number.
**Change the caps and the risk target follows, with no code change.** When
arm-state cannot be read, fall back to `FALLBACK_BASE_RISK` and label
`base_source="fallback"` — a fallback is only ever more conservative.

### 2.2 scale <= the confidence interval on edge

```
edge CI upper <= 0   -> 0        significantly negative: deploy nothing
edge CI lower >  0   -> base     significantly positive: full size
CI straddles zero    -> base * (CI upper / CI width)
                                  the further below zero, the harder it shrinks
```

Example: a line with post-attribution ROI of -0.532% and a 95% CI of
[-2.114%, +1.050%] gives `scale = 1.050 / 3.164 = 0.332`, so the risk target is
50% * 0.332 = **16.6%**.

**This implements "do not raise risk before the edge turns positive"
automatically** — computed from the interval rather than remembered by a person.
Scaling up on the winning side and shrinking on the losing side is itself an
alpha mechanism, expressed in sizing.

### 2.3 Risk control uses the notional worst case; fill probability is diagnostic only

Queue loss on passive orders is usually large — most resting orders never fill —
so notional deployed and true risk can differ by a multiple. **Risk control does
not discount by expected fill.** Everything filling at once is genuinely possible,
and an expected-value risk control blows up on the day it happens. This matches
how mature venues margin on notional. Fill probability goes into `expected_*`
diagnostic fields for capital-efficiency analysis and **relaxes no constraint**.

## 3. Boundary

A read-only computation. It places no order and touches no arm state, caps or
kill file. It returns a budget plus diagnostics and the caller decides whether to
use them. When metadata is unavailable or exposure cannot be computed it
**fails safe back to the simpler formula** and labels `fallback_reason`. A failure
in this module never causes the caller to trade larger.
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Optional

from marketflow.paths import PROJECT_DIR as REPO, runtime_path
# Fallback portfolio risk target when arm-state cannot be read. A fallback may
# only ever be more conservative than a stated policy, never less: 5% of the book
# at true risk is a defensible institutional floor; a third of the book is not.
FALLBACK_BASE_RISK = 0.05
ARM_STATE_PATH = runtime_path("execution", "polymarket_arm_state.json")
# Floor on the marginal risk ratio. A mutually exclusive leg can genuinely have
# a marginal risk of zero, but dividing by it would manufacture an infinite budget.
MIN_MARGINAL_RATIO = 0.02
# Probe notional used to measure the marginal true-risk ratio before scaling it
# linearly (true exposure is piecewise linear in cost). It must be small relative
# to a position and large enough not to be lost in rounding, so it tracks the
# per-trade cap rather than being a fixed number of dollars.
PROBE_FRACTION_OF_PER_TRADE_CAP = 0.002
PROBE_FLOOR_USD = 1.0


def probe_usd_default() -> float:
    """Probe notional for this deployment: a slice of the per-trade cap, floored
    so it never rounds to nothing on a small book."""
    try:
        from marketflow.execution import orders as pmx  # noqa: PLC0415
        cap = float(pmx.DEFAULT_MAX_PER_TRADE_USD)
    except Exception:
        cap = 0.0
    return max(PROBE_FLOOR_USD, cap * PROBE_FRACTION_OF_PER_TRADE_CAP)


def _load_exposure_module():
    try:
        from marketflow.risk import exposure as ee
        return ee
    except Exception:
        return None


def risk_target_base(arm_state_path: str = ARM_STATE_PATH) -> tuple[float, str]:
    """Base risk capacity from the arm caps: `max_drawdown / max_total_deploy`.

    Setting caps already states how much loss is acceptable against how much is
    deployed. Reading that is more honest than inventing a second number, and the
    target tracks the caps automatically. **Arm-state is read, never written.**
    """
    try:
        with open(arm_state_path, encoding="utf-8") as fh:
            a = json.load(fh)
        dd = float(a.get("max_drawdown_usd") or 0.0)
        tot = float(a.get("max_total_deploy_usd") or 0.0)
        if dd > 0 and tot > 0:
            return min(1.0, dd / tot), "arm_state"
    except Exception:
        pass
    return FALLBACK_BASE_RISK, "fallback"


# Minimum sample size before an edge interval is allowed to scale anything. Below
# it the interval itself is not trustworthy: the half-width scales as 1/sqrt(n), so
# a small sample produces a wide interval whose upper bound sits high — which would
# hand the most capital to the strategy with the weakest evidence. That inversion
# has been observed directly: a short mirror track produced a materially higher
# target than the full ledger of the same strategy.
EDGE_MIN_N = 30


def edge_scaled_risk_target(
    *,
    edge_ci_lower_pct: Optional[float],
    edge_ci_upper_pct: Optional[float],
    edge_n: Optional[int] = None,
    base: Optional[float] = None,
    arm_state_path: str = ARM_STATE_PATH,
) -> dict[str, Any]:
    """Risk target = base * f(edge confidence interval). A losing strategy is
    defunded automatically.

    edge_ci_*_pct are in **percentage points**, the same unit as an ROI interval.
    If either is None the target is not scaled — it falls back to full base and
    labels a reason. Missing data must never quietly enlarge or shrink a
    position.
    """
    if base is None:
        base, base_src = risk_target_base(arm_state_path)
    else:
        base_src = "explicit"
    out = {"base": round(base, 6), "base_source": base_src,
           "edge_ci_lower_pct": edge_ci_lower_pct, "edge_ci_upper_pct": edge_ci_upper_pct}
    out["edge_n"] = edge_n
    if edge_ci_lower_pct is None or edge_ci_upper_pct is None:
        out.update({"scale": 1.0, "risk_target": round(base, 6), "scale_reason": "edge_ci_unavailable"})
        return out
    if edge_n is not None and int(edge_n) < EDGE_MIN_N:
        # Below the sample floor the interval is untrustworthy: return zero
        # rather than letting a wide interval push the target up.
        out.update({"scale": 0.0, "risk_target": 0.0, "scale_reason": "edge_sample_too_small"})
        return out
    lo, hi = float(edge_ci_lower_pct), float(edge_ci_upper_pct)
    if hi <= 0:
        out.update({"scale": 0.0, "risk_target": 0.0, "scale_reason": "edge_significantly_negative"})
        return out
    if lo > 0:
        out.update({"scale": 1.0, "risk_target": round(base, 6), "scale_reason": "edge_significantly_positive"})
        return out
    span = hi - lo
    scale = (hi / span) if span > 0 else 0.0
    out.update({"scale": round(scale, 6), "risk_target": round(base * scale, 6),
                "scale_reason": "edge_ci_spans_zero"})
    return out


def conservative_risk_target(estimates: list[dict[str, Any]], *,
                             base: Optional[float] = None,
                             arm_state_path: str = ARM_STATE_PATH) -> dict[str, Any]:
    """With several edge measurements available, take the **most conservative**.

    `estimates` = [{"label", "ci_lower_pct", "ci_upper_pct", "n"}, ...].
    The motivating case: a short mirror track and the full ledger of the same
    strategy produced materially different targets. Both measure the same thing,
    so when they disagree the conservative side wins. Picking whichever happens to
    be favourable is not an option.
    """
    out = []
    for e in estimates:
        r = edge_scaled_risk_target(edge_ci_lower_pct=e.get("ci_lower_pct"),
                                    edge_ci_upper_pct=e.get("ci_upper_pct"),
                                    edge_n=e.get("n"), base=base, arm_state_path=arm_state_path)
        r["label"] = e.get("label")
        out.append(r)
    if not out:
        b, src = risk_target_base(arm_state_path)
        return {"risk_target": b, "base_source": src, "scale_reason": "no_estimates", "considered": []}
    winner = min(out, key=lambda r: r["risk_target"])
    return {**winner, "considered": out, "n_estimates": len(out)}


def true_exposure_usd(positions: list[dict], *, meta: Optional[dict] = None) -> Optional[float]:
    """True portfolio risk in USD. Mutually exclusive groups are enumerated exactly
    for their worst state; everything else takes the total-loss upper bound.

    positions: [{cid, side, cost_usd, shares, ...}]. None means it could not be
    computed, and the caller falls back.
    """
    ee = _load_exposure_module()
    if ee is None or not positions:
        return 0.0 if not positions else None
    try:
        rows = [ee.position_from_var(p) if hasattr(ee, "position_from_var")
                else ee.position_from_row(p, source="risk_budget") for p in positions]
        m = meta if meta is not None else ee.resolve_meta(rows, use_gamma=False)
        buckets = ee.build_event_buckets(rows, m)
        return sum(ee.bucket_exposure(b)["true_event_exposure_usd"] for b in buckets)
    except Exception:
        return None


def marginal_risk_ratio(open_positions: list[dict], candidate: dict, *,
                        meta: Optional[dict] = None, probe_usd: float | None = None) -> Optional[float]:
    """How much true risk each additional $1 of cost adds.

    Adding a leg inside a mutually exclusive group approaches 0, because the worst
    state does not change. Adding a leg on an independent event approaches 1. This
    is the computable form of "a mutually exclusive position should not consume
    concurrency".
    """
    probe_usd = probe_usd_default() if probe_usd is None else float(probe_usd)
    before = true_exposure_usd(open_positions, meta=meta)
    if before is None:
        return None
    probe = dict(candidate)
    probe["cost_usd"] = probe_usd
    probe["shares"] = probe_usd / max(float(candidate.get("entry_px") or 0.5), 1e-6)
    after = true_exposure_usd(list(open_positions) + [probe], meta=meta)
    if after is None:
        return None
    return max(0.0, (after - before) / probe_usd)


def dynamic_stake_usd(
    *,
    cash_usd: float,
    bankroll_usd: float,
    open_positions: list[dict],
    candidate: dict,
    arm_cap_usd: float,
    exchange_min_usd: float,
    kelly_fraction: Optional[float] = None,
    max_portfolio_true_risk: Optional[float] = None,
    edge_ci_lower_pct: Optional[float] = None,
    edge_ci_upper_pct: Optional[float] = None,
    edge_n: Optional[int] = None,
    fill_prob: Optional[float] = None,
    legacy_equity_fraction: Optional[float] = None,
    meta: Optional[dict] = None,
    arm_state_path: str = ARM_STATE_PATH,
) -> dict[str, Any]:
    """Per-trade budget = min(arm_cap, kelly, risk budget remaining, cash), with
    full diagnostics alongside.

    When `legacy_equity_fraction` is supplied, the diagnostics report the simpler
    formula's answer side by side for comparison (useful in shadow mode). If any
    step cannot be computed, `budget_usd` falls back to that formula and
    `fallback_reason` says why.

    **Performance: load `meta` once in the caller and reuse it.** Omitted, every
    call re-resolves it, which means scanning the markets feed each time; in a
    loop over hundreds of candidates that is fatal. Resolve once, then pass it in
    on every call.
    """
    diag: dict[str, Any] = {
        "schema": "polymarket-risk-budget-v0.1",
        "arm_cap_usd": arm_cap_usd, "cash_usd": cash_usd, "bankroll_usd": bankroll_usd,
        "n_open": len(open_positions), "fallback_reason": None,
    }
    legacy = None
    if legacy_equity_fraction is not None:
        legacy = min(arm_cap_usd, max(exchange_min_usd, cash_usd * legacy_equity_fraction))
        diag["legacy_budget_usd"] = round(legacy, 6)
        diag["legacy_equity_fraction"] = legacy_equity_fraction

    if max_portfolio_true_risk is None:
        rt = edge_scaled_risk_target(edge_ci_lower_pct=edge_ci_lower_pct,
                                     edge_ci_upper_pct=edge_ci_upper_pct,
                                     edge_n=edge_n, arm_state_path=arm_state_path)
        max_portfolio_true_risk = rt["risk_target"]
        diag["risk_target_detail"] = rt
    diag["max_portfolio_true_risk"] = round(max_portfolio_true_risk, 6)

    used = true_exposure_usd(open_positions, meta=meta)
    ratio = marginal_risk_ratio(open_positions, candidate, meta=meta) if used is not None else None
    if used is None or ratio is None:
        diag["fallback_reason"] = "true_exposure_unavailable"
        diag["budget_usd"] = round(legacy if legacy is not None else 0.0, 6)
        return diag

    naive = sum(float(p.get("cost_usd") or 0.0) for p in open_positions)
    diag.update({
        "true_exposure_usd": round(used, 6),
        "naive_exposure_usd": round(naive, 6),
        "structural_offset_pct": (round(1.0 - used / naive, 6) if naive > 0 else None),
        "marginal_risk_ratio": round(ratio, 6),
    })

    risk_left = bankroll_usd * max_portfolio_true_risk - used
    diag["risk_budget_left_usd"] = round(risk_left, 6)
    if risk_left <= 0:
        diag["budget_usd"] = 0.0
        diag["binding_constraint"] = (
            "edge_gate_closed" if max_portfolio_true_risk <= 0 else "risk_budget_exhausted")
        return diag

    risk_allowed = risk_left / max(ratio, MIN_MARGINAL_RATIO)
    kelly_allowed = (bankroll_usd * kelly_fraction) if kelly_fraction is not None else float("inf")
    diag["risk_allowed_usd"] = round(risk_allowed, 6)
    diag["kelly_allowed_usd"] = (round(kelly_allowed, 6) if kelly_allowed != float("inf") else None)

    caps = {"arm_cap": arm_cap_usd, "kelly": kelly_allowed,
            "risk_budget": risk_allowed, "cash": cash_usd}
    budget = min(caps.values())
    diag["binding_constraint"] = min(caps, key=lambda k: caps[k])
    if budget < exchange_min_usd:
        diag["budget_usd"] = 0.0
        diag["binding_constraint"] = "below_exchange_min"
        return diag
    diag["budget_usd"] = round(budget, 6)
    if legacy is not None:
        diag["vs_legacy_x"] = (round(budget / legacy, 4) if legacy > 0 else None)
    # Fill probability is **diagnostic only and relaxes nothing**. Everything
    # filling at once is genuinely possible, and expected-value risk control blows
    # up on that day. What follows is a capital-efficiency view, not a constraint.
    if fill_prob is not None:
        fp = max(0.0, min(1.0, float(fill_prob)))
        diag["fill_prob"] = round(fp, 6)
        diag["expected_deployed_usd"] = round(budget * fp, 6)
        diag["expected_true_risk_usd"] = round(budget * fp * ratio, 6)
        diag["nominal_to_expected_x"] = (round(1.0 / fp, 4) if fp > 0 else None)
    return diag


SHADOW_LEDGER = runtime_path("risk",
                             "risk_budget_shadow", "ledger.jsonl")
_META_CACHE: dict[str, Any] = {"meta": None, "ts": 0.0, "n_pos": -1}
META_TTL_S = 900.0


def _cached_meta(positions: list[dict]):
    """Loading metadata is slow enough that recomputing it per trade would stall
    the caller's tick. Cached by position count with a TTL."""
    import time as _t
    now = _t.time()
    if (_META_CACHE["meta"] is not None and now - _META_CACHE["ts"] < META_TTL_S
            and _META_CACHE["n_pos"] == len(positions)):
        return _META_CACHE["meta"]
    ee = _load_exposure_module()
    if ee is None:
        return None
    try:
        rows = [ee.position_from_row(p, source="risk_budget") for p in positions]
        m = ee.resolve_meta(rows, use_gamma=False)
    except Exception:
        return None
    _META_CACHE.update({"meta": m, "ts": now, "n_pos": len(positions)})
    return m


def shadow_record_sizing(
    *,
    track: str,
    cash_usd: float,
    bankroll_usd: float,
    open_positions: list[dict],
    candidate: dict,
    arm_cap_usd: float,
    exchange_min_usd: float,
    legacy_budget_usd: float,
    legacy_equity_fraction: float,
    edge_estimates: Optional[list[dict]] = None,
    fill_prob: Optional[float] = None,
    ledger_path: Optional[str] = None,
) -> Optional[dict]:
    """Record the current sizing decision beside the dynamic one. **It changes no
    real-money behaviour.**

    Callers must wrap this in try/except. Synthetic decisions from a selftest
    process never enter the live ledger: a shadow record is forward evidence, and
    evidence mixed with fixtures stops being evidence.
    """
    path = ledger_path or SHADOW_LEDGER
    if "--selftest" in sys.argv and path == SHADOW_LEDGER:
        return None
    meta = _cached_meta(open_positions + [candidate])
    rt = (conservative_risk_target(edge_estimates) if edge_estimates else None)
    d = dynamic_stake_usd(
        cash_usd=cash_usd, bankroll_usd=bankroll_usd, open_positions=open_positions,
        candidate=candidate, arm_cap_usd=arm_cap_usd, exchange_min_usd=exchange_min_usd,
        max_portfolio_true_risk=(rt["risk_target"] if rt else None),
        fill_prob=fill_prob, legacy_equity_fraction=legacy_equity_fraction, meta=meta)
    row = {
        "schema": "risk-budget-shadow-v0.2",
        "ts": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "track": track,
        "cid": candidate.get("cid") or candidate.get("condition_id"),
        "slug": candidate.get("slug"),
        "legacy_budget_usd": round(legacy_budget_usd, 6),
        "dynamic_budget_usd": d.get("budget_usd"),
        "binding_constraint": d.get("binding_constraint"),
        "risk_target": d.get("max_portfolio_true_risk"),
        "risk_target_source": (rt.get("scale_reason") if rt else d.get("risk_target_detail", {}).get("scale_reason")),
        "risk_target_label": (rt.get("label") if rt else None),
        "true_exposure_usd": d.get("true_exposure_usd"),
        "naive_exposure_usd": d.get("naive_exposure_usd"),
        "structural_offset_pct": d.get("structural_offset_pct"),
        "marginal_risk_ratio": d.get("marginal_risk_ratio"),
        "expected_deployed_usd": d.get("expected_deployed_usd"),
        "n_open": d.get("n_open"),
        "fallback_reason": d.get("fallback_reason"),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return None
    return row


# --------------------------------------------------------------------------- #
def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    def mk(cid: str, cost: float, side: str = "NO", px: float = 0.8, **kw) -> dict:
        return {"cid": cid, "side": side, "cost_usd": cost, "shares": cost / px,
                "entry_px": px, "status": "open", **kw}

    # no positions: the full risk budget is available and arm_cap or cash binds
    d0 = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=[],
                           candidate=mk("0xa", 0.0), arm_cap_usd=20.0,
                           exchange_min_usd=4.25, legacy_equity_fraction=0.15)
    checks["empty_book_budget_capped_by_arm"] = (d0["budget_usd"] == 20.0
                                                 and d0["binding_constraint"] == "arm_cap")
    checks["empty_book_true_exposure_zero"] = d0.get("true_exposure_usd") == 0.0

    # risk budget exhausted -> 0, and never falls back to a positive legacy number
    big = [mk(f"0x{i}", 100.0) for i in range(4)]
    d1 = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=big,
                           candidate=mk("0xz", 0.0), arm_cap_usd=20.0,
                           exchange_min_usd=4.25, legacy_equity_fraction=0.15,
                           max_portfolio_true_risk=0.333)   # explicit: this case tests exhaustion
    checks["risk_budget_can_exhaust"] = (d1["budget_usd"] == 0.0
                                         and d1["binding_constraint"] == "risk_budget_exhausted")

    # Kelly binds when it is the tighter constraint
    d2 = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=[],
                           candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25,
                           kelly_fraction=0.005)
    checks["kelly_binds_when_tighter"] = (abs(d2["budget_usd"] - 5.0) < 1e-9
                                          and d2["binding_constraint"] == "kelly")

    # cash binds when it is the tighter constraint
    d3 = dynamic_stake_usd(cash_usd=8.0, bankroll_usd=1000.0, open_positions=[],
                           candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25)
    checks["cash_binds_when_tighter"] = (d3["budget_usd"] == 8.0
                                         and d3["binding_constraint"] == "cash")

    # below the exchange minimum -> 0; never send an order certain to be rejected
    d4 = dynamic_stake_usd(cash_usd=2.40, bankroll_usd=1000.0, open_positions=[],
                           candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25)
    checks["below_exchange_min_returns_zero"] = (d4["budget_usd"] == 0.0
                                                 and d4["binding_constraint"] == "below_exchange_min")

    # fail-safe: when exposure cannot be computed, fall back to the legacy formula
    # and **never to something larger**
    _saved = globals()["_load_exposure_module"]
    globals()["_load_exposure_module"] = lambda: None
    try:
        d5 = dynamic_stake_usd(cash_usd=100.0, bankroll_usd=100.0,
                               open_positions=[mk("0xa", 20.0)], candidate=mk("0xb", 0.0),
                               arm_cap_usd=20.0, exchange_min_usd=4.25,
                               legacy_equity_fraction=0.15)
        checks["failsafe_falls_back_to_legacy"] = (
            d5["fallback_reason"] == "true_exposure_unavailable"
            and abs(d5["budget_usd"] - d5["legacy_budget_usd"]) < 1e-9)
        checks["failsafe_never_larger_than_legacy"] = d5["budget_usd"] <= d5["legacy_budget_usd"]
    finally:
        globals()["_load_exposure_module"] = _saved

    # --- the risk target is a function of two observables, not a constant ---
    import tempfile
    _arm = {"max_drawdown_usd": 100.0, "max_total_deploy_usd": 200.0, "max_per_trade_usd": 20.0}
    _tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(_arm, _tf); _tf.close()
    b, src = risk_target_base(_tf.name)
    checks["base_reads_arm_caps_not_a_magic_number"] = abs(b - 0.5) < 1e-9 and src == "arm_state"
    b2, src2 = risk_target_base("/nonexistent/arm.json")
    checks["base_falls_back_conservatively"] = src2 == "fallback" and b2 <= 0.5

    # significantly negative edge -> the gate closes by itself
    r_neg = edge_scaled_risk_target(edge_ci_lower_pct=-5.0, edge_ci_upper_pct=-1.0,
                                    arm_state_path=_tf.name)
    checks["edge_negative_closes_gate"] = (r_neg["risk_target"] == 0.0
                                           and r_neg["scale_reason"] == "edge_significantly_negative")
    # significantly positive edge -> full size
    r_pos = edge_scaled_risk_target(edge_ci_lower_pct=1.0, edge_ci_upper_pct=5.0,
                                    arm_state_path=_tf.name)
    checks["edge_positive_gives_full_base"] = abs(r_pos["risk_target"] - 0.5) < 1e-9
    # straddling zero -> shrink by the upper bound's share of the interval
    r_flb = edge_scaled_risk_target(edge_ci_lower_pct=-2.114, edge_ci_upper_pct=1.050,
                                    arm_state_path=_tf.name)
    checks["flb_real_ci_scales_to_expected"] = abs(r_flb["risk_target"] - 0.5 * (1.050 / 3.164)) < 1e-6
    checks["flb_real_ci_is_meaningfully_below_base"] = r_flb["risk_target"] < 0.5 * 0.4
    # the further the lower bound goes negative, the harder it shrinks (monotone)
    r_worse = edge_scaled_risk_target(edge_ci_lower_pct=-10.0, edge_ci_upper_pct=1.050,
                                      arm_state_path=_tf.name)
    checks["more_negative_lower_bound_shrinks_more"] = r_worse["risk_target"] < r_flb["risk_target"]
    # missing interval -> neither quietly enlarge nor quietly shrink; label a reason
    r_na = edge_scaled_risk_target(edge_ci_lower_pct=None, edge_ci_upper_pct=None,
                                   arm_state_path=_tf.name)
    checks["missing_ci_is_flagged_not_guessed"] = (r_na["scale_reason"] == "edge_ci_unavailable"
                                                   and abs(r_na["risk_target"] - 0.5) < 1e-9)
    # with the edge gate closed, the stake is 0 and it says so was the edge gate
    # rather than an exhausted budget
    d_gate = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=[],
                               candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25,
                               edge_ci_lower_pct=-5.0, edge_ci_upper_pct=-1.0,
                               arm_state_path=_tf.name)
    checks["edge_gate_closed_is_distinct_from_exhausted"] = (
        d_gate["budget_usd"] == 0.0 and d_gate["binding_constraint"] == "edge_gate_closed")

    # --- sample-size gate, and the conservative pick across measurements ---
    r_small = edge_scaled_risk_target(edge_ci_lower_pct=-50.0, edge_ci_upper_pct=60.0,
                                      edge_n=5, arm_state_path=_tf.name)
    checks["tiny_sample_gets_zero_not_max"] = (r_small["risk_target"] == 0.0
                                               and r_small["scale_reason"] == "edge_sample_too_small")
    checks["edge_min_n_from_existing_prereg"] = EDGE_MIN_N == 30
    # two measurements of one strategy -> take the conservative one
    cons = conservative_risk_target([
        {"label": "mirror", "ci_lower_pct": -5.617, "ci_upper_pct": 14.767, "n": 62},
        {"label": "ledger", "ci_lower_pct": -2.200, "ci_upper_pct": 0.977, "n": 1249},
    ], arm_state_path=_tf.name)
    checks["multi_estimate_takes_conservative"] = (cons["label"] == "ledger"
                                                   and cons["risk_target"] < 0.20)
    checks["multi_estimate_keeps_all_considered"] = len(cons["considered"]) == 2
    # stronger evidence (larger n, narrower interval) with a more negative point
    # estimate yields a lower target: a wide interval is never rewarded
    _wide = edge_scaled_risk_target(edge_ci_lower_pct=-20.0, edge_ci_upper_pct=22.0,
                                    edge_n=100, arm_state_path=_tf.name)
    _tight = edge_scaled_risk_target(edge_ci_lower_pct=-2.2, edge_ci_upper_pct=0.977,
                                     edge_n=1249, arm_state_path=_tf.name)
    checks["tighter_negative_evidence_gets_less"] = _tight["risk_target"] < _wide["risk_target"]

    # --- fill probability is diagnostic and relaxes no constraint ---
    d_np = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=[],
                             candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25,
                             edge_ci_lower_pct=1.0, edge_ci_upper_pct=5.0, arm_state_path=_tf.name)
    d_fp = dynamic_stake_usd(cash_usd=1000.0, bankroll_usd=1000.0, open_positions=[],
                             candidate=mk("0xa", 0.0), arm_cap_usd=20.0, exchange_min_usd=4.25,
                             edge_ci_lower_pct=1.0, edge_ci_upper_pct=5.0, fill_prob=0.2629,
                             arm_state_path=_tf.name)
    checks["fill_prob_does_not_change_budget"] = abs(d_fp["budget_usd"] - d_np["budget_usd"]) < 1e-12
    checks["fill_prob_reports_expected_deployment"] = (
        abs(d_fp["expected_deployed_usd"] - d_fp["budget_usd"] * 0.2629) < 1e-6)
    os.unlink(_tf.name)
    # the marginal-ratio floor exists: a mutually exclusive leg cannot manufacture
    # an infinite budget
    checks["marginal_ratio_has_floor"] = MIN_MARGINAL_RATIO > 0
    # Read-only: the module contains no order-placing or arm-writing symbol. The
    # tokens are split and joined so this line is not itself a match.
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _forbidden = ["post" + "_order", "place" + "_order", "create" + "_order", "arm" + "_state_write"]
    checks["read_only_no_order_symbols"] = not any(t in _src for t in _forbidden)

    return {"schema_version": "polymarket-risk-budget-selftest-v0.1",
            "n_checks": len(checks), "n_pass": sum(checks.values()),
            "all_pass": all(checks.values()), "checks": checks}


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        r = selftest()
        print(json.dumps(r, ensure_ascii=False, indent=1))
        sys.exit(0 if r["all_pass"] else 1)
    print(__doc__)
