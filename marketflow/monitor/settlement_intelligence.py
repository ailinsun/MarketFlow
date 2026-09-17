#!/usr/bin/env python3
"""Settlement Intelligence: UMA history, shadow risk scores, and public hit ledger.

Read-only by construction.  The historical source is UMA's official Polygon OOv2
subgraph; only the canonical Polymarket requester adapters are retained.  The
module never signs, places orders, reads a wallet secret, or changes an arm gate.

The score is deliberately shadow-only.  ``public_ready`` can become true only
after a 30-day outcome window and only when AUROC beats the category-base-rate
baseline.  Until then no caller may present the score as a product claim.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.paths import PROJECT_DIR as REPO, runtime_path
OUT_DIR = runtime_path("monitor", "settlement_intelligence")
HISTORY_PATH = os.path.join(OUT_DIR, "uma_disputes.jsonl")
STATS_PATH = os.path.join(OUT_DIR, "history_stats.json")
SHADOW_PATH = os.path.join(OUT_DIR, "risk_shadow_latest.json")
SHADOW_MARKETS_PATH = os.path.join(OUT_DIR, "risk_shadow_latest.jsonl")
PREDICTIONS_PATH = os.path.join(OUT_DIR, "risk_shadow_predictions.jsonl")
PUBLIC_LEDGER_SOURCE = os.path.join(OUT_DIR, "alert_ledger.jsonl")
PUBLIC_LEDGER_PATH = os.path.join(OUT_DIR, "public_hit_ledger.json")
CHANNEL_CONF_PATH = os.path.join(OUT_DIR, "settlement_channel.json")
# Ledger activation = the first time build_public_ledger wrote to disk. The miss
# side of the comparison (disputes_observed) is counted from that moment: disputes
# before it were never inside the observation window, and counting them would
# fabricate misses. Set this to your own deployment's activation time.
LEDGER_ACTIVATED_AT = "2026-08-12T20:11:46Z"

SCHEMA = "marketflow-settlement-intelligence-v0.1"
# UMA Optimistic Oracle v2 subgraph. The base URL is deployment-specific (a hosted
# subgraph endpoint carries the operator's own project id), so it comes from the
# environment; the path after it is the public subgraph name and version.
SUBGRAPH = os.environ.get(
    "MARKETFLOW_UMA_SUBGRAPH_BASE", ""
).rstrip("/") + "/subgraphs/polygon-optimistic-oracle-v2/1.1.0/gn"
GAMMA = "https://gamma-api.polymarket.com"
UA = "MarketFlow-Settlement-Intelligence/0.1 (read-only)"
ADAPTERS = (
    "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7",
    "0x69c47de9d4d3dad79590d61b9e05918e03775f24",
    "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d",
)
TOO_EARLY = -(2**255)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _atomic(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _append(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def http_json(url: str, *, body: dict[str, Any] | None = None, timeout: float = 25.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    last: Exception | None = None
    for attempt in range(3):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"read-only HTTP failed: {url}: {type(last).__name__}: {last}")


_FIELDS = """
 id identifier ancillaryData requester proposer disputer proposedPrice settlementPrice state
 requestTimestamp proposalTimestamp disputeTimestamp settlementTimestamp
 requestBlockNumber proposalBlockNumber disputeBlockNumber settlementBlockNumber
 requestHash proposalHash disputeHash settlementHash customLiveness bond
