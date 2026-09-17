#!/usr/bin/env python3
"""Cross-venue fee intelligence for MarketFlow (read-only; never places orders).

The hot path reads an atomically refreshed cache, so Telegram replies do not wait
for a venue catalogue crawl.  Cost math is deliberately explicit and keeps
unknown maker schedules out of the "cheapest" verdict.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO, runtime_path
RUNTIME = os.environ.get("MARKETFLOW_RUNTIME", runtime_path("monitor"))
OUT_DIR = os.path.join(RUNTIME, "fee_router")
CACHE_PATH = os.path.join(OUT_DIR, "venue_cache.json")
OBS_PATH = os.path.join(OUT_DIR, "comparisons.jsonl")
REPORT_PATH = os.path.join(OUT_DIR, "monthly_savings.json")
GUARDIAN_LEDGERS = runtime_path("guardian", "tenants", "*", "ledger.jsonl")
SCHEMA = "marketflow-fee-router-v0.1"
UA = "MarketFlow-Fee-Intelligence/0.1 (read-only)"
PM_GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def _get(url: str, timeout: float = 18.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _atomic_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _float(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _prob(v: Any) -> float | None:
    x = _float(v)
    if x is None:
        return None
    return x / 100.0 if x > 1.0 else x


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 1 and t not in {"will", "the", "a", "an", "by", "in", "on", "to"}}


def _series_ticker(ticker: str) -> str:
    # Kalshi market tickers are SERIES-EVENT-MARKET; series identifiers are the first segment.
    return str(ticker or "").split("-", 1)[0]


def refresh_cache(pm_limit: int = 1000, kalshi_limit: int = 1000) -> dict[str, Any]:
    pm_raw: list[dict[str, Any]] = []
    for offset in range(0, pm_limit, 100):
        page = _get(f"{PM_GAMMA}/markets?active=true&closed=false&limit=100&offset={offset}")
        if not isinstance(page, list):
            break
        pm_raw.extend(x for x in page if isinstance(x, dict))
        if len(page) < 100:
            break
    pm: list[dict[str, Any]] = []
    for m in pm_raw:
        prices = m.get("outcomePrices") or []
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = []
        p = _prob(prices[0]) if prices else _prob(m.get("lastTradePrice"))
        schedule = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        pm.append({
            "venue": "polymarket", "id": str(m.get("conditionId") or m.get("id") or ""),
            "ticker": str(m.get("slug") or ""), "title": str(m.get("question") or ""),
            "yes_bid": _prob(m.get("bestBid")), "yes_ask": _prob(m.get("bestAsk")),
            "last": p, "fee_rate": _float(schedule.get("rate")) or 0.0,
            "fee_exponent": _float(schedule.get("exponent")) or 1.0,
            "taker_only": bool(schedule.get("takerOnly", True)),
            "source_url": f"https://polymarket.com/event/{m.get('eventSlug') or m.get('slug') or ''}",
        })

    raw = _get(f"{KALSHI}/markets?status=open&limit={kalshi_limit}")
    km = raw.get("markets", []) if isinstance(raw, dict) else []
    series_cache: dict[str, dict[str, Any]] = {}
    series_ids = sorted({_series_ticker(str(m.get("ticker") or "")) for m in km if m.get("ticker")})

    def load_series(st: str) -> tuple[str, dict[str, Any]]:
        try:
            sd = _get(f"{KALSHI}/series/{urllib.parse.quote(st)}", timeout=8.0)
            return st, sd.get("series", {}) if isinstance(sd, dict) else {}
        except Exception:
            return st, {}

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(load_series, st) for st in series_ids]
        for future in as_completed(futures):
            st, sd = future.result()
            series_cache[st] = sd
    kalshi: list[dict[str, Any]] = []
    for m in km:
        st = _series_ticker(str(m.get("ticker") or ""))
        s = series_cache.get(st, {})
        kalshi.append({
            "venue": "kalshi", "id": str(m.get("ticker") or ""),
            "ticker": str(m.get("ticker") or ""),
            "title": str(m.get("title") or m.get("subtitle") or ""),
            "yes_bid": _prob(m.get("yes_bid_dollars") or m.get("yes_bid")),
            "yes_ask": _prob(m.get("yes_ask_dollars") or m.get("yes_ask")),
            "last": _prob(m.get("last_price_dollars") or m.get("last_price")),
            "fee_rate": round(0.07 * (_float(s.get("fee_multiplier")) or 1.0), 8),
            "fee_multiplier": _float(s.get("fee_multiplier")) or 1.0,
            "fee_type": s.get("fee_type"),
            "source_url": f"https://kalshi.com/markets/{str(m.get('ticker') or '').lower()}",
        })
    doc = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sources": {"polymarket": PM_GAMMA, "kalshi": KALSHI},
           "markets": pm + kalshi, "counts": {"polymarket": len(pm), "kalshi": len(kalshi)}}
    _atomic_json(CACHE_PATH, doc)
    return doc


def load_cache() -> dict[str, Any]:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _match_score(q: str, m: dict[str, Any]) -> float:
    query = _tokens(q)
    target = _tokens(f"{m.get('title', '')} {m.get('ticker', '')}")
    if not query or not target:
        return 0.0
    overlap = len(query & target)
    return overlap / max(len(query), 1) + overlap / max(len(target), 1)


def quote(m: dict[str, Any], outcome: str = "yes", kind: str = "taker") -> dict[str, Any] | None:
    outcome = outcome.lower()
    bid, ask = _prob(m.get("yes_bid")), _prob(m.get("yes_ask"))
    if bid is None or ask is None or not (0 <= bid <= ask <= 1):
        return None
    px = (ask if outcome == "yes" else 1.0 - bid) if kind == "taker" else (
        bid if outcome == "yes" else 1.0 - ask)
    rate = float(m.get("fee_rate") or 0.0)
    fee_known = True
    if kind == "maker":
        if m.get("venue") == "polymarket" and m.get("taker_only"):
            rate = 0.0
        elif m.get("venue") == "kalshi" and m.get("fee_type") == "quadratic_with_maker_fees":
            fee_known = False
        else:
            rate = 0.0
    fee = rate * px * (1.0 - px) if fee_known else None
    all_in = px + fee if fee is not None else None
    return {"venue": m.get("venue"), "ticker": m.get("ticker"), "title": m.get("title"),
            "outcome": outcome, "order_kind": kind, "price": round(px, 6),
            "fee_per_share": round(fee, 6) if fee is not None else None,
            "all_in_per_share": round(all_in, 6) if all_in is not None else None,
            "spread": round(ask - bid, 6), "fee_rate": rate if fee_known else None,
            "fee_type": m.get("fee_type"), "source_url": m.get("source_url")}


def compare(query: str, outcome: str = "yes", record: bool = True) -> dict[str, Any]:
    t0 = time.monotonic()
    cache = load_cache()
    markets = cache.get("markets", []) if isinstance(cache, dict) else []
    selected: list[dict[str, Any]] = []
    for venue in ("polymarket", "kalshi"):
        ranked = sorted((m for m in markets if m.get("venue") == venue),
                        key=lambda m: _match_score(query, m), reverse=True)
        if ranked and (_match_score(query, ranked[0]) >= 0.42 or query.lower() in
                       str(ranked[0].get("ticker", "")).lower()):
            selected.append(ranked[0])
    quotes = [q for m in selected for q in (quote(m, outcome, "taker"), quote(m, outcome, "maker")) if q]
    immediate = [q for q in quotes if q.get("all_in_per_share") is not None and q.get("order_kind") == "taker"]
    posted = [q for q in quotes if q.get("all_in_per_share") is not None and q.get("order_kind") == "maker"]
    cheapest = min(immediate, key=lambda q: q["all_in_per_share"]) if immediate else None
    lowest_posted = min(posted, key=lambda q: q["all_in_per_share"]) if posted else None
    out = {"schema": SCHEMA, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "query": query, "outcome": outcome, "cache_generated_at": cache.get("generated_at"),
           "cache_age_sec": None, "quotes": quotes, "cheapest_immediate": cheapest,
           "lowest_posted": lowest_posted,
           "matched_venues": sorted({q["venue"] for q in quotes}),
           "latency_ms": round((time.monotonic() - t0) * 1000, 2),
           "read_only": True, "orders_placed": 0}
    try:
        if cache.get("generated_at"):
            stamp = datetime.fromisoformat(cache["generated_at"].replace("Z", "+00:00"))
            out["cache_age_sec"] = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
    except (TypeError, ValueError):
        pass
    if record:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(OBS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def render(result: dict[str, Any]) -> str:
    if not result.get("quotes"):
        return "No verified same-event match yet. Send a market title or link and retry."
    rows = []
    for q in result["quotes"]:
        fee = "unknown" if q["fee_per_share"] is None else f"{100*q['fee_per_share']:.3f}¢"
        total = "excluded" if q["all_in_per_share"] is None else f"{100*q['all_in_per_share']:.3f}¢"
        rows.append(f"• {q['venue']} {q['order_kind']}: px {100*q['price']:.2f}¢ · fee {fee} · all-in {total}")
    best = result.get("cheapest_immediate") or {}
    posted = result.get("lowest_posted") or {}
    head = "Cross-venue cost (read-only)"
    bestline = f"Lowest immediate cost: {best.get('venue')} taker"
    postedline = f"Lowest posted cost: {posted.get('venue')} maker (fill not guaranteed)"
    return (f"{head}\n" + "\n".join(rows)
            + f"\n{bestline}\n{postedline}\nNo order was placed.")


def monthly_report(month: str | None = None) -> dict[str, Any]:
    """Aggregate only comparisons carrying an explicit executed snapshot.

    Older fills lack cross-venue snapshots, so they are excluded rather than
    reconstructed with today's prices. This makes every reported dollar replayable.
    """
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    eligible, saved = 0, 0.0
    eligible_ids: set[str] = set()
    try:
        lines = open(OBS_PATH, encoding="utf-8", errors="ignore")
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(r.get("generated_at", "")).startswith(month):
            continue
        x = r.get("executed_snapshot")
        if not isinstance(x, dict) or x.get("executed_all_in_usd") is None or x.get("best_all_in_usd") is None:
            continue
        eligible += 1
        if x.get("execution_id"): eligible_ids.add(str(x["execution_id"]))
        saved += max(0.0, float(x["executed_all_in_usd"]) - float(x["best_all_in_usd"]))
    executed_ids: set[str] = set()
    executed_without_id = 0
    for path in glob.glob(GUARDIAN_LEDGERS):
        try:
            source = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with source:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("mode") != "LIVE_EXECUTED" or not str(row.get("generated_at", "")).startswith(month):
                    continue
                eid = str(row.get("execution_id") or "")
                if eid: executed_ids.add(eid)
                else: executed_without_id += 1
    excluded = len(executed_ids - eligible_ids) + executed_without_id
    doc = {"schema": SCHEMA, "month": month, "eligible_trades": eligible,
           "excluded_missing_at_trade_snapshot": excluded, "estimated_savings_usd": round(saved, 2),
           "executed_trades_seen": len(executed_ids) + executed_without_id,
           "method": "sum(max(0, executed_all_in_usd - best_all_in_usd)); at-trade snapshots only",
           "reconstructed_with_current_quotes": False, "orders_placed": 0}
    _atomic_json(REPORT_PATH, doc)
    return doc


def render_monthly(doc: dict[str, Any]) -> str:
    return (f"Monthly fee-savings ledger (at-trade snapshots only)\n"
            f"Replayable fills: {doc.get('eligible_trades', 0)}\n"
            f"Avoidable cost: ${float(doc.get('estimated_savings_usd') or 0):.2f}\n"
            f"Excluded without snapshot: {doc.get('excluded_missing_at_trade_snapshot', 0)}\n"
            "No reconstruction with current quotes; MarketFlow placed no orders.")


def selftest() -> int:
    pm = {"venue": "polymarket", "ticker": "x", "title": "X", "yes_bid": .48,
          "yes_ask": .52, "fee_rate": .04, "taker_only": True}
    q = quote(pm, "yes", "taker")
    assert q and q["fee_per_share"] == round(.04 * .52 * .48, 6)
    assert quote(pm, "yes", "maker")["fee_per_share"] == 0
    km = {"venue": "kalshi", "ticker": "x", "title": "X", "yes_bid": .48,
          "yes_ask": .52, "fee_rate": .07, "fee_type": "quadratic_with_maker_fees"}
    assert quote(km, "yes", "maker")["all_in_per_share"] is None
    assert "0.00" in render_monthly({"estimated_savings_usd": 0}, "zh")
    print(json.dumps({"selftest": "ok", "pm_taker_fee": q["fee_per_share"]}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("refresh", "compare", "monthly", "selftest"))
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--outcome", choices=("yes", "no"), default="yes")
    args = ap.parse_args()
    if args.command == "selftest":
        return selftest()
    value = refresh_cache() if args.command == "refresh" else (
        monthly_report(args.query or None) if args.command == "monthly" else compare(args.query, args.outcome))
    if args.command == "refresh":
        value = {"schema": value.get("schema"), "generated_at": value.get("generated_at"),
                 "counts": value.get("counts"), "cache_path": CACHE_PATH}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
