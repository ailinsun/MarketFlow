"""Public read-only Polymarket data layer (standard library only).

Two unauthenticated public endpoints:

- `positions?user=<addr>` — the current positions of any address, one row per
  outcome, carrying entry price, current price, percent PnL, end date, title and
  condition id. This is the core source: drawdown, approaching-resolution and
  new-position alerts all come from it.
- `markets?condition_ids=<cid>` — market metadata: dispute and resolution state,
  and whether a market is active, closed or archived. Fetched only when a dispute
  verdict is actually needed.

No secret, and no coupling to the execution stack. Every function is read-only,
idempotent and fails soft: a network problem raises DataError and the caller
decides whether to swallow it and retry.
"""

from __future__ import annotations

import datetime
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# 0x plus 40 hex, case-insensitive. An EIP-55 checksummed address is accepted as
# well; these are read-only queries, so the checksum is not verified.
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_UA = "MarketFlow-Alerts/0.1 (+read-only Polymarket monitor)"

# Force a direct connection. The public endpoints work directly, and a data layer
# must never inherit a proxy from whatever environment it happens to run in: that
# is how a service dies quietly when an unrelated proxy does. An empty
# ProxyHandler overrides any *_proxy environment variable.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DataError(Exception):
    """A public-endpoint fetch failed: network, HTTP or JSON."""


def is_wallet_address(value: Any) -> bool:
    """Whether this looks like an EVM address: 0x plus 40 hex."""
    return isinstance(value, str) and bool(_ADDR_RE.match(value.strip()))


def normalize_address(value: str) -> str:
    """Normalise to a lowercase address. The API is case-insensitive; lowercase is
    used internally as the key."""
    return value.strip().lower()


def _http_get_json(url: str, *, timeout: float = 20.0, retries: int = 2) -> Any:
    """GET a URL and return parsed JSON; raise DataError after retries fail.

    Deliberately a local implementation rather than an import — urllib, a UA
    header and backoff — so this package keeps its zero-import boundary against
    the execution stack.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last_error: str | None = None
    for attempt in range(retries + 1):
        try:
            with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read().decode("utf-8")
                if status < 200 or status >= 300:
                    raise DataError(f"GET {url}: HTTP {status}")
                return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
    raise DataError(f"GET {url}: failed after {retries + 1} attempts: {last_error}")


def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def fetch_positions(
    address: str,
    *,
    size_threshold: float = 1.0,
    limit: int = 500,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Fetch one address's current positions (public, unauthenticated).

    size_threshold : ignore dust positions below this many shares (default 1).
    Returns the rows as-is. The fields that matter:
      proxyWallet, asset(token id), conditionId, size, avgPrice, curPrice,
      currentValue, initialValue, cashPnl, percentPnl, title, slug, outcome,
      outcomeIndex, endDate, redeemable, negativeRisk,
      grossInitialValue / entryFeesUsdc — a fee-inclusive cost basis, which may
      be absent.
    An address with no positions returns [].
    """
    if not is_wallet_address(address):
        raise DataError(f"not a wallet address: {address!r}")
    q = urllib.parse.urlencode(
        {
            "user": address.strip(),
            "sizeThreshold": size_threshold,
            "limit": limit,
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
        }
    )
    data = _http_get_json(f"{DATA_API}/positions?{q}", timeout=timeout)
    if isinstance(data, dict) and "error" in data:
        raise DataError(f"data-api positions error: {data['error']}")
    if not isinstance(data, list):
        raise DataError(f"data-api positions: unexpected shape {type(data).__name__}")
    return [p for p in data if isinstance(p, dict)]


def position_key(pos: dict[str, Any]) -> str:
    """A stable key for one position: the asset token id, unique per outcome."""
    asset = str(pos.get("asset") or "").strip()
    if asset:
        return asset
    # fallback: conditionId + outcomeIndex
    return f"{pos.get('conditionId','')}:{pos.get('outcomeIndex','')}"


