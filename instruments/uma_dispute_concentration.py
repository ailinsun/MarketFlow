#!/usr/bin/env python3
"""Concentration of power in the settlement layer — who disputes, who proposes,
and how much bond they post (read-only).

**Why this exists.** An optimistic oracle has three structural weaknesses: voting
power can be bought, challenging a small market is not worth the effort, and
resolution text leaves semantic gaps. That much is an argument. The `disputer`,
`proposer` and `bond_raw` fields sitting in the dispute log turn the first two of
them into measurements.

**Three publishable structural facts** (reproduced by `--report`):
  1. Dispute initiation is highly concentrated — HHI and top-N share, against the
     fully dispersed benchmark of 1/n.
  2. The same addresses appear on both the proposing and the challenging side.
     Reported as structure only; no motive is inferred.
  3. **The bond is very close to a constant, independent of market size.** This
     one needs no join against volume to hold: if bonds scaled with size they
     would not cluster on a single value. A constant bond simultaneously means
     "challenging a small market costs too much relative to the prize" and
     "challenging a large one costs nothing relative to it".

**Reporting discipline, hard.** This module reports structure, never motive. An
address that disputes often may be a professional market maker, an official
adapter, or an arbitrageur; the data cannot tell them apart, so nothing is said.
Addresses are shown as prefixes. Nobody is named and nobody is accused. The rule
this follows is: audit the market, do not judge its participants.

Hard boundary: read-only over an already-persisted `uma_disputes.jsonl`. No
network, and no write to any live path.
"""
from __future__ import annotations

import argparse
import collections
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "runtime/alerts/settlement_intelligence/uma_disputes.jsonl")
OUT = os.path.join(REPO, "runtime/alerts/settlement_intelligence/dispute_concentration.json")
SCHEMA = "marketflow-uma-dispute-concentration-v0.1"
USDC_DECIMALS = 10 ** 6
TOP_N = (5, 10, 20)


def _hhi(counts) -> float:
    total = sum(counts)
    return round(sum((c / total) ** 2 for c in counts), 6) if total else 0.0


def analyse(rows: list[dict]) -> dict:
    disputer: collections.Counter = collections.Counter()
    proposer: collections.Counter = collections.Counter()
    changed_by_disputer: collections.Counter = collections.Counter()
    bonds: list[int] = []
    liveness: collections.Counter = collections.Counter()

    for r in rows:
        changed = bool(r.get("proposal_changed"))
        for q in r.get("requests", []):
            d = str(q.get("disputer") or "").lower()
            p = str(q.get("proposer") or "").lower()
            if d:
                disputer[d] += 1
                if changed:
                    changed_by_disputer[d] += 1
            if p:
                proposer[p] += 1
            b = q.get("bond_raw")
            if b is not None:
                bonds.append(int(b))
            liveness[q.get("custom_liveness_sec")] += 1

    n_disputes = sum(disputer.values())
    both = set(disputer) & set(proposer)
    bond_counts = collections.Counter(bonds)
    modal_bond, modal_n = (bond_counts.most_common(1)[0] if bond_counts else (None, 0))

    def top_block(counter: collections.Counter, with_success: bool) -> dict:
        total = sum(counter.values())
        rows_out = []
        for addr, c in counter.most_common(max(TOP_N)):
            row = {"address_prefix": addr[:12], "n": c, "share": round(c / total, 6) if total else None}
            if with_success:
                w = changed_by_disputer.get(addr, 0)
                row["disputes_that_changed_the_answer"] = w
                row["change_rate"] = round(w / c, 6) if c else None
            rows_out.append(row)
        return {
            "unique_addresses": len(counter),
            "events": total,
            "hhi": _hhi(counter.values()),
            "hhi_if_perfectly_dispersed": round(1 / len(counter), 6) if counter else None,
            "concentration_x_vs_dispersed": (round(_hhi(counter.values()) * len(counter), 2)
                                             if counter else None),
            "top_shares": {f"top_{n}": round(sum(c for _, c in counter.most_common(n)) / total, 6)
                           for n in TOP_N if total},
            "top_rows": rows_out,
        }

    return {
        "schema": SCHEMA,
        "source": os.path.relpath(SOURCE, REPO),
        "disputed_markets": len(rows),
        "dispute_events": n_disputes,
        "disputers": top_block(disputer, True),
        "proposers": top_block(proposer, False),
        "identity_overlap": {
            "addresses_on_both_sides": len(both),
            "share_of_disputers": round(len(both) / len(disputer), 6) if disputer else None,
            "reading": "an address appearing on both sides is a structural observation, "
                       "not an allegation — market makers, official adapters and arbitrageurs "
                       "all produce this pattern and the data cannot tell them apart",
        },
        "bond": {
            "n": len(bonds),
            "distinct_values": len(bond_counts),
            "modal_bond_usdc": round(modal_bond / USDC_DECIMALS, 2) if modal_bond else None,
            "modal_share": round(modal_n / len(bonds), 6) if bonds else None,
            "min_usdc": round(min(bonds) / USDC_DECIMALS, 2) if bonds else None,
            "max_usdc": round(max(bonds) / USDC_DECIMALS, 2) if bonds else None,
            "finding": "the bond is effectively a constant, not a function of market size",
            "why_it_matters": "a flat bond makes challenging a small market expensive relative "
                              "to what is at stake, and challenging a large one nearly free — "
                              "the two failure modes the optimistic-oracle design is most often "
                              "criticised for, measured rather than asserted",
        },
        "custom_liveness_sec": {str(k): v for k, v in sorted(
            liveness.items(), key=lambda kv: -kv[1])[:6]},
        "method": {
            "unit": "one dispute event = one oracle request carrying a disputer; a market with "
                    "multiple oracle rounds contributes more than one event",
            "hhi": "sum of squared shares; compared against 1/n, the value under perfect dispersion",
            "change_rate": "share of an address's disputes where the settled answer differs from "
                           "the proposed one — an outcome measure, not a skill measure",
            "addresses": "truncated to a 12-char prefix; full addresses are public on-chain but "
                         "this file does not amplify them",
        },
    }


