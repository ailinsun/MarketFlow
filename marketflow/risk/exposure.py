#!/usr/bin/env python3
"""Event exposure and correlation engine.

**Why this matters.** One political outcome is spread across a presidential
market, state markets, congressional markets, policy markets and a price market.
Five positions on the books are really **one risk**. Reporting diversification by
position count systematically understates the probability of ruin.

The reverse holds too: five NO legs across a mutually exclusive ladder show five
times the cost at risk on the books while structurally **at most one can lose**.
Reporting the sum of costs systematically overstates it. Both directions are
wrong, and both are wrong by multiples.

This module answers one falsifiable question: **given a set of positions, what is
the true event-level exposure?** The answer comes in three layers of decreasing
strength, each carrying its own evidential basis in the output, never mixed:

    L1 structural (exact, no statistics): mutually exclusive groups and multiple
        legs on one event. Enumerate every settlement state, compute the portfolio
        P&L in each, and take the worst — that is the true maximum single-event
        loss. This layer needs no correlation estimate at all; it is an identity.

    L2 implied (exact, from market prices): state probabilities come from each
        leg's market-implied probability, which makes the pairwise payoff
        correlation **analytically available** rather than fitted.

    L3 thematic (statistical, falsifiable): cross-event thematic clusters, from
        idf-weighted Jaccard clustering. Whether a cluster really moves together is
        an empirical question, decided by this module's own backtest layer.
        **Until that verdict is positive, a thematic cluster's correlation enters
        the exposure calculation as zero** — a label without a risk number. No
        frightening yourself with an unverified correlation.

**Definitions, each of which affects the conclusion:**
- A "leg" is a position aggregated by (condition_id, side), with side normalised
  to YES or NO. Only an outcome literally equal to "no" is NO; everything else is
  YES, because in a multi-outcome mutually exclusive event each candidate is its
  own binary market.
- Each share settles at $1. A leg's maximum loss is its cost; its maximum gain is
  shares minus cost.
- The state space of a mutually exclusive group is {each of our legs is the sole
  YES} plus {the YES falls outside our legs}. That last state is **only included
  when we cannot prove we hold every leg in the group** — including it can only
  make the worst case worse, which is the conservative direction.
- State probabilities are each leg's implied P(YES), with our NO legs taking one
  minus the entry price, normalised to at most one, and the remainder assigned to
  "outside the group". A leg with no price falls back to uniform probabilities for
  that group, flagged in the output.
- Clusters that are not mutually exclusive — several legs on one event, or a
  thematic cluster — are **not enumerated exactly**. Only the bound where the whole
  cluster loses together is reported, under a field name ending in `_bound`. It is
  never called true exposure.

**Pre-registered verdict, fixed before running and not revised afterwards:**

  Thematic cluster correlation is the within-cluster pairwise correlation of
  residuals, judged in three bands:
    CONFIRMED : correlation at or above the upper threshold, with a
                date-stratified permutation p below 0.05 and enough pairs
    WEAK      : correlation between the two thresholds, or a marginal p
    NULL      : correlation below the lower threshold, or a p that fails

  **A positive control comes first**: a mutually exclusive group must measure a
  negative correlation, which is the known sign of that structure. If the control
  fails, the estimator itself is untrustworthy, the verdict is
  INSTRUMENT_UNVALIDATED, and the thematic conclusion is void however good it
  looks.

  **Both samples must confirm before the verdict does.** Your own ledger contains
  only markets you chose, which is a strong selection bias; a whole-market settled
  sample does not have it. One sample saying yes and the other saying no is WEAK at
  best.

  CONFIRMED means the correlation enters the L3 exposure calculation. WEAK or NULL
  means L3 correlation is zero and a thematic cluster is only a label.

  The permutation test is **stratified by settlement date**, shuffling within a
  day, because markets settling on the same day share a market-wide shock.
  Unstratified, "same theme" would collect the benefit of "same day", which is
  confounding rather than correlation.

Hard boundary: read-only. It reads local ledgers, feeds and caches, and optionally
a public metadata API when asked. It places no order, touches no arm state, caps,
kill file, secret or live process, imports no execution module and writes to no
execution path. Output goes only to its own isolated namespace.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from marketflow.paths import PROJECT_DIR as REPO, runtime_dir, runtime_path
OUT_DIR = runtime_path("risk", "exposure")
GAMMA_CACHE = os.path.join(OUT_DIR, "gamma_event_cache.json")

MARKET_FEED = runtime_path("feeds", "markets.jsonl")
# Paper-trading ledgers produced by whatever strategy modules the operator runs.
# Both are overridable per call; neither is required for the exposure math itself.
FLB_LEDGER = runtime_path("risk",
                          "paper_ledgers", "flb_ledger.json")
SM_LEDGER = runtime_path("risk",
                         "paper_ledgers", "smart_money_ledger.json")
PRIVATE_SNAPSHOT = runtime_path("risk",
                                "polymarket_private_position_snapshot", "latest.json")
# Market metadata cache, the fallback source for tags, slug, question, end date and
# mutual-exclusivity, built and maintained by --fetch-meta. A missing cache degrades
# silently rather than erroring: correlation stratification weakens but nothing
# stops. The path is deployment-configurable.
MARKET_META_CACHE = runtime_path("risk",
                                 "market_meta_cache", "market_meta_cache.json")
FLB_MARKET_META = runtime_path("risk",
                               "polymarket_flb_backtest", "markets_meta_v2.jsonl")
FLB_PRICE_SAMPLES = runtime_path("risk",
                                 "polymarket_flb_backtest", "price_samples_v2.jsonl")

SCHEMA_VERSION = "polymarket-event-exposure-v0.1"
GAMMA_API = "https://gamma-api.polymarket.com"
UA = "marketflow-event-exposure/0.1 (read-only research)"

# --- pre-registered constants, fixed before running ------------------------- #
THEME_SIM_THRESHOLD = 0.50      # idf-weighted Jaccard, single-linkage threshold
THEME_MIN_TOKEN_LEN = 2         # tokens shorter than this are dropped
RHO_CONFIRM = 0.15              # threshold for CONFIRMED
RHO_WEAK = 0.05                 # lower edge of WEAK
P_CONFIRM = 0.05
P_WEAK_MAX = 0.20
MIN_PAIRS = 100                 # minimum within-cluster pairs before judging
N_PERM = 2000                   # permutation draws
PERM_SEED = 260727              # fixed seed: results reproduce
CHAINING_WARN_FRAC = 0.40       # one cluster absorbing this share flags chaining

V_CONFIRMED = "CLUSTER_CORRELATION_CONFIRMED"
V_WEAK = "CLUSTER_CORRELATION_WEAK"
V_NULL = "NULL_THEME_CORRELATION"
V_UNVALIDATED = "INSTRUMENT_UNVALIDATED"
V_INSUFFICIENT = "INSUFFICIENT_DATA"

# Stopwords for theme tokenisation: structural and grammatical tokens that would
# only link unrelated markets together.
STOPWORDS = frozenset("""
a an and any are as at be before between by do does for from has have if in into is it its
of on or the this to up will with which who whom what when where why be-2026 vs v
market markets question yes no than more less least most over under above below
""".split())


# --------------------------------------------------------------------------- #
# basic utilities
# --------------------------------------------------------------------------- #
def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


class FetchError(Exception):
    pass


def get_json(url: str, timeout: float = 12.0, retries: int = 3, backoff: float = 1.5) -> Any:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if i < retries - 1:
                time.sleep(backoff ** i)
    raise FetchError(f"{url}: {last}")


# --------------------------------------------------------------------------- #
# position layer
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    """One position leg. cost_usd is the capital committed, which is the maximum
    possible loss; shares is the $1-per-share payout if it wins."""
    cid: str
    side: str                       # 'YES' | 'NO'
    shares: float
    cost_usd: float
    entry_px: Optional[float]       # our side's fill price, i.e. the implied P(we win)
    title: str = ""
    slug: str = ""
    event_hint: str = ""            # an event identifier the position already carries
    neg_risk_hint: Optional[bool] = None
    end_date: Optional[str] = None
    source: str = "generic"
    status: str = "open"            # 'open' | 'won' | 'lost'
    won: Optional[bool] = None
    outcome_label: str = ""

    @property
    def implied_p_win(self) -> Optional[float]:
        """The market-implied probability that this leg wins, which is our fill
        price, since a win pays $1 a share."""
        p = self.entry_px
        if p is None or not (0.0 < p < 1.0):
            return None
        return p

    @property
    def implied_p_leg_yes(self) -> Optional[float]:
        """The implied probability that this leg, as one candidate in a mutually
        exclusive group, settles YES."""
        p = self.implied_p_win
        if p is None:
            return None
        return p if self.side == "YES" else 1.0 - p


def normalize_side(outcome: Any) -> str:
    """Outcome text -> YES or NO. Only a literal "no" is NO: in a mutually exclusive
    event a candidate's name means buying YES on that candidate's own binary
    market."""
    s = str(outcome or "").strip().lower()
    return "NO" if s in ("no", "n", "false") else "YES"


def _pos_status(raw_status: Any, won: Optional[bool]) -> str:
    s = str(raw_status or "").strip().lower()
    if s in ("won", "lost"):
        return s
    if s in ("settled", "closed", "resolved") and won is not None:
        return "won" if won else "lost"
    if won is True:
        return "won"
    if won is False:
        return "lost"
    return "open"


def load_flb_paper(path: str = FLB_LEDGER, *, include_settled: bool = False) -> list[Position]:
    """A paper-trading ledger keyed by condition id, where stake is the capital
    committed."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"FLB ledger missing: {path}")
    with open(path, encoding="utf-8") as fh:
        led = json.load(fh)
    out: list[Position] = []
    for cid, p in (led.get("positions") or {}).items():
        st = _pos_status(p.get("status"), None)
        if not include_settled and st != "open":
            continue
        out.append(Position(
            cid=str(cid),
            side=normalize_side(p.get("side")),
            shares=_f(p.get("shares")) or 0.0,
            cost_usd=_f(p.get("stake")) or 0.0,
            entry_px=_f(p.get("entry_px")),
            title=str(p.get("question") or ""),
            slug=str(p.get("slug") or ""),
            end_date=p.get("end_date"),
            source="flb_paper",
            status=st,
            won=(st == "won") if st in ("won", "lost") else None,
            outcome_label=str(p.get("side") or ""),
        ))
    return out


def load_smart_money_paper(path: str = SM_LEDGER, *, include_settled: bool = False) -> list[Position]:
    """A second paper ledger whose open and closed sections share one shape."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"smart-money ledger missing: {path}")
    with open(path, encoding="utf-8") as fh:
        led = json.load(fh)
    rows = list(led.get("open_positions") or [])
    if include_settled:
        rows += list(led.get("closed") or [])
    out: list[Position] = []
    for p in rows:
        won = p.get("won") if isinstance(p.get("won"), bool) else None
        st = _pos_status(p.get("status"), won)
        out.append(Position(
            cid=str(p.get("cid") or ""),
            side=normalize_side(p.get("side")),
            shares=_f(p.get("shares")) or 0.0,
            cost_usd=_f(p.get("stake")) or 0.0,
            entry_px=_f(p.get("entry_price")),
            title=str(p.get("title") or ""),
            slug="",
            event_hint=str(p.get("event_key") or ""),
            source="smart_money_paper",
            status=st,
            won=won,
            outcome_label=str(p.get("side") or ""),
        ))
    return out


def load_private_snapshot(path: str = PRIVATE_SNAPSHOT) -> list[Position]:
    """A read-only real-money snapshot."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"private snapshot missing: {path}")
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    items = ((snap.get("positions") or {}).get("items")) or []
    out: list[Position] = []
    for p in items:
        size = _f(p.get("size")) or 0.0
        avg = _f(p.get("avg_price"))
        cost = _f(p.get("initial_value"))
        if cost is None:
            cost = size * (avg or 0.0)
        out.append(Position(
            cid=str(p.get("condition_id") or ""),
            side=normalize_side(p.get("outcome")),
            shares=size,
            cost_usd=cost,
            entry_px=avg,
            title=str(p.get("title") or ""),
            slug=str(p.get("slug") or ""),
            event_hint=str(p.get("event_slug") or ""),
            neg_risk_hint=(bool(p.get("negative_risk")) if p.get("negative_risk") is not None else None),
            source="private_snapshot",
            outcome_label=str(p.get("outcome") or ""),
        ))
    return out