"""


def iter_oo_requests(*, disputed_only: bool, settled_only: bool = False,
                     fetch: Any = http_json) -> Iterable[dict[str, Any]]:
    """Cursor-page official OOv2 rows.  ``id_gt`` avoids Graph skip limits."""
    cursor = ""
    where = [f'requester_in:[{",".join(json.dumps(x) for x in ADAPTERS)}]']
    if disputed_only:
        where.append("disputer_not:null")
    if settled_only:
        where.append("state:Settled")
    while True:
        parts = list(where)
        if cursor:
            parts.append(f"id_gt:{json.dumps(cursor)}")
        query = (
            "{optimisticPriceRequests(first:1000,orderBy:id,orderDirection:asc,where:{"
            + ",".join(parts) + "}){" + _FIELDS + "}}"
        )
        doc = fetch(SUBGRAPH, body={"query": query})
        if not isinstance(doc, dict) or doc.get("errors"):
            raise RuntimeError(f"UMA subgraph query failed: {str(doc)[:500]}")
        rows = (doc.get("data") or {}).get("optimisticPriceRequests") or []
        for row in rows:
            if isinstance(row, dict):
                yield row
        if len(rows) < 1000:
            return
        cursor = str(rows[-1].get("id") or "")
        if not cursor:
            raise RuntimeError("UMA subgraph pagination lost its cursor")


def decode_ancillary(value: Any) -> str:
    raw = str(value or "")
    try:
        return bytes.fromhex(raw[2:] if raw.startswith("0x") else raw).decode("utf-8", "replace")
    except ValueError:
        return ""


def ancillary_parts(text: str) -> dict[str, Any]:
    market = re.search(r"\bmarket_id:\s*(\d+)", text, re.I)
    title = re.search(r"(?:^|,)\s*q:\s*title:\s*(.*?),\s*description:\s*", text, re.I | re.S)
    desc = re.search(r",\s*description:\s*(.*?)(?:\.\s*market_id:|,\s*market_id:)", text, re.I | re.S)
    return {
        "market_id": market.group(1) if market else None,
        "title": (title.group(1).strip() if title else text[:180].strip()),
        "description": (desc.group(1).strip() if desc else text),
    }


_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("politics", (" election", "president", "prime minister", "governor", "senate", "congress",
                  "primary election", "referendum", "parliament", "mayor", "political party", "nominee")),
    ("sports", (" fifa", " nba", " nfl", " nhl", " mlb", "match", "tournament", "champion", "score")),
    ("crypto", ("bitcoin", "ethereum", "crypto", "token", "blockchain", "airdrop", "market cap")),
    ("macro", (" gdp", "inflation", "interest rate", "federal reserve", "unemployment", "cpi", "recession")),
    ("legal", ("court", "convict", "sentence", "indict", "lawsuit", "supreme court", "ruling")),
    ("entertainment", ("oscar", "grammy", "movie", "album", "box office", "reality show", "celebrity")),
)


def category_of(text: str) -> str:
    hay = " " + text.lower()
    for category, words in _CATEGORIES:
        if any(word in hay for word in words):
            return category
    return "other"


# Rule-feature dictionary.  This is the single source of truth for the template
# taxonomy AND for what gets published: a dispute rate broken down by template is
# not reproducible unless the reader can rebuild the same buckets, so the exact
# needles ship inside every published snapshot (see ``build_history``).
RULE_FEATURE_NEEDLES: dict[str, tuple[str, ...]] = {
    "subjective_language": ("credible reporting", "consensus", "significant", "substantial",
                            "officially", "widely recognized", "reasonable"),
    "explicit_source": ("resolution source", "according to", "reported by", "https://", "http://"),
    "timezone_boundary": (" utc", " et", " est", " edt", "local time", "11:59", "midnight", "timezone"),
    "fallback_clause": ("otherwise", "if no ", "in the event", "will resolve to other", "will resolve 50"),
    "revision_clause": ("revision", "revised", "initial release", "preliminary", "final value"),
    "multi_source": ("however, a consensus", "multiple sources", "any credible", "overwhelming consensus"),
}
LONG_RULES_CHARS = 1200
# Templates below this many settled requests are carried in the file but flagged
# ``reportable: false``.  Without it a 4-of-8 bucket reads as a 50% dispute rate.
MIN_N_REPORTABLE = 100


def rule_features(title: str, description: str) -> dict[str, int]:
    text = f"{title}\n{description}".lower()
    out = {k: int(any(n in text for n in needles)) for k, needles in RULE_FEATURE_NEEDLES.items()}
    out["long_rules"] = int(len(description) >= LONG_RULES_CHARS)
    return out


def wilson_interval(hits: int, n: int, z: float = 1.96) -> list[float] | None:
    """Wilson score interval.  Honest at small n where the normal approximation
    would put the bound below zero; ``None`` at n=0 rather than a fake [0, 0]."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (centre - radius) / denom), 6),
            round(min(1.0, (centre + radius) / denom), 6)]


