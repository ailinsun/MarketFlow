"""Local market catalogue — an owned index of the venue's active markets
(standard library only).

**Why it exists.** Keyword search against a venue's own public search endpoint
means guessing what somebody meant, once per query, propped up by hand-written
synonym and stopword tables. Coverage is never complete, the long tail always
fails, and every guess costs a network round trip.

The first-principles facts that make a local index better:

- Matching needs only the title, tags, slug, volume and end date. Trimmed to those,
  one record is under 250 bytes, while a full event payload is two orders of
  magnitude larger. The whole catalogue fits locally without effort.
- Search therefore does not have to be a network call. It can be an in-memory
  lookup: no latency, no failure mode, and re-rankable on any dimension —
  relevance, volume, category — none of which is possible through somebody else's
  opaque keyword endpoint.

**Partitioned fetching, and a trap worth not repeating.** The events endpoint caps
`offset` in the low thousands, while the real number of active events is an order
of magnitude larger — the pagination endpoint's total is the honest figure. Naive
pagination therefore sees a fraction of the world, and **it does not error: it
just stops silently at the last page it will serve**.

This module slices queries by end-date window until each slice fits under the cap,
paginating each one separately and bisecting adaptively. Coverage is reported
honestly by `stats()`: incomplete is incomplete, never dressed up as complete.

**A catalogue is not a quote.** Prices in it are from the last refresh and exist to
sort and to fall back on. Anything actually shown to a reader is fetched live, in
batches, behind a short TTL cache. When that fails, say plainly that the price is
minutes old — a stale price can be labelled, an invented one cannot.

**Scope boundary, for whoever wires this next: do not add intent detection here.**
This module does literal matching and does not judge whether a sentence is even
asking about a market. A real example: "how much do you charge" matches "How Much
Will [player]'s Next Contract Be?" — as retrieval that is the correct literal
match, and what was wrong is running a non-market question through retrieval at
all.

Intent belongs to the caller. Let the model decide whether to search, as a tool
call, and it will not look up a billing question in a market index; the problem
disappears at the root. Adding a "does this look like a market question" keyword
test here just rewrites the patch layer somewhere else.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

# ------------------------------------------------------------- fetch adapter
#
# This module carries its own HTTP rather than reusing the data layer, for two
# independently sufficient reasons:
#   1. A host that loads these modules individually by file path has no flat import
#      available, so depending on a sibling would leave it with no catalogue at all.
#   2. A catalogue needs pagination semantics — the page-size ceiling, the offset
#      cap and its error, the pagination total — which a position-and-metadata
#      layer neither has nor should have.
# What it shares is the same hard rule: force a direct connection, with an empty
# ProxyHandler overriding any *_proxy environment variable. Coupling a public data
# read to a proxy means the service dies whenever that proxy does.

GAMMA_API = "https://gamma-api.polymarket.com"
_UA = "MarketFlow-Alerts/0.1 (+read-only Polymarket catalog)"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Gamma:
    """The catalogue's fetch surface: transport only, no matching. Inject a double
    and the tests run fully offline."""

    def __init__(self, *, timeout: float = 25.0, pause: float = 0.05) -> None:
        self.timeout = timeout
        self.pause = pause          # a small pause between pages, out of politeness
        self.calls = 0

    def _get(self, path: str, params: Any) -> Any:
        url = f"{GAMMA_API}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        self.calls += 1
        try:
            with _DIRECT_OPENER.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError, OSError):
            return None

    def fetch_events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc = self._get("events", params)
        if self.pause:
            time.sleep(self.pause)
        return [e for e in doc if isinstance(e, dict)] if isinstance(doc, list) else []

    def fetch_events_by_id(self, ids: list[str]) -> list[dict[str, Any]]:
        doc = self._get("events", [("id", str(i)) for i in ids])
        return [e for e in doc if isinstance(e, dict)] if isinstance(doc, list) else []

    def count_events(self, **filters: Any) -> int | None:
        """The true number of active events. This is the **only** place the real
        scale is visible: the events endpoint stops at its offset cap without
        erroring, so counting pages would conclude the world is that size."""
        params = {"active": "true", "closed": "false", "limit": 1, "offset": 0}
        params.update({k: v for k, v in filters.items() if v is not None})
        doc = self._get("events/pagination", params)
        page = doc.get("pagination") if isinstance(doc, dict) else None
        total = (page or {}).get("totalResults")
        return int(total) if isinstance(total, (int, float)) else None


# ------------------------------------------------------------------ storage

_HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(
    os.path.dirname(_HERE), "runtime", "alerts", "market_catalog.json"
)

# Refresh cadence. **A full rebuild must never sit on a request path**: it runs to
# roughly a hundred pages and takes minutes, so it belongs on a background thread.
# An incremental refresh takes a few pages each of the newest and busiest slices,
# which is exactly what covers the only staleness a reader notices: a market listed
# since the last rebuild. Delisted markets need no incremental chase — the live
# quote step discovers those naturally.
FULL_TTL_SEC = 6 * 3600
DELTA_TTL_SEC = 300
BOOTSTRAP_PAGES = 4          # pages per slice on a cold start: usable first, complete later
PAGE_SIZE = 100              # the endpoint's ceiling; a larger request is silently truncated
GAMMA_OFFSET_CAP = 2100      # beyond this the endpoint returns an error outright
SLICE_MAX = 2000             # bisect the window above this, leaving room under the cap

# Scope of the settled index. The full settled set runs to hundreds of thousands of
# events — tens of thousands in a single week, mostly hourly crypto candles and
# individual fixtures — so keeping all of it locally is not realistic. These two
# numbers are **importance thresholds against real magnitudes**, not guesses about
# language: nobody asks who won a market that traded under a few hundred thousand
# dollars. The resulting slice is a few thousand records, and its head is exactly
# the set people actually ask about.
SETTLED_WINDOW_DAYS = 180
SETTLED_MIN_VOLUME = 500_000

# Scoring parameters, all **relative** and scaled by log(N), so they need no
# retuning as the catalogue grows.
_DISTINCT_RATIO = 0.52       # idf above this share of log(N) counts as naming something specific
_MASS_RATIO = 1.5            # or several medium terms summing to this much information
_TAG_WEIGHT = 0.6            # a tag hit scores partially: same category is not the same market
_TITLE_PHRASE_BONUS = 3.0    # the whole phrase verbatim in the title: the strongest signal
# What share of the title the match covers. A title that is almost entirely the
# queried phrase is more likely what somebody asking for it wants than a long title
# where the phrase is a small fragment. This is the standard document-coverage term
# in retrieval, not a heuristic patch.
_TITLE_FIT = 2.0
# Volume is an **additive** prior, not a multiplicative one. Multiplying scales it
# with relevance, letting a busy market outrank a quieter one that matches the words
# better — a query naming one country's leader losing to another country's, purely
# on volume.
#
# The magnitude is normalised against the index's own maximum. A fixed ceiling
# destroys discrimination in an index spanning four orders of magnitude: the
# largest market and a minor fixture score identically, and a query for the former
# returns the latter. Normalised, the span adapts, and it stays far below the weight
# of one distinctive term — so it breaks ties between close matches and never
# overtakes a genuinely better one.
_HOT_SPAN = 2.0

_TOKEN_RE = re.compile(r"[^0-9a-z]+")


def _tokens(text: str) -> list[str]:
    """Text -> retrieval tokens: alphanumeric runs of length two or more.

    Single characters are dropped, being far more noise than information, while two
    and three character runs are kept: ai, us, uk, btc, fed are all real terms.

    Non-Latin scripts produce no tokens here, and that is **correct**. Catalogue
    titles are English, so a query in another script can never match one literally.
    The cross-language step belongs to a model that already speaks both, not to a
    hand-written lookup table — a table is a patch, and its long tail always leaks.
    """
    return [t for t in _TOKEN_RE.split((text or "").lower()) if len(t) >= 2]


def _record(ev: dict[str, Any]) -> dict[str, Any] | None:
    """An event -> a trimmed catalogue record. Field names are short deliberately:
    across tens of thousands of records every byte counts."""
    title = str(ev.get("title") or "").strip()
    eid = str(ev.get("id") or "").strip()
    if not title or not eid:
        return None
    outs: list[list[Any]] = []
    for m in ev.get("markets") or []:
        if not isinstance(m, dict) or not m.get("active") or m.get("closed"):
            continue
        prices = _parse_prices(m.get("outcomePrices"))
        if not prices:
            continue
        label = str(m.get("groupItemTitle") or m.get("question") or "").strip()
        if label:
            outs.append([round(prices[0], 4), label])
    if not outs:
        return None
    outs.sort(key=lambda r: r[0], reverse=True)
    tags = [str(t.get("label") or "").strip()
            for t in (ev.get("tags") or []) if isinstance(t, dict)]
    return {
        "i": eid,
        "t": title,
        "s": str(ev.get("slug") or ""),
        "g": [g for g in tags if g][:8],
        "v": round(_f(ev.get("volume24hr")), 1),
        "l": round(_f(ev.get("liquidity")), 1),
        "e": str(ev.get("endDate") or ""),
        "o": outs[:12],
    }


def _settled_record(ev: dict[str, Any]) -> dict[str, Any] | None:
    """A closed event -> which legs settled to what. **It does not force a "who
    won" framing.**

    Only legs that are closed and whose price has snapped to an endpoint are
    admitted: that is settled on-chain fact. An unsettled leg never enters, because
    its price is a probability rather than a result.

    Three shapes coexist, and being structurally honest about them is mandatory:
      * mutually exclusive groups: exactly one leg settles Yes, which reads as a
        winner;
      * threshold ladders: several legs settle Yes at once, because the value
        crossed several levels, so there is no single winner at all;
      * single binary markets whose outcomes are not Yes/No but a pair of labels.

    So this reports facts and lets the rendering layer choose wording from the leg
    count. `nno` is the number of legs that settled No: with all of them No it can
    say what did not happen, and cannot say what did.
    """
    title = str(ev.get("title") or "").strip()
    eid = str(ev.get("id") or "").strip()
    if not title or not eid:
        return None
    rows: list[list[str]] = []
    n_no = 0
    for m in ev.get("markets") or []:
        if not isinstance(m, dict) or not m.get("closed"):
            continue
        prices = _parse_prices(m.get("outcomePrices"))
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = None
        if not prices or not isinstance(outcomes, list):
            continue
        top = max(range(len(prices)), key=lambda i: prices[i])
        if prices[top] < 0.99:
            continue          # not yet at an endpoint: unsettled, never reported as a result
        won = str(outcomes[top]).strip() if top < len(outcomes) else ""
        if not won:
            continue
        if won.lower() == "no":
            n_no += 1
            continue
        label = str(m.get("groupItemTitle") or m.get("question") or "").strip()
        if label:
            rows.append([label, won])
    if not rows and not n_no:
        return None
    tags = [str(t.get("label") or "").strip()
            for t in (ev.get("tags") or []) if isinstance(t, dict)]
    return {
        "i": eid,
        "t": title,
        "s": str(ev.get("slug") or ""),
        "g": [g for g in tags if g][:8],
        "v": round(_f(ev.get("volume")), 1),      # settled records use lifetime volume
        "e": str(ev.get("closedTime") or ev.get("endDate") or "")[:10],
        "w": rows[:12],
        "nno": n_no,
        "o": [],                                   # same shape as a live record, so one parser serves both
    }


def _f(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def _parse_prices(value: Any) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    out = []
    for x in value:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return []
    return out


# -------------------------------------------------------------------- index


class Index:
    """An immutable catalogue snapshot with its inverted index. Replacing the index
    replaces the whole object, so the read path needs no lock."""

    def __init__(self, records: list[dict[str, Any]], *, built_at: float,
                 full_at: float = 0.0, total_upstream: int = 0) -> None:
        self.records = records
        self.built_at = built_at
        self.full_at = full_at
        self.total_upstream = total_upstream
        self.by_id = {r["i"]: r for r in records}
        self._title_tokens: list[set[str]] = []
        df: dict[str, int] = {}
        self._postings: dict[str, list[int]] = {}
        for i, r in enumerate(records):
            title = set(_tokens(r["t"]))
            tags = {tok for g in r["g"] for tok in _tokens(g)}
            toks = title | set(_tokens(r["s"].replace("-", " "))) | tags
            self._title_tokens.append(title)
            for tk in toks:
                df[tk] = df.get(tk, 0) + 1
                self._postings.setdefault(tk, []).append(i)
        n = max(len(records), 1)
        self._log_vmax = max(
            math.log1p(max((r.get("v") or 0.0) for r in records)) if records else 1.0, 1.0)
        # idf = log(N / (1 + df)). This also **replaces a hand-written stopword
        # list**: common words are frequent in the catalogue and their idf tends to
        # zero on its own. What counts as filler is decided by the data rather than
        # enumerated by hand, so "the list did not include it" cannot be a failure
        # mode.
        self._idf = {tk: math.log(n / (1.0 + c)) for tk, c in df.items()}
        self.max_idf = math.log(n)
        self.distinct_floor = _DISTINCT_RATIO * self.max_idf
        cats: dict[str, int] = {}
        for r in records:
            for g in r["g"]:
                cats[g] = cats.get(g, 0) + 1
        self.category_counts = cats

    def __len__(self) -> int:
        return len(self.records)

    def idf(self, token: str) -> float:
        return self._idf.get(token, 0.0)

    def _hot(self, i: int) -> float:
        """Volume prior: additive, normalised against this index's maximum. It
        favours the larger market only when relevance is close."""
        return _HOT_SPAN * math.log1p(max(self.records[i]["v"], 0.0)) / self._log_vmax

    def score(self, query: str, *, category: str | None = None,
              limit: int = 5) -> tuple[list[dict[str, Any]], bool]:
        """Search -> (matching records, whether this counts as an exact match).

        **`exact` is the single most important bit here**, more than ranking
        quality. A bad reader-facing result — asking about one thing and getting an
        unrelated market with a similar-sounding name — is rarely a matching
        failure. It is reporting a fuzzy match **as an exact one**: the consumer
        sees it is irrelevant and can only answer "nothing found", while holding
        perfectly good data.

        The test: at least one matched token has an idf above the distinctive floor,
        meaning the specific thing somebody named really is in the catalogue.
        Matching only generic words like election, market or price is not an exact
        match; it is labelled related, and the consumer still has real data to use.
        """
        q_tokens = _tokens(query)
        if not q_tokens:
            return [], False
        uniq = set(q_tokens)
        cand: dict[int, float] = {}
        hits: dict[int, set[str]] = {}
        title_hit: dict[int, int] = {}
        for tk in uniq:
            w = self.idf(tk)
            if w <= 0.0:            # absent from the catalogue entirely; nothing to distinguish
                continue
            for i in self._postings.get(tk, ()):
                # a tag hit scores partially: belonging to a category is weaker than
                # the term appearing in the title
                in_title = tk in self._title_tokens[i]
                cand[i] = cand.get(i, 0.0) + (w if in_title else w * _TAG_WEIGHT)
                hits.setdefault(i, set()).add(tk)
                if in_title:
                    title_hit[i] = title_hit.get(i, 0) + 1
        phrase = " ".join(q_tokens)
        phrase_hit: set[int] = set()
        if len(phrase) >= 5:
            # Look for the whole phrase among candidates only. A title containing the
            # phrase necessarily contains its tokens, so nothing is missed and a full
            # scan is avoided.
            for i in list(cand):
                if phrase in self.records[i]["t"].lower():
                    cand[i] += _TITLE_PHRASE_BONUS
                    phrase_hit.add(i)
        if category:
            cl = category.strip().lower()
            cand = {i: s for i, s in cand.items()
                    if any(cl == g.lower() for g in self.records[i]["g"])}
        if not cand:
            return [], False
        def _rank(kv: tuple[int, float]) -> float:
            i, base = kv
            fit = title_hit.get(i, 0) / max(len(self._title_tokens[i]), 1)
            return base + _TITLE_FIT * fit + self._hot(i)

        ranked = sorted(cand.items(), key=_rank, reverse=True)
        best = ranked[0][0]
        matched = hits.get(best, set())
        # Three conditions for exact, each corresponding to a real bad match:
        #   1. at least one distinctive term matched. Otherwise the hit is only on
        #      generic words, which is how a query lands on an arbitrary market of
        #      the same category;
        #   2. a multi-word query must match at least two of its words. Matching only
        #      the generic half of a two-word phrase and calling it exact is the same
        #      lie in a different shape;
        #   3. the whole phrase appearing in the title settles it outright.
        #
        # A long sentence falls conservatively to related through condition 2, and
        # that is **correct**: the reader still gets real markets and real prices,
        # with the wording downgraded from "this is the one" to "the closest few".
        # Claim less rather than overstate precision.
        #
        # "Specific enough" can be satisfied two ways, and accepting only the first
        # kills good matches: one distinctive term, **or** several medium ones summing
        # to the same information. A three-word phrase whose words each fall below the
        # floor can sum well above it while uniquely identifying the largest market in
        # the index; taking only the per-word maximum would call that inexact and fail
        # to answer a question it can answer. The summed threshold is raised so a pile
        # of generic words cannot manufacture false precision.
        mass = sum(self.idf(t) for t in matched)
        strong = (any(self.idf(t) >= self.distinct_floor for t in matched)
                  or mass >= _MASS_RATIO * self.distinct_floor)
        exact = bool(strong and (len(matched) >= 2 or len(uniq) == 1)) or best in phrase_hit
        return [self.records[i] for i, _ in ranked[:limit]], exact

    def trending(self, *, category: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """The busiest markets right now by 24h volume, optionally filtered by
        category.

        A fallback with only one dimension — busiest overall — answers a sports
        question with a screen of politics. Events carry tags, so filtering by
        category costs nothing; whether to use it is the caller's decision.
        """
        rows = self.records
        if category:
            cl = category.strip().lower()
            rows = [r for r in rows if any(cl == g.lower() for g in r["g"])]
            if not rows:  # unknown category or none active: fall back rather than
                #           return nothing
                rows = self.records
        return sorted(rows, key=lambda r: r["v"], reverse=True)[:limit]

    def categories(self, *, limit: int = 24, min_events: int = 8) -> list[str]:
        """Categories actually present in the catalogue, by market count. **Derived
        from the data, never a hardcoded enumeration**: a new category on the venue
        appears here at the next refresh."""
        rows = [(c, n) for c, n in self.category_counts.items() if n >= min_events]
        rows.sort(key=lambda kv: kv[1], reverse=True)
        return [c for c, _ in rows[:limit]]


EMPTY = Index([], built_at=0.0)


# ------------------------------------------------------- fetching (background)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _slice_windows(data: Any, lo0: float, hi0: float, *, depth: int = 7,
                   **filters: Any) -> list[tuple[float, float]]:
    """Adaptively bisect [lo0, hi0) by end date into windows each under SLICE_MAX.

    It exists because the offset cap is far below the number of active events:
    without partitioning you see only the first slice, **and upstream does not
    error**. Ask the pagination endpoint how large a window is, and halve it until
    it fits.
    """
    todo = [(lo0, hi0)]
    out: list[tuple[float, float]] = []
    while todo:
        lo, hi = todo.pop()
        n = data.count_events(end_date_min=_iso(lo), end_date_max=_iso(hi), **filters)
        if n is None:            # count unavailable: paginate anyway and take what
            #                      the cap allows
            out.append((lo, hi))
            continue
        if n == 0:
            continue
        if n <= SLICE_MAX or hi - lo < 3600 or len(out) + len(todo) > 2 ** depth:
            out.append((lo, hi))
            continue
        mid = lo + (hi - lo) / 2.0
        todo.append((lo, mid))
        todo.append((mid, hi))
    out.sort()
    return out


def _page_slice(data: Any, params: dict[str, Any], *, max_pages: int) -> list[dict[str, Any]]:
    """Paginate one slice until it empties, returns a short page, or hits the cap."""
    got: list[dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * PAGE_SIZE
        if offset >= GAMMA_OFFSET_CAP:
            break
        rows = data.fetch_events(dict(params, limit=PAGE_SIZE, offset=offset))
        if not rows:
            break
        got.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
    return got


def build_full(data: Any = None, *, now: float | None = None) -> Index:
    """Full rebuild. Blocking and slow — **call it from a background thread only**."""
    d = data or Gamma()
    now = now if now is not None else time.time()
    base = {"active": "true", "closed": "false"}
    total = d.count_events(end_date_min=_iso(now)) or 0
    seen: dict[str, dict[str, Any]] = {}
    for lo, hi in _slice_windows(d, now, now + 80 * 365 * 86400):
        params = dict(base, end_date_min=_iso(lo), end_date_max=_iso(hi),
                      order="endDate", ascending="true")
        for ev in _page_slice(d, params, max_pages=GAMMA_OFFSET_CAP // PAGE_SIZE):
            rec = _record(ev)
            if rec:
                seen[rec["i"]] = rec
    return Index(list(seen.values()), built_at=time.time(), full_at=time.time(),
                 total_upstream=total)


def build_settled(data: Any = None, *, now: float | None = None) -> Index:
    """The settled-facts index.

    **Why it has to exist.** Half of what people ask is what can be traded now; the
    other half is how something turned out. The second answer is on-chain fact — a
    closed market whose outcome price snapped to an endpoint — and **a gap there in
    the context is exactly where a language model's training memory fills in**. The
    recorded failure: asked about a tournament, a model asserted the wrong winner
    from memory, while the real result sat in the same API response and had been
    filtered out by us.

    **Why it is also a local index.** A public search endpoint's recall can be poor
    enough that even a generous limit fails to return the largest market matching an
    obvious query, forcing a hardcoded guess to compensate. A local index has no such
    problem: the same query hits the main market directly under idf matching, and the
    guess can be deleted outright.

    **Scale and threshold.** The full settled set is far too large to keep locally,
    dominated by hourly candles and individual fixtures. Taking a recency window plus
    a lifetime-volume floor reduces it to a few thousand records. That floor is an
    **importance threshold against real magnitudes**, not a guess about language:
    nobody asks who won a market that barely traded. Coverage is reported honestly by
    `stats()`.
    """
    d = data or Gamma()
    now = now if now is not None else time.time()
    lo0, hi0 = now - SETTLED_WINDOW_DAYS * 86400, now + 86400
    flt = {"closed": "true", "volume_min": SETTLED_MIN_VOLUME}
    total = d.count_events(end_date_min=_iso(lo0), end_date_max=_iso(hi0), **flt) or 0
    seen: dict[str, dict[str, Any]] = {}
    for lo, hi in _slice_windows(d, lo0, hi0, **flt):
        params = dict(flt, end_date_min=_iso(lo), end_date_max=_iso(hi),
                      order="endDate", ascending="true")
        for ev in _page_slice(d, params, max_pages=GAMMA_OFFSET_CAP // PAGE_SIZE):
            rec = _settled_record(ev)
            if rec:
                seen[rec["i"]] = rec
    return Index(list(seen.values()), built_at=time.time(), full_at=time.time(),
                 total_upstream=total)


_DELTA_SLICES = (
    # Newly listed markets: the only staleness a reader notices, because a breaking
    # event lists the same day.
    {"order": "startDate", "ascending": "false"},
    # The busiest slice, which the fallback path consumes directly and which also
    # refreshes prices on popular markets.
    {"order": "volume24hr", "ascending": "false"},
)


def build_delta(prev: Index, data: Any = None, *, pages: int = 3,
                now: float | None = None) -> Index:
    """Incremental refresh: upsert the newest and busiest slices onto the existing
    index. A few seconds."""
    d = data or Gamma()
    now = now if now is not None else time.time()
    merged = {r["i"]: r for r in prev.records}
    base = {"active": "true", "closed": "false", "end_date_min": _iso(now)}
    changed = 0
    for extra in _DELTA_SLICES:
        for ev in _page_slice(d, dict(base, **extra), max_pages=pages):
            rec = _record(ev)
            if rec:
                merged[rec["i"]] = rec
                changed += 1
    if not changed and not prev.records:
        return prev
    return Index(list(merged.values()), built_at=time.time(), full_at=prev.full_at,
                 total_upstream=prev.total_upstream or (d.count_events(end_date_min=_iso(now)) or 0))


def build_bootstrap(data: Any = None, *, now: float | None = None) -> Index:
    """Cold start with no snapshot: a few pages each of the busiest, newest and
    recently settled slices. Usable in seconds, with the full rebuild following in
    the background. Usable first, complete later."""
    d = data or Gamma()
    now = now if now is not None else time.time()
    base = {"active": "true", "closed": "false", "end_date_min": _iso(now)}
    seen: dict[str, dict[str, Any]] = {}
    for extra in ({"order": "volume24hr", "ascending": "false"},
                  {"order": "startDate", "ascending": "false"},
                  {"order": "endDate", "ascending": "true"}):
        for ev in _page_slice(d, dict(base, **extra), max_pages=BOOTSTRAP_PAGES):
            rec = _record(ev)
            if rec:
                seen[rec["i"]] = rec
    return Index(list(seen.values()), built_at=time.time(),
                 total_upstream=d.count_events(end_date_min=_iso(now)) or 0)


# ----------------------------------------------------------- snapshot storage


def save_snapshot(idx: Index, settled: Index | None = None,
                  path: str | None = None) -> bool:
    """Atomic write. Several processes share one snapshot: whichever refreshes first
    writes it, and the others consume it on cold start instead of repeating a
    multi-minute rebuild. Writing identical content again is harmless, and the rename
    guarantees a reader never sees a half-written file."""
    path = path or SNAPSHOT_PATH   # resolved in the body, not as a default argument:
    try:                           # a default binds at def time, so a test that
        #                            redirects SNAPSHOT_PATH would have no effect
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        doc: dict[str, Any] = {
            "built_at": idx.built_at, "full_at": idx.full_at,
            "total_upstream": idx.total_upstream, "events": idx.records,
        }
        if settled is not None and len(settled):
            doc.update({"settled_at": settled.built_at, "settled": settled.records,
                        "settled_upstream": settled.total_upstream})
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_snapshot(path: str | None = None) -> tuple[Index | None, Index | None]:
    """Returns (live, settled). Either is None when absent, so an older snapshot
    format still yields its live half."""
    path = path or SNAPSHOT_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None, None

    def _idx(key: str, at_key: str) -> Index | None:
        evs = doc.get(key)
        if not isinstance(evs, list) or not evs:
            return None
        return Index([e for e in evs if isinstance(e, dict) and e.get("i")],
                     built_at=_f(doc.get(at_key)), full_at=_f(doc.get(at_key)),
                     total_upstream=int(_f(doc.get(key + "_upstream"))))

    live = _idx("events", "built_at")
    if live is not None:
        live.full_at = _f(doc.get("full_at"))
        live.total_upstream = int(_f(doc.get("total_upstream")))
    return live, _idx("settled", "settled_at")


# ------------------------------------------------------------------ singleton


_lock = threading.Lock()
_index: Index = EMPTY          # live markets: what can be traded now
_settled: Index = EMPTY        # settled facts: how something turned out
_refreshing = False
_last_error: str | None = None


def current() -> Index:
    return _index


def settled_index() -> Index:
    return _settled


def stats() -> dict[str, Any]:
    idx, st = _index, _settled
    age = time.time() - idx.built_at if idx.built_at else None
    cov = (len(idx) / idx.total_upstream) if idx.total_upstream else None
    scov = (len(st) / st.total_upstream) if st.total_upstream else None
    return {
        "n_events": len(idx),
        "total_upstream": idx.total_upstream,
        "coverage": round(cov, 3) if cov is not None else None,
        "age_sec": int(age) if age is not None else None,
        "full_age_sec": int(time.time() - idx.full_at) if idx.full_at else None,
        "n_settled": len(st),
        "settled_upstream": st.total_upstream,
        "settled_coverage": round(scov, 3) if scov is not None else None,
        "settled_age_sec": int(time.time() - st.built_at) if st.built_at else None,
        "refreshing": _refreshing,
        "last_error": _last_error,
    }


def _install(idx: Index | None = None, settled: Index | None = None,
             *, persist: bool = True) -> None:
    """Install a new index. **An empty refresh never overwrites a populated
    catalogue**: stale beats absent."""
    global _index, _settled
    if idx is not None and (len(idx) > 0 or len(_index) == 0):
        _index = idx
    if settled is not None and (len(settled) > 0 or len(_settled) == 0):
        _settled = settled
    if persist:
        save_snapshot(_index, _settled)


def refresh(data: Any = None, *, force_full: bool = False) -> Index:
    """Blocking refresh, for a background thread, the CLI or the selftest. Choose
    bootstrap, full or delta."""
    global _refreshing, _last_error
    with _lock:
        if _refreshing:
            return _index
        _refreshing = True
    try:
        idx = _index
        now = time.time()
        if len(idx) == 0:
            # Bootstrap is a transitional "usable now" slice and is **never
            # persisted**: writing it would overwrite a complete catalogue on disk
            # with a fragment, while another process may be serving from it.
            _install(build_bootstrap(data, now=now), persist=False)
            _install(build_full(data, now=now))
        elif force_full or (now - idx.full_at) > FULL_TTL_SEC:
            _install(build_full(data, now=now))
        else:
            _install(build_delta(idx, data, now=now))
        # The settled index changes far more slowly than live markets — once settled
        # a market never changes again — so the full cadence is enough.
        if force_full or len(_settled) == 0 or (now - _settled.built_at) > FULL_TTL_SEC:
            _install(settled=build_settled(data, now=now))
        _last_error = None
        return _index
    except Exception as exc:  # noqa: BLE001 - a failed refresh keeps the old catalogue
        _last_error = f"{type(exc).__name__}: {exc}"
        return _index
    finally:
        _refreshing = False


def ensure_loaded(data: Any = None, *, background: bool = True) -> Index:
    """Call once at process start. Reads the snapshot from disk instantly and hands
    any refresh to a background thread.

    **It never blocks the caller.** A full refresh takes minutes, and sitting on a
    request path that would be a disaster.
    """
    global _index, _settled
    if len(_index) == 0:
        live, st = load_snapshot()
        if live is not None:
            _index = live
        if st is not None:
            _settled = st
    if not background:
        return refresh(data)
    if _stale() and not _refreshing:
        threading.Thread(target=refresh, kwargs={"data": data},
                         name="market-catalog", daemon=True).start()
    return _index


def _stale() -> bool:
    now = time.time()
    return (len(_index) == 0
            or len(_settled) == 0
            or (now - _index.built_at) > DELTA_TTL_SEC
            or (now - _index.full_at) > FULL_TTL_SEC)


def start_refresher(data: Any = None, *, interval: float = DELTA_TTL_SEC,
                    stop: Callable[[], bool] | None = None) -> threading.Thread:
    """The resident background refresh thread; it exits with the process.

    It consumes the on-disk snapshot synchronously, which is a file read, and moves
    every real refresh onto the thread. Startup is therefore never blocked by the
    catalogue, and during a cold start consumers fall back to the trending list.
    """
    global _index, _settled
    if len(_index) == 0:
        live, st = load_snapshot()
        if live is not None:
            _index = live
        if st is not None:
            _settled = st

    def _loop() -> None:
        while not (stop() if stop else False):
            if _stale():
                refresh(data)
            for _ in range(int(max(interval, 5))):
                if stop and stop():
                    return
                time.sleep(1.0)

    th = threading.Thread(target=_loop, name="market-catalog-refresher", daemon=True)
    th.start()
    return th


# -------------------------------------------------------------- live quotes

_price_cache: dict[str, tuple[float, dict[str, Any]]] = {}
PRICE_TTL_SEC = 60.0


def live_prices(event_ids: Iterable[str], data: Any = None,
                *, now: float | None = None) -> dict[str, dict[str, Any]]:
    """Fetch current prices for a batch of event ids in one call, behind a short TTL
    cache.

    A catalogue price may be minutes old, which is fine for sorting and not fine to
    quote. This runs only for the handful of markets actually being cited, so the
    cost is small and bounded. On failure it returns nothing and the caller falls
    back to the catalogue price, labelled with its age.
    """
    d = data or Gamma()
    now = now if now is not None else time.time()
    ids = [str(i) for i in event_ids if str(i).strip()]
    out: dict[str, dict[str, Any]] = {}
    miss: list[str] = []
    for i in ids:
        hit = _price_cache.get(i)
        if hit and now - hit[0] < PRICE_TTL_SEC:
            out[i] = hit[1]
        else:
            miss.append(i)
    if miss:
        try:
            for ev in d.fetch_events_by_id(miss) or []:
                rec = _record(ev)
                if rec:
                    out[rec["i"]] = rec
                    _price_cache[rec["i"]] = (now, rec)
        except Exception:  # noqa: BLE001 - fall back to the catalogue price
            pass
    return out


# ------------------------------------------------------- rendering (for humans)
#
# The single source of truth for what a record looks like. Live and settled forms
# are **deliberately identical in shape**, so a consumer does not have to tell them
# apart; the distinction belongs to the caller's wording.


def render_live(records: Iterable[dict[str, Any]], *, max_outcomes: int = 8,
                fresh: dict[str, dict[str, Any]] | None = None) -> str | None:
    """A live market -> a human-readable price line. `fresh` is the result of a live
    fetch and takes precedence when present."""
    lines: list[str] = []
    for r in records:
        cur = (fresh or {}).get(r["i"]) or r
        outs = cur.get("o") or []
        if not outs:
            continue
        head = f"• {r['t']}" + (f" (24h vol ${r['v']:,.0f})" if r.get("v") else "")
        body = ", ".join(f"{lab} {round(p * 100)}¢" for p, lab in outs[:max_outcomes])
        extra = f" (+{len(outs) - max_outcomes} more)" if len(outs) > max_outcomes else ""
        lines.append(f"{head}\n  {body}{extra}")
    return "\n".join(lines) or None


def render_settled(records: Iterable[dict[str, Any]], *, max_outcomes: int = 8) -> str | None:
    """A settled event -> a factual result line.

    **Carrying the settlement date is deliberate.** A question may be about one
    particular edition of a recurring event, and the date is what lets an answer say
    which one it is instead of merging two into one — the exact shape of a recorded
    fabrication. The wording is chosen by leg count and never forced into a "who
    won" framing.
    """
    lines: list[str] = []
    for r in records:
        rows, n_no = r.get("w") or [], int(r.get("nno") or 0)
        if not rows and not n_no:
            continue
        when = r.get("e") or ""
        head = f"• {r['t']} — SETTLED" + (f" {when}" if when else "") + "."
        if not rows:
            # Every leg No: it can say none of them happened, and cannot say what did
            # — the winning leg is not in this slice.
            body = f"All {n_no} outcome(s) here resolved NO."
        elif len(rows) == 1 and not n_no:
            body = f"Resolved: {rows[0][0]} → {rows[0][1]}."
        else:
            shown = ", ".join(f"{lab} → {won}" for lab, won in rows[:max_outcomes])
            more = f" (+{len(rows) - max_outcomes} more)" if len(rows) > max_outcomes else ""
            tail = f" Other {n_no} outcome(s) resolved NO." if n_no else ""
            body = f"Resolved TRUE: {shown}{more}.{tail}"
        lines.append(f"{head}\n  {body}")
    return "\n".join(lines) or None


# --------------------------------------------------------- selftest (offline)


class _FakeGamma:
    """An offline double: a small world of markets plus a scriptable pagination and
    counting surface.

    No real data enters the selftest. It pins **behaviour**; the network is covered
    by a manual smoke run.
    """

    def __init__(self, events: list[dict[str, Any]] | None = None,
                 total: int | None = None) -> None:
        self.events = events if events is not None else _FIXTURE
        self.total = total
        self.pages: list[dict[str, Any]] = []
        self.id_calls = 0

    def fetch_events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.pages.append(dict(params))
        off = int(params.get("offset") or 0)
        return self.events[off:off + int(params.get("limit") or PAGE_SIZE)]

    def fetch_events_by_id(self, ids: list[str]) -> list[dict[str, Any]]:
        self.id_calls += 1
        return [e for e in self.events if str(e.get("id")) in set(ids)]

    def count_events(self, **filters: Any) -> int | None:
        return self.total if self.total is not None else len(self.events)


def _ev(eid: str, title: str, slug: str, tags: list[str], vol: float,
        outs: list[tuple[str, str]], *, closed: bool = False) -> dict[str, Any]:
    return {
        "id": eid, "title": title, "slug": slug,
        "volume24hr": vol, "volume": vol, "liquidity": vol,
        "endDate": "2027-01-01T00:00:00Z", "closedTime": "2026-07-20T00:00:00Z",
        "tags": [{"label": t} for t in tags],
        # Complementary leg prices plus explicit outcome labels: the settled path
        # reads what a leg settled to from the label at the winning price, and
        # without it neither Yes/No nor a non-binary pair of labels is readable.
        "markets": [{"active": not closed, "closed": closed, "groupItemTitle": lab,
                     "outcomes": '["Yes", "No"]',
                     "outcomePrices": f'["{p}", "{round(1.0 - float(p), 4)}"]'}
                    for lab, p in outs],
    }


_FIXTURE = [
    _ev("1", "What price will Bitcoin hit in July?", "btc-july", ["Crypto", "Bitcoin"],
        900000.0, [(">$120k", "0.12"), (">$100k", "0.81")]),
    _ev("2", "Fed Decision in July?", "fed-july", ["Economy", "Politics"],
        3700000.0, [("No change", "0.90"), ("25 bps decrease", "0.09")]),
    _ev("3", "Sanae Takaichi out as Prime Minister of Japan in 2026?", "japan-pm",
        ["Politics", "PM"], 20.0, [("Yes", "0.14")]),
    _ev("4", "Who will be the next Prime Minister of Israel?", "israel-pm",
        ["Politics", "Elections"], 350000.0, [("Netanyahu", "0.31")]),
    _ev("5", "Presidential Election Winner 2028", "pres-2028", ["Politics", "Elections"],
        450000.0, [("Vance", "0.28"), ("Newsom", "0.19")]),
    _ev("6", "MLS Cup Winner 2026", "mls-cup", ["Sports", "MLS"], 12000.0,
        [("Inter Miami", "0.22")]),
    _ev("7", "Retired league champion", "dead-league", ["Sports"], 5.0,
        [("A", "0.5")], closed=True),
] + [
    # Filler records, so that common words really are frequent in the corpus and idf
    # has something to work with. Without them a six-record corpus makes every word
    # look rare, filler words in a full sentence become strong signals, and the test
    # measures the fixture's deformity rather than the scorer's behaviour.
    _ev(str(100 + k), f"What price will {name} hit in July? (market)", f"px-{k}",
        ["Crypto"], 100.0 * k, [("Up", "0.5")])
    for k, name in enumerate(
        ("Solana", "XRP", "Dogecoin", "Cardano", "Avalanche", "Polkadot", "Chainlink"), 1)
]


_SETTLED_FIXTURE = [
    # one of each shape; all three coexist in practice and rendering must handle each
    _ev("900", "World Cup Winner", "wc-winner", ["Sports", "Soccer"], 4.3e9,
        [("Spain", "1.0"), ("France", "0.0"), ("Brazil", "0.0")], closed=True),
    _ev("901", "Bitcoin above ___ on July 1?", "btc-jul1", ["Crypto"], 9.9e6,
        [("$100k", "1.0"), ("$110k", "1.0"), ("$200k", "0.0")], closed=True),
    _ev("902", "US strikes Iran by June 30?", "iran-strike", ["Geopolitics"], 5.3e8,
        [("Yes", "0.0")], closed=True),
]


def selftest() -> int:
    """Offline selftest: `python3 -m marketflow.monitor.market_catalog --selftest`"""
    ok = fail = 0

    def check(cond: Any, label: str) -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    # **Redirect the snapshot path wholesale for the duration of the selftest.**
    # Install and refresh persist normally, so running once against the production
    # path overwrites a catalogue of tens of thousands with a fixture of thirteen.
    # Redirecting is more reliable than remembering to pass persist=False at every
    # call site: a newly added persisting path needs nobody to remember.
    global SNAPSHOT_PATH
    _real_snapshot, SNAPSHOT_PATH = SNAPSHOT_PATH, os.path.join(
        os.path.dirname(SNAPSHOT_PATH), f".selftest-snapshot-{os.getpid()}.json")

    recs = [r for r in (_record(e) for e in _FIXTURE) if r]
    idx = Index(recs, built_at=time.time(), full_at=time.time(), total_upstream=len(_FIXTURE))
    check(len(idx) == 13, "a closed event stays out of the live catalogue: its price is a result, not a quote")
    check(idx.by_id["1"]["o"][0] == [0.81, ">$100k"], "outcomes sort by Yes price descending, prices preserved")

    # --- retrieval: idf replaces a word list ---------------------------------
    check(idx.idf("prime") < idx.idf("bitcoin"),
          "idf down-weights frequent words; the data replaces a stopword list")
    hits, exact = idx.score("bitcoin")
    check(exact and hits[0]["i"] == "1", "a single-word exact match")
    hits, exact = idx.score("japan prime minister")
    check(exact and hits[0]["i"] == "3",
          "a better literal match outranks a busier market: the volume prior is additive and bounded")
    hits, exact = idx.score("what is the current price on the Fed decision market?")
    check(exact and hits[0]["i"] == "2", "a full sentence still matches, with no stopword stripping")

    # --- the three conditions for exact, each from a real bad match ----------
    hits, exact = idx.score("election Zorbland")
    check(hits and not exact,
          "matching only a generic word is related, not exact, and still returns real data")
    hits, exact = idx.score("world cup")
    check(hits and not exact, "matching one word of a phrase never claims to be exact")
    check(idx.score("nvidia earnings")[0] == [], "genuinely absent -> zero hits, nothing forced")
    check(idx.score("")[0] == [] and idx.score("\u4f60\u597d")[0] == [],
          "empty or non-Latin queries return nothing; the cross-language step belongs to a model")

    # --- fallback: never empty-handed, and filterable by category ------------
    check([r["i"] for r in idx.trending(limit=2)] == ["2", "1"], "the fallback sorts by 24h volume")
    check(all("Sports" in r["g"] for r in idx.trending(category="Sports", limit=3)),
          "the fallback can filter by category, so a sports question is not answered with politics")
    check(idx.trending(category="Nonexistent", limit=2), "an unknown category falls back rather than returning nothing")
    check("Politics" in idx.categories(min_events=1), "the category list is data-driven, not hardcoded")

    # --- pagination and partitioning around the offset cap -------------------
    g = _FakeGamma()
    rows = _page_slice(g, {"active": "true"}, max_pages=3)
    check(len(rows) == len(_FIXTURE) and len(g.pages) == 1, "a short page stops paging; no wasted call")
    big = _FakeGamma(total=SLICE_MAX + 1)
    wins = _slice_windows(big, 1_000_000.0, 1_000_000.0 + 80 * 365 * 86400)
    check(len(wins) >= 2, "a slice above SLICE_MAX bisects its window, routing around the offset cap")
    check(GAMMA_OFFSET_CAP // PAGE_SIZE == 21,
          "the page ceiling is the cap, beyond which the endpoint errors rather than saying there is no more")

    # --- settled facts -------------------------------------------------------
    st = Index([r for r in (_settled_record(e) for e in _SETTLED_FIXTURE) if r],
               built_at=time.time(), total_upstream=len(_SETTLED_FIXTURE))
    check(len(st) == 3, "settled extraction handles all three shapes")
    hits, exact = st.score("world cup")
    check(exact and hits[0]["i"] == "900",
          "the local index hits the main market directly, which a public search endpoint failed to recall")
    txt = render_settled(hits[:1]) or ""
    check("Spain" in txt and "Yes" in txt and "2026-07-20" in txt,
          "a settled result carries its date, so an answer can say which edition it was")
    multi = render_settled([st.by_id["901"]]) or ""
    check(multi.count("→") == 2, "a threshold ladder reports every Yes leg rather than forcing a single winner")
    allno = render_settled([st.by_id["902"]]) or ""
    check("resolved NO" in allno and "→" not in allno,
          "with every leg No it says none happened and does not claim what did")
    check(render_settled([]) is None, "nothing settled returns None for the caller to branch on")

    # --- snapshot round trip, and never overwriting a good catalogue ---------
    tmp = os.path.join(os.path.dirname(SNAPSHOT_PATH), f".selftest-{os.getpid()}.json")
    try:
        st_idx = Index([r for r in (_settled_record(e) for e in _SETTLED_FIXTURE) if r],
                       built_at=time.time(), total_upstream=len(_SETTLED_FIXTURE))
        check(save_snapshot(idx, st_idx, tmp), "snapshot writes atomically")
        back, back_st = load_snapshot(tmp)
        check(back is not None and len(back) == len(idx) and back.by_id["2"] == idx.by_id["2"],
              "the snapshot round-trips losslessly, so processes can share one")
        check(back_st is not None and len(back_st) == len(st_idx),
              "the settled index round-trips in the same snapshot")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    global _index
    keep, _index = _index, idx
    try:
        _install(Index([], built_at=time.time()))
        check(len(_index) == 13, "an empty refresh never overwrites a populated catalogue")
        _price_cache.clear()
        gp = _FakeGamma()
        live_prices(["1"], gp, now=1000.0)
        live_prices(["1"], gp, now=1030.0)
        check(gp.id_calls == 1, "the quote cache holds within its TTL")
        live_prices(["1"], gp, now=1200.0)
        check(gp.id_calls == 2, "past the TTL it refetches; a stale price never poses as current")

        class _Boom:
            def fetch_events(self, params):  # noqa: ANN001, ANN201
                raise RuntimeError("network down")

            def fetch_events_by_id(self, ids):  # noqa: ANN001, ANN201
                raise RuntimeError("network down")

            def count_events(self, **kw):  # noqa: ANN003, ANN201
                return None

        refresh(_Boom())
        check(len(_index) == 13 and _last_error is not None,
              "a failed refresh keeps the old catalogue and records the error rather than staying silent")
        check(live_prices(["1"], _Boom(), now=9999.0) == {},
              "a quote-layer exception does not escape; the caller falls back and never invents a price")
    finally:
        _index = keep
        _price_cache.clear()
        try:
            os.remove(SNAPSHOT_PATH)     # the temporary snapshot the selftest redirected to
        except OSError:
            pass
        SNAPSHOT_PATH = _real_snapshot

    print(f"market_catalog selftest: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "--selftest" in argv:
        raise SystemExit(selftest())
    # Manual smoke test: python3 -m marketflow.monitor.market_catalog [--build] [query...]
    # Without --build it reads the on-disk snapshot instantly; --build really
    # rebuilds and blocks for minutes.
    ensure_loaded(background=False) if "--build" in argv else ensure_loaded(background=True)
    print(json.dumps(stats(), ensure_ascii=False))
    q = " ".join(a for a in argv if not a.startswith("--"))
    if q:
        hits, ok_ = current().score(q, limit=5)
        print(f"exact={ok_}")
        for h in hits:
            outs = ", ".join(f"{lab} {round(p * 100)}¢" for p, lab in h["o"][:5])
            print(f"  • {h['t']}  [{'/'.join(h['g'][:3])}]  24h ${h['v']:,.0f}\n    {outs}")