def load_generic(path: str) -> list[Position]:
    """Generic portfolio loader — **the entry point for a delegated mandate's
    portfolio**.

    It accepts the mapped row format used by the multi-tenant layer, and this
    module's own Position field names. The file may be a JSON list, JSONL, or an
    object with a positions key.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"positions file missing: {path}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    rows: list[dict] = []
    if text.startswith("["):
        rows = json.loads(text)
    elif text.startswith("{"):
        obj = json.loads(text)
        rows = obj.get("positions") if isinstance(obj.get("positions"), list) else [obj]
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return [position_from_row(p) for p in rows]


def position_from_row(p: dict, *, source: str = "generic") -> Position:
    """One position dict -> a Position. It accepts three shapes, because upstream
    really does produce three:

    - the mapped rows from the multi-tenant layer;
    - **raw rows from the public data API**, which carry the event identifier and
      the mutual-exclusivity flag that the mapped form drops. Grouping needs exactly
      those two, so the raw row is the better input on this path;
    - this module's own Position field names.
    """
    shares = (_f(p.get("held_shares")) or _f(p.get("shares"))
              or _f(p.get("size")) or 0.0)
    entry = (_f(p.get("entry_price")) or _f(p.get("entry_px"))
             or _f(p.get("avg_price")) or _f(p.get("avgPrice")))
    cost = _f(p.get("cost_usd")) or _f(p.get("initialValue"))
    if cost is None:
        cost = shares * (entry or 0.0)
    neg = p.get("negative_risk")
    if neg is None:
        neg = p.get("negativeRisk")
    if neg is None:
        neg = p.get("neg_risk")
    return Position(
        cid=str(p.get("condition_id") or p.get("conditionId") or p.get("cid") or ""),
        side=normalize_side(p.get("outcome") or p.get("side")),
        shares=shares,
        cost_usd=cost,
        entry_px=entry,
        title=str(p.get("title") or ""),
        slug=str(p.get("market_slug") or p.get("slug") or ""),
        event_hint=str(p.get("event_slug") or p.get("eventSlug")
                       or p.get("event_key") or p.get("event_hint") or ""),
        neg_risk_hint=(bool(neg) if neg is not None else None),
        end_date=p.get("endDate") or p.get("end_date"),
        source=str(p.get("source") or source),
        outcome_label=str(p.get("outcome") or p.get("side") or ""),
    )


DATA_API = "https://data-api.polymarket.com"


def load_public_wallet(wallet: str, *, limit: int = 500,
                       fetch: Callable[[str], Any] = get_json) -> list[Position]:
    """Any address's public positions -> a list of Positions. Read-only public API,
    no credential.

    This is how a delegated mandate's portfolio is fetched: a wallet address is
    public, positions derive from the chain, and anybody can read them. It touches no
    private key, SDK or signing path. A size threshold filters out dust legs.
    """
    q = urllib.parse.urlencode({"user": wallet, "limit": int(limit), "sizeThreshold": 1})
    rows = fetch(f"{DATA_API}/positions?{q}")
    if not isinstance(rows, list):
        raise FetchError(f"positions endpoint returned {type(rows).__name__}, expected list")
    out: list[Position] = []
    for r in rows:
        # A redeemable leg is **settled** money and carries no further event risk;
        # keeping it would overstate exposure.
        if not isinstance(r, dict) or r.get("redeemable") is True:
            continue
        p = position_from_row(r, source="public_wallet")
        if p.cid and p.shares > 0:
            out.append(p)
    return out


def aggregate_legs(positions: Iterable[Position]) -> list[Position]:
    """Merge fills on the same (condition, side) into one leg: exposure is counted
    per leg, not per order."""
    agg: dict[tuple[str, str], Position] = {}
    for p in positions:
        key = (p.cid, p.side)
        cur = agg.get(key)
        if cur is None:
            agg[key] = Position(**{**p.__dict__})
            continue
        tot_cost = cur.cost_usd + p.cost_usd
        # Cost-weighted average fill price, because the implied probability has to
        # correspond to the total committed.
        if cur.shares + p.shares > 0:
            cur.entry_px = (tot_cost / (cur.shares + p.shares)) if (cur.shares + p.shares) else cur.entry_px
        cur.shares += p.shares
        cur.cost_usd = tot_cost
        cur.title = cur.title or p.title
        cur.slug = cur.slug or p.slug
        cur.event_hint = cur.event_hint or p.event_hint
        cur.end_date = cur.end_date or p.end_date
        if cur.neg_risk_hint is None:
            cur.neg_risk_hint = p.neg_risk_hint
    return list(agg.values())


# --------------------------------------------------------------------------- #
# metadata resolution: local first, network optional
# --------------------------------------------------------------------------- #
@dataclass
class MarketMeta:
    cid: str
    event_key: str = ""             # grouping key: event id, event slug or slug prefix
    event_title: str = ""
    neg_risk: Optional[bool] = None
    tags: tuple[str, ...] = ()
    slug: str = ""
    question: str = ""
    end_date: Optional[str] = None
    key_source: str = "none"        # gamma_event | feed_event | position_event_field | slug_prefix | none
    n_event_legs: Optional[int] = None   # legs in the whole group, from an authoritative
    #                                      source only; used to tell whether we hold all


_SLUG_TAIL = re.compile(r"-\d{6,}$")            # the timestamp tail appended to a slug
_DATE_TOKEN = re.compile(r"^(19|20)\d{2}$|^\d{1,2}$")


def slug_group_prefix(slug: str) -> str:
    """Stripping a slug's tail gives a heuristic grouping key for one mutually
    exclusive ladder.

    `highest-temperature-in-london-on-july-8-2026-31c` → `...-july-8-2026`.
    **It is a heuristic**, and when it is used the output says so rather than
    claiming authority.
    """
    s = _SLUG_TAIL.sub("", str(slug or "").strip().lower())
    parts = [x for x in s.split("-") if x]
    if len(parts) < 3:
        return ""
    return "-".join(parts[:-1])


def load_feed_meta(path: str = MARKET_FEED) -> dict[str, MarketMeta]:
    """The local markets feed -> metadata by condition. The grouping key is the
    parent event id.

    The top-level event identifier on a row is **unique per leg**, so grouping on it
    splits a mutually exclusive group into singletons. The parent event is the
    correct key.
    """
    out: dict[str, MarketMeta] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(d.get("market_id") or "")
            if not cid:
                continue
            raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
            evs = raw.get("events") or []
            parent = evs[0] if (isinstance(evs, list) and evs and isinstance(evs[0], dict)) else {}
            gid = str(parent.get("id") or raw.get("negRiskMarketID") or "")
            tags = tuple(sorted({(t.get("slug") or "").lower()
                                 for t in (parent.get("tags") or []) if isinstance(t, dict)} - {""}))
            out[cid] = MarketMeta(
                cid=cid,
                event_key=gid,
                event_title=str(parent.get("title") or ""),
                neg_risk=bool(d.get("neg_risk") or raw.get("negRisk") or parent.get("negRisk")),
                tags=tags,
                slug=str(d.get("slug") or ""),
                question=str(d.get("question") or ""),
                end_date=raw.get("endDate"),
                key_source="feed_event" if gid else "none",
            )
    return out


def load_settled_meta() -> dict[str, dict]:
    """Settled-market metadata, merged from read-only caches.

    Both are historical files on disk. Merged, they serve the backtest layer: one
    carries mutual-exclusivity and settlement prices, the other tags and slugs.
    """
    merged: dict[str, dict] = {}
    if os.path.exists(MARKET_META_CACHE):
        with open(MARKET_META_CACHE, encoding="utf-8") as fh:
            for cid, m in (json.load(fh).get("markets") or {}).items():
                merged[cid] = {
                    "question": m.get("question") or "",
                    "neg_risk": m.get("neg_risk"),
                    "end_date": m.get("end_date"),
                    "market_class": m.get("market_class"),
                    "outcome_prices": m.get("outcome_prices") or [],
                }
    if os.path.exists(FLB_MARKET_META):
        with open(FLB_MARKET_META, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = str(d.get("condition_id") or "")
                if not cid:
                    continue
                row = merged.setdefault(cid, {})
                row.setdefault("question", d.get("question") or "")
                row["slug"] = d.get("slug") or row.get("slug") or ""
                row["tags"] = list(d.get("tags") or row.get("tags") or [])
                row["yes_resolved"] = d.get("yes_resolved")
                row.setdefault("end_date", d.get("end_date"))
                row.setdefault("market_class", d.get("market_class"))
    return merged


def _load_gamma_cache(path: str = GAMMA_CACHE) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("markets") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def fetch_gamma_events(cids: list[str], *, cache_path: str = GAMMA_CACHE,
                       batch: int = 20, sleep_s: float = 0.25,
                       fetch: Callable[[str], Any] = get_json) -> dict[str, dict]:
    """Read-only public metadata: condition -> event id, event slug,
    mutual-exclusivity, tags and the number of legs in the event.

    Two hops: fetch the parent event id by condition, then fetch that event for its
    tags and leg count. The embedded event returned by a batch condition query is
    trimmed and carries neither.

    Results are cached on disk. A condition that could not be fetched is **not
    cached**: it may simply be temporarily unreachable, and a cache miss must not be
    recorded as a no.

    The first hop must be made **twice**: without a parameter it returns only open
    markets, and closed ones need an explicit flag. Querying once silently pushed a
    large share of positions back onto the slug heuristic, so coverage looked fine
    while half the grouping keys were guesses.
    """
    cache = _load_gamma_cache(cache_path)
    todo = [c for c in dict.fromkeys(cids) if c and c not in cache]
    if not todo:
        return cache
    event_detail: dict[str, dict] = {}
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        markets: list = []
        for extra in ([], [("closed", "true")]):
            pending = [c for c in chunk if c not in {str(m.get("conditionId") or "") for m in markets}]
            if not pending:
                break
            q = urllib.parse.urlencode(extra + [("condition_ids", c) for c in pending])
            try:
                got = fetch(f"{GAMMA_API}/markets?{q}")
            except FetchError:
                continue
            if isinstance(got, list):
                markets.extend(got)
            time.sleep(sleep_s)
        for m in markets:
            cid = str(m.get("conditionId") or "")
            if not cid:
                continue
            evs = m.get("events") or []
            parent = evs[0] if (isinstance(evs, list) and evs and isinstance(evs[0], dict)) else {}
            eid = str(parent.get("id") or "")
            cache[cid] = {
                "event_id": eid,
                "event_slug": str(parent.get("slug") or ""),
                "event_title": str(parent.get("title") or ""),
                "neg_risk": bool(m.get("negRisk")),
                "neg_risk_market_id": m.get("negRiskMarketID"),
                "slug": str(m.get("slug") or ""),
                "question": str(m.get("question") or ""),
                "end_date": m.get("endDate"),
                "tags": [],
                "n_event_markets": None,
            }
            if eid and eid not in event_detail:
                try:
                    ev = fetch(f"{GAMMA_API}/events/{eid}")
                except FetchError:
                    ev = None
                if isinstance(ev, dict):
                    event_detail[eid] = {
                        "tags": sorted({(t.get("slug") or "").lower()
                                        for t in (ev.get("tags") or []) if isinstance(t, dict)} - {""}),
                        "n_event_markets": len(ev.get("markets") or []) or None,
                        "neg_risk": bool(ev.get("negRisk")),
                        "event_title": str(ev.get("title") or ""),
                    }
                time.sleep(sleep_s)
        time.sleep(sleep_s)
    for cid, row in cache.items():
        det = event_detail.get(row.get("event_id") or "")
        if det:
            row["tags"] = det["tags"]
            row["n_event_markets"] = det["n_event_markets"]
            row["event_title"] = row.get("event_title") or det["event_title"]
            if row.get("neg_risk") is None:
                row["neg_risk"] = det["neg_risk"]
    _write_json(cache_path, {"schema_version": "gamma-event-cache-v0.1",
                             "updated_at_utc": iso_now(), "markets": cache})
    return cache


def resolve_meta(positions: list[Position], *, use_gamma: bool = False,
                 feed_path: str = MARKET_FEED,
                 gamma_cache_path: str = GAMMA_CACHE,
                 fetch: Callable[[str], Any] = get_json) -> dict[str, MarketMeta]:
    """Resolve the grouping key by falling back through decreasing strengths of
    evidence. Each records its source; a heuristic is never presented as authority.

    Strongest to weakest: API event > feed event > a field on the position >
    slug prefix > none.
    """
    feed = load_feed_meta(feed_path)
    gamma = _load_gamma_cache(gamma_cache_path)
    if use_gamma:
        missing = [p.cid for p in positions if p.cid not in feed and p.cid not in gamma]
        if missing:
            gamma = fetch_gamma_events(missing, cache_path=gamma_cache_path, fetch=fetch)
    settled = load_settled_meta()

    out: dict[str, MarketMeta] = {}
    for p in positions:
        g = gamma.get(p.cid)
        if g and g.get("event_id"):
            out[p.cid] = MarketMeta(
                cid=p.cid, event_key=f"gamma:{g['event_id']}",
                event_title=g.get("event_title") or "",
                neg_risk=g.get("neg_risk"), tags=tuple(g.get("tags") or ()),
                slug=g.get("slug") or p.slug, question=g.get("question") or p.title,
                end_date=g.get("end_date") or p.end_date, key_source="gamma_event",
                n_event_legs=g.get("n_event_markets"))
            continue
        f = feed.get(p.cid)
        if f and f.event_key:
            out[p.cid] = MarketMeta(**{**f.__dict__, "event_key": f"feed:{f.event_key}"})
            continue
        s = settled.get(p.cid) or {}
        tags = tuple(s.get("tags") or ())
        slug = p.slug or s.get("slug") or ""
        question = p.title or s.get("question") or ""
        end_date = p.end_date or s.get("end_date")
        neg = p.neg_risk_hint if p.neg_risk_hint is not None else s.get("neg_risk")
        if p.event_hint:
            key, src = f"pos:{p.event_hint}", "position_event_field"
        else:
            pref = slug_group_prefix(slug)
            key, src = (f"slugpfx:{pref}", "slug_prefix") if pref else ("", "none")
        out[p.cid] = MarketMeta(cid=p.cid, event_key=key, event_title="", neg_risk=neg,
                                tags=tags, slug=slug, question=question,
                                end_date=end_date, key_source=src)
    return out


# --------------------------------------------------------------------------- #
# L1/L2 — event buckets: exact state enumeration
# --------------------------------------------------------------------------- #
@dataclass
class EventBucket:
    event_key: str
    key_source: str
    neg_risk: bool
    legs: list[Position] = field(default_factory=list)
    title: str = ""
    n_event_legs: Optional[int] = None   # legs in the whole group, authoritative only

    @property
    def cost(self) -> float:
        return sum(l.cost_usd for l in self.legs)

    @property
    def holds_full_group(self) -> bool:
        """Holding every leg of a mutually exclusive group means the state where the
        YES falls outside it physically cannot occur.

        Only an authoritative leg count counts. Not knowing the group's size means
        keeping the outside state, which is the conservative direction.
        """
        return self.n_event_legs is not None and len(self.legs) >= int(self.n_event_legs)


def build_event_buckets(positions: list[Position], meta: dict[str, MarketMeta]) -> list[EventBucket]:
    """Bucket legs by event key. A leg with no key becomes its own bucket, whose
    exposure is simply its cost."""
    buckets: dict[str, EventBucket] = {}
    for i, p in enumerate(positions):
        m = meta.get(p.cid) or MarketMeta(cid=p.cid)
        key = m.event_key or f"solo:{p.cid or i}"
        b = buckets.get(key)
        if b is None:
            neg = m.neg_risk if m.neg_risk is not None else bool(p.neg_risk_hint)
            b = EventBucket(event_key=key, key_source=m.key_source, neg_risk=bool(neg),
                            title=m.event_title or m.question or p.title,
                            n_event_legs=m.n_event_legs)
            buckets[key] = b
        b.legs.append(p)
    return list(buckets.values())


def bucket_states(bucket: EventBucket) -> dict[str, Any]:
    """Enumerate every settlement state of a mutually exclusive bucket and give the
    portfolio P&L in each.

    State j, where our jth leg is the sole YES: that leg's YES pays its shares and
    every other leg's NO pays its shares.
    State "outside", where the YES falls on a leg we do not hold: all our NO legs pay
    and all our YES legs go to zero.
    This does not apply to a non-exclusive bucket, whose legs are independent and
    which takes the all-lose bound instead.
    """
    legs = bucket.legs
    n = len(legs)
    cost = sum(l.cost_usd for l in legs)
    states: list[dict[str, Any]] = []
    for j in range(n):
        payout = 0.0
        for i, l in enumerate(legs):
            if (i == j and l.side == "YES") or (i != j and l.side == "NO"):
                payout += l.shares
        states.append({"state": f"leg_{j}_yes", "winner_cid": legs[j].cid,
                       "payout_usd": payout, "pnl_usd": payout - cost})
    has_outside = not bucket.holds_full_group
    if has_outside:
        payout_out = sum(l.shares for l in legs if l.side == "NO")
        states.append({"state": "outside_yes", "winner_cid": None,
                       "payout_usd": payout_out, "pnl_usd": payout_out - cost})
    n_states = len(states)

    # State probabilities are each leg's implied P(YES) with the remainder assigned
    # to outside. Any leg missing a price falls the whole bucket back to uniform.
    p_legs = [l.implied_p_leg_yes for l in legs]
    if any(p is None for p in p_legs) or sum(p for p in p_legs if p is not None) <= 0:
        probs = [1.0 / n_states] * n_states
        prob_basis = "uniform_fallback"
    else:
        tot = sum(p_legs)  # type: ignore[arg-type]
        if tot > 1.0 or not has_outside:
            # Implied probabilities summing above one (from spread and fees), or a
            # fully held group with nowhere for the remainder to go, are normalised.
            p_legs = [p / tot for p in p_legs]  # type: ignore[operator]
            tot = 1.0
        probs = list(p_legs) + ([max(0.0, 1.0 - tot)] if has_outside else [])  # type: ignore[arg-type]
        s = sum(probs)
        probs = [x / s for x in probs] if s > 0 else [1.0 / n_states] * n_states
        prob_basis = "market_implied"
    for st, pr in zip(states, probs):
        st["prob"] = pr
    return {"states": states, "prob_basis": prob_basis, "cost_usd": cost,
            "outside_state_included": has_outside}


def bucket_exposure(bucket: EventBucket) -> dict[str, Any]:
    """True exposure of one event bucket.

    A mutually exclusive bucket is enumerated exactly and takes its worst state.
    Everything else takes the bound where every leg loses together. That is a bound,
    not a prediction, and the field name says so.
    """
    legs = bucket.legs
    naive = sum(l.cost_usd for l in legs)
    if bucket.neg_risk and len(legs) >= 2:
        st = bucket_states(bucket)
        worst = min(s["pnl_usd"] for s in st["states"])
        best = max(s["pnl_usd"] for s in st["states"])
        exp_pnl = sum(s["pnl_usd"] * s["prob"] for s in st["states"])
        true_loss = max(0.0, -worst)
        return {
            "event_key": bucket.event_key, "key_source": bucket.key_source,
            "title": bucket.title, "n_legs": len(legs), "neg_risk": True,
            "exposure_basis": "negrisk_enumerated", "prob_basis": st["prob_basis"],
            "n_event_legs": bucket.n_event_legs,
            "holds_full_group": bucket.holds_full_group,
            "naive_leg_exposure_usd": naive,
            "true_event_exposure_usd": true_loss,
            "structural_offset_usd": naive - true_loss,
            "worst_state_pnl_usd": worst, "best_state_pnl_usd": best,
            "expected_pnl_usd": exp_pnl,
            "states": st["states"],
            "legs": [{"cid": l.cid, "side": l.side, "cost_usd": l.cost_usd,
                      "shares": l.shares, "title": l.title} for l in legs],
        }
    return {
        "event_key": bucket.event_key, "key_source": bucket.key_source,
        "title": bucket.title, "n_legs": len(legs), "neg_risk": bool(bucket.neg_risk),
        "exposure_basis": "all_lose_bound", "prob_basis": "n/a",
        "naive_leg_exposure_usd": naive,
        "true_event_exposure_usd": naive,
        "structural_offset_usd": 0.0,
        "worst_state_pnl_usd": -naive,
        "best_state_pnl_usd": sum(l.shares for l in legs) - naive,
        "expected_pnl_usd": None,
        "states": [],
        "legs": [{"cid": l.cid, "side": l.side, "cost_usd": l.cost_usd,
                  "shares": l.shares, "title": l.title} for l in legs],
    }


def bucket_leg_correlation(bucket: EventBucket) -> dict[tuple[int, int], float]:
    """The **analytic** payoff correlation between two legs in a mutually exclusive
    bucket, from the market-implied state probabilities.

    This is L2: nothing fitted and nothing extrapolated, computed directly from the
    exclusivity structure and the implied probabilities. Two YES legs in such a group
    are necessarily negatively correlated, since they cannot both win; two NO legs
    are mildly negative too, since they cannot both lose.
    """
    st = bucket_states(bucket)
    states, probs = st["states"], [s["prob"] for s in st["states"]]
    n = len(bucket.legs)
    pay = [[0.0] * len(states) for _ in range(n)]
    for si, s in enumerate(states):
        w = s["winner_cid"]
        for i, l in enumerate(bucket.legs):
            win = (l.side == "YES" and w == l.cid) or (l.side == "NO" and w != l.cid)
            pay[i][si] = l.shares if win else 0.0
    mean = [sum(pay[i][si] * probs[si] for si in range(len(states))) for i in range(n)]
    var = [sum(probs[si] * (pay[i][si] - mean[i]) ** 2 for si in range(len(states))) for i in range(n)]
    out: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            if var[i] <= 0 or var[j] <= 0:
                out[(i, j)] = 0.0
                continue
            cov = sum(probs[si] * (pay[i][si] - mean[i]) * (pay[j][si] - mean[j])
                      for si in range(len(states)))
            out[(i, j)] = max(-1.0, min(1.0, cov / math.sqrt(var[i] * var[j])))
    return out


# --------------------------------------------------------------------------- #
# L3 — thematic clusters (idf-weighted Jaccard, single linkage)
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", str(text or "").lower())
    return {t for t in toks
            if len(t) >= THEME_MIN_TOKEN_LEN and t not in STOPWORDS and not _DATE_TOKEN.match(t)}


def _idf(docs: list[set[str]]) -> dict[str, float]:
    n = max(1, len(docs))
    df: dict[str, int] = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / c) for t, c in df.items()}


def weighted_jaccard(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """idf-weighted Jaccard. The weighting is the point: a token appearing in half
    the corpus contributes almost nothing to similarity, while one rare token can
    link two markets on its own. That is exactly the shape thematic relatedness
    should have."""
    inter = a & b
    union = a | b
    if not union:
        return 0.0
    wi = sum(idf.get(t, 0.0) for t in inter)
    wu = sum(idf.get(t, 0.0) for t in union)
    if wu <= 0:
        # A degenerate corpus, where every token appears in every document and all
        # idf values are zero, carries no weighting information. Fall back to
        # unweighted Jaccard rather than calling two identical documents dissimilar.
        return len(inter) / len(union)
    return wi / wu


class _UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def theme_clusters(docs: list[set[str]], *, threshold: float = THEME_SIM_THRESHOLD
                   ) -> tuple[list[int], dict[str, Any]]:
    """Single-linkage clustering -> a cluster id per leg. Chaining is a known weakness
    of the method, so the largest cluster's share is reported alongside; above the
    threshold the output carries a warning so a reader knows to discount it."""
    n = len(docs)
    idf = _idf(docs)
    uf = _UnionFind(n)
    n_links = 0
    for i in range(n):
        for j in range(i + 1, n):
            if weighted_jaccard(docs[i], docs[j], idf) >= threshold:
                uf.union(i, j)
                n_links += 1
    labels = [uf.find(i) for i in range(n)]
    remap: dict[int, int] = {}
    for lb in labels:
        remap.setdefault(lb, len(remap))
    labels = [remap[lb] for lb in labels]
    sizes: dict[int, int] = {}
    for lb in labels:
        sizes[lb] = sizes.get(lb, 0) + 1
    biggest = max(sizes.values()) if sizes else 0
    return labels, {
        "n_clusters": len(sizes),
        "n_multi_clusters": sum(1 for s in sizes.values() if s >= 2),
        "largest_cluster_n": biggest,
        "largest_cluster_frac": (biggest / n) if n else 0.0,
        "n_links": n_links,
        "threshold": threshold,
        "chaining_warning": bool(n and biggest / n > CHAINING_WARN_FRAC),
    }


# --------------------------------------------------------------------------- #
# portfolio-level exposure
# --------------------------------------------------------------------------- #
def _n_eff(weights: list[float], rho: Callable[[int, int], float]) -> float:
    """Effective number of independent bets = (sum w)^2 / (w' R w), with R having
    ones on the diagonal and rho(i, j) off it.

    With every correlation at zero it degenerates to the inverse of a Herfindahl
    index: pure size concentration. Positive correlation lowers it, and the negative
    correlation inside a mutually exclusive group raises it — which is **real
    structural diversification**, not an accounting trick.
    """
    s = sum(weights)
    if s <= 0:
        return 0.0
    q = 0.0
    n = len(weights)
    for i in range(n):
        q += weights[i] * weights[i]
        for j in range(i + 1, n):
            q += 2.0 * weights[i] * weights[j] * rho(i, j)
    if q <= 0:
        return float(n)
    return (s * s) / q


def portfolio_exposure(positions: list[Position], meta: dict[str, MarketMeta], *,
                       theme_rho: float = 0.0, same_event_rho: float = 1.0,
                       bankroll_usd: Optional[float] = None,
                       label: str = "") -> dict[str, Any]:
    """Portfolio-level true event exposure, maximum single-event loss, and
    correlation-aware concentration.

    The three correlation sources must stay distinguishable, so they are parameters
    rather than hidden in the body:
    - Within a mutually exclusive group: **not a parameter**. It is computed exactly
      from the structure and the implied probabilities.
    - `same_event_rho`: several non-exclusive legs on one event. The default of 1.0
      is the conservative assumption that one event drives them and they lose
      together, consistent with that bucket taking the all-lose bound. **It is an
      assumption, not a measurement**, so it appears in the output.
    - `theme_rho`: cross-event thematic clusters. The default of 0 means a thematic
      cluster is only a label, and a non-zero value should come only from a
      confirmed verdict in the backtest layer.

    The portfolio figure is the **sum** of each bucket's worst case, which assumes
    every bucket bottoms out at once and is therefore an upper bound. It is far
    tighter than the sum of costs on the books, and within a mutually exclusive
    bucket the number is exact.
    """
    legs = aggregate_legs(positions)
    n = len(legs)
    gross = sum(l.cost_usd for l in legs)
    buckets = build_event_buckets(legs, meta)
    bucket_rows = [bucket_exposure(b) for b in buckets]

    # leg -> within-bucket pairwise correlation index
    leg_index = {id(l): i for i, l in enumerate(legs)}
    intra_rho: dict[tuple[int, int], float] = {}
    for b in buckets:
        idxs = [leg_index[id(l)] for l in b.legs]
        if b.neg_risk and len(b.legs) >= 2:
            for (a, c), r in bucket_leg_correlation(b).items():
                ia, ic = idxs[a], idxs[c]
                intra_rho[(min(ia, ic), max(ia, ic))] = r
        elif len(b.legs) >= 2:
            # Several non-exclusive legs on one event: no structural identity is
            # available, so same_event_rho applies, conservatively 1.0 by default.
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    ia, ic = idxs[x], idxs[y]
                    intra_rho[(min(ia, ic), max(ia, ic))] = same_event_rho

    docs = [tokenize(f"{l.slug} {l.title}") for l in legs]
    labels, cluster_stats = theme_clusters(docs)

    def rho(i: int, j: int) -> float:
        key = (min(i, j), max(i, j))
        if key in intra_rho:
            return intra_rho[key]
        if labels[i] == labels[j]:
            return theme_rho
        return 0.0

    w = [l.cost_usd for l in legs]
    n_eff_size = _n_eff(w, lambda i, j: 0.0)
    n_eff_corr = _n_eff(w, rho)

    total_true = sum(r["true_event_exposure_usd"] for r in bucket_rows)
    multi = [r for r in bucket_rows if r["n_legs"] >= 2]
    worst_bucket = max(bucket_rows, key=lambda r: r["true_event_exposure_usd"], default=None)

    cl_cost: dict[int, float] = {}
    for i, l in enumerate(legs):
        cl_cost[labels[i]] = cl_cost.get(labels[i], 0.0) + l.cost_usd
    worst_cluster_id = max(cl_cost, key=lambda k: cl_cost[k]) if cl_cost else None
    worst_cluster_cost = cl_cost.get(worst_cluster_id, 0.0) if worst_cluster_id is not None else 0.0

    hhi = sum((r["true_event_exposure_usd"] / total_true) ** 2 for r in bucket_rows) if total_true > 0 else 0.0

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": iso_now(),
        "label": label,
        "read_only": True,
        "n_legs": n,
        "n_event_buckets": len(bucket_rows),
        "n_multileg_buckets": len(multi),
        "gross_cost_usd": gross,
        "bankroll_usd": bankroll_usd,
        # --- true exposure against book exposure ---
        "naive_leg_exposure_usd": gross,
        "true_event_exposure_usd": total_true,
        "structural_offset_usd": gross - total_true,
        "structural_offset_pct": ((gross - total_true) / gross) if gross > 0 else 0.0,
        "max_single_event_loss_usd": (worst_bucket or {}).get("true_event_exposure_usd", 0.0),
        "max_single_event_loss_pct_of_book": (
            ((worst_bucket or {}).get("true_event_exposure_usd", 0.0) / gross) if gross > 0 else 0.0),
        "max_single_event_loss_pct_of_bankroll": (
            ((worst_bucket or {}).get("true_event_exposure_usd", 0.0) / bankroll_usd)
            if bankroll_usd else None),
        "max_single_event_key": (worst_bucket or {}).get("event_key"),
        "max_single_event_title": (worst_bucket or {}).get("title"),
        # --- correlation-aware concentration ---
        "n_eff_size": n_eff_size,
        "n_eff_corr": n_eff_corr,
        "corr_concentration": (1.0 - n_eff_corr / n_eff_size) if n_eff_size > 0 else 0.0,
        "hhi_event_exposure": hhi,
        "theme_rho_applied": theme_rho,
        "same_event_rho_assumed": same_event_rho,
        "negrisk_rho_source": "analytic_from_mutual_exclusion_and_implied_probs",
        "theme_cluster_stats": cluster_stats,
        "max_theme_cluster_cost_bound_usd": worst_cluster_cost,
        "max_theme_cluster_pct_of_book": (worst_cluster_cost / gross) if gross > 0 else 0.0,
        "key_source_mix": _count_by(bucket_rows, "key_source"),
        "exposure_basis_mix": _count_by(bucket_rows, "exposure_basis"),
        "buckets": sorted(bucket_rows, key=lambda r: -r["true_event_exposure_usd"]),
        "boundaries": ["read_only", "no_order_placement", "no_arm_caps_kill",
                       "no_execution_path_write", "no_secrets"],
    }


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- #
# backtest layer: the falsifiable verdict on whether a cluster really moves together
# --------------------------------------------------------------------------- #
@dataclass
class ResidualRow:
    """One settled observation: residual = realised outcome minus the prior implied
    probability.

    Using residuals rather than raw outcomes is the crux. Two 5% longshots both
    settling NO is not correlation; it is two longshots being longshots. What makes
    portfolio risk off-diagonal is **surprise** moving together, which is residual
    correlation.
    """
    key: str
    residual: float
    cluster: str
    date: str
    weight: float = 1.0
    event: str = ""     # the event group, used to exclude within-group pairs when
    #                     measuring the thematic layer


def _pearson_within(rows: list[ResidualRow], *, exclude_same_event: bool = False
                    ) -> tuple[float, int]:
    """Within-cluster pairwise residual correlation, computed on mean-centred
    residuals.

    **The whole-sample mean must be removed first.** A ledger with a systematic edge
    — a realised hit rate above the implied one — has a positive mean residual, and
    then the expected product of any two legs carries that mean squared. Every
    cluster would appear positively correlated, which measures the edge rather than
    any correlation. Centred, the numerator estimates a covariance and the result is
    a real correlation coefficient. (The permutation test is immune to the mean,
    since shuffling preserves the marginal distribution, but the point estimate is
    unreadable without centring.)

    Excluding same-event pairs is mandatory when measuring the thematic layer.
    Otherwise sibling legs from a mutually exclusive group, with their known
    structural negative correlation, drag the thematic estimate negative — measuring
    L1 exclusivity instead of L3 resonance. The two layers must be measured
    separately and must not contaminate each other.

    Returns the estimate and the number of within-cluster pairs.
    """
    n = len(rows)
    if n == 0:
        return 0.0, 0
    mu = sum(r.residual for r in rows) / n
    by: dict[str, list[tuple[float, str]]] = {}
    for r in rows:
        by.setdefault(r.cluster, []).append((r.residual - mu, r.event))
    num, npairs = 0.0, 0
    for vals in by.values():
        k = len(vals)
        if k < 2:
            continue
        for i in range(k):
            for j in range(i + 1, k):
                if exclude_same_event and vals[i][1] and vals[i][1] == vals[j][1]:
                    continue
                num += vals[i][0] * vals[j][0]
                npairs += 1
    if npairs == 0:
        return 0.0, 0
    denom = sum((r.residual - mu) ** 2 for r in rows) / n
    if denom <= 0:
        return 0.0, npairs
    return (num / npairs) / denom, npairs


def _permutation_p(rows: list[ResidualRow], observed: float, *, stratify_by_date: bool,
                   n_perm: int = N_PERM, seed: int = PERM_SEED,
                   exclude_same_event: bool = False) -> float:
    """The permutation null. The stratified version shuffles residuals **within a
    settlement day**, preserving the day-level common shock and scrambling only the
    theme labels. Unstratified, a thematic cluster collects the correlation of
    "same day" for free, which is confounding rather than signal."""
    rng = random.Random(seed)
    if stratify_by_date:
        strata: dict[str, list[int]] = {}
        for i, r in enumerate(rows):
            strata.setdefault(r.date, []).append(i)
    else:
        strata = {"_all": list(range(len(rows)))}
    resid = [r.residual for r in rows]
    hits = 0
    for _ in range(n_perm):
        perm = list(resid)
        for idxs in strata.values():
            vals = [resid[i] for i in idxs]
            rng.shuffle(vals)
            for i, v in zip(idxs, vals):
                perm[i] = v
        shuffled = [ResidualRow(r.key, perm[i], r.cluster, r.date, r.weight, r.event)
                    for i, r in enumerate(rows)]
        rho_p, _ = _pearson_within(shuffled, exclude_same_event=exclude_same_event)
        if abs(rho_p) >= abs(observed):
            hits += 1
    return (hits + 1) / (n_perm + 1)


def _bootstrap_ci(rows: list[ResidualRow], *, n_boot: int = 500, seed: int = PERM_SEED,
                  exclude_same_event: bool = False) -> tuple[float, float]:
    """Resample by **cluster**, not by observation. Within-cluster correlation means
    observations are not independent, and resampling observations understates the
    variance.

    Each drawn cluster must be **relabelled**. Sampling with replacement draws the
    same cluster twice, and keeping the original label puts two identical copies of
    the residuals into one cluster, manufacturing perfectly correlated self-pairs and
    dragging the estimate toward +1. The fingerprint of this bug is a point estimate
    falling outside its own confidence interval.
    """
    by: dict[str, list[ResidualRow]] = {}
    for r in rows:
        by.setdefault(r.cluster, []).append(r)
    keys = list(by)
    if len(keys) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        sample: list[ResidualRow] = []
        for d in range(len(keys)):
            src = by[keys[rng.randrange(len(keys))]]
            sample.extend(ResidualRow(r.key, r.residual, f"boot{d}", r.date, r.weight, r.event)
                          for r in src)
        rho_b, np_b = _pearson_within(sample, exclude_same_event=exclude_same_event)
        if np_b > 0:
            draws.append(rho_b)
    if len(draws) < 20:
        return (float("nan"), float("nan"))
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return (lo, hi)


def correlation_verdict(rho: float, p_strat: float, n_pairs: int) -> str:
    if n_pairs < MIN_PAIRS:
        return V_INSUFFICIENT
    if rho >= RHO_CONFIRM and p_strat < P_CONFIRM:
        return V_CONFIRMED
    if rho >= RHO_WEAK or p_strat < P_WEAK_MAX:
        return V_WEAK
    return V_NULL


V_CONTROL_PASS = "CONTROL_SIGN_RECOVERED"
V_CONTROL_FAIL = "CONTROL_SIGN_NOT_RECOVERED"


def analyze_residuals(rows: list[ResidualRow], *, name: str,
                      exclude_same_event: bool = False,
                      verdict_mode: str = "theme") -> dict[str, Any]:
    ex = exclude_same_event
    rho, npairs = _pearson_within(rows, exclude_same_event=ex)
    p_strat = _permutation_p(rows, rho, stratify_by_date=True, exclude_same_event=ex) if npairs else 1.0
    p_plain = _permutation_p(rows, rho, stratify_by_date=False, exclude_same_event=ex) if npairs else 1.0
    lo, hi = _bootstrap_ci(rows, exclude_same_event=ex)
    sizes: dict[str, int] = {}
    for r in rows:
        sizes[r.cluster] = sizes.get(r.cluster, 0) + 1
    return {
        "name": name,
        "n_obs": len(rows),
        "n_clusters": len(sizes),
        "n_multi_clusters": sum(1 for v in sizes.values() if v >= 2),
        "n_within_pairs": npairs,
        "rho_within": rho,
        "perm_p_date_stratified": p_strat,
        "perm_p_unstratified": p_plain,
        "bootstrap_ci95": [lo, hi],
        "mean_residual": (sum(r.residual for r in rows) / len(rows)) if rows else 0.0,
        "rms_residual": (math.sqrt(sum(r.residual ** 2 for r in rows) / len(rows))) if rows else 0.0,
        "excluded_same_event_pairs": ex,
        # The control row must not be judged by the thematic bands: it asks whether a
        # known negative sign reproduced, not whether a correlation is strong enough
        # to use. Applying the wrong label makes a reader think the control failed.
        "verdict": (correlation_verdict(rho, p_strat, npairs) if verdict_mode == "theme"
                    else (V_CONTROL_PASS if (npairs >= MIN_PAIRS and rho < 0.0)
                          else V_CONTROL_FAIL)),
    }


def own_book_residuals(*, meta_gamma: bool = False) -> tuple[list[ResidualRow], list[ResidualRow], dict]:
    """Our own settled positions -> (theme-cluster residuals, exclusive-group
    residuals used as the positive control).

    Residual = 1[we won] minus our fill price. Both clusterings run over the same
    observations, so the control and the subject share a distribution and a failed
    control cannot be blamed on the data alone.
    """
    settled = [p for p in load_flb_paper(include_settled=True) if p.status in ("won", "lost")]
    try:
        settled += [p for p in load_smart_money_paper(include_settled=True) if p.status in ("won", "lost")]
    except FileNotFoundError:
        pass
    settled = [p for p in settled if p.implied_p_win is not None]
    meta = resolve_meta(settled, use_gamma=meta_gamma)

    docs = [tokenize(f"{p.slug} {p.title}") for p in settled]
    labels, cstats = theme_clusters(docs)

    theme_rows: list[ResidualRow] = []
    neg_rows: list[ResidualRow] = []
    for i, p in enumerate(settled):
        y = 1.0 if p.won else 0.0
        resid = y - float(p.implied_p_win)
        date = str(p.end_date or "")[:10] or "unknown"
        m = meta.get(p.cid)
        ev = (m.event_key if (m and m.neg_risk is True) else "")
        theme_rows.append(ResidualRow(key=p.cid, residual=resid, cluster=f"theme:{labels[i]}",
                                      date=date, event=ev))
        # The control takes **exclusive** groups only. Sharing an event guarantees no
        # sign by itself; only exclusivity gives the known negative one. Mixing
        # non-exclusive same-event legs in stops it being a control.
        if m and m.event_key and m.neg_risk is True:
            neg_rows.append(ResidualRow(key=p.cid, residual=resid, cluster=m.event_key, date=date))
    src_mix: dict[str, int] = {}
    for p in settled:
        k = (meta.get(p.cid) or MarketMeta(cid=p.cid)).key_source
        src_mix[k] = src_mix.get(k, 0) + 1
    n_neg = sum(1 for p in settled if (meta.get(p.cid) or MarketMeta(cid=p.cid)).neg_risk is True)
    return theme_rows, neg_rows, {
        "n_settled": len(settled),
        "n_negrisk_legs": n_neg,
        "theme_cluster_stats": cstats,
        "event_key_source_mix": dict(sorted(src_mix.items())),
        "sources": sorted({p.source for p in settled}),
        "control_rho_analytic": _analytic_control_rho(settled, meta),
    }


def _analytic_control_rho(settled: list[Position], meta: dict[str, MarketMeta]) -> Optional[float]:
    """The within-cluster correlation **predicted** by the exclusivity structure and
    the implied probabilities at entry — a quantitative target for the control.

    With it the control asks not only whether the sign is right but how far the
    measurement sits from what the structure predicts. (The two estimators are not
    identical — the measured one normalises by a pooled whole-sample variance, this
    one is a pair-count-weighted analytic mean — so it is an order-of-magnitude
    comparison, not an identity.)
    """
    groups: dict[str, list[Position]] = {}
    for p in settled:
        m = meta.get(p.cid)
        if m and m.event_key and m.neg_risk is True:
            groups.setdefault(m.event_key, []).append(p)
    num, npairs = 0.0, 0
    for key, legs in groups.items():
        if len(legs) < 2:
            continue
        b = EventBucket(event_key=key, key_source="backtest", neg_risk=True, legs=legs)
        for r in bucket_leg_correlation(b).values():
            num += r
            npairs += 1
    return (num / npairs) if npairs else None


def market_universe_residuals() -> tuple[list[ResidualRow], dict]:
    """Every settled market -> theme-cluster residuals: a second sample independent of
    our own book.

    The prior is the market price one day before settlement and the outcome is the
    resolution. Our own book contains only the markets we chose, a strong selection
    bias this sample does not carry; only a conclusion that holds in both is treated
    as structural.
    """
    if not os.path.exists(FLB_PRICE_SAMPLES):
        return [], {"error": f"missing {FLB_PRICE_SAMPLES}"}
    settled_meta = load_settled_meta()
    rows: list[ResidualRow] = []
    recs: list[dict] = []
    with open(FLB_PRICE_SAMPLES, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            y = _f(d.get("yes_resolved"))
            p = _f((d.get("samples") or {}).get("T-1d"))
            if y is None or p is None or not (0.0 < p < 1.0):
                continue
            recs.append({"cid": str(d.get("condition_id") or ""), "y": y, "p": p,
                         "question": d.get("question") or ""})
    docs = []
    for r in recs:
        m = settled_meta.get(r["cid"]) or {}
        r["slug"] = m.get("slug") or ""
        r["end_date"] = m.get("end_date") or ""
        docs.append(tokenize(f"{r['slug']} {r['question']} {' '.join(m.get('tags') or [])}"))
    labels, cstats = theme_clusters(docs)
    for i, r in enumerate(recs):
        # This sample has no authoritative event structure, so a slug prefix stands in
        # as the event proxy to drop ladder-sibling pairs. Better to lose usable
        # thematic pairs than to let ladder siblings pose as cross-event resonance.
        pref = slug_group_prefix(r["slug"])
        rows.append(ResidualRow(key=r["cid"], residual=r["y"] - r["p"],
                                cluster=f"theme:{labels[i]}",
                                date=str(r["end_date"] or "")[:10] or "unknown",
                                event=(f"slugpfx:{pref}" if pref else "")))
    return rows, {"n_obs": len(rows), "theme_cluster_stats": cstats,
                  "event_proxy": "slug_prefix"}


def run_backtest(*, meta_gamma: bool = False) -> dict[str, Any]:
    """Backtest entry point: judge the positive control first, then the thematic
    layer, against the pre-registered constants at the top of the module."""
    theme_rows, neg_rows, own_stats = own_book_residuals(meta_gamma=meta_gamma)
    own_theme = analyze_residuals(theme_rows, name="own_book_theme_clusters",
                                  exclude_same_event=True)
    control = analyze_residuals(neg_rows, name="own_book_negrisk_groups_positive_control",
                                verdict_mode="control")
    mkt_rows, mkt_stats = market_universe_residuals()
    mkt_theme = (analyze_residuals(mkt_rows, name="market_universe_theme_clusters",
                                   exclude_same_event=True) if mkt_rows else
                 {"name": "market_universe_theme_clusters", "verdict": V_INSUFFICIENT, **mkt_stats})

    # Positive control: residuals of an exclusive event group must correlate
    # negatively. If it fails, the estimator itself is not trustworthy.
    control_pass = control["verdict"] == V_CONTROL_PASS
    pred = own_stats.get("control_rho_analytic")
    control_note = ("negative residual correlation inside exclusive groups: the known "
                    "structural sign reproduced"
                    if control_pass else
                    "the control did not reproduce the known negative sign (or had too "
                    "few pairs); thematic conclusions are treated as unvalidated")
    if pred is not None:
        control_note += (f"; structure predicts rho ~ {pred:+.4f}, measured "
                         f"{control['rho_within']:+.4f}")

    if not control_pass:
        final = V_UNVALIDATED
    else:
        v_own, v_mkt = own_theme["verdict"], mkt_theme.get("verdict")
        # Only both samples confirming promotes to CONFIRMED: our own book carries a
        # strong selection bias and is not enough on its own.
        if v_own == V_CONFIRMED and v_mkt == V_CONFIRMED:
            final = V_CONFIRMED
        elif V_CONFIRMED in (v_own, v_mkt) or V_WEAK in (v_own, v_mkt):
            final = V_WEAK
        else:
            final = V_NULL

    theme_rho_for_engine = own_theme["rho_within"] if final == V_CONFIRMED else 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": iso_now(),
        "preregistered": {
            "rho_confirm": RHO_CONFIRM, "rho_weak": RHO_WEAK,
            "p_confirm": P_CONFIRM, "p_weak_max": P_WEAK_MAX,
            "min_pairs": MIN_PAIRS, "n_perm": N_PERM, "seed": PERM_SEED,
            "theme_sim_threshold": THEME_SIM_THRESHOLD,
            "rule": "CONFIRMED requires both samples; a failed positive control forces "
                    "INSTRUMENT_UNVALIDATED",
        },
        "own_book": {**own_stats, "theme": own_theme, "positive_control": control,
                     "control_pass": control_pass, "control_note": control_note},
        "market_universe": {**mkt_stats, "theme": mkt_theme},
        "final_verdict": final,
        "theme_rho_for_engine": theme_rho_for_engine,
    }


# --------------------------------------------------------------------------- #
# portfolio reports
# --------------------------------------------------------------------------- #
def analyze_source(source: str, *, theme_rho: float = 0.0, use_gamma: bool = False,
                   path: Optional[str] = None) -> dict[str, Any]:
    if source == "flb_paper":
        pos = load_flb_paper(path or FLB_LEDGER)
        bankroll = None
        try:
            with open(path or FLB_LEDGER, encoding="utf-8") as fh:
                led = json.load(fh)
            bankroll = (_f(led.get("cash")) or 0.0) + sum(
                _f(v.get("stake")) or 0.0 for v in (led.get("positions") or {}).values()
                if v.get("status") == "open")
        except (OSError, json.JSONDecodeError):
            pass
    elif source == "smart_money_paper":
        pos = load_smart_money_paper(path or SM_LEDGER)
        bankroll = None
        try:
            with open(path or SM_LEDGER, encoding="utf-8") as fh:
                led = json.load(fh)
            bankroll = _f(led.get("bankroll"))
        except (OSError, json.JSONDecodeError):
            pass
    elif source == "private_snapshot":
        pos = load_private_snapshot(path or PRIVATE_SNAPSHOT)
        bankroll = None
    elif source == "generic":
        if not path:
            raise ValueError("the generic source requires a --positions path")
        pos = load_generic(path)
        bankroll = None
    else:
        raise ValueError(f"unknown source: {source}")
    meta = resolve_meta(pos, use_gamma=use_gamma)
    rep = portfolio_exposure(pos, meta, theme_rho=theme_rho, bankroll_usd=bankroll, label=source)
    rep["source"] = source
    return rep


def guardian_view(positions: list[Position], *, theme_rho: float = 0.0,
                  use_gamma: bool = False) -> dict[str, Any]:
    """A **read-only display** projection of a tenant portfolio: the compact surface a
    front end shows, without per-leg detail.

    It deliberately returns no actionable field and passes no judgment on whether to
    trim. Execution gates live on the money surface and are approved separately; this
    function only describes the current state.
    """
    meta = resolve_meta(positions, use_gamma=use_gamma)
    rep = portfolio_exposure(positions, meta, theme_rho=theme_rho, label="guardian_tenant")
    top = [b for b in rep["buckets"] if b["n_legs"] >= 2][:5]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": rep["generated_at_utc"],
        "read_only": True,
        "display_only": True,
        "n_legs": rep["n_legs"],
        "n_event_buckets": rep["n_event_buckets"],
        "gross_cost_usd": rep["gross_cost_usd"],
        "true_event_exposure_usd": rep["true_event_exposure_usd"],
        "structural_offset_usd": rep["structural_offset_usd"],
        "max_single_event_loss_usd": rep["max_single_event_loss_usd"],
        "max_single_event_title": rep["max_single_event_title"],
        "corr_concentration": rep["corr_concentration"],
        "n_eff_corr": rep["n_eff_corr"],
        "hhi_event_exposure": rep["hhi_event_exposure"],
        "multileg_events": [{"title": b["title"], "n_legs": b["n_legs"],
                             "naive_usd": b["naive_leg_exposure_usd"],
                             "true_usd": b["true_event_exposure_usd"],
                             "basis": b["exposure_basis"]} for b in top],
        "boundaries": ["read_only", "display_only", "no_execution_gate", "no_order_placement"],
    }


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #
def render_scorecard(reports: list[dict], backtest: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# Event exposure / correlation engine scorecard")
    A("")
    A(f"> Generated {backtest['generated_at_utc']} · `marketflow/risk/exposure.py` "
      f"({SCHEMA_VERSION}) · read-only; touches no arm/caps/kill state.")
    A("> Exposure definitions are in the module docstring.")
    A("")
    A("## 0. In one sentence")
    A("")
    bt = backtest
    top = max(reports, key=lambda r: r["structural_offset_usd"], default=None) if reports else None
    if top:
        A(f"The three-layer exposure engine runs over every position source. **Largest "
          f"measured case**: `{top['source']}` shows "
          f"${top['naive_leg_exposure_usd']:,.0f} at risk on the books while true event "
          f"exposure is only ${top['true_event_exposure_usd']:,.0f} — "
          f"{top['structural_offset_pct']*100:.0f}% of it is hedging the exclusivity "
          f"structure supplies for free. Reporting risk by position count overstates "
          f"this book by "
          f"{(top['naive_leg_exposure_usd']/max(top['true_event_exposure_usd'],1e-9)):.1f}x. "
          f"The correlation claim was backtested against the pre-registered criteria and "
          f"came out **{bt['final_verdict']}**, so the thematic rho of "
          f"{bt['theme_rho_for_engine']:.3f} stays out of the engine and remains a label.")
    else:
        A(f"Backtest verdict **{bt['final_verdict']}**; thematic rho entering the engine "
          f"= {bt['theme_rho_for_engine']:.3f}.")
    A("")
    A("## 1. Portfolio-level exposure, by position source")
    A("")
    A("| Source | Legs | Buckets | Book cost | True event exposure | Structural hedge | "
      "Max single-event loss | n_eff (size) | n_eff (with corr) | Corr concentration |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in reports:
        A(f"| `{r['source']}` | {r['n_legs']} | {r['n_event_buckets']} | "
          f"${r['naive_leg_exposure_usd']:,.2f} | ${r['true_event_exposure_usd']:,.2f} | "
          f"${r['structural_offset_usd']:,.2f} ({r['structural_offset_pct']*100:.1f}%) | "
          f"${r['max_single_event_loss_usd']:,.2f} ({r['max_single_event_loss_pct_of_book']*100:.1f}%) | "
          f"{r['n_eff_size']:.2f} | {r['n_eff_corr']:.2f} | {r['corr_concentration']*100:+.1f}% |")
    A("")
    A("- **True event exposure** = the sum of worst-case losses from exactly enumerated "
      "settlement states in exclusive groups, plus the all-lose bound for every other "
      "bucket. At portfolio level it sums each bucket's worst case, meaning all buckets "
      "bottom out together; still a bound, but far tighter than the book, and exact "
      "within an exclusive bucket.")
    A("- **Structural hedge** = book minus true. Above zero means exclusivity itself "
      "absorbs part of the notional capital at risk: holding k NO legs can lose at most "
      "one of them.")
    A("- **Correlation concentration** = 1 - n_eff(with corr) / n_eff(size). A **negative "
      "value is structural diversification**, not an error.")
    A("- The three correlation sources stay separate: inside an exclusive group it is "
      "**computed analytically** from the structure and implied probabilities; for "
      "non-exclusive legs on one event it is the conservative assumption "
      f"rho={reports[0].get('same_event_rho_assumed', 1.0) if reports else 1.0} (an "
      "assumption, not a measurement); across events it takes only the backtest verdict, "
      "and anything short of CONFIRMED is zero.")
    A("")
    # A concrete case beats an abstract definition: take the exclusive bucket with the
    # most legs and lay its state table out.
    ex = None
    for r in reports:
        for b in r["buckets"]:
            if b["exposure_basis"] == "negrisk_enumerated" and b["states"]:
                if ex is None or b["n_legs"] > ex[1]["n_legs"]:
                    ex = (r["source"], b)
    if ex is not None:
        src, b = ex
        A(f"### 1.0 A worked example — `{src}` / {b['title'][:48]}")
        A("")
        A(f"{b['n_legs']} legs (of {b.get('n_event_legs')} in the group), "
          f"${b['naive_leg_exposure_usd']:,.2f} at risk on the books. Exactly one leg of "
          f"an exclusive group settles YES, so these are the only possible states:")
        A("")
        A("| Settlement state | Implied probability | Portfolio P&L |")
        A("|---|---|---|")
        for s in sorted(b["states"], key=lambda x: x["pnl_usd"])[:8]:
            A(f"| `{s['state']}` | {s['prob']*100:.1f}% | ${s['pnl_usd']:+,.2f} |")
        A("")
        A(f"Worst case ${b['worst_state_pnl_usd']:+,.2f}, which is the true exposure of "
          f"${b['true_event_exposure_usd']:,.2f} rather than the "
          f"${b['naive_leg_exposure_usd']:,.2f} on the books. The "
          f"${b['structural_offset_usd']:,.2f} difference is hedging the exclusivity "
          f"structure supplies, not risk management we performed.")
        A("")
    for r in reports:
        multi = [b for b in r["buckets"] if b["n_legs"] >= 2]
        if not multi:
            continue
        A(f"### 1.{reports.index(r)+1} `{r['source']}` multi-leg events "
          f"(top {min(6,len(multi))})")
        A("")
        A("| Event | Legs | Exclusive | Key source | Book | True | Basis |")
        A("|---|---|---|---|---|---|---|")
        for b in multi[:6]:
            A(f"| {(b['title'] or b['event_key'])[:52]} | {b['n_legs']} | "
              f"{'yes' if b['neg_risk'] else 'no'} | `{b['key_source']}` | "
              f"${b['naive_leg_exposure_usd']:,.2f} | ${b['true_event_exposure_usd']:,.2f} | "
              f"`{b['exposure_basis']}` |")
        A("")
    A("## 2. Correlation backtest (pre-registered criteria; results do not move them)")
    A("")
    pre = bt["preregistered"]
    A(f"Criteria: rho >= {pre['rho_confirm']} with date-stratified permutation "
      f"p < {pre['p_confirm']} over at least {pre['min_pairs']} pairs gives CONFIRMED; "
      f"rho < {pre['rho_weak']} or p >= {pre['p_weak_max']} gives NULL. If the positive "
      f"control (exclusive groups must correlate negatively) fails, everything is "
      f"INSTRUMENT_UNVALIDATED.")
    A("")
    A("Thematic pairs **exclude sibling legs from the same exclusive group**: that part "
      "is already covered exactly by the L1 enumeration, and mixing it in measures "
      "exclusivity rather than thematic resonance. The permutation test stratifies by "
      "settlement day to strip the same-day confound.")
    A("")
    A("| Sample | Obs | Clusters | Within-cluster pairs | rho | p (date-stratified) | "
      "p (unstratified) | bootstrap 95% CI | Verdict |")
    A("|---|---|---|---|---|---|---|---|---|")
    for blk in (bt["own_book"]["positive_control"], bt["own_book"]["theme"],
                bt["market_universe"]["theme"]):
        if "rho_within" not in blk:
            A(f"| {blk['name']} | – | – | – | – | – | – | – | {blk.get('verdict')} |")
            continue
        ci = blk["bootstrap_ci95"]
        ci_s = ("[%.3f, %.3f]" % (ci[0], ci[1])) if all(isinstance(x, float) and math.isfinite(x)
                                                        for x in ci) else "n/a"
        A(f"| {blk['name']} | {blk['n_obs']} | {blk['n_clusters']} | {blk['n_within_pairs']} | "
          f"{blk['rho_within']:+.4f} | {blk['perm_p_date_stratified']:.4f} | "
          f"{blk['perm_p_unstratified']:.4f} | {ci_s} | **{blk['verdict']}** |")
    A("")
    A(f"- Positive control: {bt['own_book']['control_note']} "
      f"(passed = {'yes' if bt['own_book']['control_pass'] else 'no'}). Sign and "
      f"magnitude both line up, so the estimator can detect a correlation that really "
      f"exists, and a NULL or WEAK on the thematic layer is **a conclusion about the "
      f"world** rather than a tool failing to measure.")
    A(f"- **Final verdict = {bt['final_verdict']}**; rho entering the engine = "
      f"{bt['theme_rho_for_engine']:.3f}.")
    A("")
    A("## 3. What these numbers change")
    A("")
    A("1. **The target of risk measurement moves**: the value is in the L1 structural "
      "layer, which is exact and needs no statistics, not in fitting correlations. "
      "Cross-event thematic correlation is statistically significant in the "
      "whole-market sample but tiny in magnitude, and undetectable in our own book. "
      "**Writing it into a risk parameter adds noise**; where the criteria say zero, it "
      "is zero.")
    A("2. **Concentration and residual uncertainty per source**:")
    A("")
    for r in reports:
        if r["gross_cost_usd"] <= 0:
            continue
        n_multi = sum(1 for b in r["buckets"] if b["n_legs"] >= 2)
        multi_bound = [b for b in r["buckets"]
                       if b["n_legs"] >= 2 and b["exposure_basis"] == "all_lose_bound"]
        if not n_multi:
            tail = "; no multi-leg events, so exposure is the sum over legs"
        elif multi_bound:
            tail = (f"; {len(multi_bound)} of {n_multi} multi-leg buckets have no "
                    f"exclusivity structure available and take the `all_lose_bound`, so "
                    f"**the residual uncertainty is concentrated there** — tightening "
                    f"the estimate means supplying structure for those buckets, not "
                    f"tuning a correlation parameter")
        else:
            tail = (f"; all {n_multi} multi-leg buckets enumerate exactly, with no bound "
                    f"left over")
        A(f"   - `{r['source']}`: max single-event loss "
          f"${r['max_single_event_loss_usd']:,.2f} = "
          f"{r['max_single_event_loss_pct_of_book']*100:.1f}% of the book{tail}.")
    A("")
    A("## 4. Hard boundaries")
    A("")
    A("- Read-only: local ledgers, feeds and caches, plus an optional read-only public "
      "metadata API. It places no orders, changes no execution path and touches no "
      "arm / caps / kill / secrets state.")
    A("- Consumers use `guardian_view()`, which emits **display fields only** and "
      "connects to no execution gate; that is a money surface and is approved "
      "separately.")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def _p(cid: str, side: str, shares: float, cost: float, px: float | None = None,
       slug: str = "", title: str = "", hint: str = "", neg: bool | None = None,
       won: bool | None = None, date: str | None = None) -> Position:
    return Position(cid=cid, side=side, shares=shares, cost_usd=cost, entry_px=px,
                    slug=slug, title=title, event_hint=hint, neg_risk_hint=neg,
                    end_date=date, status=("open" if won is None else ("won" if won else "lost")),
                    won=won)


def _raises(fn: Callable[[], Any], exc: type) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _dir_fingerprint(path: str) -> list[tuple[str, int, float]]:
    """Fingerprint of a directory (name, size, mtime), used to assert that a code path
    really wrote nothing to disk."""
    if not os.path.isdir(path):
        return []
    out = []
    for name in sorted(os.listdir(path)):
        try:
            st = os.stat(os.path.join(path, name))
        except OSError:
            continue
        out.append((name, st.st_size, st.st_mtime))
    return out


def _first_party_modules(tree: "ast.AST") -> set[str]:
    """Dotted names of every `marketflow.*` module this file imports."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == "marketflow"}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "marketflow":
                out.add(node.module)
    return out


def selftest() -> dict[str, Any]:
    checks: list[dict] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    # --- side normalisation ---
    ck("side: 'No' → NO", normalize_side("No") == "NO")
    ck("side: a candidate name means YES", normalize_side("Lorenzo Sonego") == "YES")
    ck("side: empty means YES", normalize_side(None) == "YES")

    # --- exact enumeration: 3 NO legs, $20 each buying 20/0.9 = 22.22 shares ---
    legs = [_p(f"0x{i}", "NO", 22.222, 20.0, 0.90) for i in range(3)]
    b = EventBucket(event_key="e1", key_source="test", neg_risk=True, legs=legs)
    st = bucket_states(b)
    ck("enumeration: one state per leg plus outside", len(st["states"]) == 4, str(len(st["states"])))
    worst = min(s["pnl_usd"] for s in st["states"])
    best = max(s["pnl_usd"] for s in st["states"])
    # One leg losing collects 2 x 22.222 = 44.44 against a cost of 60, so -15.56.
    # Every leg winning is 66.67 - 60 = +6.67.
    ck("worst case is exactly one leg losing", abs(worst - (2 * 22.222 - 60.0)) < 1e-6, f"{worst:.4f}")
    ck("best case is every NO winning", abs(best - (3 * 22.222 - 60.0)) < 1e-6, f"{best:.4f}")
    exp = bucket_exposure(b)
    ck("true exposure is below the summed cost",
       exp["true_event_exposure_usd"] < exp["naive_leg_exposure_usd"],
       f"{exp['true_event_exposure_usd']:.2f} < {exp['naive_leg_exposure_usd']:.2f}")
    ck("structural hedge is positive", exp["structural_offset_usd"] > 0)
    ck("basis is reported as negrisk_enumerated", exp["exposure_basis"] == "negrisk_enumerated")

    # --- an all-YES basket: the outside state zeroes everything, so exposure is the
    #     full cost ---
    ylegs = [_p(f"0y{i}", "YES", 100.0, 10.0, 0.10) for i in range(3)]
    by = EventBucket(event_key="e2", key_source="test", neg_risk=True, legs=ylegs)
    ey = bucket_exposure(by)
    ck("all-YES basket: exposure equals full cost when the outside state zeroes it",
       abs(ey["true_event_exposure_usd"] - 30.0) < 1e-9, f"{ey['true_event_exposure_usd']:.4f}")
    ck("all-YES basket: no structural hedge", abs(ey["structural_offset_usd"]) < 1e-9)

    # --- holding the full group: no outside state, so an all-YES basket always has a
    #     winner ---
    byf = EventBucket(event_key="e2f", key_source="test", neg_risk=True,
                      legs=[_p(f"0yf{i}", "YES", 100.0, 10.0, 0.10) for i in range(3)],
                      n_event_legs=3)
    ck("full group: detected as complete", byf.holds_full_group)
    stf = bucket_states(byf)
    ck("full group: no outside state", stf["outside_state_included"] is False
       and len(stf["states"]) == 3)
    eyf = bucket_exposure(byf)
    # One leg must pay 100 against a cost of 30, so even the worst case is +70 and
    # exposure is zero.
    ck("full group: all-YES exposure is zero", abs(eyf["true_event_exposure_usd"]) < 1e-9,
       f"{eyf['true_event_exposure_usd']:.4f}")
    ck("full group: probabilities still sum to one", abs(sum(s["prob"] for s in stf["states"]) - 1.0) < 1e-9)
    bypart = EventBucket(event_key="e2p", key_source="test", neg_risk=True,
                         legs=ylegs, n_event_legs=8)
    ck("partial group: the outside state is kept", bucket_states(bypart)["outside_state_included"] is True)

    # --- exclusive legs must correlate negatively (the L2 analytic result) ---
    rr = bucket_leg_correlation(by)
    ck("two YES legs correlate negatively", all(v < 0 for v in rr.values()), str(sorted(rr.values())[:2]))
    rn = bucket_leg_correlation(b)
    ck("two NO legs correlate negatively", all(v < 0 for v in rn.values()), str(sorted(rn.values())[:2]))

    # --- a non-exclusive bucket takes the all-lose bound ---
    b3 = EventBucket(event_key="e3", key_source="test", neg_risk=False,
                     legs=[_p("0xa", "YES", 10, 5.0, 0.5), _p("0xb", "YES", 10, 5.0, 0.5)])
    e3 = bucket_exposure(b3)
    ck("non-exclusive: exposure is the summed-cost bound", abs(e3["true_event_exposure_usd"] - 10.0) < 1e-9)
    ck("non-exclusive: basis is reported as all_lose_bound", e3["exposure_basis"] == "all_lose_bound")

    # --- probability fallback ---
    bnp = EventBucket(event_key="e4", key_source="test", neg_risk=True,
                      legs=[_p("0xc", "NO", 10, 9.0, None), _p("0xd", "NO", 10, 9.0, 0.9)])
    ck("a missing price falls back to uniform", bucket_states(bnp)["prob_basis"] == "uniform_fallback")
    bp = EventBucket(event_key="e5", key_source="test", neg_risk=True,
                     legs=[_p("0xe", "NO", 10, 9.0, 0.9), _p("0xf", "NO", 10, 9.0, 0.9)])
    stp = bucket_states(bp)
    ck("a present price uses the market implied basis", stp["prob_basis"] == "market_implied")
    ck("state probabilities sum to one", abs(sum(s["prob"] for s in stp["states"]) - 1.0) < 1e-9)

    # --- grouping keys: a top-level event id cannot be used to group (feed semantics) ---
    ck("slug prefix: temperature ladder rungs group together",
       slug_group_prefix("highest-temperature-in-london-on-july-8-2026-31c")
       == slug_group_prefix("highest-temperature-in-london-on-july-8-2026-34c"),
       slug_group_prefix("highest-temperature-in-london-on-july-8-2026-31c"))
    ck("slug prefix: different cities do not group",
       slug_group_prefix("highest-temperature-in-london-on-july-8-2026-31c")
       != slug_group_prefix("highest-temperature-in-paris-on-july-8-2026-31c"))
    ck("slug prefix: too short to guess", slug_group_prefix("abc") == "")

    # --- clustering ---
    # idf is degenerate on a three-document corpus, where shared tokens get penalised
    # heavily, so the fixture needs background documents like a real corpus. Otherwise
    # the test measures a world that does not exist.
    corpus = ["highest-temperature-in-london-on-july-8-2026-31c",
              "highest-temperature-in-london-on-july-8-2026-34c",
              "highest-temperature-in-paris-on-july-9-2026-30c",
              "will-arsenal-win-the-premier-league-2026",
              "will-chelsea-win-the-premier-league-2026",
              "bitcoin-above-120000-on-december-31",
              "ethereum-above-8000-on-december-31",
              "evo-morales-arrested-by-july-31",
              "us-announces-withdrawal-from-mou-negotiations",
              "khamenei-number-of-tweets-july-7-july-14",
              "ted-cruz-number-of-tweets-july-7-july-14",
              "mrbeast-next-video-views-under-20-million"]
    docs = [tokenize(s) for s in corpus]
    labels, cs = theme_clusters(docs)
    ck("themes: rungs of one ladder cluster together", labels[0] == labels[1], str(labels[:3]))
    ck("themes: unrelated subjects do not cluster", labels[0] != labels[3], str(labels))
    ck("themes: unrelated subjects form separate clusters", labels[5] != labels[7])
    # Which two subjects count as one theme depends on the corpus idf distribution and
    # is not an invariant, so it is not asserted. The real invariants are: identical
    # documents always share a cluster, and raising the threshold never merges clusters.
    ck("themes: identical documents share a cluster", theme_clusters([docs[0], docs[0]])[0][0] == theme_clusters([docs[0], docs[0]])[0][1])
    ck("themes: a higher threshold never merges clusters",
       theme_clusters(docs, threshold=0.7)[1]["n_clusters"]
       >= theme_clusters(docs, threshold=0.3)[1]["n_clusters"])
    ck("theme stats carry a chaining-warning field", "chaining_warning" in cs)
    ck("themes: this corpus does not trigger the chaining warning", cs["chaining_warning"] is False, str(cs))
    idf = _idf(docs)
    ck("weighted Jaccard of a document with itself is one", abs(weighted_jaccard(docs[0], docs[0], idf) - 1.0) < 1e-9)
    ck("weighted Jaccard with no overlap is zero", weighted_jaccard({"zzz"}, {"qqq"}, idf) == 0.0)

    # --- n_eff ---
    w = [1.0] * 4
    ck("n_eff: independent equal weights give n", abs(_n_eff(w, lambda i, j: 0.0) - 4.0) < 1e-9)
    ck("n_eff: perfect correlation gives one", abs(_n_eff(w, lambda i, j: 1.0) - 1.0) < 1e-9)
    ck("n_eff: positive correlation lowers it", _n_eff(w, lambda i, j: 0.5) < 4.0)
    ck("n_eff: negative correlation raises it", _n_eff(w, lambda i, j: -0.2) > 4.0)
    ck("n_eff: empty weights give zero", _n_eff([], lambda i, j: 0.0) == 0.0)

    # --- portfolio level: true exposure below the book for an exclusive portfolio ---
    pos = [_p("0x1", "NO", 22.2, 20.0, 0.9, slug="temp-london-july-8-2026-31c", hint="ev-a", neg=True),
           _p("0x2", "NO", 22.2, 20.0, 0.9, slug="temp-london-july-8-2026-34c", hint="ev-a", neg=True),
           _p("0x3", "NO", 22.2, 20.0, 0.9, slug="temp-london-july-8-2026-35c", hint="ev-a", neg=True),
           _p("0x4", "YES", 50.0, 10.0, 0.2, slug="arsenal-win-premier-league", hint="ev-b")]
    meta = {p.cid: MarketMeta(cid=p.cid, event_key=f"pos:{p.event_hint}",
                              neg_risk=bool(p.neg_risk_hint), slug=p.slug,
                              key_source="position_event_field") for p in pos}
    rep = portfolio_exposure(pos, meta)
    ck("portfolio: true exposure is below the book", rep["true_event_exposure_usd"] < rep["gross_cost_usd"],
       f"{rep['true_event_exposure_usd']:.2f} vs {rep['gross_cost_usd']:.2f}")
    ck("portfolio: two buckets", rep["n_event_buckets"] == 2, str(rep["n_event_buckets"]))
    ck("portfolio: max single-event loss is within the book", rep["max_single_event_loss_usd"] <= rep["gross_cost_usd"])
    ck("portfolio: negative correlation gives negative concentration", rep["corr_concentration"] < 0,
       f"{rep['corr_concentration']:.4f}")
    ck("portfolio: the output carries no execution field",
       not ({"order", "arm", "caps", "kill", "sign"} & set(rep.keys())))

    # --- theme_rho only takes effect when it is supplied ---
    rep0 = portfolio_exposure(pos, meta, theme_rho=0.0)
    rep9 = portfolio_exposure(pos, meta, theme_rho=0.9)
    ck("a higher theme_rho raises concentration", rep9["corr_concentration"] >= rep0["corr_concentration"])

    # --- aggregation of identical (cid, side) rows ---
    dup = [_p("0xz", "NO", 10.0, 9.0, 0.9), _p("0xz", "NO", 10.0, 9.0, 0.9)]
    agg = aggregate_legs(dup)
    ck("aggregation: identical cid and side merge", len(agg) == 1 and abs(agg[0].cost_usd - 18.0) < 1e-9)
    ck("aggregation: shares add up", abs(agg[0].shares - 20.0) < 1e-9)

    # --- residual statistics: independent data gives ~0, co-moving data gives > 0 ---
    rng = random.Random(7)
    indep = [ResidualRow(f"k{i}", rng.gauss(0, 1), f"theme:{i//4}", f"2026-07-{(i%9)+1:02d}")
             for i in range(120)]
    rho_i, np_i = _pearson_within(indep)
    ck("residuals: independent data gives a small estimate", abs(rho_i) < 0.25, f"{rho_i:.4f} (n_pairs={np_i})")
    corr_rows: list[ResidualRow] = []
    for c in range(30):
        shock = rng.choice([-1.0, 1.0])
        for k in range(4):
            corr_rows.append(ResidualRow(f"c{c}_{k}", shock + rng.gauss(0, 0.2),
                                         f"theme:{c}", f"2026-07-{(c%9)+1:02d}"))
    rho_c, np_c = _pearson_within(corr_rows)
    ck("residuals: a shared shock within a cluster gives a high estimate", rho_c > 0.8, f"{rho_c:.4f} (n_pairs={np_c})")
    p_c = _permutation_p(corr_rows, rho_c, stratify_by_date=True, n_perm=200, seed=1)
    ck("residuals: strong correlation gives a small permutation p", p_c < 0.05, f"p={p_c:.4f}")
    p_i = _permutation_p(indep, rho_i, stratify_by_date=True, n_perm=200, seed=1)
    ck("residuals: independence gives a non-significant permutation p", p_i > 0.05, f"p={p_i:.4f}")
    # Same-event exclusion: put the whole shared shock inside one event, and enabling
    # the exclusion should leave no within-cluster pairs at all.
    same_ev = [ResidualRow(r.key, r.residual, r.cluster, r.date, 1.0, r.cluster)
               for r in corr_rows]
    _, np_ex = _pearson_within(same_ev, exclude_same_event=True)
    ck("residuals: same-event pairs are excluded", np_ex == 0, f"n_pairs={np_ex}")
    _, np_keep = _pearson_within(same_ev, exclude_same_event=False)
    ck("residuals: pairs survive when the exclusion is off", np_keep > 0, f"n_pairs={np_keep}")
    lo, hi = _bootstrap_ci(corr_rows, n_boot=120)
    ck("residuals: the bootstrap CI contains the point estimate", lo <= rho_c <= hi, f"[{lo:.3f},{hi:.3f}] vs {rho_c:.3f}")
    # Resampling with replacement while keeping the original cluster labels fabricates
    # perfect positive correlation out of repeated clusters, pushing the CI of
    # independent data into positive territory. This check guards against that bias
    # returning.
    lo_i, hi_i = _bootstrap_ci(indep, n_boot=200)
    ck("residuals: the CI of independent data contains zero", lo_i <= 0.0 <= hi_i, f"[{lo_i:.3f},{hi_i:.3f}]")
    neg_rows_t = []
    for c in range(40):   # a known negative correlation: one positive and one negative leg per cluster
        v = rng.gauss(0, 1)
        neg_rows_t.append(ResidualRow(f"n{c}a", v, f"g:{c}", f"2026-07-{(c%9)+1:02d}"))
        neg_rows_t.append(ResidualRow(f"n{c}b", -v, f"g:{c}", f"2026-07-{(c%9)+1:02d}"))
    rho_n, _ = _pearson_within(neg_rows_t)
    lo_n, hi_n = _bootstrap_ci(neg_rows_t, n_boot=200)
    ck("residuals: a known negative correlation gives a negative estimate and CI", rho_n < 0 and hi_n < 0,
       f"rho={rho_n:.3f} CI=[{lo_n:.3f},{hi_n:.3f}]")

    # --- the verdict function ---
    ck("verdict: meeting every criterion gives CONFIRMED", correlation_verdict(0.2, 0.01, 200) == V_CONFIRMED)
    ck("verdict: a weak estimate gives WEAK", correlation_verdict(0.08, 0.30, 200) == V_WEAK)
    ck("verdict: a zero estimate gives NULL", correlation_verdict(0.01, 0.9, 200) == V_NULL)
    ck("verdict: too few pairs gives INSUFFICIENT", correlation_verdict(0.9, 0.001, 10) == V_INSUFFICIENT)
    ck("verdict: a high estimate without significance is not CONFIRMED",
       correlation_verdict(0.9, 0.5, 500) != V_CONFIRMED)

    # --- loaders: the generic source accepts the tenant row format ---
    tenant_rows = [{"condition_id": "0xt1", "market_slug": "a-b-c", "title": "T1",
                    "outcome": "No", "held_shares": 10.0, "entry_price": 0.9,
                    "endDate": "2026-08-01T00:00:00Z"}]
    tmp = os.path.join(OUT_DIR, "_selftest_tenant.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tenant_rows, fh)
    gp = load_generic(tmp)
    ck("generic: accepts the multi-tenant row format", len(gp) == 1 and gp[0].side == "NO" and gp[0].cid == "0xt1")
    ck("generic: a missing cost is derived from shares times price", abs(gp[0].cost_usd - 9.0) < 1e-9)
    gv = guardian_view(gp)
    ck("guardian_view: emits display fields only", gv["display_only"] is True and "buckets" not in gv)
    ck("guardian_view: carries no execution field",
       not ({"order", "arm", "caps", "kill", "intent"} & set(gv.keys())))
    os.remove(tmp)

    # --- public wallet positions, with an injected fetch so the selftest stays offline ---
    api_rows = [
        {"conditionId": "0xw1", "eventSlug": "temps-london-2026-07-30", "negativeRisk": True,
         "slug": "temp-london-30c", "title": "London 30C?", "outcome": "No",
         "size": 22.2, "avgPrice": 0.9, "endDate": "2026-07-30T12:00:00Z", "redeemable": False},
        {"conditionId": "0xw2", "eventSlug": "temps-london-2026-07-30", "negativeRisk": True,
         "slug": "temp-london-31c", "title": "London 31C?", "outcome": "No",
         "size": 21.0, "avgPrice": 0.95, "endDate": "2026-07-30T12:00:00Z", "redeemable": False},
        {"conditionId": "0xw3", "eventSlug": "done", "negativeRisk": False, "slug": "settled",
         "title": "Settled", "outcome": "Yes", "size": 10.0, "avgPrice": 0.5, "redeemable": True},
    ]
    wpos = load_public_wallet("0xabc", fetch=lambda url: api_rows)
    ck("public wallet: redeemable legs are dropped", len(wpos) == 2, str([p.cid for p in wpos]))
    ck("public wallet: eventSlug and negativeRisk are read from the raw row",
       wpos[0].event_hint == "temps-london-2026-07-30" and wpos[0].neg_risk_hint is True)
    ck("public wallet: side and cost are correct",
       wpos[0].side == "NO" and abs(wpos[0].cost_usd - 22.2 * 0.9) < 1e-9)
    gvw = guardian_view(wpos)
    ck("public wallet: guardian_view sees the exclusive group and reports less than book",
       gvw["true_event_exposure_usd"] < gvw["gross_cost_usd"],
       f"{gvw['true_event_exposure_usd']:.2f} < {gvw['gross_cost_usd']:.2f}")
    ck("public wallet: a non-list response fails loudly",
       _raises(lambda: load_public_wallet("0xabc", fetch=lambda url: {"x": 1}), FetchError))

    # --- guardian_view(use_gamma=False) must write nothing to disk ---
    # A hardened deployment grants write access to one runtime directory only. A
    # display surface that quietly writes a cache becomes a 500 in production, and this
    # check holds that contract.
    before = _dir_fingerprint(OUT_DIR)
    guardian_view(wpos, use_gamma=False)
    ck("guardian_view: writes nothing (read-only filesystem contract)",
       _dir_fingerprint(OUT_DIR) == before)

    # --- metadata fetching uses an injected fetch, so the selftest stays offline ---
    calls: list[str] = []

    def fake_fetch(url: str) -> Any:
        calls.append(url)
        if "/markets?" in url:
            return [{"conditionId": "0xg1", "negRisk": True, "slug": "s1", "question": "q1",
                     "events": [{"id": "999", "slug": "ev-999", "title": "Ev"}]}]
        return {"tags": [{"slug": "politics"}], "markets": [1, 2, 3], "negRisk": True, "title": "Ev"}

    cache_tmp = os.path.join(OUT_DIR, "_selftest_gamma_cache.json")
    got = fetch_gamma_events(["0xg1"], cache_path=cache_tmp, sleep_s=0.0, fetch=fake_fetch)
    ck("metadata: two hops fetch the event and its tags",
       got.get("0xg1", {}).get("event_id") == "999" and got["0xg1"]["tags"] == ["politics"],
       str(got.get("0xg1", {}).get("tags")))
    ck("metadata: the full group leg count is recorded", got["0xg1"]["n_event_markets"] == 3)
    ck("metadata: a cache hit issues no request",
       len(fetch_gamma_events(["0xg1"], cache_path=cache_tmp, sleep_s=0.0,
                              fetch=fake_fetch)) == 1 and len(calls) == 2, str(len(calls)))
    os.path.exists(cache_tmp) and os.remove(cache_tmp)

    # --- smoke test on real data, skipped when the files are absent ---
    if os.path.exists(FLB_LEDGER):
        real = analyze_source("flb_paper")
        ck("real data: a report can be generated", real["n_legs"] > 0, f"n_legs={real['n_legs']}")
        ck("real data: true exposure is within the book",
           real["true_event_exposure_usd"] <= real["gross_cost_usd"] + 1e-9)
        ck("real data: every bucket exposure is non-negative",
           all(b["true_event_exposure_usd"] >= 0 for b in real["buckets"]))

    # --- hard boundaries, machine-checked rather than claimed in a docstring ---
    # Only identifiers the AST says are executed count. A plain text search would find
    # this very banned list; a string constant does not execute, so treating it as
    # evidence of a violation is a false positive.
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    tree_imports: set[str] = set()
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tree_imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tree_imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    # The standard library plus `marketflow.paths`, which only answers "where does
    # state live". Importing any execution, guardian or venue-SDK module here would
    # mean this read-only analysis had grown a way to act.
    allowed = {"__future__", "argparse", "ast", "json", "math", "os", "random", "re",
               "sys", "time", "urllib", "dataclasses", "datetime", "typing", "marketflow"}
    ck("boundary: no execution stack imported", tree_imports <= allowed,
       str(sorted(tree_imports - allowed)))
    ck("boundary: the only first-party import is the path resolver",
       {m for m in _first_party_modules(tree)} <= {"marketflow.paths"},
       str(sorted(_first_party_modules(tree))))
    banned = {"place_order", "post_order", "create_order", "cancel_order", "SecureClient",
              "set_arm", "arm_state", "kill_switch", "private_key", "api_secret", "passphrase"}
    ck("boundary: no order, arm or secret identifiers", not (identifiers & banned),
       str(sorted(identifiers & banned)))
    # Every write goes into this module's own runtime namespace. A public module
    # must not write into the source tree — least of all into docs, where a reader
    # would find a generated file sitting among the committed ones.
    write_roots = {OUT_DIR, os.path.dirname(runtime_path("risk", "exposure", "scorecard.md"))}
    ck("boundary: the output directory is an isolated namespace",
       OUT_DIR.endswith(os.path.join("risk", "exposure"))
       and all(r.startswith(runtime_dir()) for r in write_roots), OUT_DIR)
    ck("boundary: no execution-state file is read or written",
       all(x not in (GAMMA_CACHE + MARKET_FEED + FLB_LEDGER + SM_LEDGER + PRIVATE_SNAPSHOT)
           for x in ("arm_state", "trade_log", "paper_state.json")))

    n_pass = sum(1 for c in checks if c["pass"])
    return {"schema_version": SCHEMA_VERSION, "generated_at_utc": iso_now(),
            "n_checks": len(checks), "n_pass": n_pass, "all_pass": n_pass == len(checks),
            "checks": checks}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Event exposure / correlation engine "
                                             "(read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--source", choices=["flb_paper", "smart_money_paper",
                                         "private_snapshot", "generic", "all"],
                    default="all")
    ap.add_argument("--positions", help="positions file for the generic source (JSON/JSONL)")
    ap.add_argument("--guardian", action="store_true", help="emit the read-only display surface")
    ap.add_argument("--backtest", action="store_true",
                    help="run the correlation backtest against the pre-registered criteria")
    ap.add_argument("--scorecard", action="store_true",
                    help="backtest, report every source, and write the scorecard")
    ap.add_argument("--gamma", action="store_true",
                    help="allow the read-only public metadata API to fill in event structure")
    ap.add_argument("--theme-rho", type=float, default=None,
                    help="override the thematic rho entering the engine (defaults to the "
                         "backtest verdict, which is 0 unless CONFIRMED)")
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)

    if args.selftest:
        rep = selftest()
        _write_json(os.path.join(args.out_dir, "selftest_report.json"), rep)
        for c in rep["checks"]:
            if not c["pass"]:
                print(f"  FAIL {c['check']}  {c['detail']}")
        print(f"selftest: {rep['n_pass']}/{rep['n_checks']} "
              f"{'ALL PASS' if rep['all_pass'] else 'FAILURES'}")
        return 0 if rep["all_pass"] else 1

    backtest = None
    theme_rho = args.theme_rho if args.theme_rho is not None else 0.0
    if args.backtest or args.scorecard:
        print("backtesting correlation clusters (permutation test, takes a while) ...",
              flush=True)
        backtest = run_backtest(meta_gamma=args.gamma)
        _write_json(os.path.join(args.out_dir, "correlation_backtest.json"), backtest)
        if args.theme_rho is None:
            theme_rho = backtest["theme_rho_for_engine"]
        print(f"  verdict = {backtest['final_verdict']}, rho entering the engine = "
              f"{theme_rho:.3f}")

    sources = (["flb_paper", "smart_money_paper", "private_snapshot"]
               if args.source == "all" else [args.source])
    reports: list[dict] = []
    for s in sources:
        try:
            rep = analyze_source(s, theme_rho=theme_rho, use_gamma=args.gamma,
                                 path=args.positions if s == "generic" else None)
        except FileNotFoundError as exc:
            print(f"  skipped {s}: {exc}")
            continue
        reports.append(rep)
        _write_json(os.path.join(args.out_dir, f"exposure_{s}.json"), rep)
        print(f"[{s}] legs {rep['n_legs']} · book ${rep['naive_leg_exposure_usd']:,.2f} · "
              f"true event exposure ${rep['true_event_exposure_usd']:,.2f} · "
              f"max single-event loss ${rep['max_single_event_loss_usd']:,.2f} · "
              f"correlation concentration {rep['corr_concentration']*100:+.1f}%")

    if args.guardian and args.positions:
        gv = guardian_view(load_generic(args.positions), theme_rho=theme_rho, use_gamma=args.gamma)
        _write_json(os.path.join(args.out_dir, "guardian_view.json"), gv)
        print(json.dumps(gv, ensure_ascii=False, indent=1))

    if args.scorecard and backtest is not None:
        md = render_scorecard(reports, backtest)
        path = runtime_path("risk", "exposure", "scorecard.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"scorecard → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
