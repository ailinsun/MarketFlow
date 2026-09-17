#!/usr/bin/env python3
"""Independent Polymarket resolution-cleanliness verifier (daemon-owned).

The autotrade daemon's entry fuse refuses any BUY whose market is not
`resolution_confirmed_clean` (polymarket_market_gate.RESOLUTION_NOT_CONFIRMED_CLEAN
+ polymarket_autotrade_daemon.resolution_attestation_for_entry). The belief organ
(and any LLM-driven proposer) deliberately NEVER self-attests clean resolution —
it ships intents with `resolution_confirmed_clean=False`. So the fuse fails closed
and a fully armed daemon would simply idle: safe, but it can never trade.

This module is the missing TRUSTWORTHY INPUT to that fuse — it is NOT a bypass.
The daemon, independently of the proposer, pulls the market's authoritative
on-chain resolution metadata from Polymarket Gamma and decides whether the
resolution mechanics are objectively clean enough to back with real money. A
clean verdict becomes an INDEPENDENT attestation (resolution_attestation_source),
which the existing fuse already recognises as a non-self-attested clean signal.
Everything the fuse already rejects, it still rejects; this only lets through
markets that pass strict, objective, fail-closed checks.

Cleanliness model (ALL must hold for clean=True; any miss => not clean):
  1. binary two-outcome market with two CLOB token ids (Yes/No OR a head-to-head
     pair like ["K27","Walczaki"] — the belief organ buys outcome[0] as the YES side);
  2. objective resolution domain (v1 whitelist = sports + esports, where the
     outcome is settled off an official scoreline / tournament result), and NOT a
     subjective question (tweet counts, "X out by <date>", geopolitical deals, …);
  3. a standard Polymarket UMA optimistic-oracle adapter as resolver (unknown /
     custom resolvers fail closed);
  4. no pending or disputed UMA proposal (umaResolutionStatuses empty == a fresh,
     uncontested market — "proposed"/"disputed" both fail closed for a new entry);
  5. a real UMA economic bond (umaBond > 0) behind the resolution;
  6. the market is still genuinely open (active, accepting orders, not closed/archived).

Hard boundaries (never crossed here):
  - read-only public Gamma metadata only; no SDK, no secrets, no orders, no
    arm-state, no kernel/state.mx;
  - this NEVER weakens the capital fuses (caps / kill / arm-state) or the band /
    longshot / edge gates; it only supplies an independent resolution attestation;
  - fail closed: any fetch error, missing field, or unrecognised shape => not clean.

v1 scope is deliberately narrow (sports + esports objective settlement) because
that is where Polymarket resolution is genuinely uncontested and where the belief
organ's L3 forward batch lives (FIFA World Cup match markets + CS2/Dota/Valorant
matches). Other domains (price thresholds, politics, geopolitics) fail closed by
design; extend the whitelist only with evidence, never by loosening the checks.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

SCHEMA_VERSION = "polymarket-resolution-verifier-v0.1"
ATTESTATION_SOURCE = "uma_objective_verifier_v1"

# Standard Polymarket UMA optimistic-oracle adapters. Provenance: enumerated
# 2026-06-23 as the `resolvedBy` of every top-volume active Polymarket market
# sampled (these three cover >99% of them, including both adapters used by the
# live FIFA World Cup and CS2 markets the belief organ selects). A market whose
# resolver is NOT in this set fails closed (not clean) — extend only after
# confirming a new adapter is a canonical Polymarket UMA adapter. Lowercased for
# case-insensitive comparison.
CANONICAL_UMA_RESOLVERS = {
    "0x65070be91477460d8a7aeeb94ef92fe056c2f2a7",  # UMA CTF adapter (binary)
    "0x69c47de9d4d3dad79590d61b9e05918e03775f24",  # NegRisk UMA CTF adapter
    "0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d",  # NegRisk CTF adapter
}

# Objective-settlement slug prefixes (Polymarket assigns these per family; very
# reliable category signals). Sports + esports only in v1. Observed 2026-06-23.
OBJECTIVE_SLUG_PREFIXES = {
    # football / soccer
    "fifwc", "epl", "ucl", "uefa", "laliga", "seriea", "bundesliga", "mls", "col1",
    # other ball sports
    "nba", "nfl", "nhl", "mlb", "nascar", "f1",
    # tennis / cricket
    "wta", "atp", "itf", "crint",
    # esports
    "cs2", "csgo", "dota2", "dota", "lol", "val", "valorant", "ow", "ow2",
}

# Sport / esports keywords (objective domains). A question containing any of these
# is treated as an objective-settlement domain.
SPORTS_ESPORTS_KEYWORDS = (
    "world cup", "fifa", "champions league", "premier league", "la liga",
    "serie a", "bundesliga", "uefa", "euro 2", "copa", "concacaf",
    "counter-strike", "counter strike", "dota", "valorant", "league of legends",
    "esports", "e-sports",
    "nba", "nfl", "nhl", "mlb", "tennis", "cricket", "boxing", "ufc", "mma",
    "golf", "formula 1", "soccer", "basketball", "baseball", "hockey",
)

# Subjective / dispute-prone language. Hard-reject even if a sports keyword also
# appears — these resolve by interpretation or contested counting, not a scoreline.
SUBJECTIVE_REJECT_PATTERNS = (
    "tweet", "tweets", " post ", " posts ", "out as", "step down", "resign",
    "ceasefire", "peace deal", "peace agreement", "peace treaty", "nuclear",
    "enrichment", "signed into law", "released by", "seen in public", "regime",
    "dissolved", "sanction", "deal by", "clash", "invade", "annex", "indicted",
)

# Head-to-head and dated-match shapes (objective sports/esports fixtures).
HEAD_TO_HEAD_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)
DATED_MATCH_RE = re.compile(
    r"win on \d{4}-\d{2}-\d{2}|end in a draw|\bbo[357]\b|game \d+ winner|\bo/u\b",
    re.IGNORECASE,
)

DEFAULT_FETCH_TIMEOUT = 20.0
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _parse_json_field(value: Any) -> Any:
    """Gamma serialises list fields as JSON strings; parse defensively."""
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _norm_addr(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def classify_objective_resolution(question: Any, slug: Any) -> dict[str, Any]:
    """Classify whether a market's question resolves off an objective, official
    source (v1: sports / esports). Pure, deterministic, fail-closed."""
    q = _safe_str(question).lower()
    s = _safe_str(slug).lower()
    prefix = s.split("-", 1)[0] if s else ""
    matched: list[str] = []

    if prefix in OBJECTIVE_SLUG_PREFIXES:
        matched.append(f"slug_prefix:{prefix}")
    for kw in SPORTS_ESPORTS_KEYWORDS:
        if kw in q:
            matched.append(f"keyword:{kw}")
            break
    if HEAD_TO_HEAD_RE.search(q):
        matched.append("pattern:head_to_head")
    if DATED_MATCH_RE.search(q):
        matched.append("pattern:dated_match")

    subjective_hits = [p.strip() for p in SUBJECTIVE_REJECT_PATTERNS if p in q]
    objective = bool(matched)
    subjective = bool(subjective_hits)
    return {
        "objective": objective,
        "subjective": subjective,
        "clean_category": objective and not subjective,
        "slug_prefix": prefix,
        "matched": matched,
        "subjective_hits": subjective_hits,
    }


def verify_market_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Pure resolution-cleanliness verdict from Gamma market metadata.

    Returns {clean, attestation_source, reasons, checks, fingerprint}. clean=True
    iff every check passes. No network here — caller supplies the metadata.
    """
    if not isinstance(meta, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "clean": False,
            "attestation_source": None,
            "reasons": ["no_market_metadata"],
            "checks": {},
            "fingerprint": {},
        }

    outcomes = _parse_json_field(meta.get("outcomes")) or []
    token_ids = _parse_json_field(meta.get("clobTokenIds")) or []
    outcomes = [str(x).strip() for x in outcomes] if isinstance(outcomes, list) else []
    token_ids = [str(x).strip() for x in token_ids if str(x).strip()] if isinstance(token_ids, list) else []

    category = classify_objective_resolution(meta.get("question"), meta.get("slug"))

    resolver = _norm_addr(meta.get("resolvedBy"))
    uma_statuses = _parse_json_field(meta.get("umaResolutionStatuses"))
    uma_statuses = uma_statuses if isinstance(uma_statuses, list) else []
    uma_status_tokens = {str(x).strip().lower() for x in uma_statuses}
    bond = _to_float(meta.get("umaBond"))

    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes"}

    active = _truthy(meta.get("active"))
    accepting = _truthy(meta.get("acceptingOrders"))
    closed = _truthy(meta.get("closed"))
    archived = _truthy(meta.get("archived"))

    checks = {
        "binary_two_outcome": len(outcomes) == 2 and len(token_ids) == 2,
        "objective_category": bool(category["clean_category"]),
        "standard_uma_resolver": resolver in CANONICAL_UMA_RESOLVERS,
        "uma_no_pending_or_dispute": len(uma_status_tokens) == 0,
        "economic_bond": bond is not None and bond > 0,
        "live_open_market": active and accepting and not closed and not archived,
    }
    reasons: list[str] = []
    if not checks["binary_two_outcome"]:
        reasons.append("not_binary_two_outcome")
    if not checks["objective_category"]:
        if category["subjective"]:
            reasons.append("subjective_resolution:" + ",".join(category["subjective_hits"]))
        else:
            reasons.append("non_objective_resolution_domain")
    if not checks["standard_uma_resolver"]:
        reasons.append(f"non_standard_uma_resolver:{resolver or 'missing'}")
    if not checks["uma_no_pending_or_dispute"]:
        reasons.append("uma_resolution_pending_or_disputed:" + ",".join(sorted(uma_status_tokens)))
    if not checks["economic_bond"]:
        reasons.append("missing_or_zero_uma_bond")
    if not checks["live_open_market"]:
        reasons.append("market_not_open_for_clean_entry")

    clean = all(checks.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "clean": clean,
        "attestation_source": ATTESTATION_SOURCE if clean else None,
        "reasons": reasons,
        "checks": checks,
        "category": category,
        "fingerprint": {
            "question": _safe_str(meta.get("question"))[:160],
            "slug": _safe_str(meta.get("slug"))[:120],
            "outcomes": outcomes,
            "resolved_by": resolver,
            "uma_resolution_statuses": sorted(uma_status_tokens),
            "uma_bond": bond,
            "neg_risk": _truthy(meta.get("negRisk")),
        },
    }


