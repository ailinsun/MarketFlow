#!/usr/bin/env python3
"""Whole-market volume census plus adjusted volume (read-only).

**Why this has to exist.** Adjusted volume needs a denominator, and the filter
alone is not one. A rolling feed can look like tens of thousands of rows while
holding only a couple of hundred unique markets, and a one-off catalogue snapshot
goes stale quickly. Without whole-market volume there is nothing for "adjusted"
to be adjusted against.

**How the data is fetched.** Cursor pagination over the metadata API's keyset
endpoint. The cursor parameter name matters: get it wrong and **nothing errors**.
Plausible alternatives are silently ignored, the endpoint still answers 200, and
it returns the first page forever. That mistake produced dozens of pages of the
same rows while the "rows scanned" counter climbed the whole time.

Two guards follow from it: **de-duplicate across pages by condition id**, and
**abort the moment the cursor stops advancing**, marking `cursor_stalled` in the
metadata rather than quietly accumulating duplicates.

**Adjustment definition (pre-registered — defined before looking at the numbers,
because otherwise this is marking your own homework):**

    reported          = venue-reported volume (24h / lifetime)
    farm_wallet_vol   = buying in this market by wallets already identified as
                        wash-trading farms
    near_certain_vol  = buying at p >= 0.99 in this market, a structural proxy for
                        wash volume that needs no wallet identification at all
    adjusted          = reported − max(farm_wallet_vol, near_certain_vol)

**Why max rather than a sum.** The two adjustments overlap almost entirely: what
a farm wash-trades IS the near-certain ticket, because `rate * (1 - p)` goes to
zero as p approaches 1, making that the cheapest possible way to manufacture
volume. Adding them would deduct the same activity twice.

**This is a lower-bound adjustment, and that has to be said in the same breath.**
Farm wallets are only visible above the large-print threshold; wash trading below
it is invisible. Real contamination is therefore **at least** the deduction, which
makes `adjusted` an **upper** bound on adjusted volume. Reporting a bound as a
point estimate is precisely the practice this module exists to measure.

Hard boundary: read-only over the public metadata API. It places no order and
touches no arm state, caps, kill file or any live write path.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "runtime/execution/volume_census")
HEAT = os.path.join(REPO, "runtime/execution/polymarket_farm_filter/market_heat.json")
HEAT_TAPE = os.path.join(REPO, "runtime/execution/polymarket_farm_filter/market_heat_tape.json")
ADJUSTED = os.path.join(OUT_DIR, "adjusted_volume.json")
GAMMA = "https://gamma-api.polymarket.com"
UA = "MarketFlow-Volume-Census/0.1 (read-only)"
SCHEMA = "marketflow-volume-census-v0.1"
# Markets with zero reported volume are not persisted: dead markets dominate the
# tail and storing them only inflates the snapshot without adding information.
MIN_VOLUME_USD = 1.0


def _get(url: str, timeout: float = 25.0, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001  retry honestly; never swallow
            last = e
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"gamma keyset failed after {retries} tries: {last}")


def _num(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0
    except (TypeError, ValueError):
        return 0.0


def census(closed: bool = False, sleep_s: float = 0.25, page: int = 500,
           max_pages: int = 4000) -> tuple[str, dict]:
    """Scan every market by cursor pagination and write one snapshot for the day
    as jsonl.gz. Returns (path, meta)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(OUT_DIR, f"census_{stamp}.jsonl.gz")
    cursor, n_seen, n_kept, pages = "", 0, 0, 0
    tot_vol = tot_24h = 0.0
    seen_ids: set[str] = set()
    stalled = False
    with gzip.open(path, "wt") as fh:
        while pages < max_pages:
            q = {"limit": page, "closed": "true" if closed else "false"}
            if cursor:
                # The parameter name matters. Plausible alternatives are silently
                # ignored: the endpoint still returns 200 and still returns the
                # first page. The measured cost of getting it wrong was dozens of
                # pages of identical rows while the row counter kept climbing.
                q["after_cursor"] = cursor
            doc = _get(f"{GAMMA}/markets/keyset?{urllib.parse.urlencode(q)}")
            rows = doc.get("markets") or []
            for m in rows:
                n_seen += 1
                cid_seen = str(m.get("conditionId") or "")
                if cid_seen and cid_seen in seen_ids:
                    continue          # cross-page dedup: silent repeats disguise
                                      # "how much was scanned" as "how much exists"
                if cid_seen:
                    seen_ids.add(cid_seen)
                vol = _num(m.get("volumeNum") or m.get("volume"))
                v24 = _num(m.get("volume24hr"))
                if vol < MIN_VOLUME_USD and v24 < MIN_VOLUME_USD:
                    continue
                cid = m.get("conditionId")
                if not cid:
                    continue
                n_kept += 1
                tot_vol += vol
                tot_24h += v24
                fh.write(json.dumps({
                    "condition_id": cid, "slug": m.get("slug"),
                    "volume_usd": round(vol, 2), "volume_24h_usd": round(v24, 2),
                    "volume_1wk_usd": round(_num(m.get("volume1wk")), 2),
                    "liquidity_usd": round(_num(m.get("liquidityNum") or m.get("liquidity")), 2),
                    "closed": bool(m.get("closed")), "end_date": m.get("endDate"),
                }, separators=(",", ":")) + "\n")
            pages += 1
            nxt = str(doc.get("next_cursor") or "")
            if not rows or not nxt:
                break
            if nxt == cursor:      # a stalled cursor means pagination is broken:
                                   # stop and report rather than pile up duplicates
                stalled = True
                break
            cursor = nxt
            if pages % 20 == 0:
                print(f"  [page {pages}] scanned {n_seen:,} / unique {len(seen_ids):,} / "
                      f"kept {n_kept:,} / reported ${tot_vol:,.0f}", flush=True)
            time.sleep(sleep_s)
    meta = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "closed_filter": closed, "pages": pages, "markets_seen": n_seen,
            "unique_markets_seen": len(seen_ids), "cursor_stalled": stalled,
            "markets_kept": n_kept, "exhausted": not stalled,
            "reported_volume_usd": round(tot_vol, 2), "reported_volume_24h_usd": round(tot_24h, 2),
            "min_volume_usd": MIN_VOLUME_USD, "path": os.path.relpath(path, REPO)}
    with open(path.replace(".jsonl.gz", "_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print(f"census: {n_kept:,} markets with volume / {n_seen:,} scanned / "
          f"reported ${tot_vol:,.0f} -> {path}")
    return path, meta


def _load_heat(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {r["condition_id"]: r for r in json.load(fh).get("markets", [])}


def adjust(census_path: str, heat_paths: tuple[str, ...] = (HEAT_TAPE, HEAT)) -> dict:
    """Join census volume with farm observations to produce adjusted volume. The
    definition is in the module docstring."""
    heat: dict[str, dict] = {}
    used_sources = []
    for p in heat_paths:                      # later files fill gaps; earlier wins
        h = _load_heat(p)
        if h:
            used_sources.append(os.path.relpath(p, REPO))
            for k, v in h.items():
                heat.setdefault(k, v)

    n = matched = 0
    reported = farm_v = nearc = adjusted_v = 0.0
    matched_reported = 0.0
    with gzip.open(census_path, "rt") as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            rep = m.get("volume_usd") or 0.0
            reported += rep
            h = heat.get(m["condition_id"])
            if not h:
                adjusted_v += rep           # unobserved means no deduction; never
                                            # pretend to know
                continue
            matched += 1
            matched_reported += rep
            fw = (h.get("buy_usd") or 0.0) * (h.get("farm_trader_share") or 0.0)
            nc = h.get("buy_usd_near_certain") or 0.0
            cut = min(rep, max(fw, nc))     # a deduction cannot exceed reported volume
            farm_v += fw
            nearc += nc
            adjusted_v += rep - cut

    return {
        "schema": "marketflow-adjusted-volume-v0.1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "census_path": os.path.relpath(census_path, REPO),
        "farm_observation_sources": used_sources,
        "markets": {"in_census": n, "with_farm_observation": matched,
                    "coverage_share": round(matched / n, 6) if n else None},
        "volume_usd": {
            "reported": round(reported, 2),
            "reported_on_matched_markets": round(matched_reported, 2),
            "farm_wallet_observed": round(farm_v, 2),
            "near_certain_observed": round(nearc, 2),
            "adjusted": round(adjusted_v, 2),
            "deduction": round(reported - adjusted_v, 2),
            "deduction_share": round((reported - adjusted_v) / reported, 6) if reported else None,
            # A whole-sample deduction rate is diluted into meaninglessness by
            # markets nobody observed: they enter the denominator contributing only
            # "not looked at". The readable number is the contamination share
            # among the markets that were actually observed.
            "deduction_share_on_observed": (round((reported - adjusted_v) / matched_reported, 6)
                                            if matched_reported else None),
            "observed_market_share_of_reported_volume": (round(matched_reported / reported, 6)
                                                         if reported else None),
        },
        "method": {
            "formula": "adjusted = reported - min(reported, max(farm_wallet_vol, near_certain_vol))",
            "why_max_not_sum": "the two overlap by construction — farms buy near-certain tickets "
                               "because rate*(1-p) vanishes as p approaches 1",
            "unmatched_markets": "left at reported volume; absence of observation is not evidence "
                                 "of cleanliness",
            "bound": "LOWER-BOUND deduction: farm wallets are only observable in the >=$2,000 "
                     "print layer, so true contamination >= deduction and `adjusted` is an "
                     "UPPER bound on clean volume",
            "preregistered": "definition fixed before the first run; changing it requires a "
                             "version bump and a changelog entry",
        },
    }


def selftest() -> int:
    import tempfile
    rows = [{"condition_id": "a", "volume_usd": 1000.0},
            {"condition_id": "b", "volume_usd": 500.0},
            {"condition_id": "c", "volume_usd": 200.0}]
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "census_x.jsonl.gz")
        with gzip.open(cp, "wt") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        hp = os.path.join(td, "heat.json")
        with open(hp, "w", encoding="utf-8") as fh:
            json.dump({"markets": [
                {"condition_id": "a", "buy_usd": 800.0, "farm_trader_share": 0.5,
                 "buy_usd_near_certain": 100.0},          # max(400, 100) = 400
                {"condition_id": "b", "buy_usd": 900.0, "farm_trader_share": 0.9,
                 "buy_usd_near_certain": 50.0},           # max(810, 50) > reported 500 -> capped at 500
            ]}, fh)
        r = adjust(cp, (hp,))
    v = r["volume_usd"]
    checks = {
        "reported": v["reported"] == 1700.0,
        "deduction takes max, not a sum": v["adjusted"] == 1000 - 400 + 0 + 200,
        "deduction never exceeds reported": v["deduction"] == 900.0,
        "unobserved markets are not deducted": r["markets"]["with_farm_observation"] == 2,
        "coverage share": abs(r["markets"]["coverage_share"] - 2 / 3) < 1e-6,
    }
    failed = [k for k, ok in checks.items() if not ok]
    print(json.dumps({"schema": SCHEMA + "-selftest", "PASS": not failed,
                      "checks": checks, "failed": failed}, ensure_ascii=False, indent=1))
    return 0 if not failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Whole-market volume census plus adjusted volume (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--census", action="store_true",
                    help="scan reported volume across all markets into a daily snapshot")
    ap.add_argument("--closed", action="store_true",
                    help="scan closed markets (default: open only)")
    ap.add_argument("--adjust", action="store_true",
                    help="produce adjusted volume from the latest snapshot")
    ap.add_argument("--census-path", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    path = args.census_path
    if args.census:
        path, _ = census(closed=args.closed)
    if args.adjust:
        if not path:
            snaps = sorted(f for f in os.listdir(OUT_DIR) if f.startswith("census_")
                           and f.endswith(".jsonl.gz")) if os.path.isdir(OUT_DIR) else []
            if not snaps:
                raise SystemExit("no census snapshot - run --census first")
            path = os.path.join(OUT_DIR, snaps[-1])
        res = adjust(path)
        tmp = ADJUSTED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, ADJUSTED)
        v = res["volume_usd"]
        print(f"reported ${v['reported']:,.0f} -> adjusted ${v['adjusted']:,.0f}")
        print(f"  deducted ${v['deduction']:,.0f} - whole sample {v['deduction_share']:.2%} / "
              f"**among observed markets {v['deduction_share_on_observed']:.2%}**")
        print(f"  farm observation covers {res['markets']['with_farm_observation']:,}/"
              f"{res['markets']['in_census']:,} markets ({res['markets']['coverage_share']:.2%}), "
              f"carrying {v['observed_market_share_of_reported_volume']:.1%} of reported volume")
        print(f"→ {ADJUSTED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