def template_of(features: dict[str, int]) -> str:
    active = [k for k in ("subjective_language", "explicit_source", "timezone_boundary",
                          "fallback_clause", "revision_clause", "multi_source") if features.get(k)]
    return "+".join(active) if active else "plain"


def normalized_request(row: dict[str, Any]) -> dict[str, Any]:
    anc = decode_ancillary(row.get("ancillaryData"))
    parts = ancillary_parts(anc)
    proposed = _int(row.get("proposedPrice"))
    settled = _int(row.get("settlementPrice"))
    changed = bool(settled is not None and proposed is not None and settled != TOO_EARLY
                   and proposed != TOO_EARLY and settled != proposed)
    return {
        "request_id": row.get("id"), "market_id": parts["market_id"],
        "title": parts["title"], "description": parts["description"],
        "category": category_of(f"{parts['title']} {parts['description']}"),
        "rule_features": rule_features(parts["title"], parts["description"]),
        "requester": str(row.get("requester") or "").lower(),
        "proposer": row.get("proposer"), "disputer": row.get("disputer"),
        "proposed_price_e18": proposed, "settlement_price_e18": settled,
        "proposal_changed": changed, "state": row.get("state"),
        "timeline": {
            "requested_at": iso(row.get("requestTimestamp")),
            "proposed_at": iso(row.get("proposalTimestamp")),
            "disputed_at": iso(row.get("disputeTimestamp")),
            "settled_at": iso(row.get("settlementTimestamp")),
        },
        "blocks": {k: _int(row.get(k + "BlockNumber")) for k in
                   ("request", "proposal", "dispute", "settlement")},
        "tx": {k: row.get(k + "Hash") for k in ("request", "proposal", "dispute", "settlement")},
        "custom_liveness_sec": _int(row.get("customLiveness")),
        "bond_raw": _int(row.get("bond")),
        "source": "uma_official_polygon_oov2_subgraph",
    }


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _duration_hours(req: dict[str, Any]) -> float | None:
    t = req.get("timeline") or {}
    try:
        a = datetime.fromisoformat(str(t["disputed_at"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(t["settled_at"]).replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds() / 3600)
    except (KeyError, TypeError, ValueError):
        return None


def _quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return round(vals[min(len(vals) - 1, max(0, math.ceil(q * len(vals)) - 1))], 2)


def build_history(*, fetch: Any = http_json) -> dict[str, Any]:
    disputes = [normalized_request(r) for r in iter_oo_requests(disputed_only=True, fetch=fetch)]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for req in disputes:
        key = str(req.get("market_id") or req["request_id"])
        grouped[key].append(req)
    markets: list[dict[str, Any]] = []
    for key, reqs in grouped.items():
        reqs.sort(key=lambda r: ((r.get("timeline") or {}).get("requested_at") or ""))
        last = reqs[-1]
        markets.append({
            "schema": SCHEMA, "market_id": last.get("market_id"), "title": last.get("title"),
            "category": last.get("category"), "rule_template": template_of(last["rule_features"]),
            "rule_features": last["rule_features"], "request_count": len(reqs),
            "challenge_count": len(reqs), "requests": reqs,
            "settled": any((r.get("timeline") or {}).get("settled_at") for r in reqs),
            "proposal_changed": any(r.get("proposal_changed") for r in reqs),
        })
    markets.sort(key=lambda r: max(((q.get("timeline") or {}).get("disputed_at") or "")
                                   for q in r["requests"]), reverse=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for market in markets:
            fh.write(json.dumps(market, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, HISTORY_PATH)

    # Denominator = every settled canonical-adapter request.  Multiple OO rounds
    # collapse by market_id before rates are calculated.
    all_settled: dict[str, dict[str, Any]] = {}
    for raw in iter_oo_requests(disputed_only=False, settled_only=True, fetch=fetch):
        req = normalized_request(raw)
        all_settled[str(req.get("market_id") or req["request_id"])] = req
    disputed_keys = set(grouped)
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"settled": 0, "disputed": 0})
    by_tpl: dict[str, dict[str, int]] = defaultdict(lambda: {"settled": 0, "disputed": 0})
    for key, req in all_settled.items():
        cat, tpl = req["category"], template_of(req["rule_features"])
        by_cat[cat]["settled"] += 1; by_tpl[tpl]["settled"] += 1
        if key in disputed_keys:
            by_cat[cat]["disputed"] += 1; by_tpl[tpl]["disputed"] += 1
    def rates(d: dict[str, dict[str, int]]) -> dict[str, Any]:
        out = {}
        for k, v in sorted(d.items()):
            n, hits = v["settled"], v["disputed"]
            out[k] = {**v,
                      "dispute_rate": round(hits / n, 6) if n else None,
                      "dispute_rate_ci95": wilson_interval(hits, n),
                      "reportable": n >= MIN_N_REPORTABLE}
        return out
    durations = [x for m in markets for x in (_duration_hours(q) for q in m["requests"]) if x is not None]
    dur_by_cat: dict[str, list[float]] = defaultdict(list)
    for m in markets:
        for q in m["requests"]:
            x = _duration_hours(q)
            if x is not None:
                dur_by_cat[m.get("category") or "other"].append(x)
    settled_disputes = [m for m in markets if m["settled"]]
    changed = [m for m in settled_disputes if m["proposal_changed"]]
    # Observation window over the settled denominator, not the disputed subset:
    # a rate without the window it was measured over is not comparable to the
    # next snapshot, and every published ratio here is a per-window quantity.
    stamps = [t for req in all_settled.values()
              for t in ((req.get("timeline") or {}).get("requested_at"),
                        (req.get("timeline") or {}).get("settled_at")) if t]
    stats = {
        "schema": SCHEMA, "generated_at": iso(time.time()),
        "window": {"from": min(stamps) if stamps else None, "to": max(stamps) if stamps else None,
                   "basis": "requested_at / settled_at over every settled canonical-adapter request"},
        "coverage": {"source": SUBGRAPH, "canonical_requesters": list(ADAPTERS),
                     "settled_requests_grouped": len(all_settled),
                     "disputed_markets": len(markets), "settled_disputed_markets": len(settled_disputes),
                     "covers_all_settled_disputes_in_source": True},
        "proposal_change_rate": round(len(changed) / len(settled_disputes), 6) if settled_disputes else None,
        "proposal_change_rate_ci95": wilson_interval(len(changed), len(settled_disputes)),
        "dispute_duration_hours": {"n": len(durations), "p50": _quantile(durations, .5),
                                   "p90": _quantile(durations, .9), "max": _quantile(durations, 1)},
        "dispute_duration_hours_by_category": {
            k: {"n": len(v), "p50": _quantile(v, .5), "p90": _quantile(v, .9), "max": _quantile(v, 1),
                "reportable": len(v) >= MIN_N_REPORTABLE}
            for k, v in sorted(dur_by_cat.items())},
        "by_category": rates(by_cat), "by_rule_template": rates(by_tpl),
        "min_n_reportable": MIN_N_REPORTABLE,
        "rule_feature_definitions": {
            "method": "case-insensitive substring match over `title\\n description`; "
                      "template = the active features joined by '+', or 'plain' when none fire",
            "needles": {k: list(v) for k, v in RULE_FEATURE_NEEDLES.items()},
            "long_rules_chars": LONG_RULES_CHARS},
    }
    _atomic(STATS_PATH, stats)
    return stats


def _history_stats() -> dict[str, Any]:
    try:
        with open(STATS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def score_market(market: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    title = str(market.get("question") or market.get("title") or "")
    desc = str(market.get("description") or "")
    cat = category_of(f"{title} {desc}")
    feats = rule_features(title, desc)
    base = (((stats.get("by_category") or {}).get(cat) or {}).get("dispute_rate"))
    base = float(base) if isinstance(base, (int, float)) else 0.015
    # Transparent shadow heuristic.  Coefficients express direction only; the
    # 30-day AUROC gate, not these weights, decides whether this ever ships.
    logit = math.log(max(1e-5, min(.99999, base)) / max(1e-5, 1 - base))
    logit += 0.95 * feats["subjective_language"] - 0.55 * feats["explicit_source"]
    logit += 0.45 * feats["timezone_boundary"] + 0.55 * feats["fallback_clause"]
    logit += 0.5 * feats["revision_clause"] + 0.75 * feats["multi_source"]
    logit += 0.2 * feats["long_rules"]
    probability = 1 / (1 + math.exp(-max(-12, min(12, logit))))
    return {
        "market_id": str(market.get("id") or ""), "condition_id": market.get("conditionId"),
        "slug": market.get("slug"), "title": title, "category": cat,
        "score": round(100 * probability, 2), "category_base_rate": round(100 * base, 2),
        "features": feats, "shadow": True,
    }


def iter_active_market_pages(*, fetch: Any = http_json, max_pages: int = 3000):
    """Yield deduplicated market pages plus a terminal coverage marker.

    The catalogue is large enough that retaining full market metadata and scores
    in memory exceeded 1 GB.  Page streaming keeps the shadow crawler bounded.
    """
    seen: set[str] = set(); cursor = ""
    for _ in range(max_pages):
        # Events keyset carries its markets inline.  It is materially faster than
        # the direct market endpoint while retaining rule text; duplicate markets
        # across events are removed by id.
        params = {"closed": "false", "limit": 100}
        if cursor:
            params["after_cursor"] = cursor
        doc = fetch(f"{GAMMA}/events/keyset?{urllib.parse.urlencode(params)}")
        if not isinstance(doc, dict): break
        events = doc.get("events") or []
        page: list[dict[str, Any]] = []
        for event in events:
            for m in (event.get("markets") or []) if isinstance(event, dict) else []:
                mid = str(m.get("id") or "") if isinstance(m, dict) else ""
                if mid and mid not in seen and m.get("active") is not False and not m.get("closed"):
                    seen.add(mid); page.append(m)
        if page: yield page, False
        nxt = str(doc.get("next_cursor") or "")
        if not events or not nxt:
            yield [], True; return
        if nxt == cursor: return
        cursor = nxt


def run_shadow(*, fetch: Any = http_json, now: float | None = None) -> dict[str, Any]:
    now = now or time.time(); stats = _history_stats()
    known: set[str] = set()
    try:
        with open(PREDICTIONS_PATH, encoding="utf-8") as fh:
            known = {str(json.loads(line).get("market_id") or "") for line in fh if line.strip()}
    except (OSError, json.JSONDecodeError):
        pass
    stamp = iso(now); complete = False; count = 0; sample: list[dict[str, Any]] = []
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = SHADOW_MARKETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as latest, open(PREDICTIONS_PATH, "a", encoding="utf-8") as predictions:
        for page, terminal in iter_active_market_pages(fetch=fetch):
            if terminal:
                complete = True; break
            for market in page:
                row = score_market(market, stats); count += 1
                latest.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                if len(sample) < 100: sample.append(row)
                if row["market_id"] and row["market_id"] not in known:
                    predictions.write(json.dumps({**row, "predicted_at": stamp},
                                                 ensure_ascii=False, sort_keys=True) + "\n")
                    known.add(row["market_id"])
    os.replace(tmp, SHADOW_MARKETS_PATH)
    doc = {"schema": SCHEMA, "generated_at": stamp, "shadow": True, "public_ready": False,
           "gate": "30d AUROC must beat category-base-rate baseline",
           "coverage": {"active_markets_scored": count, "gamma_pagination_complete": complete},
           "market_scores_path": os.path.relpath(SHADOW_MARKETS_PATH, REPO),
           "markets_sample": sample}
    _atomic(SHADOW_PATH, doc)
    return doc


def auroc(pairs: list[tuple[float, int]]) -> float | None:
    pos = sum(y for _, y in pairs); neg = len(pairs) - pos
    if not pos or not neg:
        return None
    wins = 0.0
    for sp, yp in pairs:
        if not yp: continue
        for sn, yn in pairs:
            if yn: continue
            wins += 1.0 if sp > sn else 0.5 if sp == sn else 0.0
    return wins / (pos * neg)


def evaluate_shadow(*, fetch: Any = http_json, now: float | None = None) -> dict[str, Any]:
    now = now or time.time(); rows: list[dict[str, Any]] = []
    try:
        with open(PREDICTIONS_PATH, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError):
        pass
    labels = set()
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            labels = {str(json.loads(line).get("market_id") or "") for line in fh if line.strip()}
    except (OSError, json.JSONDecodeError):
        pass
    eligible, pending = [], 0
    for row in rows:
        try:
            age = now - datetime.fromisoformat(row["predicted_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if age < 30 * 86400:
            pending += 1; continue
        mid = str(row.get("market_id") or "")
        market = fetch(f"{GAMMA}/markets/{mid}") if mid else None
        if not isinstance(market, dict) or not market.get("closed"):
            pending += 1; continue
        eligible.append((float(row["score"]), float(row["category_base_rate"]), int(mid in labels)))
    model = auroc([(a, y) for a, _, y in eligible]); baseline = auroc([(b, y) for _, b, y in eligible])
    passed = bool(model is not None and baseline is not None and model > baseline)
    report = {"schema": SCHEMA, "evaluated_at": iso(now), "min_horizon_days": 30,
              "n": len(eligible), "pending": pending, "model_auroc": model,
              "category_baseline_auroc": baseline, "public_ready": passed,
              "gate": "PASS" if passed else "CLOSED"}
    try:
        with open(SHADOW_PATH, encoding="utf-8") as fh: shadow = json.load(fh)
    except (OSError, json.JSONDecodeError): shadow = {"schema": SCHEMA, "shadow": True}
    shadow["evaluation"] = report; shadow["public_ready"] = passed
    _atomic(SHADOW_PATH, shadow)
    return report


def is_election_market(meta: dict[str, Any] | None) -> bool:
    if not isinstance(meta, dict):
        return False
    return category_of(f"{meta.get('question','')} {meta.get('description','')}") == "politics" and any(
        x in f" {meta.get('question','')} {meta.get('description','')}".lower()
        for x in (" election", " primary", " nominee", " vote", " ballot", "referendum")
    )


def _channel_conf() -> dict[str, Any]:
    try:
        with open(CHANNEL_CONF_PATH, encoding="utf-8") as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def publish_channel_anchor(text: str) -> str | None:
    """Mirror a qualifying alert to a public channel and return the message link,
    so the claim carries a third-party timestamp.

    An emitted_at field in your own JSON is self-attested and worth nothing as
    evidence. A message timestamp on somebody else's server cannot be altered
    after the fact, so a ledger row that carries the channel link is evidence in a
    way the row alone is not.

    With no channel configured this returns None and accounting proceeds
    unchanged: anchoring strengthens evidence, it is not a precondition for
    recording. Config shape:
    {"bot_token_path": "<path>", "channel": "<channel>"}."""
    conf = _channel_conf()
    token_path, channel = conf.get("bot_token_path"), conf.get("channel")
    if not token_path or not channel:
        return None
    try:
        with open(os.path.expanduser(str(token_path)), encoding="utf-8") as fh:
            token = fh.read().strip()
        resp = http_json(f"https://api.telegram.org/bot{token}/sendMessage",
                         body={"chat_id": channel, "text": text, "disable_web_page_preview": True})
        mid = ((resp or {}).get("result") or {}).get("message_id")
        if mid and str(channel).startswith("@"):
            return f"https://t.me/{str(channel)[1:]}/{mid}"
    except (OSError, urllib.error.URLError, TypeError, ValueError, KeyError):
        return None
    return None


def record_election_alert(*, market: dict[str, Any], alert: dict[str, Any],
                          uma: dict[str, Any] | None, emitted_at: str | None = None) -> None:
    """Append a zero-PnL, zero-wallet capability record after successful delivery."""
    if not is_election_market(market):
        return
    uma = uma if isinstance(uma, dict) else {}
    rec = {"schema": SCHEMA, "emitted_at": emitted_at or iso(time.time()),
           "market_id": str(market.get("id") or ""), "condition_id": market.get("conditionId"),
           "slug": market.get("slug"), "title": market.get("question"),
           "alert_rule": alert.get("rule"), "phase": uma.get("state_label"),
           "request_key": uma.get("request_key"), "request_at": iso(uma.get("request_timestamp")),
           "proposal_expires_at": iso(uma.get("expiration_time")),
           "contains_pnl": False, "contains_holdings": False}
    # The channel text says exactly what the ledger row says — title, phase, time,
    # no position and no PnL. The wording stays neutral on purpose: a public post
    # gets screenshotted, and a loaded verb travels further than the caveat.
    rec["tg_link"] = publish_channel_anchor(
        f"Election market · UMA {uma.get('state_label') or 'activity'} · "
        f"{market.get('question') or market.get('slug') or rec['market_id']} · "
        f"alert logged {rec['emitted_at']}")
    _append(PUBLIC_LEDGER_SOURCE, rec)


# Fields in the public artefact. Market titles and identifiers are published in
# full: stripping them defensively would destroy the reader's ability to check
# the claim, which is the only reason the artefact exists. Two boundaries are
# absolute and never relax: no PnL and no position. The oracle request key,
# condition id and dispute transaction stay in, so a third party can verify every
# row on-chain independently. The internal ledger and the public artefact are
# built from the same source.
_PUBLIC_EVENT_FIELDS = ("schema", "emitted_at", "alert_rule", "phase", "topic",
                        "market_id", "slug", "title",
                        "request_key", "request_at", "proposal_expires_at", "condition_id",
                        "contains_pnl", "contains_holdings",
                        "dispute_at", "dispute_tx", "lead_seconds", "hit_before_dispute", "tg_link")
_PUBLIC_TOPIC = "politics — election market"


def _watching_counts() -> dict[str, Any] | None:
    """The "currently watching" evidence block: how many live election markets the
    shadow scan covers. Fails soft — with no shadow file present the result is
    None, the ledger simply carries no watching block, and the consumer shows an
    empty state rather than a fabricated number."""
    try:
        with open(SHADOW_PATH, encoding="utf-8") as fh:
            head = json.load(fh)
        scanned = int(((head.get("coverage") or {}).get("active_markets_scored")) or 0)
        election = 0
        with open(SHADOW_MARKETS_PATH, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if is_election_market({"question": row.get("title") or ""}):
                    election += 1
        return {"election_markets": election, "markets_scanned": scanned,
                "as_of": head.get("generated_at")}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def build_public_ledger() -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    try:
        with open(PUBLIC_LEDGER_SOURCE, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
    except (OSError, json.JSONDecodeError):
        pass
    dispute_by_market: dict[str, dict[str, Any]] = {}
    election_disputes: list[dict[str, Any]] = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                m = json.loads(line); latest = (m.get("requests") or [{}])[-1]
                dispute_by_market[str(m.get("market_id") or "")] = latest
                # The miss side of the comparison: every election-market dispute
                # inside the observation window, including the ones nothing warned
                # about. A ledger that shows only hits is marketing, not
                # measurement; recording a miss and publishing a zero are the same
                # discipline.
                disputed_at = ((latest.get("timeline") or {}).get("disputed_at"))
                if (disputed_at and disputed_at >= LEDGER_ACTIVATED_AT
                        and is_election_market({"question": m.get("title") or "",
                                                "description": latest.get("description") or ""})):
                    election_disputes.append({
                        "market_id": str(m.get("market_id") or ""), "title": m.get("title"),
                        "request_id": latest.get("request_id"), "topic": _PUBLIC_TOPIC,
                        "disputed_at": disputed_at, "dispute_tx": (latest.get("tx") or {}).get("dispute")})
    except (OSError, json.JSONDecodeError):
        pass
    out = []
    for event in events:
        req = dispute_by_market.get(str(event.get("market_id") or ""), {})
        dispute_at = ((req.get("timeline") or {}).get("disputed_at"))
        lead = None
        try:
            lead = round((datetime.fromisoformat(dispute_at.replace("Z", "+00:00")) -
                          datetime.fromisoformat(event["emitted_at"].replace("Z", "+00:00"))).total_seconds())
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        full = {**event, "topic": _PUBLIC_TOPIC,
                "dispute_at": dispute_at, "dispute_tx": (req.get("tx") or {}).get("dispute"),
                "lead_seconds": lead, "hit_before_dispute": lead is not None and lead >= 0}
        out.append({k: full[k] for k in _PUBLIC_EVENT_FIELDS if k in full})
    alerted_ids = {str(e.get("market_id") or "") for e in events}
    observed = sorted(election_disputes, key=lambda d: d["disputed_at"], reverse=True)
    for d in observed:
        d["alerted"] = d["market_id"] in alerted_ids
    doc = {"schema": SCHEMA, "generated_at": iso(time.time()), "ledger_scope": "system alerts only",
           "activated_at": LEDGER_ACTIVATED_AT,
           "contains_pnl": False, "contains_holdings": False, "n": len(out), "events": out,
           "disputes_observed": observed,
           "disputes_covered": {"covered": sum(1 for d in observed if d["alerted"]),
                                "observed": len(observed)},
           "identifier_note": ("rows carry venue market ids and titles for readability, plus "
                               "UMA request keys and CTF condition hashes for independent "
                               "on-chain verification; no PnL, no holdings")}
    watching = _watching_counts()
    if watching:
        doc["watching"] = watching
    _atomic(PUBLIC_LEDGER_PATH, doc)
    return doc


def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    anc = "q: title: Will A win the election?, description: Resolves according to official results at 11:59 PM ET. market_id: 42 res_data: p1: 0"
    p = ancillary_parts(anc); f = rule_features(p["title"], p["description"])
    checks["ancillary"] = p["market_id"] == "42" and "election" in p["title"]
    checks["category"] = category_of(anc) == "politics"
    checks["features"] = f["explicit_source"] == 1 and f["timezone_boundary"] == 1
    checks["score_shadow"] = score_market({"id": 42, "question": p["title"], "description": p["description"]}, {})["shadow"]
    checks["auc"] = auroc([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]) == 1.0
    checks["election"] = is_election_market({"question": p["title"], "description": p["description"]})
    checks["non_election"] = not is_election_market({"question": "Will BTC exceed $1m?"})
    # Channel anchoring must fail soft: unconfigured or misconfigured returns None
    # rather than raising. Anchoring strengthens evidence; it is not a
    # precondition for recording, and it must not be able to take the alert path
    # down with it.
    checks["channel_fail_soft"] = publish_channel_anchor("selftest — not delivered") is None \
        if not _channel_conf().get("channel") else True
    ledger = build_public_ledger()
    checks["ledger_miss_side"] = (isinstance(ledger.get("disputes_observed"), list)
                                  and isinstance(ledger.get("disputes_covered"), dict)
                                  and ledger.get("activated_at") == LEDGER_ACTIVATED_AT)
    # The boundary, asserted rather than remembered: the public artefact never
    # carries money — no PnL, no holdings, no amounts, no positions. Titles and
    # identifiers are published in full.
    blob = json.dumps(ledger, ensure_ascii=False)
    checks["ledger_no_money_fields"] = (
        ledger.get("contains_pnl") is False and ledger.get("contains_holdings") is False
        and '"pnl"' not in blob and '"holdings"' not in blob and '"position"' not in blob
        and '"stake"' not in blob and '"size_usd"' not in blob)
    checks["ledger_rows_verifiable"] = all(
        ("request_key" in e or "condition_id" in e) for e in ledger.get("events") or [True] if isinstance(e, dict))
    ok = all(checks.values())
    return {"schema": SCHEMA + "-selftest", "PASS": ok, "checks": checks,
            "failed": [k for k, v in checks.items() if not v]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("backfill", "shadow", "evaluate", "public-ledger", "run", "selftest"))
    args = ap.parse_args(argv)
    if args.action == "selftest": result = selftest()
    elif args.action == "backfill": result = build_history()
    elif args.action == "shadow": result = run_shadow()
    elif args.action == "evaluate": result = evaluate_shadow()
    elif args.action == "public-ledger": result = build_public_ledger()
    else:
        result = {"history": build_history(), "shadow": run_shadow(),
                  "evaluation": evaluate_shadow(), "public_ledger": build_public_ledger()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("PASS", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
