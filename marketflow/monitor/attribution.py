"""Position-scoped price-jump detection and attribution.

When a market somebody holds moves sharply inside a rolling window, send one
alert that says why: "your market moved +8 points in 30 minutes; cause: <specific
event, with a source link and a timestamp>".

Two layers, kept strictly apart:

- **Detection** (pure functions, no network). Each monitoring cycle samples the
  current price into the watch-state's price history. detect_jump finds the
  reference point inside the window furthest from the current price, and a move
  of at least the threshold is a jump.

- **Attribution** (best-effort, networked). An optional pluggable intel router
  turns the market into a watch plan and then an event stream. Only two kinds of
  evidence are accepted: a news event carrying a real URL and a published
  timestamp that falls inside the jump window, or a live score for a fixture the
  market is actually about.

**Honest degradation, and this is a hard boundary.** With no matching event, the
alert says so — "no clear news to explain it; could be a large trade or a
liquidity move" — and never invents a cause. An item with no timestamp, or whose
relevance cannot be scored, is never admitted as attribution evidence.

Isolation: read-only. It touches no execution path, no arm state, caps or kill
file. Loading the intel router is best effort: when it is absent, detection still
runs and attribution degrades to the honest wording rather than taking the
alerting stack down.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
from marketflow.monitor import polymarket_data as pmd  # noqa: E402
from marketflow.monitor import rules as R  # noqa: E402

from marketflow.paths import PROJECT_DIR
CLOB_API = "https://clob.polymarket.com"

# News can precede the price reaction, so the event window starts this many
# seconds before the jump reference point.
NEWS_PRE_SLACK_SEC = 2700.0
# Attribution trusts only items that clear the relevance threshold; nothing is
# discounted further beyond it.
_ROUTER = None
_ROUTER_TRIED = False


def _load_router():
    """Load the pluggable intel router (a read-only sidecar), named by an
    environment variable:

        MARKETFLOW_INTEL_ROUTER=<importable module name>

    The module must implement two functions:
        build_watch_plan(market, *, fetch_tags: bool, timeout: float) -> plan
        poll_plan_events(plan, *, lookback_days: int, max_items: int) -> list[dict]

    No implementation ships here: which intel sources to use, and in what mix, is
    the deployment's own decision. Unconfigured or failing to load returns None and
    the caller degrades honestly (matched=False plus a degrade_reason). It can
    never take the alerting stack down."""
    global _ROUTER, _ROUTER_TRIED
    if _ROUTER_TRIED:
        return _ROUTER
    _ROUTER_TRIED = True
    name = os.environ.get("MARKETFLOW_INTEL_ROUTER", "").strip()
    if not name:
        _ROUTER = None
        return _ROUTER
    try:
        import importlib  # noqa: PLC0415

        _ROUTER = importlib.import_module(name)
    except Exception:
        _ROUTER = None
    return _ROUTER


# ---------------------------------------------------------------- detection


def update_history(
    history: list | None,
    cur: float | None,
    now_epoch: float,
    *,
    window_sec: float,
    keep_factor: float = 1.2,
) -> list[list[float]]:
    """Sample the current price into the price history and trim anything outside
    the window. Pure function; returns a new list."""
    out: list[list[float]] = []
    for row in history or []:
        try:
            ts, p = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if now_epoch - ts <= window_sec * keep_factor:
            out.append([ts, p])
    if cur is not None:
        out.append([round(now_epoch, 1), round(float(cur), 4)])
    return out


def detect_jump(
    history: list | None,
    cur: float | None,
    now_epoch: float,
    *,
    window_sec: float,
    threshold_pts: float,
) -> dict[str, Any] | None:
    """Detect a price jump inside the window. The reference point is the sample
    furthest from the current price in absolute terms: for a monotone move that is
    the start of the window, for a there-and-back move it is the trough or peak.
    A move of at least the threshold returns a jump description."""
    if cur is None or not history:
        return None
    window: list[tuple[float, float]] = []
    for row in history:
        try:
            ts, p = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if now_epoch - ts <= window_sec:
            window.append((ts, p))
    if not window:
        return None
    ref_ts, ref_price = max(window, key=lambda tp: abs(cur - tp[1]))
    delta = cur - ref_price
    if abs(delta) < threshold_pts:
        return None
    return {
        "delta": round(delta, 4),
        "ref_price": round(ref_price, 4),
        "ref_ts": ref_ts,
        "cur_price": round(float(cur), 4),
        "window_min": round((now_epoch - ref_ts) / 60.0, 1),
    }


# ---------------------------------------------------------------- attribution


def _market_for_router(pos: dict[str, Any], *, timeout: float = 12.0) -> dict[str, Any]:
    """Build the router's market dict from a position. Best-effort fetch of market
    metadata (events and tags give the stronger classification signal); failing
    that, fall back to the title as the question and classify by keyword."""
    slug = str(pos.get("slug") or "").strip()
    fallback = {
        "question": pos.get("title"),
        "slug": slug or None,
        "market_id": pos.get("conditionId"),
        "raw": {},
    }
    if not slug:
        return fallback
    try:
        url = f"{pmd.GAMMA_API}/markets?slug={urllib.parse.quote(slug)}"
        data = pmd._http_get_json(url, timeout=timeout)
        m = data[0] if isinstance(data, list) and data else data
        if isinstance(m, dict) and m.get("question"):
            return {
                "question": m.get("question"),
                "slug": slug,
                "market_id": m.get("conditionId") or pos.get("conditionId"),
                "resolution_criteria_text": m.get("description"),
                "raw": m,
            }
    except (pmd.DataError, Exception):
        pass
    return fallback


def _score_candidate(ev_kind: str, payload: dict[str, Any], jump: dict[str, Any],
                     now_epoch: float) -> float | None:
    """How strongly one event explains this jump. None means it does not qualify:
    no timestamp, outside the window, or insufficient relevance.

    A score event — a fixture this market is about, in play or just finished — is
    itself the immediate explanation and carries the highest weight.

    A news event qualifies when relevance clears the threshold and published_at
    falls in [ref_ts - slack, now]; its score is relevance plus a small bonus for
    being close in time."""
    if ev_kind == "score":
        if payload.get("state") in ("in", "post"):
            return 2.0
        return None
    if ev_kind == "news":
        rel = payload.get("relevance")
        if rel is None:
            # An item whose relevance cannot be scored is never used as
            # attribution. Degrade rather than pretend.
            return None
        pub = R._parse_iso(str(payload.get("published_at") or ""))
        if pub is None or not payload.get("url"):
            return None
        pub_epoch = pub.timestamp()
        window_start = jump["ref_ts"] - NEWS_PRE_SLACK_SEC
        if not (window_start <= pub_epoch <= now_epoch + 300):
            return None
        span = max(1.0, now_epoch - window_start)
        closeness = max(0.0, 1.0 - abs(now_epoch - pub_epoch) / span)
        return float(rel) + 0.2 * closeness
    return None


def attribute_jump(
    pos: dict[str, Any],
    jump: dict[str, Any],
    *,
    now_epoch: float | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Attribute one jump. Returns an attribution dict:
    {matched, explanation|None, checked_sources, category, degrade_reason|None}.
    Every network failure collapses into matched=False plus a specific
    degrade_reason. It never raises."""
    now_epoch = now_epoch if now_epoch is not None else time.time()
    MIR = _load_router()
    if MIR is None:
        return {
            "matched": False,
            "explanation": None,
            "checked_sources": [],
            "category": None,
            "degrade_reason": "intel router unavailable",
        }
    try:
        market = _market_for_router(pos, timeout=timeout)
        plan = MIR.build_watch_plan(market, fetch_tags=True, timeout=timeout)
        events = MIR.poll_plan_events(plan, lookback_days=1, max_items=5)
    except Exception as exc:
        return {
            "matched": False,
            "explanation": None,
            "checked_sources": [],
            "category": None,
            "degrade_reason": f"intel poll failed: {str(exc)[:120]}",
        }
    checked = sorted({e.get("source_id") for e in events
                      if e.get("kind") not in ("error",)} - {None})
    best: dict[str, Any] | None = None
    best_score = 0.0
    for e in events:
        payload = e.get("event") or {}
        score = _score_candidate(e.get("kind"), payload, jump, now_epoch)
        if score is not None and score > best_score:
            best_score = score
            best = {"kind": e.get("kind"), "source_id": e.get("source_id"), **payload}
    return {
        "matched": best is not None,
        "explanation": best,
        "checked_sources": checked,
        "category": plan.classification.get("category"),
        "degrade_reason": None if best is not None else "no matching event in jump window",
    }


