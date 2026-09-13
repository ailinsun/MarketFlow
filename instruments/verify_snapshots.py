#!/usr/bin/env python3
"""Verify frozen aggregate arithmetic and dataset structure without network access.

This checks the published summaries; it does not recreate a historical raw tape
or independently re-audit contract labels against venue rules.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"


def verify(data: Path) -> dict:
    def read(name):
        return json.loads((data / name).read_text())

    ledger = read("zero_sum_ledger/zero_sum_ledger.json")
    raw = read("zero_sum_ledger/ledger_raw_2026-08-13.json")
    close = ledger["three_party_close"]
    assert close == raw["three_party_close"], "Ledger summary differs from raw aggregate"
    usd = raw["usd"]
    assert abs(usd["taker_net"] + usd["maker_net"] + usd["protocol"]) <= .03
    assert abs(usd["gross"] - usd["fee"] - usd["taker_net"]) <= .03
    assert abs(usd["gross"] + usd["maker_net"]) <= .02
    assert abs(usd["fee"] - usd["protocol"]) <= .02
    for k in ("taker_net", "maker_net", "protocol"):
        assert abs(100 * usd[k] / usd["notional"] - close[k + "_pct"]) <= .0001
    assert abs(sum(close[k] for k in ("taker_net_pct", "maker_net_pct", "protocol_pct"))) <= .0002
    bands = ledger["by_price_band"]
    assert sum(v["n"] for v in bands.values()) == ledger["sample"]["trades_reconciled"]
    assert abs(sum(v["notional_usd"] for v in bands.values()) - usd["notional"]) <= .10
    assert bands == raw["by_price_band"]

    settlement = read("settlement_quality/settlement_quality.json")
    coverage = settlement["coverage"]
    for dimension in ("by_category", "by_rule_template"):
        rows = settlement[dimension].values()
        assert sum(v["settled"] for v in rows) == coverage["settled_requests"]
        assert sum(v["disputed"] for v in rows) == coverage["disputed_markets"]
        for v in rows:
            assert 0 <= v["disputed"] <= v["settled"]
            if v["reportable"]:
                assert abs(v["disputed"] / v["settled"] - v["dispute_rate"]) <= .000001

    clean = read("farm_signature/clean_data_2026-09-01_aggregates.json")
    for key in ("raw", "clean"):
        assert "list" not in clean[key], "Per-wallet list found"
    rank = read("farm_signature/rank_audit_2026-08-13.json")
    for row in rank["by_top_n"].values():
        for key in ("by_dollar_volume", "by_activity_x_breadth"):
            v = row[key]
            assert abs(v["farm"] / v["n"] - v["farm_share"]) <= .000001

    exposure = read("whale_exposure/whale_exposure_v1v2_comparison_2026-08-13.json")
    assert len(exposure["rows"]) == exposure["n_wallets"]
    def no_identity(x):
        if isinstance(x, dict):
            assert not {"address", "wallet", "label", "username", "name"}.intersection(x)
            for v in x.values(): no_identity(v)
        elif isinstance(x, list):
            for v in x: no_identity(v)
    no_identity(exposure)

    pairs = [json.loads(s) for s in (data / "contract_equivalence/gold_pairs.jsonl").read_text().splitlines() if s.strip()]
    schema = read("contract_equivalence/gold_pair.schema.json")
    assert len(pairs) == 20 and len({p["pair_id"] for p in pairs}) == len(pairs)
    allowed_labels = schema["properties"]["relation"]["properties"]["label"]["enum"]
    for p in pairs:
        assert set(schema["required"]).issubset(p)
        assert p["relation"]["label"] in allowed_labels
        assert p["review_status"] in schema["properties"]["review_status"]["enum"]
        assert {p["pair"][s]["venue"] for s in ("left", "right")} == {"polymarket", "kalshi"}
        assert all(e["source_url"].startswith("https://") for e in p["evidence"])
    return {"PASS": True, "ledger_fills": ledger["sample"]["trades_reconciled"],
            "settled_requests": coverage["settled_requests"], "contract_pairs": len(pairs),
            "scope": "aggregate arithmetic and structure; not a raw-data replication"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DATA)
    args = ap.parse_args()
    try:
        result = verify(args.data)
    except (AssertionError, OSError, ValueError, KeyError, TypeError) as exc:
        ap.exit(1, "Snapshot verification failed: " + type(exc).__name__ + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
