#!/usr/bin/env python3
"""Collector for large prints and holder concentration (read-only).

Pulls two fact streams from the venue's public data API and writes them to an
isolated jsonl:
  1. Large prints: who bought or sold how much, at what price, on which leg.
  2. Holder concentration: top-holder share and Herfindahl index, describing the
     control structure of a market.

FRAMING, and this is a hard discipline: what this produces is raw observation
substrate. It is not a directional prediction, and it is emphatically not a
"smart money" signal. A single wallet, a single fill or a net flow proves nothing
about a future outcome. Consumers must aggregate, label the time window and
source, and carry directional=false. This collector records facts. It reaches no
verdict and touches no decision or execution path.

Endpoints (free, unauthenticated, public):
  - data-api.polymarket.com/trades : filterType=CASH & filterAmount / limit / offset / side / market.
  - data-api.polymarket.com/holders?market=<conditionId> : top token holders per outcome.
  Leaderboard endpoints have been measured returning 404, so wallet activity is
  aggregated from the public large-print stream by wallet and carries no skill
  label of any kind.

Discipline: read-only public data; no keys/orders/wallet. Writes ONLY under
runtime/feeds/smart_money/ (isolated). 0 touch engine/state.mx/kernel/paper.py/
arm state, caps or other feeds. Forces a direct connection with an empty
ProxyHandler: coupling a collector to a proxy it does not control is how a feed
dies quietly when that proxy does.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
RUNTIME_DIR = runtime_path()
OUT_DIR = os.path.join(RUNTIME_DIR, "feeds", "smart_money")
WHALE_PATH = os.path.join(OUT_DIR, "whale_trades.jsonl")
CONCENTRATION_PATH = os.path.join(OUT_DIR, "holder_concentration.jsonl")
CURSOR_PATH = os.path.join(OUT_DIR, "cursor.json")
HEARTBEAT_PATH = os.path.join(OUT_DIR, "heartbeat.json")
FARM_WALLETS_PATH = os.path.join(
    RUNTIME_DIR, "instruments", "farm_filter", "farm_wallets.json")

SCHEMA_VERSION = "polymarket-smart-money-feed-v0.1"
USER_AGENT = "marketflow-smart-money/0.1 (read-only research)"

DATA_API = "https://data-api.polymarket.com"
DEFAULT_MIN_CASH = 2000.0  # cash notional at or above which a print counts as large.
#                            Deliberately well below any "whale" definition: a low
#                            threshold buys signal density, and consumers can stratify
#                            by notional afterwards.
DEFAULT_TRADE_LIMIT = 500  # newest N large prints per cycle
DEFAULT_TOP_MARKETS = 12   # take holder concentration for the K busiest markets
HOLDERS_LIMIT = 20         # top-N holders per outcome; the endpoint is top-N, not the
#                            full holder set, and the output labels it as such
CURSOR_KEEP_KEYS = 2000    # trade keys retained in the cursor for cross-cycle dedup

# Force a direct connection: the public data endpoints work directly, and a
# collector must never inherit a proxy from the environment it happens to run in.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ----------------------------- http -----------------------------


def http_json(url: str, *, timeout: float = 25.0, retries: int = 3,
              backoff: float = 1.5) -> Any | None:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                              "Connection": "close"})
            with _DIRECT_OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                return None
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError, socket.timeout,
                ssl.SSLError, http.client.HTTPException, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(backoff * (attempt + 1))
    log(f"http_json gave up: {url[:90]} : {type(last).__name__}: {last}")
    return None


# ----------------------------- io -----------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def append_rows(path: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return len(rows)


def load_cursor() -> dict[str, Any]:
    try:
        with open(CURSOR_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"last_max_ts": 0, "recent_keys": []}


def save_cursor(cursor: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(CURSOR_PATH), exist_ok=True)
        tmp = CURSOR_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cursor, f, sort_keys=True)
        os.replace(tmp, CURSOR_PATH)
    except OSError as exc:
        log(f"cursor write failed: {exc}")


def write_heartbeat(stats: dict[str, Any]) -> None:
    rec = {"schema_version": SCHEMA_VERSION, "type": "heartbeat", "ts_ingest_ms": now_ms(),
           "whale_path": WHALE_PATH, "concentration_path": CONCENTRATION_PATH, **stats}
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2, sort_keys=True)
        os.replace(tmp, HEARTBEAT_PATH)
    except OSError as exc:
        log(f"heartbeat write failed: {exc}")


def load_farm_wallets(path: str = FARM_WALLETS_PATH) -> tuple[set[str], str | None]:
    """Load the current reproducible farm classification; missing means false + stale evidence.

    `farm_flag` stays a boolean on every row.  The heartbeat carries the classifier
    timestamp so downstream pages can distinguish a clean false from missing evidence.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        wallets = {str(w).lower() for w in (doc.get("farm_wallets") or [])}
        return wallets, doc.get("generated_at_utc")
    except (OSError, json.JSONDecodeError, AttributeError):
        return set(), None


