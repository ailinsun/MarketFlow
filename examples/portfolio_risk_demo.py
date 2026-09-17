#!/usr/bin/env python3
"""End-to-end demonstration: what is actually at risk, and what may be traded.

Runs the real modules — nothing here is a mock — over a synthetic portfolio, in the
order the live system runs them:

    1. exposure     what is genuinely at risk, as opposed to what the book says
    2. budget       what a declared capital base permits
    3. gate         which candidate markets are tradeable at all
    4. sizing       how large the position should be
    5. structure    refusing an arbitrage that cannot actually be bought

Offline, deterministic, standard library only. It places no orders, needs no
credentials, opens no network connection and writes nothing outside a temporary
directory.

    python3 -B examples/portfolio_risk_demo.py
    make demo
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:          # runnable from a plain checkout
    sys.path.insert(0, os.path.dirname(HERE))

PORTFOLIO = os.path.join(HERE, "data", "portfolio.json")


def rule(title: str) -> None:
    print(f"\n{'─' * 78}\n{title}\n{'─' * 78}")


def money(x: float) -> str:
    return f"${x:,.2f}"


def step_exposure():
    from marketflow.risk import exposure as ex

    rule("1. EXPOSURE — the book overstates what is at risk")
    positions = ex.load_generic(PORTFOLIO)
    meta = ex.resolve_meta(positions, use_gamma=False)
    report = ex.portfolio_exposure(positions, meta, label="demo")

    print(f"  legs on the book                {report['n_legs']}")
    print(f"  event buckets                   {report['n_event_buckets']}")
    print(f"  cost paid (the book figure)     {money(report['gross_cost_usd'])}")
    print(f"  true event exposure             {money(report['true_event_exposure_usd'])}")
    print(f"  structural hedge                {money(report['structural_offset_usd'])}"
          f"  ({report['structural_offset_pct'] * 100:.1f}% of the book)")
    print(f"  worst single event              {money(report['max_single_event_loss_usd'])}")
    print(f"  independent bets by size        {report['n_eff_size']:.2f}")
    print(f"  independent bets with structure {report['n_eff_corr']:.2f}")
    print(f"  correlation concentration       {report['corr_concentration'] * 100:+.1f}%"
          "   (negative = structural diversification)")

    print("\n  per bucket:")
    for b in report["buckets"]:
        print(f"    {b['title'][:44]:44} {b['n_legs']:>2} legs  "
              f"book {money(b['naive_leg_exposure_usd']):>12}  "
              f"true {money(b['true_event_exposure_usd']):>12}  {b['exposure_basis']}")

    excl = next((b for b in report["buckets"] if b["exposure_basis"] == "negrisk_enumerated"
                 and b["states"]), None)
    if excl:
        print(f"\n  every settlement state of `{excl['event_key']}`, enumerated exactly:")
        for s in sorted(excl["states"], key=lambda x: x["pnl_usd"]):
            print(f"    {s['state']:<28} p={s['prob'] * 100:5.1f}%   P&L {money(s['pnl_usd']):>12}")
        print(f"    worst case {money(excl['worst_state_pnl_usd'])} — which is the true exposure,")
        print(f"    not the {money(excl['naive_leg_exposure_usd'])} of cost sitting on the book.")
        print("    Exactly one leg of a mutually exclusive group settles YES, so at most one")
        print("    of these NO legs can lose. Counting each leg separately is not conservative;")
        print("    it is wrong, and it is wrong by a factor that grows with the number of legs.")
    return report


def step_budget(report):
    from marketflow.execution import orders

    rule("2. BUDGET — limits are fractions of a declared capital base")
    base = orders.REFERENCE_CAPITAL_USD
    print(f"  capital base (MARKETFLOW_CAPITAL_BASE_USD)   {money(base)}")
    print(f"  default total deployment   {orders.DEFAULT_TOTAL_DEPLOY_FRACTION * 100:5.2f}%"
          f"  = {money(orders.DEFAULT_MAX_TOTAL_DEPLOY_USD)}")
    print(f"  default per trade          {orders.DEFAULT_PER_TRADE_FRACTION * 100:5.2f}%"
          f"  = {money(orders.DEFAULT_MAX_PER_TRADE_USD)}")
    print(f"  default drawdown fuse      {orders.DEFAULT_DRAWDOWN_FRACTION * 100:5.2f}%"
          f"  = {money(orders.DEFAULT_MAX_DRAWDOWN_USD)}")
    print(f"  owner ceiling, per trade   {orders.CEILING_PER_TRADE_FRACTION * 100:5.2f}%"
          f"  = {money(orders.OWNER_CAP_CEILING_PER_TRADE_USD)}")
    print("\n  Not one of these is a dollar constant in the source. Set a different capital")
    print("  base and every gate moves with it; the ceilings stay ceilings because they are")
    print("  fractions too. The defaults are illustrative, not a recommendation and not an")
    print("  industry standard — a deployment sets its own from its own mandate.")

    used = report["true_event_exposure_usd"] / orders.DEFAULT_MAX_TOTAL_DEPLOY_USD
    print(f"\n  this portfolio uses {used * 100:.2f}% of the default deployment budget,")
    print("  measured on true exposure rather than on the cost sitting on the book.")


def step_gate():
    from marketflow.execution import market_gate

    rule("3. GATE — most markets are not tradeable, and the reason is stated")
    candidates = [
        ("a longshot",            0.03, 0.09),
        ("a near-certainty",      0.985, 0.995),
        ("mid-band, no edge",     0.52, 0.53),
        ("mid-band, real edge",   0.44, 0.60),
    ]
    print(f"  {'candidate':<24} {'ask':>6} {'model p':>8}  {'band':<6} {'verdict':<10} why")
    for name, ask, p in candidates:
        g = market_gate.market_quality_gate(
            entry_ask_price=ask, model_probability=p,
            resolution_confirmed_clean=True, require_resolution=True)
        why = ", ".join(g["reject_reason_codes"]) or f"edge {g['edge_after_cost']:+.3f} after cost"
        print(f"  {name:<24} {ask:>6.3f} {p:>8.3f}  {g['band']:<6} "
              f"{g['authorization']:<10} {why[:40]}")
    print("\n  Note the last two rows: both are APPROVED, and one of them has a negative")
    print("  after-cost edge. The band filter is a hard gate; the edge test is advisory by")
    print("  default and is reported rather than enforced, because the module that owns the")
    print("  edge is the sizer, which refuses it in the next step. Set edge_advisory=False")
    print("  to make it blocking here instead. Two components must not both silently own")
    print("  the same decision.")
    print("\n  The band filter is not a preference. Fees are levied as rate * p * (1 - p),")
    print("  so the cost of a round trip relative to the upside that remains explodes at")
    print("  both ends of the probability range. instruments/fee_geometry.py is the")
    print("  arithmetic, and it is recomputable in one command.")


def step_sizing():
    from marketflow.risk import sizing

    rule("4. SIZING — from an edge to a number of shares")
    cfg = sizing.SizerConfig()

    def run(label: str, p_mean: float, p_sd: float, mid: float, bid: float, ask: float):
        inp = sizing.SizerInputs(
            p_yes_mean=p_mean, p_sd=p_sd, q_mid=mid,
            yes_bid=bid, yes_ask=ask, no_bid=round(1 - ask, 4), no_ask=round(1 - bid, 4),
            fee_rate=0.05, maker_entry=False, bankroll_usd=250_000.0,
            held_side=None, held_shares=0.0,
            resolution_clean=True, market_quality_ok=True,
        )
        d = sizing.compute_target_position(inp, cfg)
        print(f"\n  {label}")
        print(f"    model probability          {p_mean:.3f}  (sd {p_sd:.3f})")
        print(f"    market mid / ask           {mid:.3f} / {ask:.3f}")
        print(f"    raw edge against the mid   {p_mean - mid:+.3f}")
        print(f"    after shrink to the market {'—' if d.p_eff is None else format(d.p_eff, '.4f')}")
        print(f"    after its own uncertainty  "
              f"{'—' if d.p_conservative is None else format(d.p_conservative, '.4f')}")
        print(f"    decision                   {d.action} "
              f"{d.trade_side or ''} {d.trade_direction or ''}".rstrip())
        print(f"    target notional            {money(d.target_notional_usd)}"
              f"   ({d.target_fraction * 100:.3f}% of bankroll, {d.trade_shares:,.0f} shares)")
        print(f"    reason                     {d.reason}")
        return d

    skipped = run("a 13-point raw edge, thin and uncertain:", 0.60, 0.07, 0.47, 0.45, 0.48)
    taken = run("a 25-point raw edge, tight and confident:", 0.72, 0.04, 0.47, 0.45, 0.48)
    print("\n  The same machinery, two answers. The raw model probability is never sized on")
    print("  directly: it is shrunk toward the market price, then taken down by its own")
    print("  standard deviation, then a quarter of Kelly is applied to whatever survives,")
    print("  then per-market and per-cluster caps apply. Three independent haircuts, each")
    print("  with its own justification, because the failure being defended against is")
    print("  confidence. A 13-point edge does not survive them; a 25-point edge does, and")
    print("  even then it buys "
          f"{taken.target_fraction * 100:.2f}% of the bankroll rather than the "
          f"{sizing.kelly_fraction_binary(0.72, 0.48) * 100:.1f}% that full Kelly would ask for.")
    assert skipped.action == "SKIP"


def step_structure():
    from marketflow.risk import complete_sets

    rule("5. STRUCTURE — refusing an arbitrage that cannot be bought")
    legs = [{"name": "Alpha", "ask": 0.31, "depth_usd": 900.0},
            {"name": "Bravo", "ask": 0.29, "depth_usd": 640.0},
            {"name": "Charlie", "ask": 0.33, "depth_usd": 410.0}]
    good = complete_sets.assess_complete_set(legs)
    print(f"  a genuinely closed, buyable set   -> {good['verdict']}"
          f"   gross edge {good.get('gross_edge_pct')}%"
          f"   executable up to {money(good.get('max_executable_usd') or 0)}")

    placeholders = legs + [{"name": f"Party {c}", "ask": None, "depth_usd": 0.0}
                           for c in "DEFGHIJKL"] + [{"name": "Other", "ask": None, "depth_usd": 0.0}]
    bad = complete_sets.assess_complete_set(placeholders)
    print(f"  the same set plus placeholders    -> {bad['verdict']}"
          f"   ({bad['n_no_quote']} legs unquoted, {bad['n_open_ended']} open-ended)")
    print("\n  Priced off mid quotes the second set shows a large underround, because an")
    print("  unquoted leg's mid reads as zero. It is not an opportunity: those legs cannot")
    print("  be bought at any size, so the set cannot be completed. This gate exists because")
    print("  that exact false signal was measured on a live multi-candidate election market.")


def main() -> int:
    os.environ.setdefault("MARKETFLOW_RUNTIME", os.path.join(tempfile.gettempdir(),
                                                             "marketflow-demo-runtime"))
    print(__doc__.splitlines()[0])
    print(f"portfolio fixture: {os.path.relpath(PORTFOLIO)}  (synthetic — see its _comment)")
    report = step_exposure()
    step_budget(report)
    step_gate()
    step_sizing()
    step_structure()
    rule("Done")
    print("  Nothing above was mocked: these are the modules the live system runs.")
    print("  Next:  make verify     every gate and self-test in the repository")
    print("         ARCHITECTURE.md how the layers fit together")
    print("         RISK_MODEL.md   what each number means and what it does not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
