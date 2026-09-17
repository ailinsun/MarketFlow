"""One-shot cross-venue spread scanner: Polymarket against Kalshi.

**The question.** If the same real-world event trades on two venues, is the price
difference large enough to lock in — buy YES on one side and NO on the other — after
both venues' taker fees? This runs a single scan and answers it with numbers rather
than adding another collector that accumulates data toward a future answer.

**The answer it has produced so far, and why it is the interesting one.** Matched
pairs differ by at most about one cent, while the combined fee wall is two and a
half to three cents thick. The spread is real and it is smaller than the cost of
capturing it. That is the same conclusion the fee and ledger work reaches from a
different direction: in event-contract markets the binding constraint is friction,
not the difficulty of forming a view.

**Method**

1. Kalshi `/events?status=open&with_nested_markets=true`, paginated, public and
   unauthenticated. Multi-outcome parlays are excluded; binary markets contribute
   their yes bid and ask.
2. Polymarket Gamma `/markets?active=true&closed=false`, paginated by 24-hour
   volume, contributing best bid and ask. When the outcomes are not Yes/No — a
   pair of team names, say — bid and ask are quoted against the first outcome, and
   that entity name is folded into the text used for matching.
3. Pairs are matched on informative-token Jaccard similarity, agreement of any
   numbers, and closeness of expiry, resolved greedily one-to-one. **The match
   score is a blocking heuristic, not a judgment that two contracts settle the
   same way.** Differences in resolution terms are the largest hidden risk in this
   kind of trade, so every reported pair carries both sides' title and rules text
   for a human to check. The scanner does not and cannot check them.
4. Fees. Kalshi taker fees are modelled at 0.07 * p * (1 - p); some series charge
   0.035 and makers pay nothing, so this errs high. Polymarket taker fees use
   `feeSchedule.rate` where the venue reports it, falling back to 0.05 when fees
   are enabled but no rate is given. Locked margins are then

       edge_a = pm_bid - kalshi_ask - fees      (buy YES on Kalshi, NO on Polymarket)
       edge_b = kalshi_bid - pm_ask  - fees      (the reverse)
       best   = max(edge_a, edge_b)

**Boundaries.** Read-only public endpoints. It does not authenticate, place orders,
touch a wallet, or read execution state. It writes only under its own runtime
directory.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(os.environ.get("MARKETFLOW_RUNTIME") or os.path.join(REPO, "runtime"),
                       "instruments", "cross_venue_spread")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "marketflow-cross-venue-spread/0.1 (read-only research)"

KALSHI_FEE_RATE = 0.07          # general taker tier; some series charge 0.035 and
#                                 makers pay nothing, so this errs high on purpose.
# The Polymarket fallback rate was corrected from 0.03 to 0.05 after the venue raised
# its sports taker fee, confirmed twice by reading the live schedule. Erring low here
# would make the fee wall look thinner than it is, which is the false-positive
# direction: this scanner's standing result is that spreads are at most about a cent
# while the wall is two and a half to three cents, and understating fees attacks
# exactly that conclusion.
PM_SPORTS_FEE_RATE = 0.05       # used when feesEnabled is set but no rate is given

STOPWORDS = frozenset((
    "will", "the", "a", "an", "of", "in", "on", "to", "be", "is", "are", "at", "by",
    "for", "vs", "and", "or", "before", "after", "during", "this", "that", "it",
    "yes", "no", "who", "what", "which", "when", "next", "than", "more", "less",
    "win", "wins", "winner", "market", "resolve", "resolves", "if", "as", "with",
    "there", "his", "her", "its", "their", "any", "all",
))


# --------------------------------------------------------------------------- #
# http (minimal, retrying, read-only)
# --------------------------------------------------------------------------- #
def http_json(url: str, *, timeout: float = 30.0, retries: int = 3, backoff: float = 1.5) -> Any:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                              "Connection": "close"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return None
            last_err = exc
        except Exception as exc:  # noqa: BLE001 - network errors are diverse; retried
            last_err = exc
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"http_json failed after {retries} tries: {url}: {last_err}")


def _f(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# venue pulls -> normalized rows
# --------------------------------------------------------------------------- #
def fetch_kalshi(*, min_volume: float, max_pages: int, verbose: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    now = datetime.now(timezone.utc)
    for page in range(max_pages):
        q: dict[str, Any] = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            q["cursor"] = cursor
        data = http_json(f"{KALSHI_API}/events?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            break
        events = data.get("events", []) or []
        for ev in events:
            if str(ev.get("event_ticker", "")).startswith("KXMVE"):
                continue  # parlay/multivariate collections are not single-event contracts
            category = str(ev.get("category") or "")
            ev_title = str(ev.get("title") or "")
            for m in ev.get("markets") or []:
                if m.get("market_type") != "binary" or m.get("mve_collection_ticker"):
                    continue
                bid = _f(m.get("yes_bid_dollars"))
                ask = _f(m.get("yes_ask_dollars"))
                vol = _f(m.get("volume_fp")) or 0.0
                if bid is None or ask is None or not (0.0 < bid < 1.0) or not (0.0 < ask < 1.0):
                    continue
                if ask <= bid or vol < min_volume:
                    continue
                close_dt = parse_iso(m.get("close_time"))
                if close_dt is None or close_dt <= now:
                    continue
                title = str(m.get("title") or "").strip()
                if not title:
                    continue
                rows.append({
                    "venue": "kalshi",
                    "id": str(m.get("ticker")),
                    "title": title,
                    "entity": str(m.get("yes_sub_title") or "").strip(),
                    "event_title": ev_title,
                    "rules": str(m.get("rules_primary") or "").strip(),
                    "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
                    "volume": vol,
                    "close_time": close_dt.isoformat(),
                    "category": category,
                    "fee_rate": KALSHI_FEE_RATE,
                    "url": f"https://kalshi.com/markets/{str(ev.get('series_ticker') or '').lower()}",
                })
        cursor = data.get("cursor", "") or ""
        if verbose:
            print(f"  kalshi page {page + 1}: events={len(events)} kept_total={len(rows)}", flush=True)
        if not cursor:
            break
    return rows


def fetch_polymarket(*, min_volume: float, max_pages: int, verbose: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        q = urllib.parse.urlencode({
            "active": "true", "closed": "false", "limit": 500, "offset": page * 500,
            "order": "volume24hr", "ascending": "false",
        })
        data = http_json(f"{GAMMA_API}/markets?{q}")
        if not isinstance(data, list) or not data:
            break
        for m in data:
            bid = _f(m.get("bestBid"))
            ask = _f(m.get("bestAsk"))
            vol = _f(m.get("volumeNum")) or 0.0
            if bid is None or ask is None or not (0.0 < bid < 1.0) or not (0.0 < ask < 1.0):
                continue
            if ask <= bid or vol < min_volume:
                continue
            # endDate on Gamma is often the event start (already past on live in-play
            # markets) — active=true is the in-market filter, so keep past endDates.
            close_dt = parse_iso(m.get("endDate"))
            question = str(m.get("question") or "").strip()
            if not question:
                continue
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
            except (TypeError, ValueError):
                outcomes = []
            # bestBid/bestAsk quote outcome[0]; for non-Yes/No pairs that entity IS the YES side.
            entity = ""
            if outcomes and str(outcomes[0]).strip().lower() not in ("yes", "no"):
                entity = str(outcomes[0]).strip()
            fee_sched = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
            fee_rate = _f(fee_sched.get("rate"))
            if fee_rate is None:
                fee_rate = PM_SPORTS_FEE_RATE if m.get("feesEnabled") else 0.0
            rows.append({
                "venue": "polymarket",
                "id": str(m.get("slug") or m.get("id")),
                "title": question,
                "entity": entity,
                "event_title": "",
                "rules": str(m.get("description") or "").strip(),
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2.0,
                "volume": vol,
                "close_time": close_dt.isoformat() if close_dt else "",
                "category": str(m.get("category") or ""),
                "fee_rate": fee_rate,
                "url": f"https://polymarket.com/market/{m.get('slug')}",
            })
        if verbose:
            print(f"  polymarket page {page + 1}: kept_total={len(rows)}", flush=True)
        if len(data) < 500:
            break
    return rows


# --------------------------------------------------------------------------- #
# matching (informative-token jaccard + number agreement + close-time proximity)
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def _fold(text: str) -> str:
    """Strip diacritics so Mbappé/Muñoz tokenize identically across venues."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def tokens(row: dict[str, Any]) -> frozenset[str]:
    text = _fold(f"{row.get('title', '')} {row.get('entity', '')} {row.get('event_title', '')}")
    # single-char digits survive: "set 1" / "9+" carry strike semantics the tier gate needs
    return frozenset(t for t in _TOKEN_RE.findall(text)
                     if t not in STOPWORDS and (len(t) > 1 or t.isdigit()))