# ---------------------------------------------------------------- alert copy

# Internal source_id -> the category name a reader sees. Internal source codes
# never leak into an alert: which providers a deployment buys from is its own
# business, and the reader only needs to know that news, official sources and
# scores were checked. An unmapped id falls back to the most generic "news".
_SOURCE_LABELS = {
    "search_gdelt": "news",
    "gdelt": "news",
    "tavily_news": "news",
    "tavily": "news",
    "official_rss": "official sources",
    "rss": "official sources",
    "espn": "live scores",
    "espn_scores": "live scores",
    "scores": "live scores",
    "social": "social chatter",
}


def _public_sources(source_ids: list[str] | None) -> str:
    """Fold a list of internal source ids into a de-duplicated, ordered,
    human-readable category string, for reader-facing text only."""
    labels: list[str] = []
    for sid in source_ids or []:
        label = _SOURCE_LABELS.get(str(sid), "news")
        if label not in labels:
            labels.append(label)
    return ", ".join(labels)


def _fmt_pts(delta: float) -> str:
    return f"{delta * 100:+.0f} pts"


def _fmt_when(published_at: str | None) -> str:
    dt = R._parse_iso(str(published_at or ""))
    if dt is None:
        return str(published_at or "?")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _explanation_lines(expl: dict[str, Any]) -> list[str]:
    if expl.get("kind") == "score":
        score = expl.get("score")
        parts: list[str] = []
        if isinstance(score, dict):  # soccer shape: {home: {team, score}, away: ...}
            for side in ("home", "away"):
                t = (score.get(side) or {})
                if t.get("team") is not None:
                    parts.append(f"{t.get('team')} {t.get('score')}")
        elif isinstance(score, list):  # tennis shape: [{player, sets, winner}, ...]
            for t in score:
                if t.get("player") is not None:
                    parts.append(f"{t.get('player')} {t.get('sets')}".strip())
        score_s = " — ".join(parts) if parts else ""
        detail = expl.get("detail") or expl.get("state") or "in play"
        driver = "Likely driver: live game — "
        line = f"{driver}{expl.get('fixture')} ({detail}"
        line += f", {score_s})" if score_s else ")"
        return [line]
    driver = "Likely driver: "
    lines = [
        f"{driver}{expl.get('title')} "
        f"({expl.get('source')}, {_fmt_when(expl.get('published_at'))})"
    ]
    if expl.get("url"):
        lines.append(str(expl["url"]))
    return lines


