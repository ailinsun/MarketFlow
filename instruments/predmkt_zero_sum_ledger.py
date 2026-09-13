#!/usr/bin/env python3
"""Reconcile local taker BUY fills into a three-party zero-sum ledger.

预测市场零和账本。保留原始逐笔会计口径和自测；公开版只读调用方提供的本地数据。
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
    """每 $1 名义投入的 taker 费。

    referenced section 核实机制: 每股费 = rate·p·(1−p); $1 买到 1/p 股 ⟹ 每 $1 费 = rate·(1−p)。
    近确定区 (p→1) 费趋近 0 —— 这本身就是层 C 的结构优势之一。
    """
    return rate * (1.0 - price)

def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """小概率的置信区间必须用 Wilson —— 正态近似在 k 很小时会给出负下界那种废数。"""
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
    """token 在该市场的**结算价** (1.0 赢 / 0.0 输)。未结算 / 不在该市场 → None。

    C4 之后 cache 里同时有未结算市场, 它们的 `outcome_prices` 是当前市价 —— 拿它当结算价
    会把一个 0.992 的活票记成「已经赢了」。所以这里以 `closed` 位为准硬闸;
    要盯市值请走 `token_mark`, 两个口径分开记, 不合并。
    旧缓存条目 (无 `closed` 字段) 一律按已结算处理 —— 它们是 closed=true 抓回来的。"""
    if not meta:
        return None
    if "closed" in meta and not meta.get("closed"):
        return None
    return _token_price(meta, token)

# ── 逐笔口径 (纯函数, 零 IO — 外部人可以只抄这一段) ────────────────────────

def trade_rows(price: float, size: float, settle: float, rate: float) -> dict[str, float]:
    """一笔 taker BUY 在三方账本上各记多少钱 (美元)。

    notional  = 投入的名义额 (分母)
    gross     = 毛选边 = (settle − px)·sz   ← taker 相对对手方赢/输的
    fee       = rate·px·(1−px)·sz           ← 每股费 × 股数
    taker_net = gross − fee
    maker_net = −gross                      ← 对手方不付 taker 费
    protocol  = +fee
    三者相加恒为 0, 与 price/settle/rate 取值无关 (见 selftest)。
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


# ── 账本 ────────────────────────────────────────────────────────────────

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
        if settle is None:                       # 未结算 → 无结果可记, 绝不拿市价冒充
            drop["unsettled"] += 1
            markets_dropped.add(cid or "")
            continue
        f = fees.get(cid or "") or {}
        rate = f.get("rate")
        if rate is None:                         # 不知道费率就不猜, 整笔丢弃
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

    # 恒等式必须与取值无关 —— 这是整个模块唯一不能错的东西
    for px, sz, settle, rate in ((0.30, 100, 1.0, 0.05), (0.97, 3, 0.0, 0.05),
                                 (0.5, 7, 1.0, 0.0), (0.02, 1000, 0.0, 0.07)):
        r = trade_rows(px, sz, settle, rate)
        check(f"闭合 px={px} settle={settle}",
              abs(r["taker_net"] + r["maker_net"] + r["protocol"]) < 1e-9)
    # 每 $1 费必须等于既有单点真理 fee_per_dollar
    r = trade_rows(0.30, 100, 1.0, 0.05)
    check("费口径与 fee_per_dollar 一致",
          abs(r["fee"] / r["notional"] - fee_per_dollar(0.30, 0.05)) < 1e-12)
    # maker 不付 taker 费
    check("maker 净 = −毛选边", abs(r["maker_net"] + r["gross"]) < 1e-12)
    # taker 净 = 毛 − 费
    check("taker 净 = 毛 − 费", abs(r["taker_net"] - (r["gross"] - r["fee"])) < 1e-12)
    # 0 费率市场: 协议行必须真的是 0
    r0 = trade_rows(0.5, 7, 1.0, 0.0)
    check("0 费率 ⟹ 协议行为 0", r0["protocol"] == 0.0 and r0["taker_net"] == r0["gross"])
    # 价带边界
    check("价带边界", _band(0.049) == "0.00-0.05" and _band(0.05) == "0.05-0.10"
          and _band(0.999) == "0.98-1.00")
    # Wilson 复用的是既有实现
    lo, hi = wilson_interval(68, 955)
    check("wilson 复用", lo < 0.0712 < hi)

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