def numbers(toks: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in toks if any(c.isdigit() for c in t))


def number_agreement(a: frozenset[str], b: frozenset[str]) -> float:
    na, nb = numbers(a), numbers(b)
    if not na and not nb:
        return 0.5  # neutral: neither side pins a number/date
    union = na | nb
    return len(na & nb) / len(union) if union else 0.5


def close_time_proximity(a_iso: str, b_iso: str) -> float:
    da, db = parse_iso(a_iso), parse_iso(b_iso)
    if da is None or db is None:
        return 0.0
    delta_days = abs((da - db).total_seconds()) / 86_400.0
    if delta_days <= 3.0:
        return 1.0
    if delta_days <= 30.0:
        return 0.5
    return 0.0


def pair_score(k_row: dict[str, Any], p_row: dict[str, Any],
               k_toks: frozenset[str], p_toks: frozenset[str]) -> float:
    union = k_toks | p_toks
    jac = len(k_toks & p_toks) / len(union) if union else 0.0
    return 0.65 * jac + 0.20 * number_agreement(k_toks, p_toks) \
        + 0.15 * close_time_proximity(k_row["close_time"], p_row["close_time"])


def match_rows(kalshi: list[dict[str, Any]], polymarket: list[dict[str, Any]], *,
               min_score: float, min_shared_tokens: int = 2) -> list[dict[str, Any]]:
    """Global greedy 1:1 matching over blocked candidate pairs."""
    k_toks = [tokens(r) for r in kalshi]
    p_toks = [tokens(r) for r in polymarket]
    inverted: dict[str, list[int]] = {}
    for j, toks in enumerate(p_toks):
        for t in toks:
            inverted.setdefault(t, []).append(j)
    # background = tokens shared by many PM markets (floor keeps small universes usable)
    background = {t for t, js in inverted.items() if len(js) > max(3.0, 0.08 * len(polymarket))}

    scored: list[tuple[float, int, int]] = []
    for i, ktoks in enumerate(k_toks):
        counts: dict[int, int] = {}
        informative = ktoks - background
        for t in informative:
            for j in inverted.get(t, ()):
                counts[j] = counts.get(j, 0) + 1
        need = min(min_shared_tokens, len(informative)) or min_shared_tokens
        for j, shared in counts.items():
            if shared < need:
                continue
            s = pair_score(kalshi[i], polymarket[j], ktoks, p_toks[j])
            if s >= min_score:
                scored.append((s, i, j))

    scored.sort(reverse=True)
    used_k: set[int] = set()
    used_p: set[int] = set()
    matches: list[dict[str, Any]] = []
    for s, i, j in scored:
        if i in used_k or j in used_p:
            continue
        used_k.add(i)
        used_p.add(j)
        matches.append({"score": round(s, 4), "kalshi": kalshi[i], "polymarket": polymarket[j]})
    return matches