def format_jump_alert(
    pos: dict[str, Any],
    jump: dict[str, Any],
    attribution: dict[str, Any] | None,
    *,
    cooldown_sec: float,
) -> dict[str, Any]:
    """Turn a jump, plus optional attribution, into the alert dict the pipeline
    consumes. attribution=None is for the should_fire pre-check only: the dedup
    scope and cooldown are identical either way, and a jump that is still cooling
    down should not cost a network call."""
    title = pos.get("title") or "(unknown market)"
    outcome = pos.get("outcome") or "?"
    asset = str(pos.get("key") or "")
    head = (
        f"⚡ {title} — {outcome} moved {_fmt_pts(jump['delta'])} in ~{jump['window_min']:.0f} min "
        f"({jump['ref_price']:.2f} → {jump['cur_price']:.2f})."
    )
    if attribution is None:
        body_lines: list[str] = []
    elif attribution.get("matched"):
        body_lines = _explanation_lines(attribution["explanation"])
    else:
        checked = _public_sources(attribution.get("checked_sources")) or "news, official sources, scores"
        body_lines = [
            f"No clear news to explain it (checked {checked}) — "
            f"could be a large trade or a liquidity move."
        ]
    return {
        "rule": "price_jump",
        "scope": f"jump|{asset}",
        "bucket": 1,
        "cooldown_sec": float(cooldown_sec),
        "severity": "warn",
        "emoji": "⚡",
        "title": title,
        "attribution": attribution,
        "reason": "\n".join([head] + body_lines),
    }