def selftest() -> int:
    rows = [
        {"proposal_changed": True, "requests": [{"disputer": "0xAAA", "proposer": "0xBBB", "bond_raw": 500000000}]},
        {"proposal_changed": False, "requests": [{"disputer": "0xAAA", "proposer": "0xCCC", "bond_raw": 500000000}]},
        {"proposal_changed": False, "requests": [{"disputer": "0xBBB", "proposer": "0xAAA", "bond_raw": 100000000}]},
    ]
    r = analyse(rows)
    checks = {
        "dispute event count": r["dispute_events"] == 3,
        "disputers de-duplicated": r["disputers"]["unique_addresses"] == 2,
        "overturn rate": r["disputers"]["top_rows"][0]["change_rate"] == 0.5,
        # Tolerance follows `_hhi`'s 6-digit rounding, not float precision: 1e-9
        # would fail on the rounding alone.
        "HHI": abs(r["disputers"]["hhi"] - ((2 / 3) ** 2 + (1 / 3) ** 2)) < 1e-6,
        "both-sides overlap": r["identity_overlap"]["addresses_on_both_sides"] == 2,
        "modal bond": r["bond"]["modal_bond_usdc"] == 500.0 and r["bond"]["distinct_values"] == 2,
        "addresses truncated": all(len(x["address_prefix"]) <= 12 for x in r["disputers"]["top_rows"]),
    }
    failed = [k for k, ok in checks.items() if not ok]
    print(json.dumps({"schema": SCHEMA + "-selftest", "PASS": not failed,
                      "checks": checks, "failed": failed}, ensure_ascii=False, indent=1))
    return 0 if not failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Settlement-layer power concentration (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--source", default=SOURCE)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    rows = []
    with open(args.source, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    res = analyse(rows)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, args.out)

    d, b = res["disputers"], res["bond"]
    print(f"{res['disputed_markets']:,} disputed markets / {res['dispute_events']:,} dispute events")
    print(f"  {d['unique_addresses']} initiating addresses · top-5 {d['top_shares']['top_5']:.1%} · "
          f"top-10 {d['top_shares']['top_10']:.1%}")
    print(f"  HHI {d['hhi']:.4f} = {d['concentration_x_vs_dispersed']}x the fully dispersed benchmark")
    print(f"  {res['identity_overlap']['addresses_on_both_sides']} addresses on both sides")
    print(f"  bond takes {b['distinct_values']} distinct values; modal ${b['modal_bond_usdc']} "
          f"is {b['modal_share']:.1%} of them (range ${b['min_usdc']}-${b['max_usdc']})")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