def fetch_gamma_market(
    market_id: Optional[str],
    *,
    slug: Optional[str] = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> Optional[dict[str, Any]]:
    """Read-only fetch of a single Gamma market by numeric id (primary) or slug
    (best-effort fallback). Returns None on any failure (caller fails closed)."""

    def _get(url: str) -> Optional[dict[str, Any]]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except Exception:
            return None
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    mid = _safe_str(market_id)
    # Numeric Gamma id => /markets?id=; conditionId (0x…) is not accepted by ?id=.
    if mid and not mid.lower().startswith("0x"):
        got = _get(f"{GAMMA_MARKETS_URL}?id={urllib.parse.quote(mid)}")
        if got is not None:
            return got
    sl = _safe_str(slug)
    if sl:
        got = _get(f"{GAMMA_MARKETS_URL}?slug={urllib.parse.quote(sl)}")
        if got is not None:
            return got
    return None


def verify_candidate(
    candidate: dict[str, Any],
    *,
    fetcher: Callable[..., Optional[dict[str, Any]]] = fetch_gamma_market,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    cache: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Daemon entry point: independently fetch a candidate's market metadata and
    return the resolution-cleanliness verdict. Fail-closed on any error."""
    market_id = candidate.get("market_id")
    slug = candidate.get("market_slug") or candidate.get("slug")
    cache_key = _safe_str(market_id) or _safe_str(slug)
    if cache is not None and cache_key and cache_key in cache:
        return cache[cache_key]

    verdict: dict[str, Any]
    try:
        meta = fetcher(market_id, slug=slug, timeout=timeout)
    except Exception as exc:  # fail-closed on any fetch error
        verdict = {
            "schema_version": SCHEMA_VERSION,
            "clean": False,
            "attestation_source": None,
            "reasons": [f"fetch_error:{type(exc).__name__}"],
            "checks": {},
            "fingerprint": {"market_id": _safe_str(market_id), "slug": _safe_str(slug)},
        }
    else:
        if not isinstance(meta, dict):
            verdict = {
                "schema_version": SCHEMA_VERSION,
                "clean": False,
                "attestation_source": None,
                "reasons": ["market_not_found"],
                "checks": {},
                "fingerprint": {"market_id": _safe_str(market_id), "slug": _safe_str(slug)},
            }
        else:
            verdict = verify_market_meta(meta)
            verdict.setdefault("fingerprint", {})["market_id"] = _safe_str(market_id)
    verdict["verified_at"] = iso_now()
    if cache is not None and cache_key:
        cache[cache_key] = verdict
    return verdict


def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    # --- real metadata snapshots (pulled live from Gamma 2026-06-23) ---------
    fifwc_col = {
        "question": "Will Colombia win on 2026-06-23?",
        "slug": "fifwc-col-cdr-2026-06-23-col",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "resolvedBy": "0x69c47De9D4D3Dad79590d61b9e05918E03775f24",
        "umaResolutionStatuses": "[]",
        "umaBond": "500",
        "negRisk": True,
        "active": True,
        "acceptingOrders": True,
        "closed": False,
    }
    cs2_match = {
        "question": "Counter-Strike: K27 vs Walczaki (BO3) - DraculaN Group B",
        "slug": "cs2-k271-wal2-2026-06-23",
        "outcomes": '["K27", "Walczaki"]',
        "clobTokenIds": '["333", "444"]',
        "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7",
        "umaResolutionStatuses": "[]",
        "umaBond": "500",
        "negRisk": False,
        "active": True,
        "acceptingOrders": True,
        "closed": False,
    }

    r = verify_market_meta(fifwc_col)
    checks["real_fifwc_win_clean"] = r["clean"] is True and r["attestation_source"] == ATTESTATION_SOURCE
    r = verify_market_meta(cs2_match)
    checks["real_cs2_headtohead_clean"] = r["clean"] is True
    checks["headtohead_not_literal_yes_no"] = verify_market_meta(cs2_match)["checks"]["binary_two_outcome"] is True

    # --- objective classification --------------------------------------------
    c = classify_objective_resolution("Will Colombia win on 2026-06-23?", "fifwc-col-cdr-2026-06-23-col")
    checks["classify_fifwc_objective"] = c["clean_category"] is True
    c = classify_objective_resolution("Bosnia and Herzegovina vs. Qatar end in a draw?", "fifwc-bih-qat-2026-06-24-draw")
    checks["classify_draw_objective"] = c["clean_category"] is True
    c = classify_objective_resolution("Dota 2: OG vs Grind Back - Game 4 Winner", "dota2-og-grind-2026-06-23")
    checks["classify_dota_objective"] = c["clean_category"] is True
    c = classify_objective_resolution("Lexus Eastbourne Open: Sara Bejlek vs Laura Siegemund", "wta-bejlek-siegemund")
    checks["classify_tennis_objective"] = c["clean_category"] is True

    # --- subjective / non-objective => NOT clean -----------------------------
    pol = {
        "question": "Will Belete Molla be the next Prime Minister of Ethiopia?",
        "slug": "will-belete-molla-be-the-next-prime-minister-of-ethiopia",
        "outcomes": '["Yes", "No"]', "clobTokenIds": '["1","2"]',
        "resolvedBy": "0x69c47De9D4D3Dad79590d61b9e05918E03775f24",
        "umaResolutionStatuses": "[]", "umaBond": "500",
        "active": True, "acceptingOrders": True, "closed": False,
    }
    checks["political_not_clean"] = verify_market_meta(pol)["clean"] is False
    tweets = {
        "question": "Will Elon Musk post 220-239 tweets from June 16 to June 23?",
        "slug": "elon-musk-tweets-june-16-23",
        "outcomes": '["Yes", "No"]', "clobTokenIds": '["1","2"]',
        "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7",
        "umaResolutionStatuses": "[]", "umaBond": "500",
        "active": True, "acceptingOrders": True, "closed": False,
    }
    checks["tweet_count_not_clean"] = verify_market_meta(tweets)["clean"] is False
    geo = {
        "question": "US-Iran Final Nuclear Deal by August 31, 2026?",
        "slug": "us-iran-final-nuclear-deal-2026",
        "outcomes": '["Yes", "No"]', "clobTokenIds": '["1","2"]',
        "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7",
        "umaResolutionStatuses": "[]", "umaBond": "500",
        "active": True, "acceptingOrders": True, "closed": False,
    }
    checks["geopolitical_not_clean"] = verify_market_meta(geo)["clean"] is False

    # --- UMA integrity failures (objective category but dirty mechanics) ------
    disputed = dict(fifwc_col, umaResolutionStatuses='["proposed", "disputed"]')
    checks["disputed_not_clean"] = verify_market_meta(disputed)["clean"] is False
    proposed = dict(fifwc_col, umaResolutionStatuses='["proposed"]')
    checks["pending_proposal_not_clean"] = verify_market_meta(proposed)["clean"] is False
    bad_resolver = dict(fifwc_col, resolvedBy="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    checks["unknown_resolver_not_clean"] = verify_market_meta(bad_resolver)["clean"] is False
    no_bond = dict(fifwc_col, umaBond=None)
    checks["missing_bond_not_clean"] = verify_market_meta(no_bond)["clean"] is False
    closed_mkt = dict(fifwc_col, closed=True)
    checks["closed_market_not_clean"] = verify_market_meta(closed_mkt)["clean"] is False
    not_accepting = dict(fifwc_col, acceptingOrders=False)
    checks["not_accepting_not_clean"] = verify_market_meta(not_accepting)["clean"] is False
    multi = dict(fifwc_col, outcomes='["A","B","C"]', clobTokenIds='["1","2","3"]')
    checks["non_binary_not_clean"] = verify_market_meta(multi)["clean"] is False

    # --- candidate path (offline stub fetcher) -------------------------------
    def _stub_fetch(market_id, *, slug=None, timeout=0):
        return {"1897254": fifwc_col, "9999": pol}.get(str(market_id))

    v = verify_candidate({"market_id": "1897254", "market_slug": "fifwc-col-cdr-2026-06-23-col"}, fetcher=_stub_fetch)
    checks["candidate_clean_path"] = v["clean"] is True and v["attestation_source"] == ATTESTATION_SOURCE
    v = verify_candidate({"market_id": "9999"}, fetcher=_stub_fetch)
    checks["candidate_political_not_clean"] = v["clean"] is False
    v = verify_candidate({"market_id": "0xabc"}, fetcher=_stub_fetch)
    checks["candidate_not_found_fail_closed"] = v["clean"] is False and "market_not_found" in v["reasons"]

    def _boom_fetch(market_id, *, slug=None, timeout=0):
        raise RuntimeError("network down")

    v = verify_candidate({"market_id": "1897254"}, fetcher=_boom_fetch)
    checks["candidate_fetch_error_fail_closed"] = v["clean"] is False and any(
        r.startswith("fetch_error:") for r in v["reasons"]
    )

    # cache: second call must not re-fetch.
    calls = {"n": 0}

    def _counting_fetch(market_id, *, slug=None, timeout=0):
        calls["n"] += 1
        return fifwc_col

    cache: dict[str, dict[str, Any]] = {}
    verify_candidate({"market_id": "1897254"}, fetcher=_counting_fetch, cache=cache)
    verify_candidate({"market_id": "1897254"}, fetcher=_counting_fetch, cache=cache)
    checks["cache_prevents_refetch"] = calls["n"] == 1

    ok = all(checks.values())
    return {
        "schema_version": "polymarket-resolution-verifier-selftest-v0.1",
        "generated_at": iso_now(),
        "PASS": ok,
        "checks": checks,
        "failed": [k for k, v in checks.items() if not v],
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Independent Polymarket resolution-cleanliness verifier")
    ap.add_argument("--selftest", action="store_true", help="Run offline selftest.")
    ap.add_argument("--market-id", help="Live: fetch this Gamma market id and verify.")
    ap.add_argument("--slug", help="Live: fetch this Gamma market slug and verify.")
    args = ap.parse_args(argv)
    if args.selftest:
        rep = selftest()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["PASS"] else 1
    if args.market_id or args.slug:
        verdict = verify_candidate({"market_id": args.market_id, "market_slug": args.slug})
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