# --------------------------------------------------------------------------- #
# YES-entity alignment (which side of the event does each venue's quote price?)
# --------------------------------------------------------------------------- #
_VS_RE = re.compile(r"^(.*?)\s+vs\.?\s+(.*?)(?:[:?]|$)", re.IGNORECASE)
_WILL_RE = re.compile(
    r"^will\s+(.+?)\s+(?:be|win|have|score|reach|hit|make|get|play|announce|hold|say|"
    r"visit|advance|dissent|run|remain|stay|become|lose|beat|defeat)\b", re.IGNORECASE)


def _toks(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(_fold(text)) if t not in STOPWORDS and len(t) > 1)


def entity_tokens(row: dict[str, Any]) -> frozenset[str]:
    """Tokens naming the YES side: explicit entity field, else the 'Will <X> ...' subject."""
    ent = str(row.get("entity") or "").strip()
    if ent and ent.lower() not in ("yes", "no"):
        return _toks(ent)
    m = _WILL_RE.match(str(row.get("title") or "").strip())
    return _toks(m.group(1)) if m else frozenset()


def vs_sides(title: str) -> Optional[tuple[frozenset[str], frozenset[str]]]:
    m = _VS_RE.match(title.strip())
    if not m:
        return None
    left = m.group(1)
    if ":" in left:  # "Swiss Open: Kilian Feldbausch vs ..." -> drop the tournament prefix
        left = left.rsplit(":", 1)[1]
    a, b = _toks(left), _toks(m.group(2))
    return (a, b) if a and b else None


