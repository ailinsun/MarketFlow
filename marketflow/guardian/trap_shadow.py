"""Shadow accounting for the trap rules: what they WOULD have done.

Read-only. Replays a wallet's own public fill history through the same functions
`traps.screen_buy` calls live, so the answer to "how often does this fire and on
how much money" comes from the shipped rule and not from a second copy of it that
can drift.

Nothing here places, blocks, or modifies anything. It is the instrument to run
BEFORE promoting a rule from shadow to enforce, and the numbers it prints are the
honest cost of that promotion: how many of a wallet's orders would have hit a
wall, and for how much.

    python3 marketflow/guardian/trap_shadow.py --fleet --days 90
    python3 marketflow/guardian/trap_shadow.py --wallet 0x… --days 90
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO_ROOT
from marketflow.guardian import store as gstore  # noqa: E402
from marketflow.guardian import traps as gtraps  # noqa: E402

SHADOW_PAGES = 20  # deeper than the live tz read: this one is looking backwards


def replay_buys(rows: Iterable[dict[str, Any]], *, tz_offset_hours: float | None,
                since_ts: float | None = None) -> dict[str, Any]:
    """Pure replay of BUY fills. Every trip here is decided by the same functions
    the live seam uses; only the mode layer is absent, because shadow is
    asking what the rule sees, not what the fleet would do about it."""
    per_rule: dict[str, dict[str, float]] = {
        r: {"n": 0, "usd": 0.0} for r in gtraps.BUY_RULES}
    n_buys = 0
    usd_buys = 0.0
    any_n = 0
    any_usd = 0.0
    night_applicable = tz_offset_hours is not None
    for r in rows:
        if str(r.get("side") or "").upper() != "BUY":
            continue
        ts = gtraps._to_float(r.get("timestamp"))
        if ts is None or (since_ts is not None and ts < since_ts):
            continue
        px = gtraps._to_float(r.get("price"))
        usd = gtraps._to_float(r.get("usdcSize")) or 0.0
        n_buys += 1
        usd_buys += usd
        lh = gtraps.local_hour_at(ts, tz_offset_hours)
        hits = []
        if gtraps.check_cheap_ticket(px)["tripped"]:
            hits.append(gtraps.RULE_CHEAP_TICKET)
        if gtraps.check_night_lottery(px, lh)["tripped"]:
            hits.append(gtraps.RULE_NIGHT_LOTTERY)
        for h in hits:
            per_rule[h]["n"] += 1
            per_rule[h]["usd"] += usd
        if hits:
            any_n += 1
            any_usd += usd
    return {
        "n_buys": n_buys,
        "buy_usd": round(usd_buys, 2),
        "night_rule_applicable": night_applicable,
        "per_rule": {k: {"n": v["n"], "usd": round(v["usd"], 2),
                         "share_of_buys": (round(v["n"] / n_buys, 4) if n_buys else None),
                         "share_of_usd": (round(v["usd"] / usd_buys, 4) if usd_buys else None)}
                     for k, v in per_rule.items()},
        "intercepted_n": any_n,
        "intercepted_usd": round(any_usd, 2),
        "intercepted_share_of_usd": (round(any_usd / usd_buys, 4) if usd_buys else None),
    }


def replay_wallet(address: str, *, days: int = 90, tz_offset_hours: float | None = None,
                  now_ts: float | None = None, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = float(now_ts if now_ts is not None else gtraps._now_ts())
    ok = True
    if rows is None:
        rows, ok = gtraps.fetch_activity_rows(address, max_pages=SHADOW_PAGES)
    if tz_offset_hours is None:
        hist = [0] * 24
        n = 0
        for r in rows:
            ts = gtraps._to_float(r.get("timestamp"))
            if ts is not None:
                hist[int((ts % 86400) // 3600)] += 1
                n += 1
        if n >= gtraps.MIN_TRADES_FOR_TZ:
            tz_offset_hours, _ratio = gtraps.infer_tz_offset(hist)
    out = replay_buys(rows, tz_offset_hours=tz_offset_hours, since_ts=now - days * 86400.0)
    out.update({
        "address": address,
        "days": days,
        "tz_offset_hours": tz_offset_hours,
        "history_complete": ok,
        "n_activity_rows": len(rows),
    })
    return out


def replay_fleet(*, days: int = 90, now_ts: float | None = None) -> dict[str, Any]:
    """Every registered mandate. One whose zone cannot be determined is reported
    as such rather than folded in at UTC — the night rule genuinely does not apply
    to them, and a shadow report that hid that would overstate what promoting it
    would do."""
    doc = gstore.load_registry()
    tenants = doc.get("tenants") or {}
    runs = []
    for tid, entry in sorted(tenants.items()):
        addr = entry.get("funder_address")
        if not addr:
            runs.append({"tenant_id": tid, "skipped": "no_funder_address",
                         "status": entry.get("status")})
            continue
        row = replay_wallet(str(addr), days=days,
                            tz_offset_hours=gtraps.declared_offset(entry), now_ts=now_ts)
        row["tenant_id"] = tid
        row["status"] = entry.get("status")
        runs.append(row)
    return {
        "generated_at": gstore.iso_now(),
        "days": days,
        "tenant_count": len(tenants),
        "replayed": sum(1 for r in runs if "n_buys" in r),
        "modes": {r: gtraps.rule_mode(r) for r in
                  (gtraps.RULE_CHEAP_TICKET, gtraps.RULE_NIGHT_LOTTERY, gtraps.RULE_ZOMBIE)},
        "runs": runs,
    }


def render(rep: dict[str, Any]) -> str:
    lines = []
    if "runs" in rep:
        lines.append(f"guardian traps · shadow · last {rep['days']}d · "
                     f"{rep['replayed']}/{rep['tenant_count']} tenants replayed")
        lines.append(f"modes: {rep['modes']}")
        runs = rep["runs"]
    else:
        runs = [rep]
    for r in runs:
        if "n_buys" not in r:
            lines.append(f"  {r.get('tenant_id')}: skipped ({r.get('skipped')})")
            continue
        who = r.get("tenant_id") or r.get("address")
        tz = r.get("tz_offset_hours")
        lines.append(f"  {who}: {r['n_buys']} buys / ${r['buy_usd']:,.2f} · "
                     f"tz={'UTC%+g' % tz if tz is not None else 'undetermined'}"
                     f"{'' if r.get('history_complete', True) else ' · PARTIAL HISTORY'}")
        for rule, v in r["per_rule"].items():
            if rule == gtraps.RULE_NIGHT_LOTTERY and tz is None:
                lines.append(f"    {rule}: not applicable (no timezone)")
                continue
            lines.append(f"    {rule}: {v['n']} buys / ${v['usd']:,.2f} "
                         f"({(v['share_of_usd'] or 0) * 100:.2f}% of buy $)")
        lines.append(f"    would intercept: {r['intercepted_n']} buys / "
                     f"${r['intercepted_usd']:,.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trap-rule shadow accounting (read-only).")
    ap.add_argument("--fleet", action="store_true", help="every registered mandate")
    ap.add_argument("--wallet", help="any public wallet address")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--tz", type=float, default=None, help="override inferred UTC offset")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.wallet:
        rep = replay_wallet(args.wallet, days=args.days, tz_offset_hours=args.tz)
    elif args.fleet:
        rep = replay_fleet(days=args.days)
    else:
        ap.error("one of --fleet / --wallet is required")
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