def normalize_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw position row to the clean fields the rule engine needs, with
    numerics coerced to float."""
    return {
        "key": position_key(pos),
        "conditionId": str(pos.get("conditionId") or "").strip(),
        "title": str(pos.get("title") or pos.get("slug") or "(unknown market)").strip(),
        "slug": str(pos.get("slug") or "").strip(),
        "outcome": str(pos.get("outcome") or "").strip(),
        "outcomeIndex": int(pos["outcomeIndex"]) if str(pos.get("outcomeIndex", "")).strip().isdigit() else None,
        "size": _to_float(pos.get("size")) or 0.0,
        "avgPrice": _to_float(pos.get("avgPrice")),
        "curPrice": _to_float(pos.get("curPrice")),
        "currentValue": _to_float(pos.get("currentValue")) or 0.0,
        "initialValue": _to_float(pos.get("initialValue")) or 0.0,
        # Two fee-inclusive cost-basis fields. The plain initialValue and avgPrice
        # **exclude** entry fees, and the fee-exclusive basis is
        # grossInitialValue - entryFeesUsdc. What gets left out is exactly the
        # break-even threshold rate * (1 - p), which at mid prices is a multiple of
        # the whole market's gross buy-side alpha — a question of the sign of PnL,
        # not of display precision.
        #
        # **Absent stays None; it is never filled with 0.** The API documents an
        # omitted field as unavailable rather than zero, and writing 0 would make
        # "this market charges no fee" and "this row lacks the field"
        # indistinguishable downstream.
        "grossInitialValue": _to_float(pos.get("grossInitialValue")),
        "entryFeesUsdc": _to_float(pos.get("entryFeesUsdc")),
        "cashPnl": _to_float(pos.get("cashPnl")),
        "percentPnl": _to_float(pos.get("percentPnl")),
        "endDate": str(pos.get("endDate") or "").strip(),
        "redeemable": bool(pos.get("redeemable")),
        "negativeRisk": bool(pos.get("negativeRisk")),
    }


def fetch_market_meta(condition_ids: list[str], *, timeout: float = 20.0) -> dict[str, dict[str, Any]]:
    """Batch-fetch market metadata by condition id; returns {conditionId: meta}.

    Used only for dispute and resolution verdicts. Empty input returns {}. The
    endpoint accepts several condition ids per call.
    """
    cids = [c.strip() for c in condition_ids if c and str(c).strip()]
    if not cids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    # The endpoint limits query length, so batch the ids.
    for i in range(0, len(cids), 20):
        batch = cids[i : i + 20]
        params = [("condition_ids", c) for c in batch]
        url = f"{GAMMA_API}/markets?{urllib.parse.urlencode(params)}"
        try:
            data = _http_get_json(url, timeout=timeout)
        except DataError:
            continue  # dispute verdicts are best effort; a failed fetch is skipped
                      # and price alerts are unaffected
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for m in data:
            if isinstance(m, dict):
                cid = str(m.get("conditionId") or "").strip()
                if cid:
                    out[cid] = m
    return out


def _parse_json_field(value: Any) -> Any:
    """The API serialises list fields as JSON strings; parse defensively."""
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


# The keyword-search path against the public search endpoint is retired in full.
# Search now runs against `market_catalog`'s local index: no network call, and no
# black-box recall to compensate for with hand-written synonym, stopword and
# fallback tables. What remains here is position and metadata fetching.


def _event_outcomes(ev: dict[str, Any]) -> list[tuple[float, str]]:
    """One event -> [(current Yes price, outcome label)], sorted by Yes price
    descending, active and unclosed markets only.

    The single source of truth for "how to read an event". All three views in
    `market_board` go through it, so nothing parses out a different market set
    somewhere else.
    """
    rows: list[tuple[float, str]] = []
    for m in ev.get("markets") or []:
        if not isinstance(m, dict) or not m.get("active") or m.get("closed"):
            continue
        prices = _parse_json_field(m.get("outcomePrices"))
        yes = _to_float(prices[0]) if isinstance(prices, list) and prices else None
        if yes is None:
            continue
        label = str(m.get("groupItemTitle") or m.get("question") or "").strip()
        if label:
            rows.append((yes, label))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


# ---------------------------------------------- public facts (displayable observations)
#
# Boundary, hard: this section emits **public facts only** — the trades and books
# anybody can see on the venue's own site. No opinion of this system's own
# (beliefs, effective probabilities, edge rankings, thresholds) belongs in this
# layer, and none ever leaves through it.


def _iso_to_epoch(value: Any) -> float | None:
    """ISO 8601 with a trailing Z -> epoch seconds; None when unparseable."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def active_market_count(*, timeout: float = 10.0) -> int | None:
    """The **exact** number of active events venue-wide, taken from the endpoint's
    own pagination count.

    It exists for honesty. A single events call returns at most a page, and the
    offset has a hard ceiling, so presenting a sample as "the whole venue" would be
    a lie. The total is one number the API will simply tell us, so the total is
    reported as truth while volume and open interest are reported only within an
    explicitly labelled sample. Unavailable returns None, and a consumer shows an
    em dash rather than inventing a figure.
    """
    url = f"{GAMMA_API}/events/pagination?" + urllib.parse.urlencode(
        {"limit": 1, "active": "true", "closed": "false"})
    try:
        doc = _http_get_json(url, timeout=timeout, retries=1)
    except DataError:
        return None
    if not isinstance(doc, dict):
        return None
    n = ((doc.get("pagination") or {}) if isinstance(doc.get("pagination"), dict) else {}).get("totalResults")
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def market_board(
    *, timeout: float = 15.0, sample: int = 100, rows: int = 8,
    max_outcomes: int = 3, resolving_within_h: float = 72.0,
    movers_min_vol: float = 100_000.0, now: float | None = None,
) -> dict[str, Any]:
    """One fetch -> three board views plus sample-scoped aggregates.

    The three views deliberately share a single batch of events, and a single HTTP
    call, because they are three cuts of the same reality:
      `active`    busiest by 24h volume — where the money is
      `movers`    largest 24h repricing at market level — what is changing
      `resolving` settling within N hours with open interest — what is about to pay

    `totals` covers **only the batch that was fetched**, and carries `sample` and
    `sample_note` so a caller can label the scope honestly. It never presents
    itself as venue-wide; the venue-wide count comes from `active_market_count()`.

    A network failure or a malformed response yields three empty lists and None
    totals, so a consumer shows an em dash rather than a fabricated number.
    """
    now = now if now is not None else time.time()
    empty = {"active": [], "movers": [], "resolving": [], "totals": None, "sample": 0}
    url = (
        f"{GAMMA_API}/events?"
        + urllib.parse.urlencode({
            # A single call is capped at one page, and going further needs
            # pagination with its own offset ceiling. A board does not need the
            # whole venue: the head of a 24h-volume ranking already covers most
            # real activity.
            "limit": max(1, min(sample, 100)),
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        })
    )
    try:
        doc = _http_get_json(url, timeout=timeout, retries=1)
    except DataError:
        return empty
    if not isinstance(doc, list) or not doc:
        return empty

    events = [e for e in doc if isinstance(e, dict)]
    active: list[dict[str, Any]] = []
    movers: list[dict[str, Any]] = []
    resolving: list[dict[str, Any]] = []
    vol24h_sum = oi_sum = liq_sum = 0.0

    for ev in events:
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        vol24h = _to_float(ev.get("volume24hr")) or 0.0
        liq = _to_float(ev.get("liquidity")) or 0.0
        oi = _to_float(ev.get("openInterest")) or 0.0
        vol24h_sum += vol24h
        oi_sum += oi
        liq_sum += liq
        ends_at = _iso_to_epoch(ev.get("endDate"))
        outs = _event_outcomes(ev)

        if outs:
            active.append({
                "title": title,
                "slug": str(ev.get("slug") or ""),
                "vol24h": vol24h, "liquidity": liq, "oi": oi,
                "ends_at": int(ends_at) if ends_at else None,
                "n_outcomes": len(outs),
                "outcomes": [{"label": lb, "price": p} for p, lb in outs[:max_outcomes]],
            })

        # movers reads at market level: a move belongs to one outcome, not to the
        # whole event
        for m in ev.get("markets") or []:
            if not isinstance(m, dict) or not m.get("active") or m.get("closed"):
                continue
            chg = _to_float(m.get("oneDayPriceChange"))
            price = _to_float(m.get("lastTradePrice"))
            mv24 = _to_float(m.get("volume24hr")) or 0.0
            # A volume floor. A tiny market travelling from a few cents to nearly
            # one is not a repricing, it is same-day convergence, and without the
            # floor a single category of short-dated markets fills the whole board.
            # The threshold gates on liquidity, not on subject matter.
            if chg is None or price is None or mv24 < movers_min_vol:
                continue
            movers.append({
                "title": title,
                "slug": str(ev.get("slug") or ""),
                "outcome": str(m.get("groupItemTitle") or m.get("question") or "").strip(),
                "price": price, "change24h": chg, "vol24h": mv24,
            })

        if ends_at and 0 < ends_at - now <= resolving_within_h * 3600 and oi > 0:
            resolving.append({
                "title": title,
                "slug": str(ev.get("slug") or ""),
                "ends_at": int(ends_at), "oi": oi, "vol24h": vol24h, "liquidity": liq,
                "outcomes": [{"label": lb, "price": p} for p, lb in outs[:max_outcomes]],
            })

    movers.sort(key=lambda r: abs(r["change24h"]), reverse=True)
    resolving.sort(key=lambda r: r["ends_at"])
    return {
        "active": active[:rows],
        "movers": movers[:rows],
        "resolving": resolving[:rows],
        "totals": {"vol24h": vol24h_sum, "open_interest": oi_sum, "liquidity": liq_sum},
        "sample": len(events),
    }