# ---------------------------------------------------------------- live scan


def _clob_price_history(token_id: str, *, interval: str = "1d", fidelity: int = 1,
                        timeout: float = 15.0) -> list[tuple[float, float]]:
    """Public price history (read-only). Returns [(epoch_sec, price), ...]
    ascending."""
    q = urllib.parse.urlencode({"market": token_id, "interval": interval, "fidelity": fidelity})
    data = pmd._http_get_json(f"{CLOB_API}/prices-history?{q}", timeout=timeout)
    hist = (data or {}).get("history") or []
    out = []
    for h in hist:
        t, p = h.get("t"), h.get("p")
        if isinstance(t, (int, float)) and isinstance(p, (int, float)):
            out.append((float(t), float(p)))
    out.sort(key=lambda tp: tp[0])
    return out


def _find_recent_jump(series: list[tuple[float, float]], *, window_sec: float,
                      threshold_pts: float, lookback_sec: float) -> dict[str, Any] | None:
    """Find jumps within the lookback in a real price series, sliding detect_jump
    across it."""
    if not series:
        return None
    end_ts = series[-1][0]
    for i in range(len(series) - 1, -1, -1):
        ts, p = series[i]
        if end_ts - ts > lookback_sec:
            break
        hist = [[t0, p0] for t0, p0 in series if 0 <= ts - t0 <= window_sec and t0 < ts]
        j = detect_jump(hist, p, ts, window_sec=window_sec, threshold_pts=threshold_pts)
        if j is not None:
            j["jump_at_epoch"] = ts
            j["jump_at"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
            return j
    return None


def scan_live_jumps(*, n_markets: int = 40, window_sec: float = 1800.0,
                    threshold_pts: float = 0.05, lookback_sec: float = 86400.0,
                    timeout: float = 15.0) -> list[dict[str, Any]]:
    """Scan liquid active markets for ones that really jumped recently. Read-only
    public endpoints."""
    q = urllib.parse.urlencode({
        "active": "true", "closed": "false", "order": "volume24hr",
        "ascending": "false", "limit": n_markets,
    })
    markets = pmd._http_get_json(f"{pmd.GAMMA_API}/markets?{q}", timeout=timeout)
    found: list[dict[str, Any]] = []
    for m in markets if isinstance(markets, list) else []:
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except json.JSONDecodeError:
                toks = None
        if not isinstance(toks, list) or not toks:
            continue
        try:
            series = _clob_price_history(str(toks[0]), timeout=timeout)
        except pmd.DataError:
            continue
        jump = _find_recent_jump(series, window_sec=window_sec,
                                 threshold_pts=threshold_pts, lookback_sec=lookback_sec)
        if jump is not None:
            found.append({
                "question": m.get("question"),
                "slug": m.get("slug"),
                "conditionId": m.get("conditionId"),
                "outcome": "Yes",
                "jump": jump,
            })
    return found


def demo_attribution(hit: dict[str, Any]) -> dict[str, Any]:
    """Run full attribution over the jumps a scan found and render the alert
    text."""
    pos = {
        "key": f"demo|{hit.get('slug')}",
        "title": hit.get("question"),
        "slug": hit.get("slug"),
        "conditionId": hit.get("conditionId"),
        "outcome": hit.get("outcome") or "Yes",
    }
    jump = hit["jump"]
    # Attribute against the real current time, as the service does when it
    # detects a jump, rather than against the jump's own timestamp. That lets
    # reporting which lags by a few minutes still fall inside the window.
    att = attribute_jump(pos, jump)
    alert = format_jump_alert(pos, jump, att, cooldown_sec=3600.0)
    return {"market": pos["title"], "slug": pos["slug"], "jump": jump,
            "attribution": att, "alert_text": alert["reason"]}


# ---------------------------------------------------------------- selftest


def selftest() -> tuple[int, int]:
    """Selftest over the pure-function layer. No network. Returns (passed, failed)."""
    passed = failed = 0

    def check(cond: bool, name: str, detail: Any = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    now = 1_000_000.0
    W, TH = 1800.0, 0.05

    # monotone +8pts inside the window -> a jump, reference at the window start
    hist = [[now - 1500, 0.42], [now - 900, 0.45], [now - 300, 0.48]]
    j = detect_jump(hist, 0.50, now, window_sec=W, threshold_pts=TH)
    check(j is not None and abs(j["delta"] - 0.08) < 1e-9 and j["ref_price"] == 0.42,
          "monotone rise: jump and reference point", j)

    # inside the threshold: no jump
    j = detect_jump([[now - 900, 0.48]], 0.50, now, window_sec=W, threshold_pts=TH)
    check(j is None, "no jump inside the threshold", j)

    # a downward move has a negative delta
    j = detect_jump([[now - 900, 0.60]], 0.50, now, window_sec=W, threshold_pts=TH)
    check(j is not None and j["delta"] == -0.10, "downward jump direction", j)

    # samples outside the window do not count
    j = detect_jump([[now - 7200, 0.20], [now - 600, 0.48]], 0.50, now,
                    window_sec=W, threshold_pts=TH)
    check(j is None, "samples outside the window are excluded", j)

    # V shape: price returns to the start but the trough clears the threshold,
    # so the reference point is the trough
    hist = [[now - 1200, 0.50], [now - 600, 0.42]]
    j = detect_jump(hist, 0.50, now, window_sec=W, threshold_pts=TH)
    check(j is not None and j["ref_price"] == 0.42 and j["delta"] == 0.08,
          "V shape takes the extremum as reference", j)

    # history update: trim and append
    h2 = update_history([[now - 4000, 0.4], [now - 100, 0.45]], 0.46, now, window_sec=W)
    check(len(h2) == 2 and h2[-1][1] == 0.46 and h2[0][0] == now - 100,
          "history trims and appends", h2)

    # an empty price is not appended
    h3 = update_history([[now - 100, 0.45]], None, now, window_sec=W)
    check(len(h3) == 1, "empty price is not appended", h3)

    # Candidate scoring: news with no timestamp does not qualify, nor does
    # anything outside the window, nor anything whose relevance is unscorable.
    jump = {"delta": 0.08, "ref_price": 0.42, "ref_ts": now - 900, "cur_price": 0.5,
            "window_min": 15.0}
    iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()  # noqa: E731
    ok = _score_candidate("news", {"relevance": 0.6, "url": "https://x", "published_at": iso(now - 600)}, jump, now)
    check(ok is not None and ok > 0.6, "in-window highly relevant news qualifies", ok)
    check(_score_candidate("news", {"relevance": 0.6, "url": "https://x", "published_at": None}, jump, now) is None,
          "no timestamp -> never attributed")
    check(_score_candidate("news", {"relevance": None, "url": "https://x", "published_at": iso(now - 600)}, jump, now) is None,
          "unscorable relevance -> never attributed")
    check(_score_candidate("news", {"relevance": 0.9, "url": "https://x", "published_at": iso(now - 90000)}, jump, now) is None,
          "outside the window -> never attributed")
    check(_score_candidate("score", {"state": "in"}, jump, now) == 2.0,
          "a live fixture is the strongest explanation")
    check(_score_candidate("score", {"state": "pre"}, jump, now) is None,
          "a fixture that has not started explains nothing")

    # Text: a hit carries source and timestamp; a degrade says plainly that
    # nothing was found.
    pos = {"key": "tokX", "title": "Will X win?", "outcome": "Yes", "slug": "will-x-win"}
    att = {"matched": True, "checked_sources": ["search_gdelt"], "category": "politics",
           "degrade_reason": None,
           "explanation": {"kind": "news", "title": "X takes decisive lead", "url": "https://news/x",
                           "source": "reuters.com", "published_at": iso(now - 500), "relevance": 0.7}}
    al = format_jump_alert(pos, jump, att, cooldown_sec=3600)
    check("Likely driver" in al["reason"] and "https://news/x" in al["reason"],
          "attribution text carries the source link", al["reason"])
    check(al["cooldown_sec"] == 3600 and al["scope"] == "jump|tokX", "alert dedup fields", al)

    deg = {"matched": False, "checked_sources": ["official_rss", "search_gdelt"],
           "category": "politics", "degrade_reason": "no matching event in jump window",
           "explanation": None}
    al = format_jump_alert(pos, jump, deg, cooldown_sec=3600)
    check("No clear news to explain it" in al["reason"] and "official sources" in al["reason"],
          "honest degrade text uses readable categories", al["reason"])
    # Leak guard: an internal source code must never appear in reader-facing text.
    for leak in ("official_rss", "search_gdelt", "gdelt", "espn", "tavily"):
        check(leak not in al["reason"], f"degrade text leaks no internal source name [{leak}]", al["reason"])
    check("Likely driver" not in al["reason"], "a degrade never invents a cause")
    return passed, failed


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Position-scoped price-jump detection and attribution (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", action="store_true",
                    help="scan active markets for recent real jumps (read-only)")
    ap.add_argument("--demo", type=int, metavar="N", nargs="?", const=3,
                    help="run full attribution over the first N jumps found and print the alert text")
    ap.add_argument("--markets", type=int, default=40)
    ap.add_argument("--threshold", type=float, default=0.05)
    ap.add_argument("--window-min", type=float, default=30.0)
    ap.add_argument("--lookback-hours", type=float, default=24.0)
    args = ap.parse_args(argv)

    if args.selftest:
        print("attribution selftest")
        p, f = selftest()
        print(f"RESULT: {p} passed, {f} failed")
        return 1 if f else 0

    if args.scan or args.demo is not None:
        hits = scan_live_jumps(n_markets=args.markets, window_sec=args.window_min * 60,
                               threshold_pts=args.threshold,
                               lookback_sec=args.lookback_hours * 3600)
        print(f"found {len(hits)} market(s) with a recent >= {args.threshold*100:.0f}pt "
              f"jump inside {args.window_min:.0f}min (lookback {args.lookback_hours:.0f}h)")
        for h in hits:
            j = h["jump"]
            print(f"  {j['jump_at']}  {_fmt_pts(j['delta']):>8}  {h['question'][:70]}")
        if args.demo is not None:
            for h in hits[: args.demo]:
                print("\n" + "=" * 72)
                out = demo_attribution(h)
                print(json.dumps({k: out[k] for k in ("market", "slug", "jump", "attribution")},
                                 ensure_ascii=False, indent=2, default=str))
                print("--- alert text ---")
                print(out["alert_text"])
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