# ----------------------------- whale trades -----------------------------


def trade_key(t: dict[str, Any]) -> str:
    """A stable de-duplication key for one trade. A transaction hash is unique but
    one transaction can contain several fills on different assets or sides, so the
    key combines hash + asset + side + size. With no hash available it falls back
    to wallet + asset + timestamp + price."""
    txh = str(t.get("transactionHash") or "").strip()
    asset = str(t.get("asset") or "").strip()
    side = str(t.get("side") or "").strip()
    if txh:
        return f"{txh}:{asset}:{side}:{t.get('size')}"
    return f"{t.get('proxyWallet')}:{asset}:{t.get('timestamp')}:{t.get('price')}:{t.get('size')}"


def parse_whale_trade(t: dict[str, Any], ts_ingest: int,
                      farms: set[str] | None = None) -> dict[str, Any]:
    side = str(t.get("side") or "").strip().upper()
    price = _f(t.get("price"))
    size = _f(t.get("size"))
    ts_source = t.get("timestamp")
    ts_source_ms = int(ts_source) * 1000 if isinstance(ts_source, (int, float)) else None
    notional = price * size if (price is not None and size is not None) else None
    # Nominal direction: buying YES and selling NO both bet on the outcome
    # happening. Only side and outcome are recorded; interpreting direction is the
    # consumer's job, not this collector's.
    flags = []
    if side not in ("BUY", "SELL"):
        flags.append("ambiguous_side")
    if price is None or (price is not None and not 0.0 < price < 1.0):
        flags.append("bad_price")
    if size is None or (size is not None and size <= 0):
        flags.append("bad_size")
    if ts_source_ms is None:
        flags.append("missing_source_ts")
    wallet = str(t.get("proxyWallet") or "").strip().lower()
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "polymarket_data_api_trades",
        "signal_type": "whale_print",
        "ts_source_ms": ts_source_ms,
        "ts_ingest_ms": ts_ingest,
        "latency_ms": (ts_ingest - ts_source_ms) if ts_source_ms is not None else None,
        "wallet": wallet,
        "farm_flag": wallet in (farms or set()),
        "wallet_label": (str(t.get("name") or "").strip()
                         or str(t.get("pseudonym") or "").strip() or None),
        "side": side,
        "outcome": str(t.get("outcome") or "").strip(),
        "outcome_index": t.get("outcomeIndex"),
        "price": price,
        "size": size,
        "notional_usd": round(notional, 2) if notional is not None else None,
        "condition_id": str(t.get("conditionId") or "").strip(),
        "asset": str(t.get("asset") or "").strip(),
        "title": str(t.get("title") or "").strip(),
        "slug": str(t.get("slug") or "").strip(),
        "event_slug": str(t.get("eventSlug") or "").strip(),
        "tx_hash": str(t.get("transactionHash") or "").strip() or None,
        "quality_flags": flags,
    }


