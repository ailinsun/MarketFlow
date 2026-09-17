"""Unified position sizer — posterior-calibrated dynamic fractional Kelly rebalancing.

This is the single sizing brain that replaces the asymmetric entry/exit split
(entry_should_buy = one-shot fractional-Kelly maker open; decide_position = full
SELL on adverse / Kelly TRIM only on a favourable new high). Per the 2026-06-26
an external research blueprint,
every tick we ask ONE question:

    given the most conservative current probability p and the current executable
    bid/ask, how much YES (or NO) should we hold right now?

Target up  -> BUY_MORE (scale in).   Target down -> SELL_PARTIAL (graduated trim).
Target 0   -> FULL_SELL.             Other side +EV after exit -> flip.

Scope of THIS version: it sizes to a fractional-Kelly target from the p-edge
(calibrated probability vs executable price) — optimal sizing of existing edge,
no overbet / all-or-nothing / churn. It does NOT yet harvest the larger source
the binary-option view opens: convexity from the price PATH itself (gamma /
pre-resolution swing harvesting, structural mispricing, dynamic delta hedging),
which can be an alpha source on its own. "Not losing" is dynamic asymmetric leverage
(lever up when favourable, down/out when adverse), NOT static abstention. The
convexity layer is the planned upgrade, Wolfram-native (stochastic optimal
control / ItoProcess); see docs feedback_not_losing_is_dynamic_convexity.

It is a pure-function library: no I/O, no network, no order placement. The
daemon calls it; S1 fuses (arm-state, caps, FOK, budget, kill, geoblock) remain
the only thing that can move real money and are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

EPS = 1e-6


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _logit(p: float) -> float:
    p = _clip(p, EPS, 1.0 - EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def calibrate_p_eff(p_raw: Optional[float], q_mid: Optional[float], lam: float) -> Optional[float]:
    """Residual calibration: shrink the panel probability toward the market in
    log-odds space.  logit(p_eff) = logit(q) + lam * (logit(p_raw) - logit(q)).

    lam in [0, 1] is how much of the panel's deviation-from-market we trust. With
    no proven edge lam should be small (p_eff ~ market => no bet). lam can later be
    LEARNED per market-type from forward incremental log-score (blueprint #2); a
    type that never beats the market gets lam -> 0.
    """
    if p_raw is None:
        return None
    if q_mid is None:
        return _clip(p_raw, EPS, 1.0 - EPS)
    lam = _clip(lam, 0.0, 1.0)
    resid = _logit(p_raw) - _logit(q_mid)
    return _sigmoid(_logit(q_mid) + lam * resid)


def prob_uncertainty(divergence: Optional[float], credibility: Optional[float],
                     *, base_sd: float = 0.03, div_scale: float = 0.12,
                     thin_scale: float = 0.10) -> float:
    """Map MarketFlow belief-panel signals to a probability std sigma_p used for the
    conservative quantile p_c = p - z*sigma_p.

    Higher panel divergence (disagreement) and lower evidence credibility both
    widen sigma_p, so thin/contested signals are sized down automatically.
    """
    div = 0.0 if divergence is None else _clip(divergence, 0.0, 1.0)
    cred = 1.0 if credibility is None else _clip(credibility, 0.0, 1.0)
    return base_sd + div_scale * div + thin_scale * (1.0 - cred)


def taker_fee_per_share(price: float, rate: float) -> float:
    """Verified Polymarket taker fee per share: rate*p*(1-p) (help.polymarket.com/
    trading-fees). Makers (post-only limit) pay 0. Peaks at p=0.5, vanishes at the
    extremes. rate=0.05 for sports markets since the 2026-07-10 fee change (was 0.03)."""
    p = _clip(float(price), 0.0, 1.0)
    return float(rate) * p * (1.0 - p)


def kelly_fraction_binary(p_c: float, ask_eff: float) -> float:
    """Full-Kelly fraction-of-bankroll for a binary contract bought at ask_eff
    with conservative win prob p_c: f* = (p_c - ask_eff) / (1 - ask_eff), floored
    at 0. Zero unless p_c clears the all-in executable price.
    """
    if p_c <= ask_eff:
        return 0.0
    denom = max(EPS, 1.0 - ask_eff)
    return max(0.0, (p_c - ask_eff) / denom)


@dataclass
class SizerConfig:
    kelly_fraction: float = 0.25          # fractional Kelly (full Kelly is too hot on noisy p)
    cost_buffer: float = 0.015            # execution residual (spread/2 + slippage-past-best +
                                          # adverse selection + leg risk); fee modelled SEPARATELY
                                          # per the verified mechanic (taker_fee_per_share)
    edge_buffer: float = 0.02             # extra margin a signal must clear to be worth a trade
    z: float = 1.0                        # conservative-quantile multiplier on sigma_p
    market_cap_frac: float = 0.40         # max bankroll fraction in one market
    cluster_cap_frac: float = 0.60        # max bankroll fraction in one correlated cluster
    min_trade_usd: float = 1.0            # deadband: ignore rebalances smaller than this
    exchange_min_notional_usd: float = 5.0  # Polymarket hard minimum order notional
    cost_buffer_sell: float = 0.01        # cost margin on the exit side
    residual_lambda: float = 0.5          # trust in panel deviation-from-market (calibrate later)


@dataclass
class SizerInputs:
    # Probability view
    p_yes_mean: Optional[float]           # MarketFlow calibrated YES probability (e.g. belief p_a_effective)
    p_sd: float                           # uncertainty on that probability (see prob_uncertainty)
    q_mid: Optional[float] = None         # market mid (YES) for residual calibration; None => skip
    # Live executable book (NEVER the displayed/last price)
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    # Execution cost (verified Polymarket mechanic: taker fee = rate*p*(1-p), maker = 0)
    fee_rate: float = 0.0                 # per-market feeSchedule.rate (0.05 sports since 2026-07-10), 0 if feesEnabled false
    maker_entry: bool = True              # daemon opens post-only (maker, 0 fee); taker only in in-play window
    # Account
    bankroll_usd: float = 25.0            # conservative net worth (cash + positions at bid)
    held_side: Optional[str] = None       # "YES" | "NO" | None
    held_shares: float = 0.0
    # Gates (hard, side-independent)
    resolution_clean: bool = True
    market_quality_ok: bool = True
    force_exit: bool = False              # portfolio drawdown / cluster breach -> liquidate
    meta_label_ok: bool = True            # blueprint #3 filter; default allow until trained


@dataclass
class SizerDecision:
    action: str                           # BUY_OPEN / BUY_MORE / SELL_PARTIAL / FULL_SELL / FLIP / HOLD / SKIP
    trade_side: Optional[str] = None      # which token to act on
    trade_direction: Optional[str] = None # BUY | SELL
    trade_shares: float = 0.0
    target_side: Optional[str] = None
    target_fraction: float = 0.0
    target_notional_usd: float = 0.0
    current_notional_usd: float = 0.0
    delta_notional_usd: float = 0.0
    p_eff: Optional[float] = None
    p_conservative: Optional[float] = None
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)


def _exec_fee(ask: float, fee_rate: float, maker: bool) -> float:
    """Per-share execution fee: 0 for a maker (post-only) open, else the verified
    taker mechanic rate*ask*(1-ask)."""
    return 0.0 if maker else taker_fee_per_share(ask, fee_rate)


def _side_target_fraction(p_mean, p_sd, ask, q_for_side, cfg: SizerConfig, *,
                          fee_rate: float = 0.0, maker: bool = True):
    """Conservative-Kelly target fraction for one side, after residual calibration."""
    if p_mean is None or ask is None or not (0.0 < ask < 1.0):
        return 0.0, None, None, None
    p_eff = calibrate_p_eff(p_mean, q_for_side, cfg.residual_lambda)
    p_c = _clip((p_eff if p_eff is not None else p_mean) - cfg.z * p_sd, EPS, 1.0 - EPS)
    ask_eff = min(1.0 - EPS, ask + _exec_fee(ask, fee_rate, maker) + cfg.cost_buffer + cfg.edge_buffer)
    f_full = kelly_fraction_binary(p_c, ask_eff)
    f = cfg.kelly_fraction * f_full
    f = min(f, cfg.market_cap_frac, cfg.cluster_cap_frac)
    return f, p_eff, p_c, ask_eff


def _force_full_exit_reason(inp: SizerInputs, cfg: SizerConfig) -> Optional[str]:
    """Strict, conservative conditions under which we dump the WHOLE held side
    rather than trimming. Everything else is a graduated resize."""
    if inp.held_side is None or inp.held_shares <= 0:
        return None
    if inp.force_exit:
        return "portfolio drawdown / cluster cap breached; forced liquidation"
    if not inp.resolution_clean:
        return "resolution no longer clean/objective; exit binary risk"
    if not inp.market_quality_ok:
        return "market quality failed (spread/depth); break-even unreliable, exit"
    # Even the OPTIMISTIC probability is below the sale value => the market overpays
    # for our held side; sell all (covers 'cut loser' and 'market overreacted up').
    if inp.held_side == "YES":
        bid = inp.yes_bid
        p_up = (inp.p_yes_mean if inp.p_yes_mean is not None else 0.0) + cfg.z * inp.p_sd
    else:
        bid = inp.no_bid
        p_yes_low = (inp.p_yes_mean if inp.p_yes_mean is not None else 1.0) - cfg.z * inp.p_sd
        p_up = 1.0 - p_yes_low
    if bid is not None and p_up < bid - cfg.cost_buffer_sell:
        return "even the optimistic probability is below the current sale price; full exit"
    return None


def compute_target_position(inp: SizerInputs, cfg: Optional[SizerConfig] = None) -> SizerDecision:
    """The unified lifecycle decision: scale in, partial trim, full exit, flip, or hold."""
    cfg = cfg or SizerConfig()

    # 1) Hard full-exit gate (strict).
    forced = _force_full_exit_reason(inp, cfg)
    if forced is not None:
        bid = inp.yes_bid if inp.held_side == "YES" else inp.no_bid
        return SizerDecision(
            action="FULL_SELL", trade_side=inp.held_side, trade_direction="SELL",
            trade_shares=round(inp.held_shares, 8),
            current_notional_usd=round(inp.held_shares * (bid or 0.0), 8),
            reason=forced, diagnostics={"gate": "force_full_exit"},
        )

    # 2) Meta-label veto (blueprint #3): allowed to BLOCK new risk, never to force a hold of a loser.
    meta_block_new = not inp.meta_label_ok

    # 3) Target fraction for each side (only one side can be +EV given ask_yes+ask_no ~ 1).
    q_yes = inp.q_mid
    q_no = (1.0 - inp.q_mid) if inp.q_mid is not None else None
    p_no_mean = (1.0 - inp.p_yes_mean) if inp.p_yes_mean is not None else None
    f_yes, peff_yes, pc_yes, askeff_yes = _side_target_fraction(
        inp.p_yes_mean, inp.p_sd, inp.yes_ask, q_yes, cfg, fee_rate=inp.fee_rate, maker=inp.maker_entry)
    f_no, peff_no, pc_no, askeff_no = _side_target_fraction(
        p_no_mean, inp.p_sd, inp.no_ask, q_no, cfg, fee_rate=inp.fee_rate, maker=inp.maker_entry)

    # Pick the side we should hold (the +EV one). If currently holding a side, its
    # own target governs trim/hold; the opposite side only matters for a flip.
    if inp.held_side == "YES":
        return _resolve_side(inp, cfg, side="YES", f_target=f_yes, p_eff=peff_yes, p_c=pc_yes,
                             bid=inp.yes_bid, ask=inp.yes_ask, opp_f=f_no, opp_side="NO",
                             meta_block_new=meta_block_new)
    if inp.held_side == "NO":
        return _resolve_side(inp, cfg, side="NO", f_target=f_no, p_eff=peff_no, p_c=pc_no,
                             bid=inp.no_bid, ask=inp.no_ask, opp_f=f_yes, opp_side="YES",
                             meta_block_new=meta_block_new)

    # Flat: open the better +EV side, if any clears the deadband AND the exchange minimum.
    if f_yes >= f_no and f_yes > 0:
        side, f_target, p_eff, p_c, ask = "YES", f_yes, peff_yes, pc_yes, inp.yes_ask
    elif f_no > 0:
        side, f_target, p_eff, p_c, ask = "NO", f_no, peff_no, pc_no, inp.no_ask
    else:
        return SizerDecision(action="SKIP", reason="no side has positive after-cost Kelly edge",
                             target_fraction=0.0, p_eff=peff_yes, p_conservative=pc_yes,
                             diagnostics={"f_yes": round(f_yes, 6), "f_no": round(f_no, 6)})

    target_notional = f_target * inp.bankroll_usd
    if meta_block_new:
        return SizerDecision(action="SKIP", target_side=side, target_fraction=round(f_target, 6),
                             target_notional_usd=round(target_notional, 6), p_eff=p_eff, p_conservative=p_c,
                             reason="meta-label vetoed opening new risk on this signal class")
    # skip-don't-floor: opening below the exchange minimum is an overbet, not a trade.
    if target_notional + 1e-9 < cfg.exchange_min_notional_usd:
        return SizerDecision(action="SKIP", target_side=side, target_fraction=round(f_target, 6),
                             target_notional_usd=round(target_notional, 6), p_eff=p_eff, p_conservative=p_c,
                             reason=("kelly target ${:.2f} below exchange minimum ${:.2f}; skipping to "
                                     "avoid overbet (correct action is no trade, not min size)"
                                     ).format(target_notional, cfg.exchange_min_notional_usd))
    shares = target_notional / ask if ask and ask > 0 else 0.0
    return SizerDecision(action="BUY_OPEN", trade_side=side, trade_direction="BUY",
                         trade_shares=round(shares, 8), target_side=side,
                         target_fraction=round(f_target, 6), target_notional_usd=round(target_notional, 6),
                         delta_notional_usd=round(target_notional, 6), p_eff=p_eff, p_conservative=p_c,
                         reason="open {} to fractional-Kelly target".format(side),
                         diagnostics={"ask_eff": round(min(1 - EPS, (ask or 0) + _exec_fee(ask or 0, inp.fee_rate, inp.maker_entry) + cfg.cost_buffer + cfg.edge_buffer), 6),
                                      "maker_entry": inp.maker_entry, "fee_rate": inp.fee_rate})


def _resolve_side(inp, cfg, *, side, f_target, p_eff, p_c, bid, ask, opp_f, opp_side, meta_block_new) -> SizerDecision:
    """Trim / hold / scale-in / full-exit / flip for a side we currently hold."""
    current_notional = inp.held_shares * (bid or 0.0)
    target_notional = f_target * inp.bankroll_usd
    delta = target_notional - current_notional
    base = dict(target_side=side, target_fraction=round(f_target, 6),
                target_notional_usd=round(target_notional, 6),
                current_notional_usd=round(current_notional, 6),
                delta_notional_usd=round(delta, 6), p_eff=p_eff, p_conservative=p_c)

    if f_target <= 0.0:
        # Held side lost its edge. Sell all; flag a flip if the opposite side is now +EV.
        flip = opp_f > 0
        return SizerDecision(action="FULL_SELL", trade_side=side, trade_direction="SELL",
                             trade_shares=round(inp.held_shares, 8),
                             reason=("held {} edge gone; full exit{}".format(
                                 side, " (opposite side {} now +EV -> flip on next tick)".format(opp_side) if flip else "")),
                             diagnostics={"flip_to": opp_side if flip else None, "opp_f": round(opp_f, 6)},
                             **base)

    if delta > cfg.min_trade_usd:
        # Scale in — but the ADD itself must clear the exchange minimum, else hold.
        if delta + 1e-9 < cfg.exchange_min_notional_usd:
            return SizerDecision(action="HOLD", reason="target up but add ${:.2f} below exchange minimum; hold".format(delta), **base)
        if meta_block_new:
            return SizerDecision(action="HOLD", reason="target up but meta-label vetoes adding risk; hold", **base)
        add_shares = delta / ask if ask and ask > 0 else 0.0
        return SizerDecision(action="BUY_MORE", trade_side=side, trade_direction="BUY",
                             trade_shares=round(add_shares, 8),
                             reason="favourable: after-cost Kelly target rose; scale in", **base)

    if delta < -cfg.min_trade_usd:
        sell_shares = min(inp.held_shares, (-delta) / bid) if bid and bid > 0 else 0.0
        return SizerDecision(action="SELL_PARTIAL", trade_side=side, trade_direction="SELL",
                             trade_shares=round(sell_shares, 8),
                             reason="adverse: after-cost Kelly target fell; graduated partial trim (not all-or-nothing)", **base)

    return SizerDecision(action="HOLD", reason="position within deadband of fractional-Kelly target", **base)


# ---------------------------------------------------------------------------
# Selftest: synthetic, pure, no I/O.
# ---------------------------------------------------------------------------

def selftest() -> dict:
    checks: dict[str, bool] = {}
    cfg = SizerConfig(kelly_fraction=0.5, cost_buffer=0.01, edge_buffer=0.01, z=0.0,
                      min_trade_usd=0.5, exchange_min_notional_usd=5.0, residual_lambda=1.0)

    # Flat, strong edge -> open YES.
    d = compute_target_position(SizerInputs(p_yes_mean=0.70, p_sd=0.0, q_mid=0.50,
                                            yes_bid=0.49, yes_ask=0.50, no_bid=0.49, no_ask=0.50,
                                            bankroll_usd=100.0), cfg)
    checks["flat_strong_edge_opens_yes"] = d.action == "BUY_OPEN" and d.trade_side == "YES" and d.trade_shares > 0

    # Flat, no edge (p ~ market) -> skip.
    d = compute_target_position(SizerInputs(p_yes_mean=0.505, p_sd=0.0, q_mid=0.50,
                                            yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51,
                                            bankroll_usd=100.0), cfg)
    checks["flat_no_edge_skips"] = d.action == "SKIP"

    # Flat, tiny bankroll so Kelly target < $5 -> SKIP, not floor-up (overbet fix).
    d = compute_target_position(SizerInputs(p_yes_mean=0.62, p_sd=0.0, q_mid=0.50,
                                            yes_bid=0.49, yes_ask=0.50, no_bid=0.49, no_ask=0.50,
                                            bankroll_usd=25.0), cfg)
    checks["below_min_skips_not_floors"] = d.action == "SKIP" and "overbet" in d.reason

    # Held YES, edge IMPROVES (p up) -> scale in (BUY_MORE).
    held = SizerInputs(p_yes_mean=0.80, p_sd=0.0, q_mid=0.55, yes_bid=0.55, yes_ask=0.56,
                       no_bid=0.44, no_ask=0.45, bankroll_usd=100.0, held_side="YES", held_shares=20.0)
    d = compute_target_position(held, cfg)
    checks["held_edge_up_scales_in"] = d.action == "BUY_MORE" and d.trade_shares > 0

    # Held YES, edge WEAKENS but still +EV -> partial trim, NOT full sell.
    held2 = SizerInputs(p_yes_mean=0.60, p_sd=0.0, q_mid=0.55, yes_bid=0.55, yes_ask=0.56,
                        no_bid=0.44, no_ask=0.45, bankroll_usd=100.0, held_side="YES", held_shares=80.0)
    d = compute_target_position(held2, cfg)
    checks["held_edge_down_partial_trim"] = d.action == "SELL_PARTIAL" and 0 < d.trade_shares < 80.0

    # Held YES, edge GONE (market overpays our side) -> full sell.
    d = compute_target_position(SizerInputs(p_yes_mean=0.40, p_sd=0.0, q_mid=0.70, yes_bid=0.70, yes_ask=0.71,
                                            no_bid=0.29, no_ask=0.30, bankroll_usd=100.0,
                                            held_side="YES", held_shares=50.0), cfg)
    checks["held_edge_gone_full_sell"] = d.action == "FULL_SELL" and d.trade_shares == 50.0

    # Dirty resolution -> full exit regardless of edge.
    d = compute_target_position(SizerInputs(p_yes_mean=0.90, p_sd=0.0, yes_bid=0.60, yes_ask=0.61,
                                            bankroll_usd=100.0, held_side="YES", held_shares=10.0,
                                            resolution_clean=False), cfg)
    checks["dirty_resolution_full_exit"] = d.action == "FULL_SELL"

    # Residual calibration: lambda=0 pins p_eff to market => no edge => skip.
    cfg0 = SizerConfig(kelly_fraction=0.5, residual_lambda=0.0, z=0.0)
    d = compute_target_position(SizerInputs(p_yes_mean=0.85, p_sd=0.0, q_mid=0.50,
                                            yes_bid=0.49, yes_ask=0.50, no_bid=0.49, no_ask=0.50,
                                            bankroll_usd=1000.0), cfg0)
    checks["lambda_zero_pins_market_no_edge"] = d.action == "SKIP"

    # prob_uncertainty widens with divergence.
    checks["uncertainty_widens_with_divergence"] = (
        prob_uncertainty(0.8, 0.5) > prob_uncertainty(0.0, 1.0))

    # Verified fee mechanic + mode-aware cost: a marginal edge that clears as a
    # MAKER (0 fee) but not as a TAKER (pays rate*p*(1-p)). Defaults: cost_buffer
    # 0.015, edge_buffer 0.02 => maker ask_eff 0.535, taker(0.5) ask_eff 0.5425.
    checks["taker_fee_peaks_at_half"] = abs(taker_fee_per_share(0.5, 0.03) - 0.0075) < 1e-9
    cfg2 = SizerConfig(kelly_fraction=0.5, z=0.0, residual_lambda=1.0)
    base_in = dict(p_yes_mean=0.54, p_sd=0.0, q_mid=0.50, yes_bid=0.49, yes_ask=0.50,
                   no_bid=0.49, no_ask=0.50, bankroll_usd=10000.0, fee_rate=0.03)
    mk = compute_target_position(SizerInputs(**base_in, maker_entry=True), cfg2)
    tk = compute_target_position(SizerInputs(**base_in, maker_entry=False), cfg2)
    checks["maker_opens_marginal_edge"] = mk.action == "BUY_OPEN"
    checks["taker_fee_skips_marginal_edge"] = tk.action == "SKIP"

    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "passed": sum(checks.values()), "total": len(checks)}


if __name__ == "__main__":
    import json
    import sys
    r = selftest()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
