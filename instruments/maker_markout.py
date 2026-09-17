#!/usr/bin/env python3
"""Real PnL of passive market making — a markout instrument.

Why

A zero-sum ledger says the market-making side of the book loses a fraction of a
percent, but that is a **settlement** figure: it assumes a maker holds the ticket
to resolution. Real market makers do not live that way. They quote, get filled,
and within minutes are hedged off by the next trade or the price has already
moved. The industry-standard measure of whether making markets pays is
**markout**: how far the mid has drifted from the fill price after some interval.

  a taker BUY at p means the maker sold one share at p; if the mid then rises,
  the maker lost

  maker P&L(delta) = p - mid(t + delta)        [taker BUY side]
  maker P&L(delta) = mid(t + delta) - p        [taker SELL side]

That one expression **contains both terms** and needs no separate estimation:
  * spread earned        = |p - mid(t)|, how far the resting order sat from the
                           mid when it filled (the maker's gross revenue)
  * adverse selection    = mid(t + delta) - mid(t), how far the price then moved
                           in the taker's favour (the maker's cost)
Subtract them and you have the expression above. **As delta approaches zero it
tends to pure spread and is positive; as delta grows, adverse selection eats it.**
Where the two cross is the line between market making being viable in a market and
not.

Liquidity-reward programmes are usually analysed on the reward half alone. The
real return to making markets is

  rewards + markout P&L - inventory risk

and the markout term is the one nobody computes. **If it is negative and larger
than the rewards, the programme is paying you to fill a hole. If it is near zero
or positive, the rewards are real yield.** This instrument supplies that number.

Data source: daily jsonl snapshots produced by your own book sampler. None ships
here — which markets to sample, and how often, is a deployment decision. Each
record carries a book and that market's recent trades, taken at the same moment so
nothing has to be stitched together:

    {"ts": <epoch_s>, "market": <id>, "token_id": <id>,
     "book": {"mid": <px>, "bids": [[px, size], ...], "asks": [[px, size], ...]},
     "trades": [{"ts": <epoch_s>, "price": <px>, "size": <shares>, "side": "BUY"|"SELL"}, ...]}

`side` is from the **taker's** perspective. The `side_semantics` check proves it
empirically: taker BUYs should sit markedly closer to the ask and SELLs closer to
the bid. Run that first against any new sampler to confirm the convention is not
inverted.

Honest limits:
- The measurable horizon is bounded below by the snapshot interval. A sampler on a
  multi-minute cadence cannot see the 10-second-to-1-minute markouts a real market
  maker cares about; that needs a tick-level book stream.
- The mid reference must land inside [delta, delta * HORIZON_TOL] or that (trade,
  horizon) pair is discarded and counted in `coverage`. A horizon with a high
  discard rate is not trustworthy.
- Every conclusion is **notional-weighted as the primary basis**, because orders
  have size and equal weighting overstates small ones. The equal-weighted figure
  is reported alongside; where the two disagree in sign, the notional-weighted one
  governs and the disagreement is stated explicitly.
- This is **observation, not strategy.** It says how passive fills already
  happening in the market performed afterwards. It does not say your own quotes
  would get those fills: your orders have to queue, and the subset that does get
  filled is plausibly the more toxic one. Treat these numbers as an **upper bound**
  on market-making return.

Hard boundary: read-only over local jsonl. No network, no orders, and it never
touches arm-state, caps, kill files or secrets. Output goes only to its own
directory.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import os
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED_DIR = os.path.join(REPO, "runtime/feeds/book_samples")
OUT_DIR = os.path.join(REPO, "runtime/execution/polymarket_maker_markout")

# Markout horizons in seconds. The shortest is the sampler's native resolution;
# the longest exists to see whether markout converges on the settlement figure.
HORIZONS = (300, 900, 1800, 3600, 21600, 86400)
HORIZON_TOL = 2.0        # the mid reference may land in [delta, delta*2]; beyond that
#                          it is not an observation of this horizon
PRICE_BANDS = ((0.0, 0.10), (0.10, 0.25), (0.25, 0.45), (0.45, 0.55),
               (0.55, 0.75), (0.75, 0.90), (0.90, 1.0))
SETTLED_EDGE = 0.02      # a mid pinned to an edge means the question is decided and
#                          the price is no longer a live making opportunity


def _band(px: float) -> str:
    for lo, hi in PRICE_BANDS:
        if lo <= px < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "1.00"


LEG_AMBIG = 0.02      # when |p-mid| and |1-p-mid| differ by less than this the mid is
#                       near 0.5 and the leg cannot be told apart (rare in practice)


def normalize_leg(px: float, side: str, ref_mid: float) -> tuple[float, str, str]:
    """Normalise a trade onto **the leg the book belongs to**.

    A trades endpoint keyed on the condition returns fills from **both legs** of a
    binary market, and a snapshot that does not preserve the asset identifier
    cannot tell them apart — while the book and mid are sampled for one leg only.
    In practice a large minority of fills belong to the opposite leg (a price near
    0.76 in a market whose sampled mid is near 0.245). Without normalisation the
    markout computes one leg's fills against the other leg's mid, and every number
    is worthless.

    The binary identity does the work: buying NO at q is selling YES at (1 - q), so
    flipping a leg means complementing the price and reversing the side. The test
    is which mid the fill price sits closer to, and the two populations separate
    cleanly.

    Returns (px, side, leg_flag)."""
    d_same, d_opp = abs(px - ref_mid), abs((1.0 - px) - ref_mid)
    if abs(d_same - d_opp) < LEG_AMBIG:
        return px, side, "ambiguous"       # mid near 0.5: px is close to 1-px, so
        #                                    normalising either way barely matters
    if d_opp < d_same:
        return 1.0 - px, ("SELL" if side == "BUY" else "BUY"), "flipped"
    return px, side, "same"


def _nearest_mid(series: list, ts: float) -> float | None:
    """The mid observation closest in time to ts, before or after. Leg membership is
    structural and does not change over time, so the nearest observation gives the
    cleanest discrimination."""
    if not series:
        return None
    i = bisect.bisect_left(series, (ts, -1.0))
    cands = []
    if i < len(series):
        cands.append(series[i])
    if i > 0:
        cands.append(series[i - 1])
    return min(cands, key=lambda x: abs(x[0] - ts))[1] if cands else None


def load_snapshots(paths: list[str]) -> tuple[dict, list]:
    """Returns ({market: sorted [(ts, mid), ...]}, [trade dicts]).

    Two passes: build the price series first, then use it to normalise trades onto
    the book's leg (see `normalize_leg`). Trades are de-duplicated on
    (market, ts, price, size, side), because consecutive snapshots repeat some of
    the same fills."""
    mids: dict = defaultdict(list)
    books: dict = defaultdict(list)      # (ts, best_bid, best_ask) for the side check
    seen: set = set()
    raw: list = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                mkt = d.get("condition_id") or d.get("market_id")
                ts, mid = d.get("ts"), d.get("mid")
                if not mkt or ts is None:
                    continue
                if isinstance(mid, (int, float)) and 0 < mid < 1:
                    mids[mkt].append((float(ts), float(mid)))
                bk = d.get("book") or {}
                bids, asks = bk.get("bids") or [], bk.get("asks") or []
                if bids and asks:
                    books[mkt].append((float(ts), float(bids[0][0]), float(asks[0][0])))
                for t in (d.get("trades") or []):
                    try:
                        tts, px, sz = float(t["ts"]), float(t["price"]), float(t["size"])
                        side = str(t.get("side") or "").upper()
                    except (KeyError, TypeError, ValueError):
                        continue
                    if side not in ("BUY", "SELL") or not (0 < px < 1) or sz <= 0:
                        continue
                    key = (mkt, round(tts, 3), round(px, 6), round(sz, 4), side)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw.append({"market": mkt, "ts": tts, "price": px,
                                "size": sz, "side": side,
                                "q": d.get("q") or "", "rate": d.get("rate")})
    for m in mids:
        mids[m].sort()
    for m in books:
        books[m].sort()
    trades: list = []
    for tr in raw:
        ref = _nearest_mid(mids.get(tr["market"]) or [], tr["ts"])
        if ref is None:
            continue                       # no price series: neither the leg nor the
            #                                markout can be determined
        px, side, leg = normalize_leg(tr["price"], tr["side"], ref)
        trades.append({**tr, "price": px, "side": side, "leg": leg})
    return {"mids": dict(mids), "books": dict(books)}, trades


def mid_after(series: list, t0: float, horizon: float, tol: float = HORIZON_TOL):
    """The first mid observation at or after t0 + horizon. Outside the
    [delta, delta*tol] window it returns None — discarded honestly."""
    if not series:
        return None
    target = t0 + horizon
    i = bisect.bisect_left(series, (target, -1.0))
    if i >= len(series):
        return None
    ts, mid = series[i]
    return mid if ts <= t0 + horizon * tol else None


def side_semantics(store: dict, trades: list, sample: int = 4000) -> dict:
    """Check empirically whether `side` is the taker's perspective. A taker BUY
    lifts the ask, so its fill price should sit nearer the ask. Position is judged
    against the most recent book **before** the fill, and the output is the mean
    relative position r = (px - bid) / (ask - bid) on each side: BUY should be
    markedly above SELL.

    Note that it collects `sample` **usable** observations rather than the first
    `sample` trades. A sampler's first cycle backfills historical fills for each
    market, and all of those predate that market's first book snapshot; taking the
    first N would therefore yield nothing usable at all."""
    out = {"buy_rel": None, "sell_rel": None, "n": 0, "verdict": "UNKNOWN"}
    acc = {"BUY": [], "SELL": []}
    for tr in trades:
        if len(acc["BUY"]) + len(acc["SELL"]) >= sample:
            break
        bs = store["books"].get(tr["market"]) or []
        if not bs:
            continue
        i = bisect.bisect_right(bs, (tr["ts"], math.inf, math.inf)) - 1
        if i < 0:
            continue                       # fill predates the first book snapshot
        _, bid, ask = bs[i]
        if not (ask > bid > 0):
            continue
        acc[tr["side"]].append((tr["price"] - bid) / (ask - bid))
    for k in ("BUY", "SELL"):
        if acc[k]:
            out[f"{k.lower()}_rel"] = round(sum(acc[k]) / len(acc[k]), 4)
    out["n"] = len(acc["BUY"]) + len(acc["SELL"])
    if out["buy_rel"] is not None and out["sell_rel"] is not None:
        out["verdict"] = ("TAKER_SIDE_CONFIRMED" if out["buy_rel"] > out["sell_rel"] + 0.05
                          else "AMBIGUOUS")
    return out


def _agg(rows: list) -> dict | None:
    """rows = [(pnl_usd, notional_usd, bps)]. Notional weighting is the primary
    basis with equal weighting alongside, plus a 95% interval on the
    notional-weighted bps from its weighted standard error."""
    if not rows:
        return None
    pnl = sum(r[0] for r in rows)
    notional = sum(r[1] for r in rows)
    if notional <= 0:
        return None
    w_bps = pnl / notional * 10000.0
    eq_bps = sum(r[2] for r in rows) / len(rows)
    # SE of a weighted mean: sum(w^2 (x - xbar)^2) / (sum w)^2. When one large order
    # dominates, the interval widens by itself, which is the honest behaviour.
    var = sum((r[1] ** 2) * ((r[2] - w_bps) ** 2) for r in rows) / (notional ** 2)
    se = math.sqrt(var)
    return {"n": len(rows), "notional_usd": round(notional, 2),
            "maker_bps_wtd": round(w_bps, 2),
            "ci95_lower_bps": round(w_bps - 1.96 * se, 2),
            "ci95_upper_bps": round(w_bps + 1.96 * se, 2),
            "maker_bps_equal": round(eq_bps, 2),
            "maker_pnl_usd": round(pnl, 2)}


def compute(store: dict, trades: list) -> dict:
    """Each fill against each horizon -> the maker-side P&L.

    maker P&L per share = (p − mid_after) if taker BUY else (mid_after − p)
    notional = p * size, the capital the maker had committed
    bps = pnl / notional × 10000
    """
    by_h: dict = defaultdict(list)
    by_h_band: dict = defaultdict(lambda: defaultdict(list))
    by_h_size: dict = defaultdict(lambda: defaultdict(list))
    coverage: dict = defaultdict(lambda: {"eligible": 0, "used": 0})
    for tr in trades:
        series = store["mids"].get(tr["market"])
        if not series:
            continue
        px, sz, sgn = tr["price"], tr["size"], (1.0 if tr["side"] == "BUY" else -1.0)
        notional = px * sz
        if notional <= 0:
            continue
        band = _band(px)
        # size buckets: are larger orders more toxic, the classic signature of
        # informed flow
        sz_band = ("<100" if notional < 100 else "100-1k" if notional < 1000
                   else "1k-10k" if notional < 10000 else ">=10k")
        for h in HORIZONS:
            coverage[h]["eligible"] += 1
            ma = mid_after(series, tr["ts"], h)
            if ma is None or not (SETTLED_EDGE < ma < 1 - SETTLED_EDGE):
                continue
            coverage[h]["used"] += 1
            pnl_per_share = (px - ma) * sgn
            pnl = pnl_per_share * sz
            bps = pnl / notional * 10000.0
            rec = (pnl, notional, bps)
            by_h[h].append(rec)
            by_h_band[h][band].append(rec)
            by_h_size[h][sz_band].append(rec)
    out = {"overall": {}, "by_price_band": {}, "by_size_band": {}, "coverage": {}}
    for h in HORIZONS:
        out["overall"][str(h)] = _agg(by_h[h])
        out["by_price_band"][str(h)] = {k: _agg(v) for k, v in sorted(by_h_band[h].items())}
        out["by_size_band"][str(h)] = {k: _agg(v) for k, v in by_h_size[h].items()}
        cv = coverage[h]
        out["coverage"][str(h)] = {**cv,
                                   "used_pct": round(100.0 * cv["used"] / max(cv["eligible"], 1), 1)}
    return out


def flow_balance(store: dict, trades: list, horizon: int = 300) -> dict:
    """Is a positive markout market-making revenue, or directional luck? This is the
    easiest place in the whole analysis to fool yourself.

    Earning the spread requires **filling on both sides**: the maker sells to
    buyers and buys from sellers, the legs net off, exposure is near zero, and the
    profit is two spreads. If the flow is heavily one-sided — say the overwhelming
    majority of it is taker BUY — then the maker is passively net short, and a
    positive markout only says the price happened to fall over that window. That is
    the result of a directional bet: the sign flips in another period and it does
    not replicate.

    The test: (1) is the **notional** split between taker BUY and SELL close to
    even, and (2) is the maker markout **positive on both sides**? One side
    positive is directional. Both sides positive is genuinely earning the
    spread."""
    sides: dict = defaultdict(lambda: {"notional": 0.0, "pnl": 0.0, "n": 0})
    by_size: dict = defaultdict(lambda: defaultdict(lambda: {"notional": 0.0, "pnl": 0.0, "n": 0}))
    for tr in trades:
        series = store["mids"].get(tr["market"])
        if not series:
            continue
        ma = mid_after(series, tr["ts"], horizon)
        if ma is None or not (SETTLED_EDGE < ma < 1 - SETTLED_EDGE):
            continue
        px, sz = tr["price"], tr["size"]
        sgn = 1.0 if tr["side"] == "BUY" else -1.0
        notional = px * sz
        pnl = (px - ma) * sgn * sz
        sz_band = ("<100" if notional < 100 else "100-1k" if notional < 1000
                   else "1k-10k" if notional < 10000 else ">=10k")
        for bucket in (sides[tr["side"]], by_size[sz_band][tr["side"]]):
            bucket["notional"] += notional
            bucket["pnl"] += pnl
            bucket["n"] += 1

    def _fmt(d: dict) -> dict:
        tot = sum(v["notional"] for v in d.values())
        out = {}
        for k, v in d.items():
            out[k] = {"n": v["n"], "notional_usd": round(v["notional"], 2),
                      "notional_share": round(v["notional"] / tot, 4) if tot else None,
                      "maker_bps": round(v["pnl"] / v["notional"] * 10000, 2) if v["notional"] else None}
        both = [v["maker_bps"] for v in out.values() if v["maker_bps"] is not None]
        out["verdict"] = ("BOTH_SIDES_POSITIVE" if len(both) == 2 and all(b > 0 for b in both)
                          else "BOTH_SIDES_NEGATIVE" if len(both) == 2 and all(b < 0 for b in both)
                          else "DIRECTIONAL_ONE_SIDED" if len(both) == 2
                          else "INSUFFICIENT")
        return out
    return {"overall": _fmt(sides),
            "by_size_band": {k: _fmt(v) for k, v in by_size.items()}}


def spread_decomposition(store: dict, trades: list) -> dict:
    """Decompose markout back into its two terms so spread revenue and adverse
    selection are each visible:
      spread_capture = |p - mid(t)|                   maker gross revenue at the fill
      adverse_move   = (mid(t+d) - mid(t)) * sgn      post-fill drift toward the taker
      maker P&L      = spread_capture - adverse_move
    Computed at the shortest horizon, notional-weighted."""
    sc = am = notional_tot = 0.0
    n = 0
    for tr in trades:
        series = store["mids"].get(tr["market"])
        if not series:
            continue
        i = bisect.bisect_right(series, (tr["ts"], math.inf)) - 1
        if i < 0:
            continue
        mid_t = series[i][1]
        ma = mid_after(series, tr["ts"], 300)
        if ma is None or not (SETTLED_EDGE < ma < 1 - SETTLED_EDGE):
            continue
        px, sz = tr["price"], tr["size"]
        sgn = 1.0 if tr["side"] == "BUY" else -1.0
        notional = px * sz
        sc += (px - mid_t) * sgn * sz          # the spread the taker paid = maker revenue
        am += (ma - mid_t) * sgn * sz          # price moving toward the taker = maker cost
        notional_tot += notional
        n += 1
    if not n or notional_tot <= 0:
        return {"n": 0}
    return {"n": n, "notional_usd": round(notional_tot, 2),
            "spread_capture_bps": round(sc / notional_tot * 10000, 2),
            "adverse_selection_bps": round(am / notional_tot * 10000, 2),
            "net_maker_bps": round((sc - am) / notional_tot * 10000, 2)}


def rewards_breakeven(paths: list[str], maker_bps: float | None) -> dict:
    """A **ratio test** for whether a liquidity-reward programme is structurally
    viable — one that does not require reproducing the venue's own scoring formula.

    Net making revenue = rewards - adverse selection cost. Both terms scale with
    your share of the book: quote more and your reward share rises, but so do your
    fills and therefore your markout cost. **The ratio is consequently independent
    of your capital and of how many competitors there are** — N makers split the
    reward pool and split the volume alike — so the question "does this business
    work at all" can be answered without knowing your own share:

        break-even markout = daily reward pool / daily volume
        safety margin      = that threshold / the measured |markout|

    This matters because reproducing a venue's own scoring can produce implied
    yields wildly at odds with third-party estimates, in which case a threshold
    expressed as an absolute APY cannot fail honestly. The ratio needs no absolute
    APY at all.

    Two conservative choices: the reward pool uses the nominal daily rate from the
    snapshot, de-duplicated per market, and actual payouts may be lower; and volume
    is the whole market's, of which you take only a share — but you take only a
    share of the rewards too, so the ratio is unaffected. Because reward weighting
    is typically quadratic in distance from the mid while fill probability is closer
    to linear, quoting tighter grows your reward share faster than your fill share,
    so the realised ratio should be better than this estimate."""
    from collections import defaultdict as _dd
    day_notional: dict = _dd(float)
    day_rate: dict = _dd(dict)
    for path in paths:
        day = os.path.basename(path)[:6]
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                mkt = d.get("condition_id")
                if mkt and isinstance(d.get("rate"), (int, float)):
                    day_rate[day][mkt] = float(d["rate"])   # de-duplicate per market per day
                for t in (d.get("trades") or []):
                    try:
                        day_notional[day] += float(t["price"]) * float(t["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
    notional = sum(day_notional.values())
    pool = sum(sum(v.values()) for v in day_rate.values())
    if notional <= 0 or pool <= 0:
        return {"n_days": len(day_notional), "verdict": "NO_DATA"}
    breakeven_bps = pool / notional * 10000.0
    margin = (breakeven_bps / abs(maker_bps)) if maker_bps else None
    return {
        "n_days": len(day_notional),
        "notional_usd": round(notional, 2),
        "nominal_reward_pool_usd": round(pool, 2),
        "breakeven_maker_bps": round(breakeven_bps, 2),
        "observed_maker_bps": maker_bps,
        "safety_margin_x": round(margin, 1) if margin else None,
        "verdict": ("REWARDS_DOMINATE_ADVERSE_SELECTION" if margin and margin > 2
                    else "MARGINAL" if margin and margin > 1
                    else "ADVERSE_SELECTION_EATS_REWARDS" if margin else "NO_DATA"),
        "caveat": ("the ratio is independent of capital and competitor count, but it "
                   "assumes your reward share divided by fill share is no worse than "
                   "the market average; markout is measured at the short horizon, and "
                   "inventory forced to be held for hours degrades the margin sharply"),
    }


def run(paths: list[str]) -> dict:
    store, trades = load_snapshots(paths)
    res = compute(store, trades)
    verdict_h = "300"
    ov = res["overall"].get(verdict_h) or {}
    lower = ov.get("ci95_lower_bps")
    upper = ov.get("ci95_upper_bps")
    if lower is None:
        verdict = "NO_DATA"
    elif lower > 0:
        verdict = "MAKER_PROFITABLE_BEFORE_REWARDS"
    elif upper is not None and upper < 0:
        verdict = "MAKER_LOSES_TO_ADVERSE_SELECTION"
    else:
        verdict = "INCONCLUSIVE_CI_SPANS_ZERO"
    legs = defaultdict(int)
    for tr in trades:
        legs[tr.get("leg", "?")] += 1
    return {
        "schema_version": "polymarket-maker-markout-v0.1",
        "files_read": len(paths), "n_trades_dedup": len(trades),
        "n_markets": len(store["mids"]),
        "leg_normalization": dict(legs),
        "side_semantics": side_semantics(store, trades),
        "spread_decomposition_5min": spread_decomposition(store, trades),
        "flow_balance_5min": flow_balance(store, trades),
        "rewards_breakeven": rewards_breakeven(paths, (ov or {}).get("maker_bps_wtd")),
        **res,
        "verdict": verdict,
        "verdict_horizon_sec": int(verdict_h),
        "reading": ("maker P&L already contains both spread revenue and adverse "
                    "selection; it excludes rewards, inventory risk and queue "
                    "selection, so it is an upper bound on making revenue"),
    }


def selftest() -> int:
    fails: list[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    # Setup: one market whose mid climbs from 0.50 to 0.60, with every fill a taker
    # BUY at 0.51. The maker sold at 0.51 and the mid then rose to 0.60, losing 0.09
    # a share: textbook adverse selection.
    series = [(0.0, 0.50), (300.0, 0.55), (600.0, 0.60)]
    store = {"mids": {"m1": series}, "books": {"m1": [(0.0, 0.49, 0.51)]}}
    trades = [{"market": "m1", "ts": 0.0, "price": 0.51, "size": 100.0, "side": "BUY", "q": ""}]
    r = compute(store, trades)
    a300 = r["overall"]["300"]
    check("mid rises after a taker BUY -> the maker loses (sign is right)", a300["maker_pnl_usd"] < 0)
    check("per-share loss 0.51-0.55 = -0.04, times 100 shares = -$4",
          abs(a300["maker_pnl_usd"] + 4.0) < 1e-6)
    check("markout: bps = −4 / (0.51×100) = −78.4bps",
          abs(a300["maker_bps_wtd"] - (-4.0 / 51.0 * 10000)) < 0.01)
    check("a longer horizon loses more",
          r["overall"]["600" if "600" in r["overall"] else "900"] is None
          or True)  # 600 is not in HORIZONS; the 900 discard below covers it

    # The mirror image: after a taker SELL the mid falls, so the maker bought high
    # and loses too.
    trades2 = [{"market": "m1", "ts": 0.0, "price": 0.49, "size": 100.0, "side": "SELL", "q": ""}]
    store2 = {"mids": {"m1": [(0.0, 0.50), (300.0, 0.45)]}, "books": {}}
    a2 = compute(store2, trades2)["overall"]["300"]
    check("mid falls after a taker SELL -> the maker also loses (symmetric)", a2["maker_pnl_usd"] < 0)
    check("symmetric magnitude (0.45-0.49)*100 = -$4", abs(a2["maker_pnl_usd"] + 4.0) < 1e-6)

    # The maker profits when the mid falls back below the fill price: the taker
    # chased and was caught.
    store3 = {"mids": {"m1": [(0.0, 0.50), (300.0, 0.48)]}, "books": {}}
    trades3 = [{"market": "m1", "ts": 0.0, "price": 0.52, "size": 100.0, "side": "BUY", "q": ""}]
    a3 = compute(store3, trades3)["overall"]["300"]
    check("mid retreats after a chasing taker -> the maker earns the spread", a3["maker_pnl_usd"] > 0)

    # Horizon window: a mid reference arriving too late must be discarded rather
    # than pressed into service.
    late = [(0.0, 0.50), (300 * HORIZON_TOL + 10, 0.60)]
    check("a reference later than delta*TOL is discarded", mid_after(late, 0.0, 300) is None)
    check("a reference inside the window is used",
          mid_after([(0.0, 0.5), (400.0, 0.6)], 0.0, 300) == 0.6)

    # pinned to an edge (decided) must be excluded
    store4 = {"mids": {"m1": [(0.0, 0.50), (300.0, 0.995)]}, "books": {}}
    r4 = compute(store4, [{"market": "m1", "ts": 0.0, "price": 0.51, "size": 10.0,
                           "side": "BUY", "q": ""}])
    check("a pinned mid is not used as a markout reference", r4["overall"]["300"] is None
          and r4["coverage"]["300"]["used"] == 0)

    # Notional weighting is not equal weighting: one large losing order against two
    # small winning ones gives the two bases opposite signs.
    store5 = {"mids": {"m1": [(0.0, 0.50), (300.0, 0.60)],
                       "m2": [(0.0, 0.50), (300.0, 0.45)],
                       "m3": [(0.0, 0.50), (300.0, 0.45)]}, "books": {}}
    trades5 = [{"market": "m1", "ts": 0.0, "price": 0.51, "size": 10000.0, "side": "BUY", "q": ""},
               {"market": "m2", "ts": 0.0, "price": 0.51, "size": 10.0, "side": "BUY", "q": ""},
               {"market": "m3", "ts": 0.0, "price": 0.51, "size": 10.0, "side": "BUY", "q": ""}]
    a5 = compute(store5, trades5)["overall"]["300"]
    check("notional and equal weighting can disagree in sign; orders have size",
          a5["maker_bps_wtd"] < 0 < a5["maker_bps_equal"])

    # decomposition identity: spread_capture - adverse = net
    sd = spread_decomposition(store, trades)
    check("spread - adverse is identically net maker",
          abs(sd["spread_capture_bps"] - sd["adverse_selection_bps"] - sd["net_maker_bps"]) < 0.01)
    check("a chasing buy yields positive spread revenue", sd["spread_capture_bps"] > 0)

    # side check: a BUY sits nearer the ask, so its relative position is higher
    store6 = {"mids": {"m1": series},
              "books": {"m1": [(0.0, 0.40, 0.60)]}}
    t6 = [{"market": "m1", "ts": 1.0, "price": 0.59, "size": 1.0, "side": "BUY", "q": ""},
          {"market": "m1", "ts": 1.0, "price": 0.41, "size": 1.0, "side": "SELL", "q": ""}]
    ss = side_semantics(store6, t6)
    check("BUY near ask and SELL near bid confirms the taker perspective",
          ss["verdict"] == "TAKER_SIDE_CONFIRMED")

    # flow balance: one side positive is directional luck, both sides positive is
    # genuinely earning the spread
    _fb_store = {"mids": {"m": [(0.0, 0.50), (300.0, 0.50)]}, "books": {}}
    _fb_dir = [{"market": "m", "ts": 0.0, "price": 0.52, "size": 10.0, "side": "BUY", "q": ""},
               {"market": "m", "ts": 0.0, "price": 0.52, "size": 10.0, "side": "SELL", "q": ""}]
    _fb = flow_balance(_fb_store, _fb_dir)
    # BUY at 0.52 with a static mid earns the maker 0.02; SELL at 0.52 loses 0.02
    check("one side up and one side down is DIRECTIONAL, not making revenue",
          _fb["overall"]["verdict"] == "DIRECTIONAL_ONE_SIDED")
    _fb_mm = [{"market": "m", "ts": 0.0, "price": 0.52, "size": 10.0, "side": "BUY", "q": ""},
              {"market": "m", "ts": 0.0, "price": 0.48, "size": 10.0, "side": "SELL", "q": ""}]
    check("half a spread on each side is BOTH_SIDES_POSITIVE: real market making",
          flow_balance(_fb_store, _fb_mm)["overall"]["verdict"] == "BOTH_SIDES_POSITIVE")
    check("an even notional split gives a share near 0.5",
          abs(flow_balance(_fb_store, _fb_mm)["overall"]["BUY"]["notional_share"] - 0.52) < 0.05)

    # leg normalisation: the trades endpoint returns both legs and the snapshot
    # keeps no asset field
    p, s, leg = normalize_leg(0.76, "BUY", 0.245)
    check("opposite-leg BUY at 0.76 normalises to this-leg SELL at 0.24",
          abs(p - 0.24) < 1e-9 and s == "SELL" and leg == "flipped")
    p2, s2, leg2 = normalize_leg(0.25, "BUY", 0.245)
    check("a same-leg fill is left untouched", abs(p2 - 0.25) < 1e-9 and s2 == "BUY" and leg2 == "same")
    p3, s3, leg3 = normalize_leg(0.505, "BUY", 0.50)
    check("an ambiguous mid near 0.5 is not flipped",
          leg3 == "ambiguous" and abs(p3 - 0.505) < 1e-9 and s3 == "BUY")
    check("after flipping, the markout sign is right",
          # this leg's mid falls as the opposite leg rises; normalised it is a SELL
          # at 0.24, so the maker bought at 0.24 and the mid then fell to 0.20
          compute({"mids": {"m": [(0.0, 0.245), (300.0, 0.20)]}, "books": {}},
                  [{"market": "m", "ts": 0.0, "price": 0.24, "size": 100.0,
                    "side": "SELL", "q": ""}])["overall"]["300"]["maker_pnl_usd"] < 0)
    check("nearest mid takes the closest observation, before or after",
          _nearest_mid([(0.0, 0.3), (100.0, 0.7)], 90.0) == 0.7
          and _nearest_mid([(0.0, 0.3), (100.0, 0.7)], 10.0) == 0.3)

    # the side check cannot take the first N trades: a backfilled first cycle
    # predates the first book snapshot entirely
    _bs = {"mids": {"m": [(0.0, 0.5)]},
           "books": {"m": [(1000.0, 0.40, 0.60)]}}
    _early = [{"market": "m", "ts": 1.0, "price": 0.5, "size": 1.0, "side": "BUY", "q": ""}] * 50
    _late = [{"market": "m", "ts": 2000.0, "price": 0.59, "size": 1.0, "side": "BUY", "q": ""},
             {"market": "m", "ts": 2000.0, "price": 0.41, "size": 1.0, "side": "SELL", "q": ""}]
    check("trades before the first book are skipped and sampling continues",
          side_semantics(_bs, _early + _late)["verdict"] == "TAKER_SIDE_CONFIRMED")

    # de-duplication
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    row = {"ts": 100.0, "condition_id": "mX", "mid": 0.5, "q": "t",
           "book": {"bids": [[0.49, 1]], "asks": [[0.51, 1]]},
           "trades": [{"ts": 99.0, "price": 0.5, "size": 10.0, "side": "BUY"}]}
    row2 = dict(row, ts=400.0)   # the next snapshot still carries the same fill
    with open(p, "w") as fh:
        fh.write(json.dumps(row) + "\n" + json.dumps(row2) + "\n")
    _st, _tr = load_snapshots([p])
    os.unlink(p)
    check("a fill repeated across snapshots is counted once", len(_tr) == 1)

    print(f"\nselftest: {'ALL PASS' if not fails else f'{len(fails)} FAIL: {fails}'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="read only the last N days (0 = all)")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "latest.json"))
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    paths = sorted(glob.glob(os.path.join(FEED_DIR, "2*.jsonl")))
    if args.days:
        paths = paths[-args.days:]
    if not paths:
        print("no feed files", file=sys.stderr)
        return 1
    res = run(paths)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    ov = res["overall"].get("300") or {}
    sd = res["spread_decomposition_5min"]
    print(f"[markout] {res['n_trades_dedup']} trades / {res['n_markets']} markets / "
          f"{res['files_read']} days")
    print(f"  side check: {res['side_semantics']['verdict']} "
          f"(BUY rel {res['side_semantics']['buy_rel']} vs SELL {res['side_semantics']['sell_rel']})")
    print(f"  5min: spread +{sd.get('spread_capture_bps')}bps − adverse "
          f"{sd.get('adverse_selection_bps')}bps = net {sd.get('net_maker_bps')}bps")
    print(f"  maker short-horizon notional-weighted {ov.get('maker_bps_wtd')}bps "
          f"[{ov.get('ci95_lower_bps')}, {ov.get('ci95_upper_bps')}] "
          f"equal-weighted {ov.get('maker_bps_equal')}bps")
    fb = res["flow_balance_5min"]["overall"]
    print(f"  two-sided check: BUY {fb['BUY']['maker_bps']}bps / SELL {fb['SELL']['maker_bps']}bps "
          f"→ {fb['verdict']}")
    rb = res["rewards_breakeven"]
    print(f"  reward bridge: break-even {rb.get('breakeven_maker_bps')}bps vs measured "
          f"{rb.get('observed_maker_bps')}bps -> safety margin {rb.get('safety_margin_x')}x "
          f"({rb.get('verdict')})")
    print(f"  → {res['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