def large_trades(
    *, timeout: float = 12.0, min_usd: float = 10_000.0, limit: int = 8, per_market: int = 2
) -> list[dict[str, Any]]:
    """Recent large prints -> structured rows.

    Each row is {usd, side, price, title, outcome, slug, ts} — **flow only, no
    people**. The upstream trades endpoint also returns trader identity: wallet,
    pseudonym, name, bio, avatar. Every one of those fields is dropped here. A
    trade is a public fact; republishing a third party's identity alongside it is
    a different thing entirely. The transaction hash is dropped for the same
    reason — it is a direct route back to a wallet and adds nothing to a board.

    `per_market` caps how many rows one market may occupy. A single popular event
    can produce a run of large prints that fills the entire tape; capping it means
    the same space shows more distinct markets. That is more information, not
    less.
    """
    url = (
        f"{DATA_API}/trades?"
        + urllib.parse.urlencode({
            "filterType": "CASH", "filterAmount": int(max(min_usd, 0)),
            # The per_market fold discards a lot of rows, so over-fetch upstream;
            # otherwise a quiet period cannot fill `limit` distinct markets.
            "takerOnly": "true", "limit": max(limit * 10, 60), "offset": 0,
        })
    )
    try:
        doc = _http_get_json(url, timeout=timeout, retries=1)
    except DataError:
        return []
    if not isinstance(doc, list):
        return []
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for t in doc:
        if not isinstance(t, dict):
            continue
        size = _to_float(t.get("size"))
        price = _to_float(t.get("price"))
        title = str(t.get("title") or "").strip()
        if size is None or price is None or not title:
            continue
        usd = size * price
        if usd < min_usd:
            continue
        key = title.lower()
        if seen.get(key, 0) >= per_market:
            continue
        seen[key] = seen.get(key, 0) + 1
        side = str(t.get("side") or "").strip().upper()
        out.append({
            "usd": round(usd, 2),
            "side": side if side in ("BUY", "SELL") else "",
            "price": price,
            "title": title,
            "outcome": str(t.get("outcome") or "").strip(),
            "slug": str(t.get("eventSlug") or t.get("slug") or ""),
            "ts": int(_to_float(t.get("timestamp")) or 0),
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------- market watch (watch by link)

# /event/<event-slug>[/<market-slug>] or /market/<market-slug>. The slug character
# class naturally stops before a query string or fragment.
_PM_LINK_RE = re.compile(
    r"polymarket\.com/(event|market)/([a-z0-9_-]+)(?:/([a-z0-9_-]+))?", re.IGNORECASE
)


def extract_market_link(text: str) -> tuple[str | None, str | None]:
    """Extract a venue link from a message. Returns (event_slug, market_slug), or
    (None, None) when there is no link."""
    m = _PM_LINK_RE.search(text or "")
    if not m:
        return None, None
    kind, first, second = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "").lower()
    if kind == "market":
        return None, first
    return first, second or None


def market_rec(m: dict[str, Any]) -> dict[str, Any]:
    """A market object -> the minimal record stored per watched market."""
    prices = _parse_json_field(m.get("outcomePrices"))
    yes = _to_float(prices[0]) if isinstance(prices, list) and prices else None
    return {
        "id": str(m.get("id") or ""),
        "conditionId": str(m.get("conditionId") or ""),
        "slug": str(m.get("slug") or ""),
        "question": str(m.get("question") or m.get("groupItemTitle") or "").strip(),
        "yes_price": yes,
        "end_date": str(m.get("endDate") or ""),
    }


def resolve_market_ref(
    event_slug: str | None, market_slug: str | None, *, timeout: float = 15.0, max_candidates: int = 5
) -> dict[str, Any] | None:
    """Resolve link slugs into something watchable (read-only).

    Returns {"kind": "market", "market": rec} for a single market, or
    {"kind": "event", "title": ..., "candidates": [rec, ...]} for a multi-market
    event where the reader picks, or None when nothing matches or the fetch fails
    (fail soft).
    """
    if market_slug:
        try:
            data = _http_get_json(
                f"{GAMMA_API}/markets?{urllib.parse.urlencode({'slug': market_slug})}",
                timeout=timeout, retries=1,
            )
        except DataError:
            return None
        for m in data if isinstance(data, list) else []:
            if isinstance(m, dict) and m.get("conditionId") and m.get("active") and not m.get("closed"):
                return {"kind": "market", "market": market_rec(m)}
    if event_slug:
        try:
            data = _http_get_json(
                f"{GAMMA_API}/events?{urllib.parse.urlencode({'slug': event_slug})}",
                timeout=timeout, retries=1,
            )
        except DataError:
            return None
        for ev in data if isinstance(data, list) else []:
            if not isinstance(ev, dict):
                continue
            recs = [
                market_rec(m)
                for m in (ev.get("markets") or [])
                if isinstance(m, dict) and m.get("active") and not m.get("closed") and m.get("conditionId")
            ]
            recs = [r for r in recs if r["id"]]
            if not recs:
                continue
            if len(recs) == 1:
                return {"kind": "market", "market": recs[0]}
            recs.sort(key=lambda r: r["yes_price"] or 0.0, reverse=True)
            return {
                "kind": "event",
                "title": str(ev.get("title") or event_slug).strip(),
                "candidates": recs[:max_candidates],
            }
    return None


def fetch_market_by_gamma_id(gamma_id: str, *, timeout: float = 15.0) -> dict[str, Any] | None:
    """Fetch one market by numeric id, for the event-candidate callback. None on
    failure."""
    gid = str(gamma_id).strip()
    if not gid.isdigit():
        return None
    try:
        data = _http_get_json(f"{GAMMA_API}/markets/{gid}", timeout=timeout, retries=1)
    except DataError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def market_dispute_status(meta: dict[str, Any]) -> dict[str, Any]:
    """Decide from market metadata whether a market is disputed or settled.

    Returns {disputed: bool, statuses: [...], closed: bool, archived: bool}. A
    dispute shows up as a token in the resolution-status list. Fail soft: a missing
    field counts as disputed=False.
    """
    statuses_raw = _parse_json_field(meta.get("umaResolutionStatuses"))
    statuses = [str(x).strip().lower() for x in statuses_raw] if isinstance(statuses_raw, list) else []
    disputed = any("disput" in s for s in statuses) or bool(meta.get("disputed"))

    def _truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in {"true", "1", "yes"}

    return {
        "disputed": bool(disputed),
        "statuses": statuses,
        "closed": _truthy(meta.get("closed")),
        "archived": _truthy(meta.get("archived")),
    }


if __name__ == "__main__":
    # manual smoke test: python -m marketflow.monitor.polymarket_data <address>
    import sys

    addr = sys.argv[1] if len(sys.argv) > 1 else ""
    if not is_wallet_address(addr):
        print("usage: python -m marketflow.monitor.polymarket_data 0x<40hex>")
        raise SystemExit(2)
    ps = fetch_positions(addr)
    print(f"{addr}: {len(ps)} positions")
    for p in ps[:5]:
        n = normalize_position(p)
        print(
            f"  {n['title'][:50]:50s} {n['outcome']:6s} "
            f"avg={n['avgPrice']} cur={n['curPrice']} pnl%={n['percentPnl']} end={n['endDate']}"
        )
