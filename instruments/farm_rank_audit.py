#!/usr/bin/env python3
"""Compare farm shares when the same wallets are ranked by dollars or activity.

Ranking-convention audit: given one tape and one pattern list, rank wallets by buy
notional and by trade count times market breadth, and compare the two answers.
This script preserves the original aggregation and counting method. It accepts
local files and writes aggregate counts only; the original wallet list and tape
are not distributed. Use --selftest for a synthetic example.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FARM_TAPE = os.path.join(REPO, "data/inputs/farm_wallets.json")
TAPE = os.path.join(REPO, "data/inputs/tape.jsonl.gz")
OUT = os.path.join(REPO, "data/generated/rank_audit.json")
DEFAULT_TOPS = (100, 500, 1000, 5000)


def wallet_aggregates(tape_path: str) -> dict[str, dict]:
    """Per wallet: buy notional, trade count, market breadth. BUY side only, because
    the sell leg of the same fill is its mirror and would double-count."""
    buy: dict[str, float] = collections.defaultdict(float)
    nt: collections.Counter = collections.Counter()
    mk: dict[str, set] = collections.defaultdict(set)
    op = gzip.open if tape_path.endswith(".gz") else open
    with op(tape_path, "rt") as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(t.get("side") or "").upper() != "BUY":
                continue
            w = t.get("w")
            if not w:
                continue
            try:
                buy[w] += float(t["px"]) * float(t["sz"])
            except (KeyError, TypeError, ValueError):
                continue
            nt[w] += 1
            mk[w].add(t.get("cid"))
    return {w: {"buy_usd": v, "n_trades": nt[w], "n_markets": len(mk[w])} for w, v in buy.items()}


def audit(agg: dict[str, dict], farm: set[str], tops=DEFAULT_TOPS) -> dict:
    by_usd = sorted(agg, key=lambda w: -agg[w]["buy_usd"])
    by_activity = sorted(agg, key=lambda w: -(agg[w]["n_trades"] * agg[w]["n_markets"]))
    rows = {}
    for n in tops:
        if n > len(by_usd):
            continue
        fu = sum(1 for w in by_usd[:n] if w in farm)
        fa = sum(1 for w in by_activity[:n] if w in farm)
        rows[f"top_{n}"] = {
            "by_dollar_volume": {"farm": fu, "n": n, "farm_share": round(fu / n, 6)},
            "by_activity_x_breadth": {"farm": fa, "n": n, "farm_share": round(fa / n, 6)},
            "ratio_x": round(fu / fa, 2) if fa else None,
        }
    return {
        "schema": "marketflow-farm-rank-audit-v0.1",
        "wallets_with_buys": len(agg),
        "farm_wallets_in_list": len(farm),
        "baseline_farm_share": round(sum(1 for w in agg if w in farm) / len(agg), 6) if agg else None,
        "rank_keys": {"by_dollar_volume": "sum(px*size) descending",
                      "by_activity_x_breadth": "n_trades * n_markets descending"},
        "by_top_n": rows,
        "reading": "the same wallets, the same farm list — only the sort key changes. "
                   "What a leaderboard shows is a property of its ranking key, not of the traders.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ranking-convention audit (read-only)")
    ap.add_argument("--tape", default=TAPE)
    ap.add_argument("--farm", default=FARM_TAPE)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        agg = {"a": {"buy_usd": 100.0, "n_trades": 1, "n_markets": 1},
               "b": {"buy_usd": 50.0, "n_trades": 90, "n_markets": 9},
               "c": {"buy_usd": 1.0, "n_trades": 2, "n_markets": 1}}
        r = audit(agg, {"a"}, tops=(1, 3))
        ok = (r["by_top_n"]["top_1"]["by_dollar_volume"]["farm_share"] == 1.0
              and r["by_top_n"]["top_1"]["by_activity_x_breadth"]["farm_share"] == 0.0
              and r["by_top_n"]["top_3"]["by_dollar_volume"]["farm_share"] == round(1 / 3, 6)
              and r["baseline_farm_share"] == round(1 / 3, 6))
        print(json.dumps({"PASS": ok, "detail": r["by_top_n"]}, ensure_ascii=False, indent=1))
        return 0 if ok else 1

    if not all(os.path.isfile(p) for p in (args.farm, args.tape)):
        ap.error("Raw inputs are not distributed. Supply --tape and --farm with local files.")
    with open(args.farm, encoding="utf-8") as fh:
        farm = set(json.load(fh)["farm_wallets"])
    agg = wallet_aggregates(args.tape)
    if not agg:
        ap.error("No usable BUY fills were found in the supplied tape.")
    result = audit(agg, farm)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, args.out)
    print(f"{result['wallets_with_buys']:,} wallets / pattern list "
          f"{result['farm_wallets_in_list']:,} / "
          f"baseline {result['baseline_farm_share']:.2%}")
    for k, v in result["by_top_n"].items():
        print(f"  {k:<9} by notional {v['by_dollar_volume']['farm_share']:>7.2%} | "
              f"by count x breadth {v['by_activity_x_breadth']['farm_share']:>6.2%} | "
              f"ratio {v['ratio_x']}")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