def _side_of(ent: frozenset[str], sides: tuple[frozenset[str], frozenset[str]]) -> Optional[int]:
    hit_a, hit_b = bool(ent & sides[0]), bool(ent & sides[1])
    if hit_a == hit_b:
        return None
    return 0 if hit_a else 1


def align_pair(k_row: dict[str, Any], p_row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (alignment, pm_row'), flipping PM quotes when its YES names the other side.

    alignment ∈ entity_match | flipped | unverified | conflict. Only match/flipped
    pairs can carry an edge verdict; conflict means the two YES sides name different
    entities (the dominant false-positive in raw text matching).
    """
    ke, pe = entity_tokens(k_row), entity_tokens(p_row)
    if ke and pe and ke & pe:
        return "entity_match", p_row
    for sides in (vs_sides(p_row["title"]), vs_sides(k_row["title"])):
        if not sides or not ke or not pe:
            continue
        k_side, p_side = _side_of(ke, sides), _side_of(pe, sides)
        if k_side is None or p_side is None:
            continue
        if k_side == p_side:
            return "entity_match", p_row
        flipped = dict(p_row, bid=1.0 - p_row["ask"], ask=1.0 - p_row["bid"],
                       mid=1.0 - p_row["mid"], entity=f"NOT[{p_row.get('entity') or p_row['title']}]")
        return "flipped", flipped
    if ke and pe:
        return "conflict", p_row
    return "unverified", p_row


def non_year_numbers(toks: frozenset[str]) -> frozenset[str]:
    out = set()
    for t in numbers(toks):
        if len(t) == 4 and t.isdigit() and 1900 <= int(t) <= 2099:
            continue  # bare years are context, not strike thresholds
        out.add(t)
    return frozenset(out)


def tier_of(k_row: dict[str, Any], p_row: dict[str, Any], alignment: str) -> tuple[str, list[str]]:
    """Tier A pairs are the only ones whose spread is treated as a verdict input."""
    flags: list[str] = []
    if alignment not in ("entity_match", "flipped"):
        flags.append(f"alignment:{alignment}")
    kt, pt = tokens(k_row), tokens(p_row)
    if non_year_numbers(kt) != non_year_numbers(pt):
        flags.append("threshold_mismatch")  # e.g. strike-ladder leg vs above-$X market
    for conj in (" and ", " or "):
        if (conj in k_row["title"].lower()) != (conj in p_row["title"].lower()):
            flags.append("compound_structure_mismatch")  # e.g. "Boot AND Ball" / "Messi OR Mbappe"
            break
    return ("A" if not flags else "B"), flags


# --------------------------------------------------------------------------- #
# spread math
# --------------------------------------------------------------------------- #
def taker_fee(price: float, rate: float) -> float:
    """Per-share taker fee rate·p·(1−p) — same form on both venues."""
    return rate * price * (1.0 - price)


def spread_metrics(k_row: dict[str, Any], p_row: dict[str, Any]) -> dict[str, Any]:
    """Two-leg lock edges (assuming identical resolution) + mid gap.

    edge_a: buy Kalshi YES @ k_ask, buy Polymarket NO @ (1 − pm_bid);
            payoff 1 ⇒ edge = pm_bid − k_ask − fees.
    edge_b: reverse. NO-side cost via (1 − yes_bid) is the synthetic bound on a
            binary CLOB; fee on the NO leg uses the same p·(1−p) (symmetric).
    """
    k_fee_a = taker_fee(k_row["ask"], k_row["fee_rate"])
    p_fee_a = taker_fee(p_row["bid"], p_row["fee_rate"])
    edge_a = p_row["bid"] - k_row["ask"] - k_fee_a - p_fee_a
    k_fee_b = taker_fee(k_row["bid"], k_row["fee_rate"])
    p_fee_b = taker_fee(p_row["ask"], p_row["fee_rate"])
    edge_b = k_row["bid"] - p_row["ask"] - k_fee_b - p_fee_b
    best = max(edge_a, edge_b)
    return {
        "mid_gap": round(abs(k_row["mid"] - p_row["mid"]), 4),
        "edge_kalshi_yes_pm_no": round(edge_a, 4),
        "edge_pm_yes_kalshi_no": round(edge_b, 4),
        "best_net_edge": round(best, 4),
        "direction": ("buy_kalshi_yes_pm_no" if edge_a >= edge_b else "buy_pm_yes_kalshi_no"),
    }


def quantiles(vals: list[float], qs: tuple[float, ...] = (0.5, 0.9, 0.99)) -> dict[str, float]:
    if not vals:
        return {}
    s = sorted(vals)
    out = {}
    for q in qs:
        idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
        out[f"p{int(q * 100)}"] = round(s[idx], 4)
    out["max"] = round(s[-1], 4)
    return out


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def scan(*, min_volume_kalshi: float, min_volume_pm: float, kalshi_pages: int,
         pm_pages: int, min_score: float, top: int, out_dir: str,
         verbose: bool = False) -> dict[str, Any]:
    t0 = time.time()
    kalshi = fetch_kalshi(min_volume=min_volume_kalshi, max_pages=kalshi_pages, verbose=verbose)
    polymarket = fetch_polymarket(min_volume=min_volume_pm, max_pages=pm_pages, verbose=verbose)
    matches = match_rows(kalshi, polymarket, min_score=min_score)

    for m in matches:
        alignment, pm_aligned = align_pair(m["kalshi"], m["polymarket"])
        tier, flags = tier_of(m["kalshi"], m["polymarket"], alignment)
        m["alignment"] = alignment
        m["tier"] = tier
        m["flags"] = flags
        m["pm_aligned"] = pm_aligned
        m["spread"] = spread_metrics(m["kalshi"], pm_aligned)
        if tier == "A" and m["spread"]["mid_gap"] > 0.30:
            m["flags"] = ["suspect_semantic_mismatch"]  # aligned but priced worlds apart -> human check
    matches.sort(key=lambda m: m["spread"]["best_net_edge"], reverse=True)

    tier_a = [m for m in matches if m["tier"] == "A"]
    tier_b = [m for m in matches if m["tier"] == "B"]
    a_gaps = [m["spread"]["mid_gap"] for m in tier_a]
    a_edges = [m["spread"]["best_net_edge"] for m in tier_a]
    a_positive = [m for m in tier_a if m["spread"]["best_net_edge"] > 0.0]

    def brief(m: dict[str, Any]) -> dict[str, Any]:
        k, p, s = m["kalshi"], m["pm_aligned"], m["spread"]
        return {
            "score": m["score"], "tier": m["tier"], "alignment": m["alignment"],
            "flags": m["flags"], **s,
            "kalshi": {"id": k["id"], "title": k["title"], "entity": k["entity"],
                       "bid": k["bid"], "ask": k["ask"], "volume": round(k["volume"]),
                       "close": k["close_time"], "rules_head": k["rules"][:160]},
            "polymarket": {"id": p["id"], "title": p["title"], "entity": p["entity"],
                           "bid": round(p["bid"], 4), "ask": round(p["ask"], 4),
                           "volume": round(p["volume"]), "fee_rate": p["fee_rate"],
                           "close": p["close_time"], "rules_head": p["rules"][:160]},
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - t0, 1),
        "params": {"min_volume_kalshi": min_volume_kalshi, "min_volume_pm": min_volume_pm,
                   "min_score": min_score, "kalshi_pages": kalshi_pages, "pm_pages": pm_pages,
                   "kalshi_fee_rate": KALSHI_FEE_RATE, "pm_sports_fee_fallback": PM_SPORTS_FEE_RATE},
        "universe": {"kalshi_rows": len(kalshi), "polymarket_rows": len(polymarket)},
        "matched_pairs": len(matches),
        "tier_a_pairs": len(tier_a),
        "tier_b_pairs": len(tier_b),
        "tier_a_mid_gap_distribution": quantiles(a_gaps),
        "tier_a_best_net_edge_distribution": quantiles(a_edges),
        "tier_a_pairs_with_positive_net_edge": len(a_positive),
        "tier_a_positive_edge_pairs": [brief(m) for m in a_positive[:top]],
        "tier_a_all_pairs": [brief(m) for m in sorted(tier_a, key=lambda x: x["spread"]["mid_gap"],
                                                      reverse=True)],
        "tier_b_sample": [brief(m) for m in tier_b[:top]],
        "caveats": [
            "Tier A means the entities align, every non-year number matches exactly, and "
            "neither side is a compound contract. Only Tier A edges feed the verdict.",
            "Text matching does not verify settlement terms. Every Tier A pair still needs a "
            "human to compare the two resolution rules.",
            "The NO leg is costed synthetically as 1 - yes_bid, an upper bound; a real NO "
            "book may be better.",
            "Kalshi fees are modelled at the 0.07 tier throughout, which errs high.",
            "This is a single snapshot, not a time series, and quotes are top-of-book with "
            "no depth sweep.",
        ],
        "boundaries": ["read_only", "no_auth", "no_order", "no_wallet", "isolated_output"],
    }

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(out_dir, f"scan_{stamp}.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    matches_path = os.path.join(out_dir, f"matches_{stamp}.jsonl")
    with open(matches_path, "w", encoding="utf-8") as fh:
        for m in matches:
            fh.write(json.dumps(brief(m), ensure_ascii=False) + "\n")
    report["report_path"] = report_path
    report["matches_path"] = matches_path
    return report


# --------------------------------------------------------------------------- #
# selftest (offline, no network)
# --------------------------------------------------------------------------- #
def selftest() -> dict[str, Any]:
    checks: dict[str, bool] = {}

    k = {"venue": "kalshi", "id": "K1", "title": "Will Spain win the 2026 FIFA World Cup?",
         "entity": "Spain", "event_title": "World Cup champion", "rules": "r",
         "bid": 0.20, "ask": 0.22, "mid": 0.21, "volume": 1000.0,
         "close_time": "2026-07-19T22:00:00+00:00", "category": "Sports",
         "fee_rate": KALSHI_FEE_RATE, "url": ""}
    p = {"venue": "polymarket", "id": "p1", "title": "Will Spain win the 2026 FIFA World Cup?",
         "entity": "", "event_title": "", "rules": "r",
         "bid": 0.30, "ask": 0.31, "mid": 0.305, "volume": 50_000.0,
         "close_time": "2026-07-19T20:00:00+00:00", "category": "",
         "fee_rate": 0.0, "url": ""}
    p_far = {**p, "id": "p2", "title": "Will Portugal win the 2026 FIFA World Cup?",
             "bid": 0.10, "ask": 0.11, "mid": 0.105}

    kt, pt = tokens(k), tokens(p)
    checks["tokens_drop_stopwords"] = "will" not in kt and "the" not in kt
    checks["tokens_keep_year"] = "2026" in kt
    checks["number_agreement_exact"] = number_agreement(kt, pt) == 1.0
    checks["number_agreement_neutral"] = number_agreement(frozenset(("spain",)), frozenset(("spain",))) == 0.5
    checks["close_time_tight"] = close_time_proximity(k["close_time"], p["close_time"]) == 1.0
    checks["close_time_far"] = close_time_proximity(k["close_time"], "2026-12-01T00:00:00+00:00") == 0.0

    s_same = pair_score(k, p, kt, pt)
    s_diff = pair_score(k, p_far, kt, tokens(p_far))
    checks["score_identical_high"] = s_same > 0.85  # event_title extras dilute jaccard slightly
    checks["score_prefers_same_entity"] = s_same > s_diff

    matches = match_rows([k], [p_far, p], min_score=0.4)
    checks["greedy_picks_best"] = len(matches) == 1 and matches[0]["polymarket"]["id"] == "p1"
    two_k = [k, {**k, "id": "K2", "title": "Will Portugal win the 2026 FIFA World Cup?", "entity": "Portugal"}]
    m2 = match_rows(two_k, [p, p_far], min_score=0.4)
    checks["one_to_one"] = (len(m2) == 2
                            and len({mm["kalshi"]["id"] for mm in m2}) == 2
                            and len({mm["polymarket"]["id"] for mm in m2}) == 2)

    checks["fee_formula"] = abs(taker_fee(0.5, 0.07) - 0.0175) < 1e-12
    checks["fee_zero_rate"] = taker_fee(0.5, 0.0) == 0.0

    # spread arithmetic on a constructed arb: pm_bid 0.30 vs k_ask 0.22
    sm = spread_metrics(k, p)
    expect_a = p["bid"] - k["ask"] - taker_fee(k["ask"], KALSHI_FEE_RATE) - 0.0
    checks["edge_a_exact"] = abs(sm["edge_kalshi_yes_pm_no"] - round(expect_a, 4)) < 1e-9
    checks["edge_positive_detected"] = sm["best_net_edge"] > 0 and sm["direction"] == "buy_kalshi_yes_pm_no"
    checks["mid_gap"] = abs(sm["mid_gap"] - round(abs(0.21 - 0.305), 4)) < 1e-9
    k_rich = {**k, "bid": 0.40, "ask": 0.42}
    sm2 = spread_metrics(k_rich, p)
    expect_b = k_rich["bid"] - p["ask"] - taker_fee(k_rich["bid"], KALSHI_FEE_RATE) - 0.0
    checks["edge_b_exact"] = abs(sm2["edge_pm_yes_kalshi_no"] - round(expect_b, 4)) < 1e-9

    checks["quantiles_shape"] = quantiles([0.01, 0.02, 0.03, 0.10]) == {
        "p50": 0.02, "p90": 0.10, "p99": 0.10, "max": 0.10}

    # alignment layer
    k_adv = {**k, "id": "K3", "title": "England vs Argentina: To Advance", "entity": "England",
             "bid": 0.45, "ask": 0.46, "mid": 0.455}
    p_adv = {**p, "id": "p3", "title": "England vs. Argentina: Team to Advance", "entity": "Argentina",
             "bid": 0.55, "ask": 0.55, "mid": 0.55}
    al, p_al = align_pair(k_adv, p_adv)
    checks["align_flips_opposite_side"] = al == "flipped" and abs(p_al["bid"] - 0.45) < 1e-9 \
        and abs(p_al["ask"] - 0.45) < 1e-9
    checks["align_flip_closes_gap"] = spread_metrics(k_adv, p_al)["mid_gap"] < 0.01
    al2, _ = align_pair(k_adv, {**p_adv, "entity": "England"})
    checks["align_same_side_no_flip"] = al2 == "entity_match"
    al3, _ = align_pair({**k, "entity": "Google"},
                        {**p, "title": "Will xAI have the best AI model at the end of July 2026?", "entity": ""})
    checks["align_conflict_detected"] = al3 == "conflict"
    al4, _ = align_pair({**k, "entity": "", "title": "Who will dissent at the July 2026 FOMC meeting?"},
                        {**p, "title": "Will there be no change in Fed rates?", "entity": ""})
    checks["align_unverified_when_no_entity"] = al4 == "unverified"
    checks["entity_from_will_subject"] = entity_tokens(
        {"entity": "", "title": "Will LeBron James announce his retirement in 2026?"}) == frozenset(("lebron", "james"))
    checks["vs_sides_drops_tournament_prefix"] = vs_sides("Swiss Open: Kilian Feldbausch vs Miomir Kecmanovic") == (
        frozenset(("kilian", "feldbausch")), frozenset(("miomir", "kecmanovic")))

    # tiering: threshold ladders and compound awards must fall out of Tier A
    tier_ok, fl = tier_of({**k, "title": "Bitcoin price on Jul 13, 2026?", "entity": "$63,750 to $64,249"},
                          {**p, "title": "Will the price of Bitcoin be above $62,000 on July 13?"}, "entity_match")
    checks["tier_blocks_threshold_mismatch"] = tier_ok == "B" and "threshold_mismatch" in fl
    tier2, fl2 = tier_of({**k, "title": "Will Messi win the Golden Boot and the Golden Ball?"},
                         {**p, "title": "Will Messi be the top goalscorer at the 2026 FIFA World Cup?"},
                         "entity_match")
    checks["tier_blocks_compound_structure"] = tier2 == "B" and "compound_structure_mismatch" in fl2
    tier3, _ = tier_of(k, p, "entity_match")
    checks["tier_a_clean_pair"] = tier3 == "A"
    checks["non_year_numbers_drops_years"] = non_year_numbers(frozenset(("2026", "13", "62"))) == frozenset(("13", "62"))
    tier4, fl4 = tier_of({**k, "title": "Will Joel Schwaerzler win set 1 in the Lorenzo Sonego match?",
                          "entity": "Joel Schwaerzler"},
                         {**p, "title": "Swiss Open: Lorenzo Sonego vs Joel Schwaerzler",
                          "entity": "Joel Schwaerzler"}, "entity_match")
    checks["tier_blocks_set1_vs_match"] = tier4 == "B" and "threshold_mismatch" in fl4
    checks["unicode_fold_diacritics"] = tokens({"title": "Kylian Mbappé: 1+ goals", "entity": "",
                                                "event_title": ""}) >= frozenset(("kylian", "mbappe"))

    # fee-rate resolution mirror of fetch_polymarket: feeSchedule.rate > feesEnabled fallback > 0
    fee_cases = (
        ({"feeSchedule": {"rate": "0.02"}, "feesEnabled": True}, 0.02),
        ({"feesEnabled": True}, PM_SPORTS_FEE_RATE),
        ({"feesEnabled": False}, 0.0),
    )
    ok = True
    for raw, want in fee_cases:
        fee_sched = raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {}
        got = _f(fee_sched.get("rate"))
        if got is None:
            got = PM_SPORTS_FEE_RATE if raw.get("feesEnabled") else 0.0
        ok = ok and abs(got - want) < 1e-12
    checks["pm_fee_resolution"] = ok

    return {"PASS": all(checks.values()), "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-shot Polymarket/Kalshi cross-venue spread scanner (read-only).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selftest", action="store_true")
    mode.add_argument("--scan", action="store_true")
    ap.add_argument("--min-volume-kalshi", type=float, default=1000.0, help="minimum dollar volume")
    ap.add_argument("--min-volume-pm", type=float, default=5000.0)
    ap.add_argument("--kalshi-pages", type=int, default=40)
    ap.add_argument("--pm-pages", type=int, default=4)
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        r = selftest()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["PASS"] else 1
    r = scan(min_volume_kalshi=a.min_volume_kalshi, min_volume_pm=a.min_volume_pm,
             kalshi_pages=a.kalshi_pages, pm_pages=a.pm_pages, min_score=a.min_score,
             top=a.top, out_dir=a.out, verbose=a.verbose)
    slim = {k: v for k, v in r.items()
            if k not in ("tier_a_positive_edge_pairs", "tier_a_all_pairs", "tier_b_sample")}
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    for m in r["tier_a_all_pairs"]:
        print(f"  A gap={m['mid_gap']:.3f} edge={m['best_net_edge']:+.3f} {m['alignment']:<12}"
              f" flags={','.join(m['flags']) or '-'} | K: {m['kalshi']['title'][:52]} "
              f"[{m['kalshi']['bid']:.3f}/{m['kalshi']['ask']:.3f}] | PM: {m['polymarket']['title'][:52]} "
              f"[{m['polymarket']['bid']:.3f}/{m['polymarket']['ask']:.3f}]", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
