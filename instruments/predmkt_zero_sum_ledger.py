#!/usr/bin/env python3
"""Reconcile local taker BUY fills into a three-party zero-sum ledger.

Zero-sum ledger for prediction markets. It keeps the original per-fill accounting
convention and its self-test, and reads only local data the caller supplies.
Each fill contributes taker net = gross selection minus fee, maker net = minus
gross selection, and venue receipts = fee. Missing metadata, unsettled markets,
unknown fee rates and invalid prices are dropped and counted.

Input: JSONL (optionally gzip) records with cid, asset, side, px, sz and ts;
metadata keyed by cid with closed, clob_token_ids and outcome_prices;
fee records keyed by cid with rate. Legacy metadata without closed retains the
original closed-market-cache convention. The public snapshots do not include
the raw tape or caches. Their aggregate identities can be checked separately.

Historical sample: active markets selected by large-print notional, not a venue
census. Capped API pages kept the newest trades. No wallet-level PnL is inferred.
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from typing import Any, Iterable

from pathlib import Path
import math

DATA = Path(__file__).resolve().parents[1] / "data"
SCHEMA = "marketflow-zero-sum-ledger-v0.1"


def fee_per_dollar(price: float, rate: float) -> float:
    """Taker fee per $1 of notional.

    The per-share fee is rate * p * (1 - p), and $1 buys 1/p shares, so the fee per
    dollar is rate * (1 - p). It goes to zero as p approaches 1: buying something the
    market already believes is nearly certain is nearly free, which is a structural
    property of the schedule rather than a market condition.
    """
    return rate * (1.0 - price)

def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval. A normal approximation on a small count produces a negative
    lower bound, which is not a conservative answer but a meaningless one."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))

def _token_price(meta: dict, token: str) -> float | None:
    toks, prices = meta.get("clob_token_ids") or [], meta.get("outcome_prices") or []
    if token in toks:
        i = toks.index(token)
        if i < len(prices):
            return prices[i]
    return None

def token_settlement(meta: dict, token: str) -> float | None:
    """The token's **settlement** price in this market: 1.0 won, 0.0 lost, None if the
    market has not settled or the token is not in it.

    The cache also holds unsettled markets, whose `outcome_prices` are current
    quotes. Reading those as settlement prices would book a live ticket trading at
    0.992 as already won. The `closed` flag is therefore a hard gate here. For a
    mark-to-market value use `token_mark` instead: the two conventions are recorded
    separately and are never merged.

    A legacy cache entry with no `closed` field is treated as settled, because those
    entries were only ever fetched with closed=true."""
    if not meta:
        return None
    if "closed" in meta and not meta.get("closed"):
        return None
    return _token_price(meta, token)

# -- per-fill accounting: pure functions, no I/O. This section alone is enough to
#    reproduce the ledger identity independently.

def trade_rows(price: float, size: float, settle: float, rate: float) -> dict[str, float]:
    """What one taker BUY books to each of the three parties, in dollars.

    notional  the notional put up, which is the denominator
    gross     (settle - px) * sz, what the taker won or lost against the counterparty
    fee       rate * px * (1 - px) * sz, the per-share fee times the shares
    taker_net = gross − fee
    maker_net -gross, since the counterparty pays no taker fee
    protocol  = +fee
    The three sum to exactly zero for any price, settlement and rate; the self-test
    asserts it across a grid rather than at one point.
    """
    notional = price * size
    gross = (settle - price) * size
    fee = rate * price * (1.0 - price) * size
    return {"notional": notional, "gross": gross, "fee": fee,
            "taker_net": gross - fee, "maker_net": -gross, "protocol": fee}


def iter_tape(path: str) -> Iterable[dict[str, Any]]:
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# -- the ledger ----------------------------------------------------------------

def build_ledger(tape_path: str, meta: dict[str, dict], fees: dict[str, dict],
                 buy_only: bool = True) -> dict[str, Any]:
    agg = {"notional": 0.0, "gross": 0.0, "fee": 0.0,
           "taker_net": 0.0, "maker_net": 0.0, "protocol": 0.0}
    n_used = 0
    drop = {"no_meta": 0, "unsettled": 0, "no_fee_rate": 0, "bad_price": 0, "sell_leg": 0}
    markets_used: set[str] = set()
    markets_dropped: set[str] = set()
    ts_lo = ts_hi = None
    by_band: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "notional": 0.0, "gross": 0.0, "fee": 0.0, "taker_net": 0.0})

    for t in iter_tape(tape_path):
        if buy_only and str(t.get("side") or "").upper() != "BUY":
            drop["sell_leg"] += 1
            continue
        cid, token = t.get("cid"), t.get("asset")
        m = meta.get(cid or "")
        if not m:
            drop["no_meta"] += 1
            markets_dropped.add(cid or "")
            continue
        settle = token_settlement(m, token)
        if settle is None:                       # unsettled: no outcome to book, and a
            #                                      quote is never substituted for one
            drop["unsettled"] += 1
            markets_dropped.add(cid or "")
            continue
        f = fees.get(cid or "") or {}
        rate = f.get("rate")
        if rate is None:                         # an unknown fee rate is dropped, not guessed
            drop["no_fee_rate"] += 1
            markets_dropped.add(cid or "")
            continue
        try:
            px, sz = float(t.get("px")), float(t.get("sz"))
        except (TypeError, ValueError):
            drop["bad_price"] += 1
            continue
        if not (0.0 < px < 1.0) or sz <= 0:
            drop["bad_price"] += 1
            continue

        r = trade_rows(px, sz, float(settle), float(rate))
        for k in agg:
            agg[k] += r[k]
        n_used += 1
        markets_used.add(cid or "")
        ts = t.get("ts")
        if isinstance(ts, (int, float)):
            ts_lo = ts if ts_lo is None or ts < ts_lo else ts_lo
            ts_hi = ts if ts_hi is None or ts > ts_hi else ts_hi
        b = _band(px)
        by_band[b]["n"] += 1
        for k in ("notional", "gross", "fee", "taker_net"):
            by_band[b][k] += r[k]

    N = agg["notional"]
    def pct(x: float) -> float | None:
        return round(100.0 * x / N, 4) if N else None

    closure = agg["taker_net"] + agg["maker_net"] + agg["protocol"]
    return {
        "schema": SCHEMA,
        "sample": {
            "trades_used": n_used, "markets_used": len(markets_used),
            "markets_dropped": len(markets_dropped - markets_used),
            "taker_buy_notional_usd": round(N, 2),
            "window_ts": {"from": ts_lo, "to": ts_hi},
            "dropped_trades": drop,
        },
        "three_party_close": {
            "taker_net_pct": pct(agg["taker_net"]),
            "maker_net_pct": pct(agg["maker_net"]),
            "protocol_pct": pct(agg["protocol"]),
            "sum_pct": pct(closure),
            "closes": abs(closure) < max(1e-6, 1e-9 * max(N, 1.0)),
        },
        "decomposition": {
            "taker_gross_selection_pct": pct(agg["gross"]),
            "protocol_fee_pct": pct(agg["fee"]),
            "fee_over_gross_x": (round(agg["fee"] / agg["gross"], 3)
                                 if agg["gross"] > 0 else None),
        },
        "usd": {k: round(v, 2) for k, v in agg.items()},
        "by_price_band": {
            k: {"n": v["n"], "notional_usd": round(v["notional"], 2),
                "gross_pct": round(100 * v["gross"] / v["notional"], 4) if v["notional"] else None,
                "fee_pct": round(100 * v["fee"] / v["notional"], 4) if v["notional"] else None,
                "taker_net_pct": round(100 * v["taker_net"] / v["notional"], 4) if v["notional"] else None}
            for k, v in sorted(by_band.items())},
        "method": {
            "fee_per_share": "rate * p * (1-p); maker pays zero",
            "denominator": "taker BUY notional = sum(px * size)",
            "settlement": "token_settlement — gated on the market's `closed` flag; "
                          "unsettled markets are dropped, never marked to current price",
            "fee_rate": "per-market, read from Gamma feeSchedule; markets without a "
                        "readable rate are dropped rather than defaulted",
            "closure": "taker_net + maker_net + protocol == 0 by construction",
            "sample_frame": "full tick tape of the top-N markets by whale-print notional — "
                            "a census of large active markets, not of the venue",
            "truncation": "the collector caps each market at 6,000 trades; data-api returns "
                          "newest-first, so a truncated market keeps its most recent 6,000 "
                          "fills and loses older history — a recency skew on long-lived, "
                          "high-frequency markets. Count in the tape meta file.",
        },
    }


def _band(px: float) -> str:
    for lo, hi in ((0.0, .05), (.05, .10), (.10, .20), (.20, .35), (.35, .50),
                   (.50, .65), (.65, .80), (.80, .90), (.90, .95), (.95, .98)):
        if lo <= px < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "0.98-1.00"


# ── CLI ─────────────────────────────────────────────────────────────────

def _cids_in_tape(path: str) -> list[str]:
    seen: dict[str, int] = {}
    for t in iter_tape(path):
        c = t.get("cid")
        if c:
            seen[c] = seen.get(c, 0) + 1
    return list(seen)


def selftest() -> int:
    ok, fails = 0, []

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        if cond:
            ok += 1
        else:
            fails.append(name)

    # The identity must hold for every input. It is the one thing in this module that
    # cannot be allowed to be wrong.
    for px, sz, settle, rate in ((0.30, 100, 1.0, 0.05), (0.97, 3, 0.0, 0.05),
                                 (0.5, 7, 1.0, 0.0), (0.02, 1000, 0.0, 0.07)):
        r = trade_rows(px, sz, settle, rate)
        check(f"closes at px={px} settle={settle}",
              abs(r["taker_net"] + r["maker_net"] + r["protocol"]) < 1e-9)
    # The per-fill fee must agree with the closed form for fee per dollar.
    r = trade_rows(0.30, 100, 1.0, 0.05)
    check("fee agrees with the closed form",
          abs(r["fee"] / r["notional"] - fee_per_dollar(0.30, 0.05)) < 1e-12)
    # the maker pays no taker fee
    check("maker net is minus the gross", abs(r["maker_net"] + r["gross"]) < 1e-12)
    # taker net is gross minus fee
    check("taker net is gross minus fee", abs(r["taker_net"] - (r["gross"] - r["fee"])) < 1e-12)
    # a zero-rate market books exactly zero to the venue
    r0 = trade_rows(0.5, 7, 1.0, 0.0)
    check("a zero rate books zero to the venue",
          r0["protocol"] == 0.0 and r0["taker_net"] == r0["gross"])
    # price-band boundaries
    check("price-band boundaries", _band(0.049) == "0.00-0.05" and _band(0.05) == "0.05-0.10"
          and _band(0.999) == "0.98-1.00")
    # Wilson interval, shared with the rest of the repository
    lo, hi = wilson_interval(68, 955)
    check("wilson interval brackets the point estimate", lo < 0.0712 < hi)

    print(json.dumps({"schema": SCHEMA + "-selftest", "PASS": not fails,
                      "checks_ok": ok, "failed": fails}, ensure_ascii=False, indent=2))
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile a local taker-BUY tape; no network access.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tape", type=Path, default=DATA / "inputs/tape.jsonl.gz")
    ap.add_argument("--meta", type=Path, default=DATA / "inputs/market_meta.json")
    ap.add_argument("--fees", type=Path, default=DATA / "inputs/market_fees.json")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if any(not p.is_file() for p in (args.tape, args.meta, args.fees)):
        ap.error("Raw tape and caches are not distributed. Supply --tape, --meta and --fees; use verify_snapshots.py for the frozen aggregates.")
    try:
        meta = json.loads(args.meta.read_text())
        fees = json.loads(args.fees.read_text())
        result = build_ledger(str(args.tape), meta.get("markets", meta), fees.get("fees", fees))
    except (OSError, ValueError, TypeError) as exc:
        ap.error("Cannot reconcile the supplied input: " + type(exc).__name__)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
