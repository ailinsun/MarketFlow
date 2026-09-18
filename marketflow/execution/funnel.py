#!/usr/bin/env python3
"""Execution funnel — where an intent leaks between emission and a real fill
(read-only).

**Why it exists.** Improving a fill rate is impossible while nothing can honestly
answer what the fill rate is. A ledger that records a post-only order as filled
the moment the venue accepts it onto the book reports a fill rate of 100% while
the account's real trade history shows a fraction of that. **Until a metric is
fixed, every optimisation made against it optimises an illusion.**

Definitions, each of which determines the conclusion and is therefore pinned here:

- **The only arbiter of a fill is a TRADE row in the account's public activity**,
  never an order receipt. A receipt says the venue accepted the order; it knows
  nothing about whether it later filled. A post-only order returns its receipt the
  moment it rests, and a fill hours later produces no second receipt.
- An intent's outcome takes the strongest available evidence: really filled beats
  rested-unfilled beats never placed.
- The denominator is **every intent emitted**, not the ones that made it to an
  order. The overwhelming majority of the loss happens before placement, and using
  "fill rate among orders actually placed" as the KPI hides the largest hole.

**Pre-registered tests** (fixed here, not adjusted after seeing results):
- If the post-placement fill rate is at least 60%, the resting-order step is not
  the bottleneck, and the effort belongs before placement instead.
- If the emit-to-placement rate is below 20%, the verdict is PLACEMENT_BOUND: the
  main bottleneck is before placement, so fix that first.
- Converting to a taker order is only worthwhile when
  `p_win - ask - fee > P(fill) * (p_win - maker_px)`. Otherwise report honestly
  that it is not worth it and stay a maker. A falling hazard alone is not a reason
  to cross.

The fee mechanism is the verified one: **a maker pays zero, a taker pays
`rate * p * (1 - p)`**.

Hard boundary: read-only over the intent queue, the execution ledger and the
account's public activity, all of which are public on-chain data. It places no
order, changes no execution path, and touches no arm state, caps, kill file,
secret or live process. Output goes only to its own isolated namespace.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict

from marketflow.paths import PROJECT_DIR as REPO, runtime_path
# The funnel reads what the execution layer writes. These must name the same files
# as the writers' own constants; tests/test_runtime_layout.py asserts that they do,
# because a reader pointed at a path nothing writes reports an empty funnel forever.
INTENT_QUEUE = runtime_path("execution", "polymarket_intents.jsonl")
ORDER_LEDGER = runtime_path("execution", "orders", "execution_ledger.jsonl")
DAEMON_LEDGER = runtime_path("execution", "daemon", "ledger.jsonl")
OUT_DIR = runtime_path("execution/funnel")
DATA_API = "https://data-api.polymarket.com"

# Two fee tiers are reported rather than one, so a conclusion cannot rest on a
# single rate. A survey of several hundred markets found one modal rate with a
# handful of outliers above and below it, and a few charging nothing. Using the
# highest observed rate as a conservative proxy is misleading: it corresponds to a
# couple of markets out of hundreds.
TAKER_FEE_RATES = (0.03, 0.05)

# pre-registered thresholds (see the module docstring)
MAKER_NOT_BOTTLENECK_FILL_RATE = 0.60
PLACEMENT_BOUND_RATE = 0.20
# The daemon's entry edge buffer, used during attribution to reproduce its hard
# edge gate.
DEFAULT_EDGE_BUFFER = 0.05


def _ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# the three data sources
# --------------------------------------------------------------------------- #
def load_intents(source: str | None = None, *, path: str = INTENT_QUEUE) -> list[dict]:
    """Intents as emitted, from the append-only queue; `source` narrows to one
    signal source, and None keeps every row."""
    return [r for r in _jsonl(path) if source is None or r.get("source") == source]


def load_placements(*, path: str = ORDER_LEDGER, prefix: str = "") -> list[dict]:
    """The intents the execution layer confirmed as accepted.

    Note that this reads **only that the venue took the order**, never whether it
    filled: a receipt cannot answer that."""
    out = []
    for r in _jsonl(path):
        if r.get("mode") != "LIVE_EXECUTED" or r.get("executed") is not True:
            continue
        key = str(r.get("idempotency_key") or "")
        if prefix and not key.startswith(prefix):
            continue
        it = r.get("intent") if isinstance(r.get("intent"), dict) else {}
        out.append({
            "idempotency_key": key,
            "placed_at": r.get("generated_at"),
            "market_slug": it.get("market_slug"),
            "market_id": it.get("market_id"),
            "price": it.get("price"),
            "size": it.get("size"),
            "post_only": it.get("post_only"),
            "receipt_status": (r.get("receipt") or {}).get("status"),
        })
    return out


def _openers() -> list["urllib.request.OpenerDirector"]:
    """Direct first; then the deployment's egress proxy, if one is configured.

    This is the instrument that diagnoses why the money path cannot place an
    order. With a single path, a network failure on that path would blind it
    exactly when it is needed, and a funding problem would look like an execution
    problem. Without MARKETFLOW_POLYMARKET_PROXY_URL there is only the direct path.
    """
    tunnel = os.environ.get("MARKETFLOW_POLYMARKET_PROXY_URL", "").strip()
    openers = [urllib.request.build_opener(urllib.request.ProxyHandler({}))]
    if tunnel:
        openers.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": tunnel, "https": tunnel})))
    return openers


def fetch_account_trades(funder_address: str, *, max_pages: int = 8, timeout: float = 25.0) -> list[dict]:
    """The account's public trade history: the only arbiter of whether something
    filled. A read-only public endpoint, with no credential."""
    rows: list[dict] = []
    for page in range(max_pages):
        url = f"{DATA_API}/activity?user={funder_address}&limit=500&offset={page * 500}"
        req = urllib.request.Request(url, headers={"User-Agent": "marketflow-execution-funnel/0.1 (read-only)"})
        page_rows = None
        last_exc: Exception | None = None
        for opener in _openers():
            try:
                with opener.open(req, timeout=timeout) as resp:
                    page_rows = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:  # noqa: BLE001 - try the next route
                last_exc = exc
        if page_rows is None:
            print(f"[funnel] activity page {page} failed (direct and tunnel both down): {last_exc}",
                  file=sys.stderr)
            break
        if not isinstance(page_rows, list) or not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < 500:
            break
    return rows


# --------------------------------------------------------------------------- #
# the funnel
# --------------------------------------------------------------------------- #
def build_funnel(intents: list[dict], placements: list[dict], trades: list[dict]) -> dict:
    """Emitted -> rested -> really filled: absolute counts at each layer, with the
    loss attributed."""
    bought = {str(t.get("slug")) for t in trades if str(t.get("side") or "").upper() == "BUY"}
    placed_by_key = {p["idempotency_key"]: p for p in placements}
    per_intent = []
    for it in intents:
        key = str(it.get("idempotency_key") or "")
        slug = str(it.get("market_slug") or "")
        placed = placed_by_key.get(key)
        per_intent.append({
            "idempotency_key": key,
            "market_slug": slug,
            "created_at": it.get("created_at"),
            "expires_at": it.get("expires_at"),
            "close_time": it.get("close_time"),
            "ask_price": it.get("ask_price"),
            "max_price": it.get("max_price"),
            "placed": placed is not None,
            "placed_price": (placed or {}).get("price"),
            "filled": slug in bought,
        })
    n = len(per_intent)
    n_placed = sum(1 for r in per_intent if r["placed"])
    n_filled = sum(1 for r in per_intent if r["filled"])
    # Rested but unfilled versus never placed: two entirely different problems with
    # different fixes.
    placed_unfilled = [r for r in per_intent if r["placed"] and not r["filled"]]
    never_placed = [r for r in per_intent if not r["placed"]]
    return {
        "n_emitted": n,
        "n_placed": n_placed,
        "n_filled": n_filled,
        "placement_rate": round(n_placed / n, 4) if n else None,
        "fill_rate_given_placed": round(n_filled / n_placed, 4) if n_placed else None,
        "end_to_end_rate": round(n_filled / n, 4) if n else None,
        "loss_before_placement": len(never_placed),
        "loss_after_placement": len(placed_unfilled),
        "per_intent": per_intent,
    }


def placement_loss_attribution(funnel: dict, *, band_high: float, band_low: float,
                               p_win: float, edge_buffer: float) -> dict:
    """What actually kills the large pre-placement loss, classified by whether the
    price at emission would have passed the daemon's own gate.

    This is the first question to ask, because it separates two different diseases:
      - **Dead on arrival**: the price was already outside the band at emission, so
        every tick refused it until it expired. It never had a chance, and the fix
        is to not emit it — a pre-filter at the emission end. Resting price, TTL and
        taker conversion are all irrelevant to it.
      - **Could have filled but did not**: the price passed the gate at emission and
        the intent died later to drift, budget or timing. This is the class that TTL
        and coverage can actually save.

    Collapsing both into one placement rate sends people to fix the second while
    the first dominates.
    """
    rows = [r for r in funnel.get("per_intent", []) if isinstance(r.get("ask_price"), (int, float))]
    buckets = Counter()
    for r in rows:
        px = float(r["ask_price"])
        if r.get("placed"):
            buckets["placed"] += 1
            continue
        limit_px = round(min(0.99, px * 1.01), 4)
        if not (band_low <= limit_px <= band_high):
            buckets["dead_on_arrival_out_of_band"] += 1
        elif not (p_win > px + edge_buffer):
            buckets["dead_on_arrival_no_edge"] += 1
        else:
            buckets["eligible_but_never_placed"] += 1
    n = len(rows)
    doa = buckets["dead_on_arrival_out_of_band"] + buckets["dead_on_arrival_no_edge"]
    return {
        "n_priced": n,
        "counts": dict(buckets),
        "dead_on_arrival_frac": round(doa / n, 4) if n else None,
        "eligible_placement_rate": (round(buckets["placed"] / (buckets["placed"] + buckets["eligible_but_never_placed"]), 4)
                                    if (buckets["placed"] + buckets["eligible_but_never_placed"]) else None),
        "reading": ("the dead-on-arrival share is unrelated to resting price, TTL or "
                    "taker conversion; its fix is a pre-filter at emission. Only "
                    "eligible_but_never_placed is the pool that coverage and TTL can save."),
    }


def fill_by_time_to_close(funnel: dict, placements: list[dict],
                          *, bins_min: tuple = (15.0, 60.0, 240.0)) -> dict:
    """Fill rate stratified by how long remained until market close at placement.

    Why this deserves its own measurement: a strategy that prioritises markets
    later in their life does so for a selection reason — the closer to resolution,
    the more certain the winner. But the same ordering also decides **how much time
    a resting order has left to fill**, and a post-only order only fills when
    somebody crosses into it.

    Selection and execution point in opposite directions here: the safest ticket is
    simultaneously the hardest one to get filled.
    """
    placed_at = {p["idempotency_key"]: _ts(p.get("placed_at")) for p in placements}
    rows = []
    for r in funnel.get("per_intent", []):
        if not r.get("placed"):
            continue
        t0, tc = placed_at.get(r["idempotency_key"]), _ts(r.get("close_time"))
        if not (t0 and tc):
            continue
        rows.append({"minutes_to_close": (tc - t0).total_seconds() / 60.0,
                     "filled": bool(r.get("filled")), "market_slug": r.get("market_slug")})
    if not rows:
        return {"n": 0}
    edges = list(bins_min) + [float("inf")]
    strata = []
    prev = 0.0
    for hi in edges:
        sel = [r for r in rows if prev <= r["minutes_to_close"] < hi]
        if sel:
            k = sum(1 for r in sel if r["filled"])
            strata.append({"minutes_to_close": f"[{prev:g}, {hi:g})", "n": len(sel),
                           "filled": k, "fill_rate": round(k / len(sel), 4)})
        prev = hi
    return {
        "n": len(rows),
        "by_time_to_close": strata,
        "reading": ("if the fill rate rises monotonically with time remaining, then "
                    "prioritising late-life markets is right for selection and wrong for "
                    "execution: it systematically rests orders in the markets with the "
                    "least time to fill."),
    }


def real_money_pnl(activity_rows: list[dict], *, slug_prefix: str = "highest-temperature-in-") -> dict:
    """Did real money actually make money — the only decisive evidence for whether to
    commit more.

    **Aggregate by condition, not by fill.** One market can be bought across several
    fills while the redemption is a single row; pairing fill by fill matches every
    one of them against the whole redemption and inflates the return out of nothing.
    Half the reason this function exists is that assembling the table by hand makes
    exactly that mistake.

    **Count settled markets only.** A market that has filled but not yet resolved
    redeems zero, which is not a loss — it has not been decided. Treating it as a
    loss can flip a positive ROI to a sharply negative one, reversing the direction
    entirely.

    It also reports a **fragility** measure: what remains after removing the single
    most profitable market. On a small sample, "the book is positive" often means
    only that the largest position happened to win — which is leverage on luck, not
    an edge.
    """
    by_market: dict[str, dict] = defaultdict(
        lambda: {"buy": 0.0, "sell": 0.0, "redeem": 0.0, "n_buys": 0, "slug": "", "redeemed": False})
    for r in activity_rows:
        cid = str(r.get("conditionId") or "")
        slug = str(r.get("slug") or "")
        if not cid or (slug_prefix and not slug.startswith(slug_prefix)):
            continue
        m = by_market[cid]
        m["slug"] = m["slug"] or slug
        usd = float(r.get("usdcSize") or 0)
        typ = str(r.get("type") or "").upper()
        if typ == "TRADE":
            if str(r.get("side") or "").upper() == "BUY":
                m["buy"] += usd
                m["n_buys"] += 1
            else:
                m["sell"] += usd
        elif typ == "REDEEM":
            m["redeem"] += usd
            m["redeemed"] = True
    settled = {c: m for c, m in by_market.items()
               if m["buy"] > 0 and (m["redeemed"] or m["sell"] > 0)}
    open_mkts = {c: m for c, m in by_market.items() if m["buy"] > 0 and c not in settled}
    if not settled:
        return {"n_settled_markets": 0, "verdict": "NO_SETTLED_DATA"}
    # The Wilson interval lives in the fill model, which owns the statistics for
    # this line; it is not duplicated here.
    from marketflow.execution.fill_model import wilson
    nets = {c: m["redeem"] + m["sell"] - m["buy"] for c, m in settled.items()}
    invested = sum(m["buy"] for m in settled.values())
    returned = sum(m["redeem"] + m["sell"] for m in settled.values())
    wins = sum(1 for v in nets.values() if v > 0)
    n = len(settled)
    best_cid = max(nets, key=lambda c: nets[c])
    inv_ex = invested - settled[best_cid]["buy"]
    ret_ex = returned - (settled[best_cid]["redeem"] + settled[best_cid]["sell"])
    lo, hi = wilson(wins, n)
    return {
        "n_settled_markets": n,
        "n_buy_fills": sum(m["n_buys"] for m in settled.values()),
        "invested_usd": round(invested, 2),
        "returned_usd": round(returned, 2),
        "net_usd": round(returned - invested, 2),
        "roi": round((returned - invested) / invested, 4) if invested else None,
        "win_rate": round(wins / n, 4),
        "win_rate_ci95": [round(lo, 4), round(hi, 4)],
        "best_market_net_usd": round(nets[best_cid], 2),
        "net_excluding_best_usd": round(ret_ex - inv_ex, 2),
        "roi_excluding_best": round((ret_ex - inv_ex) / inv_ex, 4) if inv_ex else None,
        "carried_by_single_market": bool((returned - invested) > 0 >= (ret_ex - inv_ex)),
        "open_markets": len(open_mkts),
        "open_cost_usd": round(sum(m["buy"] for m in open_mkts.values()), 2),
        "reading": ("carried_by_single_market means the positive sign comes entirely "
                    "from one market and turns negative without it: leverage on luck, "
                    "not evidence of an edge."),
    }


def _median_placed_price(funnel: dict, fallback: float = 0.80) -> float:
    """Median placed price, used to size what one minimum order costs. With no
    sample it falls back conservatively."""
    px = sorted(float(r["placed_price"]) for r in funnel.get("per_intent", [])
                if isinstance(r.get("placed_price"), (int, float)) and 0 < r["placed_price"] < 1)
    return px[len(px) // 2] if px else fallback


def capital_availability(ledger_rows: list[dict], *, typical_price: float,
                         exchange_min_shares: float = 5.0) -> dict:
    """Does the account have enough for **one minimum order** — a gate that gets
    mistaken for an execution problem whenever nobody checks it.

    The third ceiling on sizing is the real account balance. Below the venue's
    minimum order notional, every candidate is refused for want of a size — and the
    symptom, qualified candidates with zero orders, is **identical** to a bad
    resting price, too short a TTL, or a case for crossing the spread. The cause and
    the fix have nothing in common.

    The typical shape: available balance far below the minimum order notional all
    day, every in-band candidate placing nothing, and execution quality entirely
    irrelevant — the money is locked in settled positions nobody redeemed.

    So read this first: when it is zero, none of the execution-layer numbers below
    constitute evidence of anything.
    """
    min_notional = round(exchange_min_shares * typical_price, 4)
    vals = [r.get("available_collateral_usd") for r in ledger_rows]
    vals = [float(v) for v in vals if isinstance(v, (int, float))]
    if not vals:
        return {"n_ticks": 0, "min_notional_usd": min_notional,
                "reason": "the ledger carries no balance record; nothing is guessed"}
    starved = sum(1 for v in vals if v * 0.98 < min_notional)
    return {
        "n_ticks": len(vals),
        "min_notional_usd": min_notional,
        "latest_collateral_usd": vals[-1],
        "min_collateral_usd": min(vals),
        "max_collateral_usd": max(vals),
        "ticks_below_one_min_order": starved,
        "starved_frac": round(starved / len(vals), 4),
        "can_fund_an_order_now": bool(vals[-1] * 0.98 >= min_notional),
        "reading": ("a high starved fraction means \"qualified candidates, no orders\" is "
                    "a funding problem rather than an execution one, and changing the "
                    "resting price, the TTL or the taker rule changes nothing at all."),
    }


def redeemable_capital(funder_address: str, *, min_notional: float,
                       timeout: float = 25.0) -> dict:
    """When the balance is short: is the money **locked in settled positions** or
    **already spent**? One symptom, opposite fixes.

    The previous measure answers only whether there is enough. Once there is not,
    two different causes remain: positions settled but never redeemed, where the
    money is there and redeeming returns it, or principal already lost or spent,
    where redemption returns zero and only a deposit helps.

    A starved account has been inferred to be the first case purely because it
    followed a venue change in time — but sequence is not causation, and this is the
    number that decides it.

    The test: is the redeemable value — the sum of current values, which is zero for
    a losing position — enough for one minimum order? Enough means CAPITAL_LOCKED and
    the fix is to redeem; not enough means CAPITAL_SPENT and redeeming cannot help.

    It also records the fee-inclusive cost-basis fields where the venue provides
    them. Those give an authoritative entry fee for the first time, so the rate
    actually charged per market can be solved for instead of assuming the highest
    tier as an upper bound — an assumption that systematically condemns price bands
    that were in fact workable.
    """
    url = f"{DATA_API}/positions?user={funder_address}&limit=100"
    req = urllib.request.Request(url, headers={"User-Agent": "marketflow-execution-funnel/0.1 (read-only)"})
    rows = None
    last_exc: Exception | None = None
    for opener in _openers():
        try:
            with opener.open(req, timeout=timeout) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001 - try the next route
            last_exc = exc
    if rows is None:
        # A failed fetch returns empty-handed rather than pretending there are no
        # stranded positions, which would collapse CAPITAL_SPENT and "we could not
        # see" into the same conclusion.
        return {"verdict": "NO_DATA", "reason": f"positions unavailable (direct and tunnel both down): {last_exc}"}
    if not isinstance(rows, list):
        return {"verdict": "NO_DATA", "reason": "positions did not return a list"}
    return summarize_positions(rows, min_notional=min_notional)


def summarize_positions(rows: list[dict], *, min_notional: float) -> dict:
    """The pure-function core of redeemable_capital. Fetching and judging are kept
    apart so the judgement is testable."""
    def f(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    redeemable = [r for r in rows if r.get("redeemable")]
    red_value = sum(f(r, "currentValue") for r in redeemable)
    total_initial = sum(f(r, "initialValue") for r in rows)

    fees = []
    for r in rows:
        fee = r.get("entryFeesUsdc")
        if fee is None:
            continue
        price, size = f(r, "avgPrice"), f(r, "size")
        denom = price * (1.0 - price) * size
        fees.append({
            "slug": r.get("slug"), "avg_price": price, "size": size,
            "entry_fees_usdc": float(fee),
            # The fee is rate * p * (1 - p) per share, so solving back for the rate is
            # what makes prices comparable. At p of 0 or 1 the inversion is undefined:
            # leave None rather than 0, which would read as a zero-fee market.
            "implied_rate": round(float(fee) / denom, 6) if denom > 1e-12 else None,
        })

    if not rows:
        verdict = "NO_POSITIONS"
    elif red_value >= min_notional:
        verdict = "CAPITAL_LOCKED"
    elif redeemable:
        verdict = "CAPITAL_SPENT"
    else:
        verdict = "CAPITAL_OK"
    return {
        "n_positions": len(rows),
        "n_redeemable": len(redeemable),
        "redeemable_value_usd": round(red_value, 4),
        "redeemable_cost_usd": round(sum(f(r, "initialValue") for r in redeemable), 4),
        "total_initial_usd": round(total_initial, 4),
        # Redeemable-but-unredeemed **value** as a share of total buying.
        "locked_ratio": round(red_value / total_initial, 4) if total_initial > 0 else 0.0,
        "min_notional_usd": min_notional,
        "verdict": verdict,
        "platform_entry_fees": {
            "n_with_field": len(fees),
            "total_entry_fees_usdc": round(sum(x["entry_fees_usdc"] for x in fees), 6),
            "per_position": fees,
        },
        "reading": ("CAPITAL_LOCKED means the money sits in settled positions and "
                    "redeeming restores it. CAPITAL_SPENT means redeeming still would "
                    "not cover one order: the principal is gone and only a deposit "
                    "helps, so adding a redemption step to the exit path solves "
                    "nothing."),
    }


def ttl_economics(intents: list[dict], *, emit_interval_sec: float, daemon_tick_sec: float = 30.0) -> dict:
    """Should the TTL change — but first, what does the TTL actually govern?

    **A semantic that has to be stated first.** The expiry does **not** govern how
    long an order rests on the book. It governs **how long the daemon keeps trying
    to get the order to the venue**. Once accepted, the order is good-til-cancelled
    and rests until it fills or the market closes, and the TTL constrains it no
    further.

    So applying a resting-order hazard curve directly to the TTL is a mismatch: that
    curve describes what happens after placement, and the TTL governs what happens
    before it. Two individually correct components pointing in different directions.

    The real economics: inside the TTL window the daemon re-evaluates at the current
    price every tick, and on expiry it gives up permanently, because lifetime
    de-duplication means that market is never emitted again. The TTL is therefore the
    window left open for the price to drift back into range, and the duty cycle is
    TTL divided by the emission interval.
    """
    rows = []
    for it in intents:
        c, e, cl = _ts(it.get("created_at")), _ts(it.get("expires_at")), _ts(it.get("close_time"))
        if not (c and e):
            continue
        rows.append({
            "ttl_sec": (e - c).total_seconds(),
            "to_close_sec": (cl - c).total_seconds() if cl else None,
        })
    if not rows:
        return {}
    ttls = sorted(r["ttl_sec"] for r in rows)
    ttl = ttls[len(ttls) // 2]
    closes = sorted(r["to_close_sec"] for r in rows if r["to_close_sec"] is not None)

    def pct(xs, q):
        return xs[min(len(xs) - 1, max(0, int(len(xs) * q)))] if xs else None

    # Share where the market closed first: if high, an over-long TTL is the real
    # problem.
    outlived = sum(1 for x in closes if x < ttl)
    return {
        "median_ttl_sec": ttl,
        "emit_interval_sec": emit_interval_sec,
        "duty_cycle": round(ttl / emit_interval_sec, 4) if emit_interval_sec else None,
        "retries_per_intent": int(ttl // daemon_tick_sec) if daemon_tick_sec else None,
        "time_to_close_sec": {
            "n": len(closes), "p10": pct(closes, 0.10), "median": pct(closes, 0.50),
            "p90": pct(closes, 0.90),
        },
        "market_closes_within_ttl": outlived,
        "market_closes_within_ttl_frac": round(outlived / len(closes), 4) if closes else None,
        "reading": (
            "The TTL governs how long placement is attempted, not how long an order "
            "rests. The duty cycle, TTL over emission interval, is what share of the "
            "day a market is quoted at all, and it matters far more than the TTL in "
            "absolute terms: outside it the price can drift into range with nobody "
            "there to take it."
        ),
    }


def taker_conversion(funnel: dict, *, p_win: float, spread_cents: float = 2.0,
                     fee_rates: tuple = TAKER_FEE_RATES) -> dict:
    """Crossing the spread after a window or two: the fee against the opportunity
    cost of not filling. Arithmetic only; it changes no behaviour.

    The right comparison is **not** the maker price against the taker price, which
    yields the wrong conclusion that crossing is worse because it costs more. The
    real comparison is:
        keep resting : EV = P(fill) * (p_win - maker_px)
        cross now    : EV = p_win - ask - fee(ask)

    Crossing does not buy a better price. It converts a probabilistic position into
    a certain one. The cost is the spread plus the fee; the gain is the edge on the
    (1 - P(fill)) share that would otherwise never have been captured at all.

    **The population is orders actually placed.** Whether to cross only arises for
    an order already resting. Averaging across every emitted intent mixes in a mass
    of expensive tickets that never passed the gate at all, and the resulting
    conclusion — that the edge cannot cover the fee — is manufactured by tickets
    that were never eligible. A wrong population is harder to notice than a wrong
    formula.

    P(fill) comes from **this account's measured post-placement fill rate**, not
    from extrapolating a hazard curve: that curve's absolute level swings several-fold
    across days and is explicitly not extrapolable. Only its discrimination is
    trustworthy.
    """
    p_fill = funnel.get("fill_rate_given_placed")
    placed = [r for r in funnel.get("per_intent", []) if r.get("placed")]
    # The maker price is the one actually submitted; the ask is the executable price
    # computed at emission. Their difference is the real spread, floored at one tick,
    # because a spread cannot be negative.
    pairs = []
    for r in placed:
        mk = r.get("placed_price")
        ak = r.get("ask_price")
        if not isinstance(mk, (int, float)) or not (0 < mk < 1):
            continue
        if not isinstance(ak, (int, float)) or ak <= mk:
            ak = min(0.99, mk + spread_cents / 100.0)
        pairs.append((float(mk), float(ak)))
    if p_fill is None or not pairs:
        return {"verdict": "NO_DATA", "reason": "no post-placement fill rate or no placed sample"}
    maker_px = round(sum(m for m, _ in pairs) / len(pairs), 4)
    ask = round(sum(a for _, a in pairs) / len(pairs), 4)
    ev_wait = p_fill * (p_win - maker_px)
    out_rates = {}
    for rate in fee_rates:
        fee = rate * ask * (1.0 - ask)
        ev_take = p_win - ask - fee
        out_rates[f"rate_{rate}"] = {
            "fee_per_share": round(fee, 6),
            "fee_as_pct_of_notional": round(fee / ask, 6),
            "ev_take_now_per_share": round(ev_take, 6),
            "worth_converting": bool(ev_take > ev_wait),
            "gain_vs_waiting_per_share": round(ev_take - ev_wait, 6),
        }
    worth_all = all(v["worth_converting"] for v in out_rates.values())
    worth_any = any(v["worth_converting"] for v in out_rates.values())
    return {
        "p_win_used": p_win,
        "p_fill_used": p_fill,
        "population": "placed_intents_only",
        "n_placed_priced": len(pairs),
        "mean_ask_price": ask,
        "mean_maker_price": maker_px,
        "mean_spread_cents": round((ask - maker_px) * 100, 3),
        "ev_keep_waiting_per_share": round(ev_wait, 6),
        "by_fee_rate": out_rates,
        "verdict": ("CONVERT_WORTH_IT" if worth_all else
                    "CONVERT_MARGINAL" if worth_any else "KEEP_MAKER"),
        "reading": (
            "Crossing does not buy a better price; it converts a probabilistic "
            "position into a certain one, at the cost of the spread plus the fee. It "
            "is worthwhile only when the EV of crossing exceeds the EV of continuing "
            "to rest. Crossing merely because a hazard falls is the "
            "statistically-significant-but-economically-irrelevant trap."
        ),
    }


# --------------------------------------------------------------------------- #
# Verdicts. The thresholds are fixed in this module and do not move with results.
# --------------------------------------------------------------------------- #
def verdicts(funnel: dict, ttl: dict) -> dict:
    """The verdict against the pre-registered tests; thresholds are in the module
    docstring and do not move with results."""
    pr, fr = funnel.get("placement_rate"), funnel.get("fill_rate_given_placed")
    out: dict = {}
    if pr is not None:
        out["placement"] = {
            "placement_rate": pr,
            "threshold": PLACEMENT_BOUND_RATE,
            "verdict": "PLACEMENT_BOUND" if pr < PLACEMENT_BOUND_RATE else "PLACEMENT_OK",
        }
    if fr is not None:
        # The ceiling on the maker step: even a perfect fill rate only adds
        # (1 - fill rate) times the placement rate.
        headroom = round((1 - fr) * (pr or 0), 4)
        out["maker_fill"] = {
            "fill_rate_given_placed": fr,
            "threshold": MAKER_NOT_BOTTLENECK_FILL_RATE,
            "end_to_end_headroom_if_perfect": headroom,
            "verdict": ("MAKER_NOT_BOTTLENECK" if fr >= MAKER_NOT_BOTTLENECK_FILL_RATE
                        else "MAKER_IS_BOTTLENECK"),
        }
    if ttl.get("duty_cycle") is not None:
        out["coverage"] = {
            "duty_cycle": ttl["duty_cycle"],
            "market_closes_within_ttl_frac": ttl.get("market_closes_within_ttl_frac"),
            # Evidence of too long a TTL is markets closing first; evidence of too
            # sparse coverage is a low duty cycle.
            "verdict": ("TTL_TOO_LONG" if (ttl.get("market_closes_within_ttl_frac") or 0) > 0.5
                        else "COVERAGE_SPARSE" if ttl["duty_cycle"] < 0.5 else "COVERAGE_OK"),
        }
    return out


def run(*, emit_interval_sec: float, p_win: float, offline: bool = False,
        since: str | None = None) -> dict:
    intents = load_intents()
    if since:
        # Comparing N days before a change against N days after requires slicing by
        # emission date. Without it this module only ever reports a lifetime total
        # that mixes both sides, and no change is visible in that number.
        intents = [r for r in intents if str(r.get("created_at") or "") >= since]
    placements = load_placements()
    trades: list = []
    activity: list = []
    funder: str | None = None
    if not offline:
        try:
            from marketflow.execution import orders as pmx
            funder = pmx.read_funder_address()
            activity = fetch_account_trades(funder)
            trades = [r for r in activity if str(r.get("type") or "").upper() == "TRADE"]
        except Exception as exc:  # noqa: BLE001
            print(f"[funnel] account trades unavailable: {exc}", file=sys.stderr)
    funnel = build_funnel(intents, placements, trades)
    ttl = ttl_economics(intents, emit_interval_sec=emit_interval_sec)
    try:  # the band's single source of truth is the gate module; no copy here
        from marketflow.execution import market_gate as mgate
        band_low, band_high = mgate.AUTO_BAND_LOW, mgate.AUTO_BAND_HIGH
    except Exception:
        band_low = band_high = None
    capital = capital_availability(_jsonl(DAEMON_LEDGER), typical_price=_median_placed_price(funnel))
    # Only distinguish locked from spent when the balance is short. With enough
    # balance this column is noise and costs an extra network call; whether there is
    # enough has already been answered.
    if funder and not capital.get("can_fund_an_order_now"):
        capital["redeemable"] = redeemable_capital(
            funder, min_notional=float(capital.get("min_notional_usd") or 0.0))

    summary = {
        "schema_version": "polymarket-execution-funnel-v0.1",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account_trades_seen": len(trades),
        "fill_source": "account_public_activity" if trades else "UNAVAILABLE",
        "funnel": {k: v for k, v in funnel.items() if k != "per_intent"},
        "placement_loss": (placement_loss_attribution(
            funnel, band_high=band_high, band_low=band_low, p_win=p_win,
            edge_buffer=DEFAULT_EDGE_BUFFER) if band_high is not None else
            {"reason": "market gate band unavailable; skipped rather than guessed"}),
        "time_to_close": fill_by_time_to_close(funnel, placements),
        "capital": capital,
        "real_money_pnl": real_money_pnl(activity) if activity else {"verdict": "UNAVAILABLE"},
        "ttl": ttl,
        "taker": taker_conversion(funnel, p_win=p_win),
        "verdicts": verdicts(funnel, ttl),
    }
    if not trades:
        summary["honesty"] = ("the account's trade history could not be fetched, so the "
                              "fill-rate column is invalid and must not be read as a "
                              "conclusion. An order receipt cannot answer whether "
                              "something filled.")
    summary["window_since"] = since
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    with open(os.path.join(OUT_DIR, "per_intent.json"), "w", encoding="utf-8") as f:
        json.dump(funnel.get("per_intent", []), f, ensure_ascii=False, indent=1)
    # An append-only snapshot trail. The latest file is overwritten each run, while
    # comparing before and after a change needs a timeline that persists.
    with open(os.path.join(OUT_DIR, "history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({k: summary[k] for k in
                            ("generated_at", "window_since", "funnel", "placement_loss",
                             "time_to_close", "verdicts", "real_money_pnl")}, ensure_ascii=False) + "\n")
    return summary


# --------------------------------------------------------------------------- #
# selftest: synthetic data, no network
# --------------------------------------------------------------------------- #
def selftest() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    def mk(key, slug, created, ttl=1800, to_close=18000, ask=0.80):
        c = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=created)
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"source": "model_signal", "idempotency_key": key, "market_slug": slug,
                "created_at": iso(c), "expires_at": iso(c + dt.timedelta(seconds=ttl)),
                "close_time": iso(c + dt.timedelta(seconds=to_close)),
                "ask_price": ask, "max_price": round(ask * 1.01, 4)}

    intents = [mk("k1", "m1", 0), mk("k2", "m2", 10), mk("k3", "m3", 20), mk("k4", "m4", 30)]
    placements = [{"idempotency_key": "k1", "market_slug": "m1", "price": 0.78,
                   "receipt_status": "live", "placed_at": None, "market_id": None,
                   "size": 5, "post_only": True},
                  {"idempotency_key": "k2", "market_slug": "m2", "price": 0.78,
                   "receipt_status": "live", "placed_at": None, "market_id": None,
                   "size": 5, "post_only": True}]
    trades = [{"type": "TRADE", "side": "BUY", "slug": "m1"},
              {"type": "TRADE", "side": "SELL", "slug": "m2"}]  # a SELL is not a buy fill

    f = build_funnel(intents, placements, trades)
    check("emitted count", f["n_emitted"] == 4)
    check("placement rate is 2/4", abs(f["placement_rate"] - 0.5) < 1e-9)
    check("a fill counts only from an account BUY trade, never a receipt", f["n_filled"] == 1)
    check("post-placement fill rate is 1/2", abs(f["fill_rate_given_placed"] - 0.5) < 1e-9)
    check("end-to-end rate is 1/4", abs(f["end_to_end_rate"] - 0.25) < 1e-9)
    check("the two loss kinds are counted separately",
          f["loss_before_placement"] == 2 and f["loss_after_placement"] == 1)
    check("a SELL on the same slug is not mistaken for a buy fill",
          not any(r["filled"] for r in f["per_intent"] if r["market_slug"] == "m2"))

    # Receipt status plays no part in deciding a fill: that is exactly where the
    # first version fooled itself.
    all_live = build_funnel(intents, placements, [])
    check("with no account trades the fill count is 0, not 100% from receipts",
          all_live["n_filled"] == 0 and all_live["fill_rate_given_placed"] == 0.0)

    # attribution: dead on arrival versus could-have-filled-but-did-not
    doa = [mk("d1", "d1", 0, ask=0.95), mk("d2", "d2", 0, ask=0.97)]
    attr = placement_loss_attribution(build_funnel(intents + doa, placements, trades),
                                      band_high=0.85, band_low=0.15, p_win=0.9182,
                                      edge_buffer=0.05)
    check("an out-of-band emission is dead on arrival", attr["counts"]["dead_on_arrival_out_of_band"] == 2)
    check("placed intents are not counted as loss", attr["counts"]["placed"] == 2)
    check("in-band but never placed is its own class",
          attr["counts"]["eligible_but_never_placed"] == 2)
    check("excluding dead-on-arrival raises the placement rate above the raw one",
          abs(attr["eligible_placement_rate"] - 0.5) < 1e-9)
    edge_dead = placement_loss_attribution(
        build_funnel([mk("e1", "e1", 0, ask=0.84)], [], []),
        band_high=0.85, band_low=0.15, p_win=0.86, edge_buffer=0.05)
    check("in band but short of edge is also dead on arrival",
          edge_dead["counts"]["dead_on_arrival_no_edge"] == 1)

    # stratified by time to close: more time fills, near the close does not
    ttc_intents = [mk("a1", "a1", 0, to_close=36000), mk("a2", "a2", 0, to_close=36000),
                   mk("b1", "b1", 0, to_close=600), mk("b2", "b2", 0, to_close=600)]
    ttc_placed = [{"idempotency_key": k, "market_slug": k, "price": 0.78,
                   "placed_at": "2026-07-20T00:00:00Z", "receipt_status": "live",
                   "market_id": None, "size": 5, "post_only": True}
                  for k in ("a1", "a2", "b1", "b2")]
    ttc_trades = [{"type": "TRADE", "side": "BUY", "slug": "a1"},
                  {"type": "TRADE", "side": "BUY", "slug": "a2"}]
    ttc = fill_by_time_to_close(build_funnel(ttc_intents, ttc_placed, ttc_trades), ttc_placed)
    check("all four are included", ttc["n"] == 4)
    rates = {s["minutes_to_close"]: s["fill_rate"] for s in ttc["by_time_to_close"]}
    check("the shortest bucket fills at 0% and the longest at 100%",
          rates.get("[0, 15)") == 0.0 and rates.get("[240, inf)") == 1.0)
    check("unplaced intents do not enter the strata",
          fill_by_time_to_close(build_funnel(ttc_intents, [], []), [])["n"] == 0)

    # the funding gate: a balance short of one minimum order looks exactly like an
    # execution problem
    cap = capital_availability([{"available_collateral_usd": v} for v in (0.38, 0.38, 22.4)],
                               typical_price=0.80)
    check("minimum order notional is shares times median price", abs(cap["min_notional_usd"] - 4.0) < 1e-9)
    check("a tick with a tiny balance counts as starved",
          cap["ticks_below_one_min_order"] == 2 and abs(cap["starved_frac"] - 0.6667) < 1e-3)
    check("a tick with a sufficient balance can place", cap["can_fund_an_order_now"] is True)
    check("with no records it says so rather than guessing",
          capital_availability([], typical_price=0.8)["n_ticks"] == 0)
    # Locked versus spent: identical symptoms, opposite fixes — redeem or deposit.
    # And a third case: a redeemable position that settled as a loss, where
    # redeeming returns nothing and only a deposit helps.
    spent = summarize_positions(
        [{"redeemable": True, "currentValue": 0.0, "initialValue": 19.9952,
          "avgPrice": 0.77, "size": 25.9678, "entryFeesUsdc": 0.0, "slug": "london-27c"}],
        min_notional=3.9)
    check("redeemable but worth nothing is CAPITAL_SPENT; redeeming cannot help",
          spent["verdict"] == "CAPITAL_SPENT" and spent["redeemable_value_usd"] == 0.0)
    check("the locked ratio uses value, not cost: a large cost on a lost position is not money",
          spent["locked_ratio"] == 0.0 and spent["redeemable_cost_usd"] == 19.9952)
    locked = summarize_positions(
        [{"redeemable": True, "currentValue": 30.0, "initialValue": 20.0,
          "avgPrice": 0.5, "size": 40.0}],
        min_notional=3.9)
    check("redeemable and sufficient is CAPITAL_LOCKED", locked["verdict"] == "CAPITAL_LOCKED")
    check("no positions is reported as such, never as SPENT",
          summarize_positions([], min_notional=3.9)["verdict"] == "NO_POSITIONS")
    check("only unsettled positions is CAPITAL_OK: the money is in the market, not stuck",
          summarize_positions([{"redeemable": False, "currentValue": 9.0, "initialValue": 10.0}],
                              min_notional=3.9)["verdict"] == "CAPITAL_OK")
    # Authoritative fee values: solving back for the rate is what makes prices
    # comparable, and a missing value must stay None — 0 would read as zero fee.
    fees = summarize_positions(
        [{"redeemable": False, "currentValue": 1.0, "initialValue": 10.0,
          "avgPrice": 0.5, "size": 40.0, "entryFeesUsdc": 0.5},
         {"redeemable": False, "currentValue": 1.0, "initialValue": 10.0,
          "avgPrice": 1.0, "size": 10.0, "entryFeesUsdc": 0.0},
         {"redeemable": False, "currentValue": 1.0, "initialValue": 10.0,
          "avgPrice": 0.5, "size": 10.0}],
        min_notional=3.9)["platform_entry_fees"]
    check("only positions carrying the field are counted", fees["n_with_field"] == 2)
    check("solving back for the rate reproduces it",
          abs(fees["per_position"][0]["implied_rate"] - 0.05) < 1e-9)
    check("a zero denominator leaves None rather than 0",
          fees["per_position"][1]["implied_rate"] is None)

    check("the median placed price comes from the real sample", abs(_median_placed_price(f) - 0.78) < 1e-9)
    check("with no placed sample it falls back conservatively", abs(_median_placed_price({}) - 0.80) < 1e-9)

    # Real-money PnL: aggregated per market, settled only, with a fragility measure
    act = [
        {"conditionId": "A", "slug": "highest-temperature-in-x", "type": "TRADE", "side": "BUY", "usdcSize": 2.45},
        {"conditionId": "A", "slug": "highest-temperature-in-x", "type": "TRADE", "side": "BUY", "usdcSize": 1.60},
        {"conditionId": "A", "slug": "highest-temperature-in-x", "type": "REDEEM", "usdcSize": 5.00},
        {"conditionId": "B", "slug": "highest-temperature-in-y", "type": "TRADE", "side": "BUY", "usdcSize": 3.80},
        {"conditionId": "B", "slug": "highest-temperature-in-y", "type": "REDEEM", "usdcSize": 0.0},
        {"conditionId": "C", "slug": "highest-temperature-in-z", "type": "TRADE", "side": "BUY", "usdcSize": 18.28},
        {"conditionId": "C", "slug": "highest-temperature-in-z", "type": "REDEEM", "usdcSize": 22.02},
        {"conditionId": "D", "slug": "highest-temperature-in-open", "type": "TRADE", "side": "BUY", "usdcSize": 20.0},
        {"conditionId": "E", "slug": "some-other-market", "type": "TRADE", "side": "BUY", "usdcSize": 99.0},
    ]
    pnl = real_money_pnl(act)
    check("several buys in one market match one redemption, never fill by fill",
          pnl["n_settled_markets"] == 3 and pnl["n_buy_fills"] == 4
          and abs(pnl["invested_usd"] - 26.13) < 1e-9)
    check("an unsettled market is not counted as a loss",
          pnl["open_markets"] == 1 and abs(pnl["open_cost_usd"] - 20.0) < 1e-9)
    check("markets outside the strategy do not leak in", "E" not in str(pnl))
    check("net and ROI", abs(pnl["net_usd"] - 0.89) < 1e-9
          and abs(pnl["roi"] - round(0.89 / 26.13, 4)) < 1e-9)
    check("the win rate carries a Wilson interval", pnl["win_rate_ci95"][0] < pnl["win_rate"] < pnl["win_rate_ci95"][1])
    check("fragility: removing the best market turns it negative -> carried_by_single_market",
          pnl["carried_by_single_market"] is True and pnl["net_excluding_best_usd"] < 0)
    robust = real_money_pnl([
        {"conditionId": f"m{i}", "slug": "highest-temperature-in-a", "type": "TRADE", "side": "BUY", "usdcSize": 10.0}
        for i in range(4)] + [
        {"conditionId": f"m{i}", "slug": "highest-temperature-in-a", "type": "REDEEM", "usdcSize": 13.0}
        for i in range(4)])
    check("diversified profit is not flagged as carried by one market",
          robust["carried_by_single_market"] is False)
    check("no settled sample reports NO_SETTLED_DATA honestly",
          real_money_pnl([])["verdict"] == "NO_SETTLED_DATA")

    t = ttl_economics(intents, emit_interval_sec=21600.0)
    check("duty cycle is TTL over emission interval", abs(t["duty_cycle"] - 0.0833) < 1e-3)
    check("retries per intent is TTL over tick", t["retries_per_intent"] == 60)
    check("no market closing inside the TTL gives 0", t["market_closes_within_ttl"] == 0)
    short = ttl_economics([mk("s1", "s1", 0, to_close=600)], emit_interval_sec=21600.0)
    check("a market closing before the TTL expires is recorded as TTL outliving it",
          short["market_closes_within_ttl"] == 1)

    v = verdicts(f, t)
    check("a placement rate above the threshold is PLACEMENT_OK", v["placement"]["verdict"] == "PLACEMENT_OK")
    check("a fill rate below the threshold is MAKER_IS_BOTTLENECK",
          v["maker_fill"]["verdict"] == "MAKER_IS_BOTTLENECK")
    check("the ceiling from a perfect maker step",
          abs(v["maker_fill"]["end_to_end_headroom_if_perfect"] - 0.25) < 1e-9)
    check("a low duty cycle is COVERAGE_SPARSE", v["coverage"]["verdict"] == "COVERAGE_SPARSE")
    low = build_funnel(intents, placements[:0], trades)
    check("a placement rate below the threshold is PLACEMENT_BOUND",
          verdicts(low, t)["placement"]["verdict"] == "PLACEMENT_BOUND")

    # crossing arithmetic: worthwhile at a high win rate and wide spread, not otherwise
    tk = taker_conversion(f, p_win=0.92, spread_cents=2.0)
    check("the population is placed orders only",
          tk["population"] == "placed_intents_only" and tk["n_placed_priced"] == 2
          and abs(tk["mean_maker_price"] - 0.78) < 1e-9 and abs(tk["mean_ask_price"] - 0.80) < 1e-9)
    check("the fee follows the curve rather than a flat rate on notional",
          abs(tk["by_fee_rate"]["rate_0.03"]["fee_per_share"] - 0.03 * 0.80 * 0.20) < 1e-9)
    check("both fee tiers are computed, so the conclusion is not rate-specific",
          set(tk["by_fee_rate"]) == {f"rate_{r}" for r in TAKER_FEE_RATES})
    check("the comparison is against the EV of continuing to rest",
          abs(tk["ev_keep_waiting_per_share"] - 0.5 * (0.92 - 0.78)) < 1e-9)
    check("high win rate with a low fill rate makes crossing worthwhile", tk["verdict"] == "CONVERT_WORTH_IT")
    thin = taker_conversion(f, p_win=0.81, spread_cents=2.0)
    check("an edge too thin for the spread plus fee keeps the maker order", thin["verdict"] == "KEEP_MAKER")
    perfect = dict(f, fill_rate_given_placed=1.0)
    check("a certain fill means no reason to cross: the opportunity cost is zero",
          taker_conversion(perfect, p_win=0.92)["verdict"] == "KEEP_MAKER")
    check("no data reports NO_DATA",
          taker_conversion({"per_intent": []}, p_win=0.9)["verdict"] == "NO_DATA")
    # The wrong-population shape: mixing in expensive tickets that never placed
    # raises the mean price and flips the conclusion.
    wrong_pop = build_funnel(intents + [mk("k9", "m9", 40, ask=0.97)], placements, trades)
    check("adding an expensive never-placed ticket does not change the conclusion",
          taker_conversion(wrong_pop, p_win=0.92)["mean_ask_price"] == tk["mean_ask_price"])

    print("\nselftest:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Execution funnel: emit -> place -> fill (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--emit-interval-sec", type=float, default=21600.0,
                    help="interval at which intents are emitted (default 6h)")
    ap.add_argument("--p-win", type=float, default=None,
                    help="win probability (required): your own Wilson lower bound "
                         "or an equivalently conservative estimate")
    ap.add_argument("--offline", action="store_true",
                    help="no network; the fill-rate column will be invalid")
    ap.add_argument("--since", help="only intents emitted after this ISO date, for before/after comparison")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    p_win = a.p_win
    if p_win is None:
        print("[funnel] no p_win available; refusing to invent one. Pass --p-win.", file=sys.stderr)
        return 1
    print(json.dumps(run(emit_interval_sec=a.emit_interval_sec, p_win=p_win,
                         offline=a.offline, since=a.since),
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
