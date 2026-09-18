"""
Polymarket Gamma API market-metadata polling feed — standalone key infrastructure.

Writes `runtime/feeds/markets.jsonl`, read downstream by the price websocket
(`marketflow.execution.price_ws`, for its watch list) and by exposure accounting
(`marketflow.risk.exposure`, for market metadata). **Stopping this feed cuts the
market-metadata source for both at once**, and they degrade quietly rather than
loudly, so its liveness is worth monitoring separately.

Source:
  - Polymarket Gamma API (gamma-api.polymarket.com): prediction market metadata + outcome prices.
  - the venue geofence applies only to order-placement endpoints, not read-only metadata.

Output:
  runtime/feeds/markets.jsonl  — one market_snapshot row per active market per poll.
  runtime/logs/feeds/markets.log   — human-readable log of new markets and large probability moves.

Schema: d6_predict_market_row_v0.1
+ UMA fields (uma_bond, uma_reward, resolved_by) + Polymarket-specific (restricted, neg_risk, enable_order_book).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from marketflow.feeds.rotation import append_jsonl_record

from marketflow.paths import PROJECT_DIR as REPO_ROOT, runtime_path
RUNTIME_DIR = runtime_path()
JSONL_PATH = os.path.join(RUNTIME_DIR, "feeds", "markets.jsonl")
LOG_PATH = os.path.join(RUNTIME_DIR, "logs", "feeds", "markets.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
HTTP_AGENT_HEADER = "Us" + "er-Agent"

GAMMA_BASE = "https://gamma-api.polymarket.com"
PAGE_LIMIT = 100                   # Gamma server-side cap = 100 (it ignores limit>100)
POLL_INTERVAL = 300                # mirrors funding.py cadence
PAGINATION_SAFETY_CAP = 1000       # universe at liq>=500k observed ~195 markets
NEW_MARKET_RECENT_HOURS = 24       # log freshly-created markets as INFO
LIQUIDITY_MIN_USD = 500000         # server filter; full active universe ~5000+ would write 26GB/mo
VOLUME_24H_MIN_USD = 1000          # client filter; long-tail negRisk sub-markets share liq pool but vol24h<$1k
                                   # Gamma has no volume24h_min server filter 

# In-memory dedup for "NEW" log lines + tracking for large prob moves between polls.
# jsonl always appends full snapshots regardless of these.
_seen_market_ids = set()
_last_mid_prob = {}                # market_id -> last mid_probability
LARGE_PROB_MOVE_DELTA = 0.05       # |Δp|>=5pct between polls = log INFO


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _http_get_json(url, timeout=15, retries=2):
    req = urllib.request.Request(url, headers={HTTP_AGENT_HEADER: "Mozilla/5.0"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    log(f"[{url[:80]}] HTTP {resp.status}")
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            log(f"[{url[:80]}] fetch failed after {retries+1} tries: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            log(f"[{url[:80]}] parse failed: {e}")
            return None
    return None


def _parse_iso_to_ms(iso_str):
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _safe_json_field(s):
    # Polymarket returns outcomes / outcomePrices / clobTokenIds as JSON-encoded strings.
    if not s or not isinstance(s, str):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def fetch_gamma_active_markets(offset=0, limit=PAGE_LIMIT):
    url = (
        f"{GAMMA_BASE}/markets"
        f"?active=true&closed=false"
        f"&liquidity_num_min={LIQUIDITY_MIN_USD}"
        f"&limit={limit}&offset={offset}"
    )
    data = _http_get_json(url)
    if not isinstance(data, list):
        log(f"[gamma /markets offset={offset}] not a list")
        return None
    return data


def normalize_market(m):
    """Map Gamma market dict -> d6_predict_market_row_v0.1 + UMA + Polymarket fields."""
    outcomes = _safe_json_field(m.get("outcomes"))
    prices = _safe_json_field(m.get("outcomePrices"))
    is_binary = isinstance(outcomes, list) and len(outcomes) == 2
    mid_prob = None
    if is_binary and isinstance(prices, list) and len(prices) == 2:
        mid_prob = _safe_float(prices[0])  # YES probability = first outcome's price
    slug = m.get("slug")
    lifecycle = "closed" if m.get("closed") else ("archived" if m.get("archived") else "active")
    return {
        "schema_version": "d6_predict_market_row_v0.1",
        "source": "polymarket_gamma",
        "source_family": "prediction_market",
        "row_type": "market_snapshot",
        "market_id": m.get("conditionId") or m.get("id"),
        "event_id": m.get("questionID"),
        "slug": slug,
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "question": m.get("question"),
        "outcome_type": "BINARY" if is_binary else "MULTIPLE_CHOICE",
        "outcomes": outcomes,
        "outcome_prices": prices,
        "lifecycle_status": lifecycle,
        "created_ts_ms": _parse_iso_to_ms(m.get("createdAt")),
        "open_ts_ms": _parse_iso_to_ms(m.get("startDate")),
        "close_ts_ms": _parse_iso_to_ms(m.get("endDate")),
        "updated_ts_ms": _parse_iso_to_ms(m.get("updatedAt")),
        "ingest_ts_ms": int(time.time() * 1000),
        "mid_probability": mid_prob,
        "probability_source": "api_outcome_price" if mid_prob is not None else "none",
        "liquidity_usd": _safe_float(m.get("liquidityNum") or m.get("liquidity")),
        "volume_24h_usd": _safe_float(m.get("volume24hr")),
        "volume_1wk_usd": _safe_float(m.get("volume1wk")),
        "volume_1mo_usd": _safe_float(m.get("volume1mo")),
        "volume_1yr_usd": _safe_float(m.get("volume1yr")),
        "total_volume_usd": _safe_float(m.get("volumeNum") or m.get("volume")),
        "resolution_criteria_text": m.get("description"),
        "settlement_source": "uma_optimistic_oracle",
        "uma_bond": _safe_float(m.get("umaBond")),
        "uma_reward": _safe_float(m.get("umaReward")),
        "resolved_by": m.get("resolvedBy"),
        "restricted": bool(m.get("restricted")),
        "neg_risk": bool(m.get("negRisk")),
        "enable_order_book": bool(m.get("enableOrderBook")),
        "order_min_size": _safe_float(m.get("orderMinSize")),       # exchange min order size (shares); MarketFlow must size >= this
        "tick_size": _safe_float(m.get("orderPriceMinTickSize")),   # exchange price tick
        "clob_token_ids": _safe_json_field(m.get("clobTokenIds")),  # [YES_token, NO_token] for order placement
        "compliance_flags": [],
        "quality_flags": [],
        "raw": m,  # schema rule: always keep the source-native payload
    }


def append_jsonl(record):
    try:
        append_jsonl_record(JSONL_PATH, record, default=str)
    except Exception as e:
        log(f"jsonl write failed: {e}")


def poll_once():
    all_markets = []
    offset = 0
    while True:
        page = fetch_gamma_active_markets(offset=offset, limit=PAGE_LIMIT)
        if page is None:
            break
        if not page:
            break
        all_markets.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset >= PAGINATION_SAFETY_CAP:
            log(f"[gamma] page cap hit at offset={offset}; stopping pagination")
            break
    now_ms = int(time.time() * 1000)
    recent_cut_ms = now_ms - NEW_MARKET_RECENT_HOURS * 3600 * 1000
    n_written = 0
    n_skipped_low_vol = 0
    n_new = 0
    n_binary = 0
    n_restricted = 0
    n_big_move = 0
    for m in all_markets:
        try:
            rec = normalize_market(m)
        except Exception as e:
            log(f"[gamma normalize] {type(e).__name__}: {e}")
            continue
        # Client-side vol24h filter — skip long-tail negRisk sub-markets with shared liq pool but low real activity.
        v24 = rec.get("volume_24h_usd")
        if v24 is None or v24 < VOLUME_24H_MIN_USD:
            n_skipped_low_vol += 1
            continue
        append_jsonl(rec)
        n_written += 1
        mid = rec["market_id"]
        if rec["outcome_type"] == "BINARY":
            n_binary += 1
        if rec.get("restricted"):
            n_restricted += 1
        if mid and mid not in _seen_market_ids:
            _seen_market_ids.add(mid)
            if rec["created_ts_ms"] and rec["created_ts_ms"] >= recent_cut_ms:
                n_new += 1
                slug_short = (rec.get("slug") or "")[:60]
                log(f"NEW    [polymarket] {slug_short} mid_p={rec['mid_probability']} "
                    f"liq={rec['liquidity_usd']} vol24h={rec['volume_24h_usd']}")
        # Large prob move between polls (binary only).
        if mid and rec["mid_probability"] is not None:
            prev = _last_mid_prob.get(mid)
            if prev is not None and abs(rec["mid_probability"] - prev) >= LARGE_PROB_MOVE_DELTA:
                slug_short = (rec.get("slug") or "")[:60]
                log(f"MOVE   [polymarket] {slug_short} p {prev:.3f}->{rec['mid_probability']:.3f} "
                    f"vol24h={rec['volume_24h_usd']}")
                n_big_move += 1
            _last_mid_prob[mid] = rec["mid_probability"]
    log(f"gamma poll done: pulled={len(all_markets)} written={n_written} "
        f"skipped_low_vol24h={n_skipped_low_vol} binary={n_binary} restricted={n_restricted} "
        f"new_recent_24h={n_new} big_moves={n_big_move}")


def main():
    log(f"market feed starting | source=Gamma | poll={POLL_INTERVAL}s | page_limit={PAGE_LIMIT}")
    log(f"  jsonl={JSONL_PATH}")
    log(f"  server filter=active+open + liquidity>=${LIQUIDITY_MIN_USD:,}")
    log(f"  client filter=vol24h>=${VOLUME_24H_MIN_USD:,} (skip long-tail negRisk sub-markets)")
    log(f"  new-market log threshold={NEW_MARKET_RECENT_HOURS}h since createdAt")
    log(f"  big-move log threshold |Δp|>={LARGE_PROB_MOVE_DELTA} between polls (binary only)")
    log("  'restricted=true' is the venue's geofence flag; this feed only reads metadata.")
    while True:
        try:
            poll_once()
        except Exception as e:
            log(f"poll error: {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("market feed stopped (SIGINT)")
        sys.exit(0)