def pull_whale_trades(min_cash: float, limit: int, ts_ingest: int,
                      cursor: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch the newest large trades, de-duplicate across cycles with the cursor,
    and return only the new ones plus the updated cursor."""
    q = urllib.parse.urlencode({
        "filterType": "CASH", "filterAmount": min_cash,
        "limit": limit, "offset": 0, "takerOnly": "true",
    })
    raw = http_json(f"{DATA_API}/trades?{q}")
    if not isinstance(raw, list):
        return [], cursor
    farms, _ = load_farm_wallets()
    seen = set(cursor.get("recent_keys") or [])
    last_max = int(cursor.get("last_max_ts") or 0)
    fresh: list[dict[str, Any]] = []
    new_keys: list[str] = []
    max_ts = last_max
    for t in raw:
        if not isinstance(t, dict):
            continue
        k = trade_key(t)
        if k in seen:
            continue
        seen.add(k)
        new_keys.append(k)
        fresh.append(parse_whale_trade(t, ts_ingest, farms))
        ts = t.get("timestamp")
        if isinstance(ts, (int, float)) and int(ts) > max_ts:
            max_ts = int(ts)
    # Keep the most recent N keys, including this cycle's, so a print is not
    # recorded twice across cycles. Older keys roll out.
    kept = (list(cursor.get("recent_keys") or []) + new_keys)[-CURSOR_KEEP_KEYS:]
    new_cursor = {"last_max_ts": max_ts, "recent_keys": kept}
    return fresh, new_cursor


# ----------------------------- holder concentration -----------------------------


def parse_holder_concentration(condition_id: str, token_block: dict[str, Any],
                               ts_ingest: int) -> dict[str, Any] | None:
    """Compute concentration from one outcome's holder block. The endpoint returns
    top-N holders rather than the full set, so the result is an approximation
    within that top-N and is labelled holders_are_topn."""
    holders = token_block.get("holders")
    if not isinstance(holders, list) or not holders:
        return None
    amounts = [a for a in (_f(h.get("amount")) for h in holders if isinstance(h, dict))
               if a is not None and a > 0]
    if not amounts:
        return None
    total = sum(amounts)
    amounts.sort(reverse=True)
    top1 = amounts[0] / total if total > 0 else None
    top5 = sum(amounts[:5]) / total if total > 0 else None
    herfindahl = sum((a / total) ** 2 for a in amounts) if total > 0 else None
    top = []
    for h in holders[:5]:
        if not isinstance(h, dict):
            continue
        top.append({
            "wallet": str(h.get("proxyWallet") or "").strip().lower(),
            "label": (str(h.get("name") or "").strip()
                      or str(h.get("pseudonym") or "").strip() or None),
            "amount": _f(h.get("amount")),
            "outcome_index": h.get("outcomeIndex"),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "polymarket_data_api_holders",
        "signal_type": "holder_concentration",
        "ts_ingest_ms": ts_ingest,
        "condition_id": condition_id,
        "token": str(token_block.get("token") or "").strip(),
        "n_holders_topn": len(amounts),
        "holders_are_topn": True,
        "top1_share": round(top1, 4) if top1 is not None else None,
        "top5_share": round(top5, 4) if top5 is not None else None,
        "herfindahl_topn": round(herfindahl, 4) if herfindahl is not None else None,
        "top_holders": top,
    }


def pull_holder_concentration(condition_ids: list[str], ts_ingest: int,
                              pause_s: float = 0.3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid in condition_ids:
        if not cid:
            continue
        q = urllib.parse.urlencode({"market": cid, "limit": HOLDERS_LIMIT})
        raw = http_json(f"{DATA_API}/holders?{q}")
        if isinstance(raw, list):
            for token_block in raw:
                if isinstance(token_block, dict):
                    rec = parse_holder_concentration(cid, token_block, ts_ingest)
                    if rec is not None:
                        out.append(rec)
        time.sleep(pause_s)  # be polite to the endpoint
    return out


def top_active_markets(whale_rows: list[dict[str, Any]], k: int) -> list[str]:
    """The K markets with the most notional-weighted activity in this cycle — the
    ones whose holder concentration is worth reading."""
    weight: dict[str, float] = defaultdict(float)
    for r in whale_rows:
        cid = r.get("condition_id")
        if cid:
            weight[cid] += (r.get("notional_usd") or 0.0)
    return [cid for cid, _ in sorted(weight.items(), key=lambda kv: kv[1], reverse=True)[:k]]


# ----------------------------- run -----------------------------


def run_once(min_cash: float, trade_limit: int, top_markets: int) -> dict[str, Any]:
    ts = now_ms()
    cursor = load_cursor()
    whale_rows, new_cursor = pull_whale_trades(min_cash, trade_limit, ts, cursor)
    w_written = append_rows(WHALE_PATH, whale_rows)
    cids = top_active_markets(whale_rows, top_markets) if whale_rows else []
    conc_rows = pull_holder_concentration(cids, ts) if cids else []
    c_written = append_rows(CONCENTRATION_PATH, conc_rows)
    save_cursor(new_cursor)
    per_side: dict[str, int] = defaultdict(int)
    notional_total = 0.0
    for r in whale_rows:
        per_side[r.get("side") or "?"] += 1
        notional_total += (r.get("notional_usd") or 0.0)
    farms, farms_generated_at = load_farm_wallets()
    stats = {
        "whale_written": w_written,
        "concentration_written": c_written,
        "markets_sampled": len(cids),
        "per_side": dict(per_side),
        "whale_notional_usd": round(notional_total, 2),
        "min_cash": min_cash,
        "farm_flagged": sum(1 for r in whale_rows if r.get("farm_flag")),
        "farm_wallets_loaded": len(farms),
        "farm_filter_generated_at": farms_generated_at,
    }
    write_heartbeat(stats)
    return stats


def daemon(interval_s: float, min_cash: float, trade_limit: int, top_markets: int) -> int:
    log(f"polymarket_smart_money daemon | interval={interval_s}s | min_cash=${min_cash} | "
        f"whale={WHALE_PATH}")
    total = 0
    while True:
        try:
            st = run_once(min_cash, trade_limit, top_markets)
            total += st["whale_written"]
            log(f"cycle wrote {st['whale_written']} whale + {st['concentration_written']} conc "
                f"(notional=${st['whale_notional_usd']}); total_whale={total}")
        except Exception as exc:
            log(f"cycle error: {type(exc).__name__}: {exc}")
        time.sleep(interval_s)


# ----------------------------- summary (read-only view) -----------------------------


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def summary(window_hours: float, top_n: int = 10) -> int:
    """Read-only view: large-print flow aggregated by market and outcome, plus
    concentration and active wallets. It states no direction."""
    cutoff = now_ms() - int(window_hours * 3600 * 1000)
    whales = [r for r in _read_jsonl(WHALE_PATH)
              if (r.get("ts_source_ms") or 0) >= cutoff and not r.get("quality_flags")]
    flow: dict[tuple[str, str], dict[str, Any]] = {}
    wallet_vol: dict[str, float] = defaultdict(float)
    for r in whales:
        cid, outcome = r.get("condition_id") or "", r.get("outcome") or "?"
        notional = r.get("notional_usd") or 0.0
        signed = notional if r.get("side") == "BUY" else -notional  # net buying of this outcome
        e = flow.setdefault((cid, outcome), {"title": r.get("title") or "", "net": 0.0, "n": 0})
        e["net"] += signed
        e["n"] += 1
        wallet_vol[r.get("wallet_label") or r.get("wallet") or "?"] += notional
    ranked = sorted(flow.values(), key=lambda e: e["net"], reverse=True)
    conc: dict[str, dict[str, Any]] = {}
    for r in _read_jsonl(CONCENTRATION_PATH):
        if r.get("condition_id"):
            conc[r["condition_id"]] = r  # the most recent row wins
    top_conc = sorted(conc.values(), key=lambda r: (r.get("top1_share") or 0), reverse=True)

    print(f"\n=== Top {top_n} net buying in large prints "
          f"(last {window_hours}h, prints >= ${DEFAULT_MIN_CASH:.0f}) ===")
    for e in ranked[:top_n]:
        print(f"  +${e['net']:>11,.0f}  {e['title'][:46]:46.46s} [{e['n']} prints]")
    print("=== Top 5 net selling in large prints ===")
    for e in sorted(ranked, key=lambda e: e["net"])[:5]:
        print(f"  ${e['net']:>12,.0f}  {e['title'][:46]:46.46s}")
    print("=== Top 5 most concentrated (largest single-holder share) ===")
    for r in top_conc[:5]:
        print(f"  top1={r.get('top1_share')} herf={r.get('herfindahl_topn')} n={r.get('n_holders_topn')}")
    print("=== Top 5 most active wallets by notional in the window ===")
    for w, v in sorted(wallet_vol.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        print(f"  ${v:>11,.0f}  {w[:26]}")
    print(f"\n(read-only view over {len(whales)} large prints; it describes trades "
          f"and predicts nothing)")
    return 0


# ----------------------------- selftest -----------------------------


def selftest() -> int:
    # whale parse: BUY 12000 shares @ 0.42 = $5040 notional
    t = {"proxyWallet": "0xABC", "side": "BUY", "asset": "9911", "conditionId": "0xcid",
         "size": 12000, "price": 0.42, "timestamp": 1783315270, "title": "Team A wins?",
         "slug": "team-a", "outcome": "Yes", "outcomeIndex": 0, "name": "SmartWhale",
         "pseudonym": "ps", "transactionHash": "0xtx"}
    r = parse_whale_trade(t, 1783315300000, {"0xabc"})
    assert r["side"] == "BUY" and r["wallet"] == "0xabc", r
    assert abs(r["notional_usd"] - 5040.0) < 1e-6, r["notional_usd"]
    assert r["wallet_label"] == "SmartWhale" and r["ts_source_ms"] == 1783315270000
    assert r["latency_ms"] == 1783315300000 - 1783315270000
    assert r["quality_flags"] == [], r["quality_flags"]
    assert r["farm_flag"] is True

    # bad price (>=1) + bad size flags
    bad = parse_whale_trade({"side": "SELL", "price": 1.5, "size": -3, "timestamp": None}, 1000)
    assert "bad_price" in bad["quality_flags"] and "bad_size" in bad["quality_flags"]
    assert "missing_source_ts" in bad["quality_flags"]
    assert bad["farm_flag"] is False

    # dedup key stability
    assert trade_key(t) == "0xtx:9911:BUY:12000"
    assert trade_key({"proxyWallet": "0xW", "asset": "1", "timestamp": 5, "price": 0.3,
                      "size": 10}) == "0xW:1:5:0.3:10"

    # holder concentration: 3 holders 700/200/100 of 1000 total
    block = {"token": "tok1", "holders": [
        {"proxyWallet": "0x1", "amount": 700, "outcomeIndex": 0, "name": "big"},
        {"proxyWallet": "0x2", "amount": 200, "outcomeIndex": 0, "pseudonym": "mid"},
        {"proxyWallet": "0x3", "amount": 100, "outcomeIndex": 0},
    ]}
    c = parse_holder_concentration("0xcid", block, 1000)
    assert abs(c["top1_share"] - 0.7) < 1e-9, c["top1_share"]
    assert abs(c["top5_share"] - 1.0) < 1e-9
    assert abs(c["herfindahl_topn"] - (0.49 + 0.04 + 0.01)) < 1e-9, c["herfindahl_topn"]
    assert c["n_holders_topn"] == 3 and c["top_holders"][0]["label"] == "big"

    # top_active_markets weights by notional
    rows = [{"condition_id": "0xA", "notional_usd": 5000},
            {"condition_id": "0xB", "notional_usd": 9000},
            {"condition_id": "0xA", "notional_usd": 1000}]
    assert top_active_markets(rows, 2) == ["0xB", "0xA"]

    print(json.dumps({"selftest": "ok", "whale_notional": r["notional_usd"],
                      "top1_share": c["top1_share"], "herf": c["herfindahl_topn"]}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket large-print / concentration puller (free, read-only).")
    ap.add_argument("--once", action="store_true", help="one poll cycle then exit")
    ap.add_argument("--daemon", action="store_true", help="continuous accumulation loop")
    ap.add_argument("--summary", action="store_true",
                    help="read-only view: aggregated large prints plus concentration")
    ap.add_argument("--window-hours", type=float, default=6.0,
                    help="window for --summary (default 6h)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--min-cash", type=float, default=DEFAULT_MIN_CASH,
                    help=f"minimum cash notional for one print, USD (default ${DEFAULT_MIN_CASH})")
    ap.add_argument("--trade-limit", type=int, default=DEFAULT_TRADE_LIMIT)
    ap.add_argument("--top-markets", type=int, default=DEFAULT_TOP_MARKETS)
    ap.add_argument("--interval-sec", type=float, default=300.0)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.summary:
        return summary(args.window_hours)
    if args.daemon:
        return daemon(args.interval_sec, args.min_cash, args.trade_limit, args.top_markets)
    st = run_once(args.min_cash, args.trade_limit, args.top_markets)
    print(json.dumps({"mode": "once", **st, "whale_jsonl": WHALE_PATH,
                      "concentration_jsonl": CONCENTRATION_PATH}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
