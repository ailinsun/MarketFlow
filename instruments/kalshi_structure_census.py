#!/usr/bin/env python3
"""Venue-wide structural census — a pre-change baseline for minimum price
increments and the size of combination markets (read-only).

Why it exists
-------------
A venue tightening its minimum price increment at extreme prices by an order of
magnitude is not an API detail. First principles say the fee threshold
`rate * (1 - p)` goes to zero as p approaches 1, while the minimum increment is a
constant. Those two lines must cross, and to the right of the crossing point what
constrains "at which price can this alpha be expressed" stops being the fee and
becomes the increment. Once a change ships, a baseline from before it cannot be
recovered and the two sides of the break are no longer comparable.

It measures three things
------------------------
1. **The distribution of increment structures**, reported twice: by market count,
   and weighted by open interest and volume. The weighted view answers "where is
   the money"; a count is dominated by a long tail of markets with no liquidity.
2. **The actual `price_ranges` of each structure**, archived verbatim. A change
   log's prose and the values actually served have been measured disagreeing, so
   the baseline stores what the machine read, never a human restatement of it.
3. **The share of combination (multivariate) markets**, with a pre-registered
   rule: below 1% it is archived and no longer tracked.

   **The identification method has a trap in it, recorded here.** Filtering by
   event-ticker prefix — the correct way to EXCLUDE parlay legs — matches nothing
   at all when used to COUNT them: zero hits across every open event. Combination
   markets are not in the events tree. They are on-demand contracts under a
   separate collections endpoint, and the prefix lives on the collection or series
   ticker rather than the event ticker. The correct path is to take the series set
   from that endpoint, then query markets by series.

   **A second trap**: with no status filter the response is dominated by finalized
   markets whose open interest is a historical remnant — the same shape as a
   window filling up with dead markets. Count only open status.

Network
-------
Direct connection first, falling back to a configurable egress tunnel. A failure
shape worth knowing about: some networks run an RPZ DNS firewall that hijacks a
venue's whole domain to a placeholder address, transparently redirecting even an
explicitly specified public resolver, and a direct connection is refused instantly.
On such a network the tunnel is the only route. On an unrestricted network a
direct connection is faster and leaves the tunnel's bandwidth alone, so both stay:
hardcode one and it goes blind the moment the network changes.

Discipline: read-only public endpoints, no authentication, no orders, no wallet.
It writes only to its own directory under runtime/.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
# Optional egress proxy; unset means direct only.
TUNNEL_URL = os.environ.get("MARKETFLOW_POLYMARKET_PROXY_URL", "").strip()
USER_AGENT = "marketflow-kalshi-structure-census/0.1 (read-only research)"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "runtime", "trade", "analysis", "kalshi_market_structure_census")

# The authoritative source for combination markets is the dedicated endpoint,
# not a ticker prefix (see the trap recorded in the module docstring).
COLLECTIONS_PATH = "multivariate_event_collections"


def _openers() -> list[urllib.request.OpenerDirector]:
    openers = [urllib.request.build_opener(urllib.request.ProxyHandler({}))]
    if TUNNEL_URL:
        openers.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": TUNNEL_URL, "https": TUNNEL_URL})))
    return openers


def http_json(url: str, *, timeout: float = 30.0, retries: int = 2) -> Any:
    """Try direct, then the configured proxy if any. A 4xx returns None
    immediately: retrying will not change it."""
    last: Exception | None = None
    for attempt in range(retries):
        for opener in _openers():
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    return None
                last = exc
            except Exception as exc:  # noqa: BLE001 - many failure shapes; try the next route
                last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"http_json failed: {url}: {last}")


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_open_events(*, max_pages: int, page_size: int = 200,
                      verbose: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """Paginate fully. Returns (events, truncated). The truncated flag must be
    propagated: silent truncation turns "share of the whole venue" into "share of
    the part I happened to scan" with nothing to warn the reader."""
    events: list[dict[str, Any]] = []
    cursor = ""
    truncated = False
    for page in range(max_pages):
        q: dict[str, Any] = {"status": "open", "with_nested_markets": "true", "limit": page_size}
        if cursor:
            q["cursor"] = cursor
        data = http_json(f"{KALSHI_API}/events?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            break
        batch = data.get("events") or []
        events.extend(batch)
        if verbose:
            print(f"  page {page + 1}: +{len(batch)} events (cum {len(events)})")
        cursor = str(data.get("cursor") or "")
        if not cursor or not batch:
            break
    else:
        truncated = bool(cursor)
    return events, truncated


def fetch_combo_totals(*, max_pages_per_series: int, page_size: int = 1000,
                       verbose: bool = False) -> dict[str, Any]:
    """Size of the active combination-market set. Returns a lower bound plus an
    explicit list of what was truncated: without it, "X% of the venue" reads as an
    exact figure when it is really "at least X%"."""
    series: dict[str, int] = {}
    cursor = ""
    for _ in range(20):
        q: dict[str, Any] = {"limit": 200}
        if cursor:
            q["cursor"] = cursor
        data = http_json(f"{KALSHI_API}/{COLLECTIONS_PATH}?{urllib.parse.urlencode(q)}")
        if not isinstance(data, dict):
            break
        batch = data.get("multivariate_contracts") or []
        for c in batch:
            key = str(c.get("series_ticker") or "")
            if key:
                series[key] = series.get(key, 0) + 1
        cursor = str(data.get("cursor") or "")
        if not cursor or not batch:
            break

    n = oi = v24 = 0.0
    structures: Counter[str] = Counter()
    capped: list[str] = []
    for name in series:
        cur = ""
        for page in range(max_pages_per_series):
            q = {"series_ticker": name, "status": "open", "limit": page_size}
            if cur:
                q["cursor"] = cur
            data = http_json(f"{KALSHI_API}/markets?{urllib.parse.urlencode(q)}")
            if not isinstance(data, dict):
                break
            ms = data.get("markets") or []
            for m in ms:
                n += 1
                oi += _f(m.get("open_interest_fp"))
                v24 += _f(m.get("volume_24h_fp"))
                structures[str(m.get("price_level_structure") or "(missing)")] += 1
            cur = str(data.get("cursor") or "")
            if not cur or not ms:
                break
        else:
            capped.append(name)
        if verbose and n:
            print(f"  combo {name}: cum markets {int(n)}")
    return {
        "n_collections": sum(series.values()),
        "n_series": len(series),
        "n_markets": int(n),
        "open_interest": round(oi, 2),
        "volume_24h": round(v24, 2),
        "tick_structures": dict(structures),
        "series_hit_page_cap": capped,
        "is_lower_bound": bool(capped),
    }


def census(events: list[dict[str, Any]], *, truncated: bool = False,
           combo: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure-function core. Fetching and counting are kept apart so the counting
    is testable at all."""
    rows: list[dict[str, Any]] = []
    for ev in events:
        for m in ev.get("markets") or []:
            rows.append({
                "ticker": str(m.get("ticker") or ""),
                "series": str(ev.get("series_ticker") or ""),
                "structure": str(m.get("price_level_structure") or "(missing)"),
                "price_ranges": m.get("price_ranges"),
                "open_interest": _f(m.get("open_interest_fp")),
                "volume_24h": _f(m.get("volume_24h_fp")),
                "mutually_exclusive": bool(ev.get("mutually_exclusive")),
            })

    def totals(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n_markets": len(subset),
            "open_interest": round(sum(r["open_interest"] for r in subset), 2),
            "volume_24h": round(sum(r["volume_24h"] for r in subset), 2),
        }

    all_t = totals(rows)

    def share(part: float, whole: float) -> float | None:
        return round(part / whole, 6) if whole > 0 else None

    by_structure: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_structure[r["structure"]].append(r)
    structures = {}
    for name, subset in sorted(by_structure.items(), key=lambda kv: -len(kv[1])):
        t = totals(subset)
        structures[name] = {
            **t,
            "market_share": share(t["n_markets"], all_t["n_markets"]),
            "oi_share": share(t["open_interest"], all_t["open_interest"]),
            "volume_share": share(t["volume_24h"], all_t["volume_24h"]),
            # Archive the served values verbatim: a change log's prose has been
            # measured disagreeing with what is actually served, so a restatement
            # cannot be trusted as a baseline.
            "price_ranges_sample": subset[0]["price_ranges"],
            "example_ticker": subset[0]["ticker"],
        }

    combo = combo or {}
    combo_share_oi = share(_f(combo.get("open_interest")), all_t["open_interest"])
    combo_share_vol = share(_f(combo.get("volume_24h")), all_t["volume_24h"])
    # Pre-registered stopping rule: below 1% it is archived and no longer
    # tracked. Take the larger of open interest and volume share; looking at only
    # one gets it wrong whenever a set is small in stock but heavily turned over,
    # or the reverse.
    peak = max([x for x in (combo_share_oi, combo_share_vol) if x is not None], default=0.0)
    return {
        "schema_version": "kalshi-market-structure-census-v0.1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {
            "n_events": len(events), "n_markets": len(rows), "truncated": truncated,
            "note": "when truncated is true, every share below is a share of what\n"
                    "was scanned, not of the venue",
        },
        "totals": all_t,
        "combo_markets": {
            **combo,
            "market_share": share(_f(combo.get("n_markets")), all_t["n_markets"]),
            "oi_share": combo_share_oi,
            "volume_share": combo_share_vol,
            "verdict": ("NOT_MEASURED" if not combo
                        else "BELOW_1PCT_ARCHIVE" if peak < 0.01 else "TRACK"),
            "detection": f"series_ticker set from /{COLLECTIONS_PATH} -> /markets?status=open",
            # Stock and flow must be read separately. Combination markets are
            # generated on demand and keep open interest once anybody has held
            # them, while trading nothing today. Open interest alone reads a dead
            # market as a live one.
            "reading": "oi_share is stock, volume_share is flow; when they diverge, "
                       "trust flow",
        },
        "tick_structures": structures,
        "mutually_exclusive_events": sum(1 for e in events if e.get("mutually_exclusive")),
    }


def write_out(result: dict[str, Any]) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%y%m%d_%H%M%S")
    path = os.path.join(OUT_DIR, f"census_{ts}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    return path


def selftest() -> int:
    ok = 0

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            raise AssertionError(f"selftest FAIL: {name}")
        ok += 1
        print(f"  ok {ok:02d} {name}")

    evs = [
        {"event_ticker": "KXABC", "series_ticker": "KXABC", "mutually_exclusive": False,
         "markets": [
             {"ticker": "A1", "price_level_structure": "linear_cent", "open_interest_fp": "1000",
              "volume_24h_fp": "500", "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]},
             {"ticker": "A2", "price_level_structure": "tapered_deci_cent", "open_interest_fp": "10",
              "volume_24h_fp": "9000", "price_ranges": [{"start": "0.0000", "end": "0.1000", "step": "0.0010"}]},
         ]},
    ]
    r = census(evs, combo={"n_markets": 3, "open_interest": 5.0, "volume_24h": 0.0})
    check("venue totals count only markets in the event tree", r["totals"]["n_markets"] == 2)
    check("open-interest share is by value, not by count",
          abs(r["combo_markets"]["oi_share"] - 5 / 1010) < 1e-6)
    check("under 1% stock with no flow -> archive", r["combo_markets"]["verdict"] == "BELOW_1PCT_ARCHIVE")
    check("combination counting does not come from an event prefix (that test "
          "matched zero events in practice)",
          "multivariate_event_collections" in r["combo_markets"]["detection"])
    check("unmeasured reports NOT_MEASURED rather than a 0 meaning \"none exist\"",
          census(evs)["combo_markets"]["verdict"] == "NOT_MEASURED")
    hot = census(evs, combo={"n_markets": 9, "open_interest": 1.0, "volume_24h": 5000.0})
    check("tiny stock but dominant flow still yields TRACK (larger of the two)",
          hot["combo_markets"]["verdict"] == "TRACK")
    check("increment distribution is sorted by market count and carries weighted shares",
          list(r["tick_structures"])[0] == "linear_cent"
          and abs(r["tick_structures"]["tapered_deci_cent"]["volume_share"] - 9000 / 9500) < 1e-6)
    check("increment definitions are archived verbatim",
          r["tick_structures"]["tapered_deci_cent"]["price_ranges_sample"][0]["step"] == "0.0010")
    check("a missing price_level_structure is recorded as (missing), not dropped",
          census([{"event_ticker": "K", "markets": [{"ticker": "N", "open_interest_fp": "1"}]}])
          ["tick_structures"]["(missing)"]["n_markets"] == 1)
    check("zero totals leave shares as None rather than dividing by zero",
          census([{"event_ticker": "K", "markets": [{"ticker": "Z", "price_level_structure": "s"}]}],
                 combo={"n_markets": 1, "open_interest": 0.0, "volume_24h": 0.0})
          ["combo_markets"]["oi_share"] is None)
    check("truncated is propagated honestly", census([], truncated=True)["coverage"]["truncated"] is True)
    print(f"selftest PASS {ok}/{ok}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Price-increment structure and combination-market census (read-only)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--combo-max-pages", type=int, default=40,
                    help="page cap per combination series; hitting it lists that series "
                         "in the output and marks is_lower_bound")
    ap.add_argument("--skip-combo", action="store_true",
                    help="measure increment structure only; skip combination markets")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    events, truncated = fetch_open_events(max_pages=args.max_pages, verbose=args.verbose)
    combo = None if args.skip_combo else fetch_combo_totals(
        max_pages_per_series=args.combo_max_pages, verbose=args.verbose)
    result = census(events, truncated=truncated, combo=combo)
    path = write_out(result)
    print(json.dumps({k: result[k] for k in ("coverage", "totals", "combo_markets")},
                     ensure_ascii=False, indent=1))
    print("\nPrice-increment structure distribution:")
    for name, s in result["tick_structures"].items():
        print(f"  {name:<24} n={s['n_markets']:>5} ({(s['market_share'] or 0)*100:5.1f}%) "
              f"OI={(s['oi_share'] or 0)*100:5.1f}%  vol24h={(s['volume_share'] or 0)*100:5.1f}%")
    print(f"\nout: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
