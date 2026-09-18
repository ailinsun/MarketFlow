#!/usr/bin/env python3
"""Resting-order fill model — first cut at fill probability.

**Why this target is worth attacking.** A common dead end in this space is trying
to predict the public market price. The price level behaves close to an
integrator with a spectral radius just under one, which makes the path close to a
martingale and essentially unpredictable. That is the wrong target.

But **the price is the martingale, not the execution.** Whether an order resting
at a given price gets filled depends on book depth, spread, queue position and
counterparty arrival rate — classic point-process and queueing objects with real
structure. It also satisfies the three conditions for not simply re-running a
previous null result:

  1. A new target: fill, not price. Not the same question measured again with the
     same data and the same method.
  2. A new data source: your own book sampler, giving full depth plus incremental
     trades. Public APIs do not serve historical books.
  3. A direct line to money. Every maker-versus-taker choice is otherwise made on
     intuition, and the symptoms of that are concrete: a run of fill-or-kill
     orders that never fill, or post-only orders that cross the book.

**This module answers one falsifiable question**: is fill explained by book state?
If stratified fill rates show no monotone structure in the features and
discrimination is no better than chance, that is a null result, reported as one,
and the effort goes elsewhere.

**Definitions. Every one of these affects the conclusion, so they are pinned
here:**

- One "observation" = (market, adjacent snapshot pair t -> t+1, candidate resting
  price p). The book state comes from t, and the incremental trades at t+1 give
  the fills over that interval. Adjacent snapshots have been verified to carry
  disjoint trades, so the increment really is an increment.
- Candidate prices are bucketed by **absolute distance from the mid**, which makes
  them comparable across markets. Both sides are computed. That the sell side
  behaves symmetrically started as an assertion here and is now a measured result:
  the sell-to-buy fill ratio is close to one and both sides show the same queue
  discrimination.
- Two fill tests are computed side by side, because their difference is exactly
  the queue loss:
    naive  : the lowest trade price in the interval crossed p. Ignores the queue,
             so it is an upper bound.
    queued : the price crossed **and** the cumulative volume at that level exceeded
             the queue ahead. Closer to reality.
- Discrimination is measured with AUC (Mann-Whitney), not accuracy. With a low
  base fill rate, "predict never filled" scores a high accuracy while carrying no
  information at all.

Hard boundary: reads book samples offline. It places no order, touches no arm
state, caps, kill file, secret or live process, and writes to no execution path.
Output goes only to its own isolated namespace.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Any

from marketflow.paths import PROJECT_DIR as REPO, runtime_path
FEED_DIR = runtime_path("feeds", "book_samples")
OUT_DIR = runtime_path("execution", "fill_model")

# Candidate resting-price buckets: absolute distance from the mid, in cents. A buy
# rests below the mid, and the further out it sits the harder it fills.
OFFSET_CENTS = (0.5, 1.0, 2.0, 3.0, 5.0)
# Restrict to markets with real liquidity. Otherwise dead markets drag the fill
# rate to zero and the result claims structure it has not found.
MIN_DEPTH_USDC = 1_000.0

# Quantities drawn from the **outcome window**. They share a source with the label
# — the label is defined by those very trades — so using them to "predict" is
# predicting the answer from the answer. On a first run such a feature scored an
# AUC over 0.9 and looked like the strongest signal while carrying no predictive
# value at all. The verdict only ever reads the causal-only AUC.
LEAKY_FEATURES = frozenset({"trades_in_window", "volume_through"})


def _levels(book: Any, side: str) -> list[tuple[float, float]]:
    """(price, size) pairs; bids highest first, asks lowest first. A malformed row
    is skipped rather than guessed at.

    Samplers write `[price, size]` arrays; a dict form is accepted too, because
    upstream SDKs have produced both. **This once accepted only the dict form: the
    selftest was green against dict fixtures while real data produced not a single
    observation.** A fixture shaped differently from real data means the test is
    testing a world that does not exist."""
    out: list[tuple[float, float]] = []
    for lv in ((book or {}).get(side) or []):
        try:
            if isinstance(lv, dict):
                p, s = float(lv.get("price")), float(lv.get("size"))
            else:
                p, s = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if p <= 0 or s <= 0:
            continue
        out.append((p, s))
    return sorted(out, key=lambda x: x[0], reverse=(side == "bids"))


def load_snapshots(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows


def queue_ahead(bids: list[tuple[float, float]], price: float) -> float:
    """Volume ahead of an order resting at `price`: every strictly better (higher)
    bid plus whatever already sits at the same price. Price priority then time
    priority, so existing orders at your price are all ahead of you."""
    return sum(s for p, s in bids if p > price - 1e-9)


def queue_ahead_sell(asks: list[tuple[float, float]], price: float) -> float:
    """The sell-side mirror: better means lower. Ahead of an ask resting at `price`
    is every cheaper ask plus whatever already sits at the same price."""
    return sum(s for p, s in asks if p < price + 1e-9)


def queue_aware_maker_price(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                            *, max_price: float, tick: float) -> dict:
    """Given a book, choose a post-only buy price. This is the direct consumer of
    this module's two measured results.

    The naive rule, one tick above the best bid, systematically picks **the maker
    price least likely to fill** whenever the spread is wide. Every level inside the
    spread has zero queue ahead of it — nobody is waiting in an empty gap — so the
    dominant variable does not discriminate between them at all. What does
    discriminate is distance from the mid, and one tick above the best bid is the
    furthest from the mid of every maker level available. Measured fill rates fall
    by roughly a factor of six across that distance range.

    So the objective is not "stay a maker and pay as little as possible" but
    **"maximise fill probability subject to the edge ceiling"**:
      (i)   it must remain a maker, strictly below the best ask, never crossing;
      (ii)  it must stay at or below max_price, the caller's edge ceiling. That is
            not negotiable: filling at too high a price is still a loss;
      (iii) among the levels satisfying both, take the smallest queue ahead, which
            is the dominant variable, breaking ties by the **highest** price, which
            is closest to the mid and therefore most likely to fill.

    **It maximises fill probability, not expected value, and that trade-off has to
    be stated.** EV(p) = P(fill | p) * (p_win - p): resting higher fills more easily
    while thinning the edge per share. This function does not solve that
    optimisation, because solving it needs a point estimate of P(fill | p) — and the
    **absolute fill rate does not extrapolate**, swinging several-fold from day to
    day. Only the discrimination is trustworthy. Optimising against an untrustworthy
    probability is more dangerous than following a rule with a guaranteed floor.

    Hence a greedy rule with a floor. **The edge floor is guaranteed by the
    caller's own gate**: the entry gate has already established that the model
    probability exceeds the ask plus a buffer, and this function only ever picks a
    price at or below the ask, so a fill still leaves at least that buffer of edge.
    Above that floor, fill probability is pushed as high as it goes.

    The worst case is earning less, not losing; not filling earns nothing at all.
    The measured magnitudes support the direction, and the edge given up has a hard
    lower bound. If the buffer were ever set near zero that floor disappears, and
    this rule would have to be re-evaluated.

    **What it does not do**: pretend to a point estimate of fill probability. It
    ranks and reports no probability.

    When it returns ok=False the caller keeps its previous behaviour: failing to
    choose a price is not a reason to skip the order.
    """
    if not (tick > 0) or not (0 < max_price < 1):
        return {"ok": False, "reason": "bad tick or max_price"}
    if not asks:
        return {"ok": False, "reason": "no ask side; cannot bound maker price"}
    best_ask = asks[0][0]
    best_bid = bids[0][0] if bids else None
    # Candidates are the tick grid on [best_bid, best_ask), clipped below the edge
    # ceiling.
    ceiling = min(max_price, best_ask - tick)
    floor = best_bid if best_bid is not None and best_bid > 0 else tick
    if ceiling < floor - 1e-9:
        # Even the best bid is above the ceiling, or the spread is zero: there is no
        # price that both keeps the edge and stays a maker.
        return {"ok": False, "reason": "no maker level clears the edge ceiling",
                "best_bid": best_bid, "best_ask": best_ask, "max_price": max_price}
    levels: list[dict] = []
    n = int(round((ceiling - floor) / tick))
    for i in range(max(0, n) + 1):
        p = round(floor + i * tick, 8)
        if p >= best_ask - 1e-9 or p > max_price + 1e-9 or p <= 0:
            continue
        levels.append({"price": p, "queue_ahead": round(queue_ahead(bids, p), 4)})
    if not levels:
        return {"ok": False, "reason": "no candidate level in (best_bid, best_ask)"}
    # Smallest queue first; ties go to the highest price, which is closest to the
    # mid and measurably the most likely to fill.
    best = min(levels, key=lambda lv: (lv["queue_ahead"], -lv["price"]))
    return {
        "ok": True,
        "price": best["price"],
        "queue_ahead": best["queue_ahead"],
        "basis": "queue_aware_max_fillable",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "max_price": max_price,
        "n_levels": len(levels),
        "cheapest_price": (round(best_bid + tick, 8)
                         if best_bid is not None and best_bid > 0 and best_bid + tick < best_ask
                         else round(best_ask - tick, 8)),
    }


def observations(rows: list[dict], *, offsets: tuple = OFFSET_CENTS,
                 min_depth: float = MIN_DEPTH_USDC, side: str = "buy") -> list[dict]:
    """Flatten a snapshot series into (state, filled) observations. The incremental
    trades between adjacent snapshots are the outcome.

    `side` chooses which side to rest on. An earlier version computed the buy side
    only and asserted in its docstring that the sell side was symmetric — **an
    assertion, not a result**. Binary markets are skewed by construction, with most
    mids far from 0.5, so symmetry has to be measured. The mirror relationship:
        buy : rest at mid - off; better means a higher bid; filled if a trade
              printed at or below your price
        sell: rest at mid + off; better means a lower ask; filled if a trade
              printed at or above your price
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy|sell")
    by_mkt: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        mid = r.get("market_id")
        if mid is None:
            continue
        by_mkt[str(mid)].append(r)

    obs: list[dict] = []
    for mkt, seq in by_mkt.items():
        seq = sorted(seq, key=lambda r: float(r.get("ts") or 0))
        for i in range(len(seq) - 1):
            cur, nxt = seq[i], seq[i + 1]
            dt = float(nxt.get("ts") or 0) - float(cur.get("ts") or 0)
            if dt <= 0 or dt > 1800:        # a sampling gap is not an observation:
                #                             a long interval would look like a high fill rate
                continue
            depth = float(cur.get("depth_usdc") or 0)
            if depth < min_depth:
                continue
            mid_px = cur.get("mid")
            try:
                mid_px = float(mid_px)
            except (TypeError, ValueError):
                continue
            if not (0 < mid_px < 1):
                continue
            bids = _levels(cur.get("book"), "bids")
            asks = _levels(cur.get("book"), "asks")
            if not bids or not asks:
                continue
            best_bid, best_ask = bids[0][0], asks[0][0]
            spread = max(0.0, best_ask - best_bid)

            # trades over this interval, i.e. the next snapshot's increment
            trades = []
            for t in (nxt.get("trades") or []):
                try:
                    trades.append((float(t.get("price")), float(t.get("size"))))
                except (TypeError, ValueError, AttributeError):
                    continue
            low_px = min((p for p, _ in trades), default=None)
            high_px = max((p for p, _ in trades), default=None)

            for off in offsets:
                if side == "buy":
                    p = round(mid_px - off / 100.0, 4)
                    if p <= 0.01 or p >= best_ask:   # crossing the ask is no longer a maker
                        continue
                    q = queue_ahead(bids, p)
                    naive = bool(low_px is not None and low_px <= p + 1e-9)
                    vol_through = sum(s for tp, s in trades if tp <= p + 1e-9)
                else:
                    p = round(mid_px + off / 100.0, 4)
                    if p >= 0.99 or p <= best_bid:   # crossing the bid is no longer a maker
                        continue
                    q = queue_ahead_sell(asks, p)
                    naive = bool(high_px is not None and high_px >= p - 1e-9)
                    vol_through = sum(s for tp, s in trades if tp >= p - 1e-9)
                # queued additionally requires the cumulative volume through that
                # level to consume the queue ahead. `naive` already implies that a
                # trade happened, so no separate volume check is needed here. The
                # survival path carries no such implication and states it explicitly.
                queued = bool(naive and vol_through >= q)
                obs.append({
                    "market_id": mkt, "ts": float(cur.get("ts") or 0), "dt_sec": dt,
                    "side": side,
                    "offset_cents": off, "price": p, "mid": mid_px,
                    "spread_cents": round(spread * 100, 3),
                    "queue_ahead": round(q, 4),
                    "depth_usdc": depth,
                    "trades_in_window": len(trades),
                    "volume_through": round(vol_through, 4),
                    "fill_naive": naive, "fill_queued": queued,
                })
    return obs


def survival(rows: list[dict], *, horizon: int = 12, offsets: tuple = OFFSET_CENTS,
             min_depth: float = MIN_DEPTH_USDC) -> list[dict]:
    """Which window a resting order first fills in: a multi-window survival
    observation.

    A single window answers only whether an order fills. It cannot answer **how
    long to wait before crossing the spread instead** — which is the decision the
    execution layer actually has to make, and the one nobody answers when a run of
    orders sits pending forever.

    Definitions:
    - The resting price does **not move** once placed, which is what a real maker
    does, while the mid drifts. Its relative position therefore changes across
    later windows, and that is precisely what is being measured.
    - Volume accumulates **across windows**: a queue is consumed once and what is
    eaten does not come back.
    - Only start points with a **full horizon of later snapshots** are used.
    Otherwise "not filled yet" and "not observed" mix together as right-censoring
    and depress the hazard systematically.
    """
    by_mkt: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("market_id") is not None:
            by_mkt[str(r["market_id"])].append(r)

    out: list[dict] = []
    for mkt, seq in by_mkt.items():
        seq = sorted(seq, key=lambda r: float(r.get("ts") or 0))
        for i in range(len(seq) - horizon):
            cur = seq[i]
            depth = float(cur.get("depth_usdc") or 0)
            if depth < min_depth:
                continue
            try:
                mid_px = float(cur.get("mid"))
            except (TypeError, ValueError):
                continue
            if not (0 < mid_px < 1):
                continue
            bids, asks = _levels(cur.get("book"), "bids"), _levels(cur.get("book"), "asks")
            if not bids or not asks:
                continue
            best_ask = asks[0][0]
            # a sampling gap is not an observation: window lengths stop being comparable
            gaps = [float(seq[i + k + 1].get("ts") or 0) - float(seq[i + k].get("ts") or 0)
                    for k in range(horizon)]
            if any(g <= 0 or g > 1800 for g in gaps):
                continue

            for off in offsets:
                p = round(mid_px - off / 100.0, 4)
                if p <= 0.01 or p >= best_ask:
                    continue
                q = queue_ahead(bids, p)
                cum = 0.0
                hit = None
                for k in range(horizon):
                    nxt = seq[i + k + 1]
                    for t in (nxt.get("trades") or []):
                        try:
                            tp, ts_ = float(t.get("price")), float(t.get("size"))
                        except (TypeError, ValueError, AttributeError):
                            continue
                        if tp <= p + 1e-9:
                            cum += ts_
                    # The volume check cannot be dropped. With an empty queue,
                    # `cum >= q` is satisfied by 0 >= 0, so a window with no trades
                    # at all would count as a fill and inflate the first window's
                    # hazard. A fill has to have actually happened.
                    if hit is None and cum > 0 and cum >= q:
                        hit = k + 1          # first fill in window k+1 (1-indexed)
                        break
                out.append({"market_id": mkt, "offset_cents": off, "queue_ahead": round(q, 4),
                            "depth_usdc": depth, "fill_window": hit,
                            "window_sec": round(sum(gaps) / len(gaps), 1)})
    return out


def hazard_table(surv: list[dict], *, horizon: int = 12) -> dict:
    """Survival curve plus per-window hazard.

    hazard h(k) = P(fill in window k | still resting at window k). This is the
    decision quantity. A falling hazard means the marginal hope of waiting is
    shrinking, and past some k continuing to rest is worse than crossing. A
    constant hazard means the process is memoryless, so waiting longer changes
    nothing and the only reason to cross is opportunity cost."""
    if not surv:
        return {}
    n0 = len(surv)
    at_risk, table = n0, []
    for k in range(1, horizon + 1):
        filled = sum(1 for s in surv if s["fill_window"] == k)
        h = filled / at_risk if at_risk else 0.0
        at_risk_next = at_risk - filled
        table.append({
            "window": k,
            "at_risk": at_risk,
            "filled": filled,
            "hazard": round(h, 4),
            "cum_fill_rate": round((n0 - at_risk_next) / n0, 4),
        })
        at_risk = at_risk_next
    ever = sum(1 for s in surv if s["fill_window"] is not None)
    return {
        "n_orders": n0,
        "ever_filled": ever,
        "ever_fill_rate": round(ever / n0, 4),
        "median_window_sec": round(sorted(s["window_sec"] for s in surv)[n0 // 2], 1),
        "table": table,
    }


def hazard_shape(tbl: dict) -> dict:
    """Read the hazard shape — the conclusion that decides waiting versus crossing.

    Compares the mean hazard of the first three windows against the last three.
    **Falling means ageing**: waiting keeps getting less likely to pay, and there is
    a point at which to give up. Constant means memoryless: waiting itself changes
    nothing."""
    t = (tbl or {}).get("table") or []
    if len(t) < 6:
        return {}
    early = sum(r["hazard"] for r in t[:3]) / 3
    late = sum(r["hazard"] for r in t[-3:]) / 3
    ratio = (late / early) if early > 0 else None
    shape = "UNKNOWN"
    if ratio is not None:
        shape = "AGING" if ratio < 0.7 else ("FLAT" if ratio < 1.3 else "RISING")
    return {
        "early_hazard": round(early, 4), "late_hazard": round(late, 4),
        "late_over_early": round(ratio, 3) if ratio is not None else None,
        "shape": shape,
        "reading": {
            "AGING": "waiting pays less over time; there is a point to cross instead",
            "FLAT": "memoryless; waiting does not change the fill probability, so the "
                    "only reason to cross is opportunity cost",
            "RISING": "waiting pays more over time; do not give up early",
            "UNKNOWN": "insufficient sample",
        }[shape],
    }


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC. Discrimination is measured with this rather than accuracy:
    with a low base fill rate, "predict never filled" scores high accuracy and
    carries no information. Ties take the mean rank, as standard."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_pos = sum(r for r, y in zip(ranks, labels) if y)
    n_pos, n_neg = len(pos), len(neg)
    return (rank_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    hw = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - hw), min(1.0, c + hw))


def summarize(obs: list[dict]) -> dict:
    """Stratified fill rate plus discrimination. The conclusion rests on two things
    only: whether the strata are monotone, and whether the AUC is far from 0.5."""
    out: dict[str, Any] = {"n_observations": len(obs)}
    if not obs:
        return out

    for label in ("fill_naive", "fill_queued"):
        ys = [bool(o[label]) for o in obs]
        base = sum(ys) / len(ys)
        by_off = []
        for off in sorted({o["offset_cents"] for o in obs}):
            sel = [o for o in obs if o["offset_cents"] == off]
            k = sum(1 for o in sel if o[label])
            lo, hi = wilson(k, len(sel))
            by_off.append({"offset_cents": off, "n": len(sel), "fill_rate": round(k / len(sel), 4),
                           "ci95": [round(lo, 4), round(hi, 4)]})
        # Per-feature discrimination, with the sign aligned so that larger means
        # harder to fill.
        #
        # **Only quantities from the t snapshot are legitimate predictors.** The
        # leaky features below come from the outcome window, and the label is
        # defined by those same trades, so using them is predicting the answer from
        # the answer. They are still computed, precisely to show how convincingly a
        # leak imitates a real signal, and they are excluded from every verdict.
        feats = {
            "neg_offset": [-o["offset_cents"] for o in obs],
            "neg_queue_ahead": [-o["queue_ahead"] for o in obs],
            "neg_spread_cents": [-o["spread_cents"] for o in obs],
            "depth_usdc": [o["depth_usdc"] for o in obs],
            "trades_in_window": [o["trades_in_window"] for o in obs],
        }
        aucs = {k: (round(v, 4) if (v := auc(vals, ys)) is not None else None)
                for k, vals in feats.items()}
        out[label] = {
            "base_rate": round(base, 4), "by_offset": by_off, "auc": aucs,
            "leaky_excluded": sorted(LEAKY_FEATURES),
            "auc_causal_only": {k: v for k, v in aucs.items() if k not in LEAKY_FEATURES},
        }

    # Queue loss is the difference between the two tests: the share where the price
    # arrived but the queue never reached you.
    n_naive = sum(1 for o in obs if o["fill_naive"])
    n_queued = sum(1 for o in obs if o["fill_queued"])
    out["queue_loss"] = {
        "naive_fills": n_naive, "queued_fills": n_queued,
        "lost_to_queue": n_naive - n_queued,
        "lost_fraction": round((n_naive - n_queued) / n_naive, 4) if n_naive else None,
    }
    out["markets"] = len({o["market_id"] for o in obs})
    out["median_window_sec"] = round(sorted(o["dt_sec"] for o in obs)[len(obs) // 2], 1)
    return out


def symmetry_check(buy_summary: dict, sell_summary: dict) -> dict:
    """Whether the two sides agree.

    Disagreement is itself a useful finding. Binary markets are skewed, and if one
    side fills systematically more easily then quoting symmetrically on both sides
    carries an implicit directional exposure."""
    b = (buy_summary or {}).get("fill_queued") or {}
    s_ = (sell_summary or {}).get("fill_queued") or {}
    if not b or not s_:
        return {}
    ba = (b.get("auc_causal_only") or {}).get("neg_queue_ahead")
    sa = (s_.get("auc_causal_only") or {}).get("neg_queue_ahead")
    br, sr = b.get("base_rate"), s_.get("base_rate")
    both_strong = bool(ba is not None and sa is not None and ba >= 0.60 and sa >= 0.60)
    rate_ratio = (sr / br) if (br and sr is not None and br > 0) else None
    return {
        "buy_base_rate": br, "sell_base_rate": sr,
        "buy_auc_queue": ba, "sell_auc_queue": sa,
        "sell_over_buy_rate": round(rate_ratio, 3) if rate_ratio is not None else None,
        "both_sides_structured": both_strong,
        "reading": ("structure on both sides means the model supports two-sided "
                    "quoting; a large gap in fill rates means symmetric quoting "
                    "carries implicit directional exposure and the offsets should "
                    "differ per side."),
    }


def verdict(summary: dict) -> dict:
    """The falsifiable test, pre-registered here: structure means the strata are
    monotone AND at least one feature reaches an AUC of 0.60.

    That threshold is not arbitrary. Discrimination below it cannot change any
    sizing decision even when it is real, and "statistically significant but
    economically irrelevant" is how a microstructure line dies: an edge an order of
    magnitude smaller than the taker fee is not an edge."""
    res = {}
    for label in ("fill_naive", "fill_queued"):
        s = summary.get(label)
        if not s:
            continue
        rates = [b["fill_rate"] for b in s["by_offset"]]
        monotone = all(rates[i] >= rates[i + 1] - 1e-9 for i in range(len(rates) - 1))
        # Judge on causal features only: a leaky feature never counts, however high.
        best = max(((k, v) for k, v in s["auc_causal_only"].items() if v is not None),
                   key=lambda kv: abs(kv[1] - 0.5), default=(None, None))
        strong = bool(best[1] is not None and abs(best[1] - 0.5) >= 0.10)
        res[label] = {
            "monotone_in_offset": monotone,
            "best_feature": best[0], "best_auc": best[1],
            "verdict": "STRUCTURE_FOUND" if (monotone and strong) else
                       ("WEAK" if monotone or strong else "NULL"),
        }
    return res


def stability_by_day(obs: list[dict], label: str = "fill_queued") -> dict:
    """Fill rate and discrimination grouped by day: is the conclusion stable over
    time?

    A few days cannot prove stability over a month, but **it is enough to
    falsify**: if adjacent days already disagree, there is no point waiting a month.
    This is a cheap early chance to be proven wrong, not a proof of stability."""
    import time as _t
    by_day: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        day = _t.strftime("%Y-%m-%d", _t.gmtime(o["ts"]))
        by_day[day].append(o)
    rows = []
    for day in sorted(by_day):
        sel = by_day[day]
        ys = [bool(o[label]) for o in sel]
        k = sum(ys)
        lo, hi = wilson(k, len(sel))
        a = auc([-o["queue_ahead"] for o in sel], ys)
        rows.append({"day": day, "n": len(sel), "fill_rate": round(k / len(sel), 4),
                     "ci95": [round(lo, 4), round(hi, 4)],
                     "auc_neg_queue": round(a, 4) if a is not None else None})
    aucs = [r["auc_neg_queue"] for r in rows if r["auc_neg_queue"] is not None]
    rates = [r["fill_rate"] for r in rows]
    return {
        "by_day": rows,
        "auc_spread": round(max(aucs) - min(aucs), 4) if len(aucs) > 1 else None,
        "fill_rate_spread": round(max(rates) - min(rates), 4) if len(rates) > 1 else None,
        "reading": ("if the daily AUCs hold up and the fill rates stay the same order "
                    "of magnitude, the conclusion is at least not propped up by a "
                    "single unusual day. A chance to falsify, not a proof."),
    }


def run(paths: list[str], *, out_dir: str = OUT_DIR, horizon: int = 12) -> dict:
    rows = load_snapshots(paths)
    obs = observations(rows)
    summary = summarize(obs)
    summary["verdict"] = verdict(summary)
    # Multi-window: a single window answers whether it fills; this answers how long.
    surv = survival(rows, horizon=horizon)
    tbl = hazard_table(surv, horizon=horizon)
    summary["survival"] = tbl
    summary["hazard_shape"] = hazard_shape(tbl)
    # ever-filled rate by bucket: which price is worth resting at
    by_off = []
    for off in sorted({s["offset_cents"] for s in surv}):
        sel = [s for s in surv if s["offset_cents"] == off]
        ever = sum(1 for s in sel if s["fill_window"] is not None)
        med = sorted(s["fill_window"] for s in sel if s["fill_window"] is not None)
        by_off.append({
            "offset_cents": off, "n": len(sel),
            "ever_fill_rate": round(ever / len(sel), 4) if sel else None,
            "median_fill_window": med[len(med) // 2] if med else None,
        })
    summary["survival_by_offset"] = by_off
    # Sell-side symmetry check: an earlier version asserted it in a docstring; this
    # measures it.
    obs_sell = observations(rows, side="sell")
    sell_sum = summarize(obs_sell)
    summary["sell_side"] = {
        "n_observations": sell_sum.get("n_observations"),
        "fill_queued": sell_sum.get("fill_queued"),
        "verdict": verdict(sell_sum).get("fill_queued"),
    }
    summary["symmetry"] = symmetry_check(summary, sell_sum)
    summary["stability"] = stability_by_day(obs)
    summary["source_files"] = [os.path.basename(p) for p in paths]
    summary["snapshots_read"] = len(rows)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    return summary


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def selftest() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    # queue_ahead: price priority, plus existing volume at the same price
    bids = [(0.60, 100.0), (0.58, 50.0), (0.55, 20.0)]
    check("all better prices are ahead", queue_ahead(bids, 0.57) == 150.0)
    check("existing volume at the same price is ahead", queue_ahead(bids, 0.58) == 150.0)
    check("nobody is ahead above the best price", queue_ahead(bids, 0.61) == 0.0)

    # queue_aware_maker_price: wide and narrow spreads, plus the edge ceiling
    bk_bids = [(0.70, 500.0), (0.69, 300.0)]
    bk_asks = [(0.82, 400.0)]
    wide = queue_aware_maker_price(bk_bids, bk_asks, max_price=0.90, tick=0.01)
    check("a wide spread takes the empty level nearest the ask",
          wide["ok"] and abs(wide["price"] - 0.81) < 1e-9 and wide["queue_ahead"] == 0.0)
    check("the cheapest-price rule picks the furthest level on the same book",
          abs(wide["cheapest_price"] - 0.71) < 1e-9)
    capped = queue_aware_maker_price(bk_bids, bk_asks, max_price=0.75, tick=0.01)
    check("the edge ceiling clips the top",
          capped["ok"] and abs(capped["price"] - 0.75) < 1e-9)
    # A one-tick spread: the only maker level is the best bid, behind whatever
    # already rests there. That is the shape where crossing should be considered,
    # and the function reports the queue honestly rather than pretending it found a
    # good spot.
    tight = queue_aware_maker_price([(0.81, 900.0)], [(0.82, 100.0)], max_price=0.90, tick=0.01)
    check("a one-tick spread leaves only the best bid, with the queue reported",
          tight["ok"] and abs(tight["price"] - 0.81) < 1e-9 and tight["queue_ahead"] == 900.0)
    check("a ceiling below the best bid has no solution; it never forces a losing price",
          not queue_aware_maker_price(bk_bids, bk_asks, max_price=0.60, tick=0.01)["ok"])
    check("no ask side means no solution: there is no maker/taker boundary",
          not queue_aware_maker_price(bk_bids, [], max_price=0.90, tick=0.01)["ok"])
    check("it never crosses the best ask",
          queue_aware_maker_price(bk_bids, bk_asks, max_price=0.99, tick=0.01)["price"] < 0.82)
    # a middle level with a non-empty queue is skipped for a dearer empty one
    step = queue_aware_maker_price([(0.78, 700.0), (0.70, 100.0)], [(0.82, 50.0)],
                                   max_price=0.90, tick=0.01)
    check("skips a queued level for a higher empty one",
          step["ok"] and step["queue_ahead"] == 0.0 and step["price"] > 0.78)

    # AUC: perfectly separable, uninformative, inverted
    check("perfectly separable is 1", auc([1, 2, 3, 4], [False, False, True, True]) == 1.0)
    check("inverted is 0", auc([4, 3, 2, 1], [False, False, True, True]) == 0.0)
    check("all ties is 0.5", auc([1, 1, 1, 1], [False, True, False, True]) == 0.5)
    check("a single class returns None", auc([1, 2], [True, True]) is None)

    # both book shapes must parse: samplers write arrays, some SDKs give dicts
    check("array form [price, size]",
          _levels({"bids": [[0.60, 100.0], [0.58, 50.0]]}, "bids") == [(0.60, 100.0), (0.58, 50.0)])
    check("dict form {price, size}",
          _levels({"bids": [{"price": 0.60, "size": 100.0}]}, "bids") == [(0.60, 100.0)])
    check("malformed rows are skipped", _levels({"bids": [[0.6, 1.0], ["x", "y"], [0, 5]]}, "bids") == [(0.6, 1.0)])

    # observations: two snapshots, the second carrying a crossing trade.
    # The fixture uses the **real sampler format**. Written with dicts once, the
    # selftest went green while real data produced zero observations.
    def snap(ts, trades, bid=0.60, bidsz=100.0):
        return {"market_id": "m1", "ts": ts, "mid": 0.61, "depth_usdc": 50_000.0,
                "book": {"bids": [[bid, bidsz]], "asks": [[0.62, 100.0]]},
                "trades": trades}

    # a trade at 0.585 crosses 0.59 with volume above the queue: both tests fill
    obs = observations([snap(0, []), snap(300, [{"price": 0.585, "size": 200.0}])],
                       offsets=(2.0,))
    check("price crosses with enough volume: both tests fill",
          len(obs) == 1 and obs[0]["fill_naive"] and obs[0]["fill_queued"])
    # same crossing with volume below the queue: naive fills, queued does not
    obs2 = observations([snap(0, []), snap(300, [{"price": 0.585, "size": 10.0}])],
                        offsets=(2.0,))
    check("volume below the queue: naive fills, queued does not",
          len(obs2) == 1 and obs2[0]["fill_naive"] and not obs2[0]["fill_queued"])
    # the trade never reached the resting price
    obs3 = observations([snap(0, []), snap(300, [{"price": 0.60, "size": 500.0}])],
                        offsets=(2.0,))
    check("no crossing: neither test fills",
          len(obs3) == 1 and not obs3[0]["fill_naive"] and not obs3[0]["fill_queued"])
    # dead markets are filtered out
    dead = [dict(snap(0, []), depth_usdc=10.0), dict(snap(300, []), depth_usdc=10.0)]
    check("illiquid markets are excluded", observations(dead, offsets=(2.0,)) == [])
    # a sampling gap is not an observation
    gap = observations([snap(0, []), snap(9999, [{"price": 0.50, "size": 999.0}])], offsets=(2.0,))
    check("a sampling gap yields nothing", gap == [])

    # sell side: the queue direction reverses, better means lower
    asks = [(0.62, 100.0), (0.64, 50.0), (0.66, 20.0)]
    check("cheaper asks are ahead", queue_ahead_sell(asks, 0.65) == 150.0)
    check("existing volume at the same ask is ahead", queue_ahead_sell(asks, 0.64) == 150.0)
    check("nobody is ahead below the best ask", queue_ahead_sell(asks, 0.61) == 0.0)
    check("side accepts only buy or sell",
          _raises(lambda: observations([], side="both")))

    # sell fill: a higher-priced trade crosses the resting ask
    sell_hit = observations([snap(0, []), snap(300, [{"price": 0.635, "size": 200.0}])],
                            offsets=(2.0,), side="sell")
    check("a higher trade crosses a resting ask",
          len(sell_hit) == 1 and sell_hit[0]["fill_naive"] and sell_hit[0]["side"] == "sell")
    sell_miss = observations([snap(0, []), snap(300, [{"price": 0.60, "size": 200.0}])],
                             offsets=(2.0,), side="sell")
    check("a lower trade does not trigger a resting ask", sell_miss and not sell_miss[0]["fill_naive"])

    # stability: two days grouped
    fake_obs = ([{"ts": 1785000000, "queue_ahead": 10, "fill_queued": True} for _ in range(5)]
                + [{"ts": 1785000000, "queue_ahead": 99, "fill_queued": False} for _ in range(5)]
                + [{"ts": 1785100000, "queue_ahead": 10, "fill_queued": True} for _ in range(5)]
                + [{"ts": 1785100000, "queue_ahead": 99, "fill_queued": False} for _ in range(5)])
    stab = stability_by_day(fake_obs)
    check("grouped by day", len(stab["by_day"]) == 2)
    check("AUC computed per day", all(r["auc_neg_queue"] == 1.0 for r in stab["by_day"]))
    check("cross-day spread", stab["auc_spread"] == 0.0)

    # the Wilson lower bound tightens with sample size
    lo1, _ = wilson(9, 10)
    lo2, _ = wilson(900, 1000)
    check("same proportion, larger sample, higher bound", lo2 > lo1)

    # --- multi-window survival ---
    # five snapshots; the cumulative volume only consumes the queue in a later window
    seq = [snap(0, [])] + [snap(300 * k, [{"price": 0.585, "size": 60.0}]) for k in range(1, 5)]
    sv = survival(seq, horizon=2, offsets=(2.0,))
    # each valid start point yields one observation, each filling in the second window
    check("volume accumulates across windows",
          len(sv) == 3 and all(s["fill_window"] == 2 for s in sv))
    # never enough volume: no fill inside the horizon
    seq2 = [snap(0, [])] + [snap(300 * k, [{"price": 0.585, "size": 1.0}]) for k in range(1, 5)]
    sv2 = survival(seq2, horizon=2, offsets=(2.0,))
    check("never enough volume gives fill_window None", sv2 and sv2[0]["fill_window"] is None)
    # right-censoring guard: a start point without a full horizon is not observed
    check("a start point short of a full horizon is skipped",
          survival(seq, horizon=10, offsets=(2.0,)) == [])

    tbl = hazard_table([{"fill_window": 1, "window_sec": 300, "offset_cents": 2},
                        {"fill_window": 2, "window_sec": 300, "offset_cents": 2},
                        {"fill_window": None, "window_sec": 300, "offset_cents": 2},
                        {"fill_window": None, "window_sec": 300, "offset_cents": 2}], horizon=3)
    check("first-window hazard is 1/4", abs(tbl["table"][0]["hazard"] - 0.25) < 1e-9)
    check("the second window at_risk excludes those already filled", tbl["table"][1]["at_risk"] == 3)
    check("cumulative fill rate", abs(tbl["table"][1]["cum_fill_rate"] - 0.5) < 1e-9)
    check("ever-filled rate", abs(tbl["ever_fill_rate"] - 0.5) < 1e-9)

    aging = hazard_shape({"table": [{"hazard": h} for h in (.3, .3, .3, .05, .05, .05)]})
    check("a clearly falling hazard reads AGING", aging["shape"] == "AGING")
    flat = hazard_shape({"table": [{"hazard": .2} for _ in range(6)]})
    check("a constant hazard reads FLAT", flat["shape"] == "FLAT")

    # verdict: monotone strata plus a strong AUC
    def _mk(rates, aucs):
        return {"fill_naive": {
            "base_rate": .2,
            "by_offset": [{"offset_cents": i + 1, "fill_rate": r, "n": 10, "ci95": [0, 1]}
                          for i, r in enumerate(rates)],
            "auc": aucs,
            "auc_causal_only": {k: v for k, v in aucs.items() if k not in LEAKY_FEATURES}}}

    check("monotone plus strong discrimination is STRUCTURE_FOUND",
          verdict(_mk([.5, .3, .1], {"neg_offset": 0.75}))["fill_naive"]["verdict"] == "STRUCTURE_FOUND")
    check("non-monotone plus weak discrimination is NULL",
          verdict(_mk([.2, .3], {"neg_offset": 0.51}))["fill_naive"]["verdict"] == "NULL")
    # A leaky feature must never lift the verdict, however strong: this is the
    # easiest place in the module to fool yourself.
    leaked = verdict(_mk([.2, .3], {"neg_offset": 0.51, "trades_in_window": 0.99}))["fill_naive"]
    check("a leaky feature takes no part in the verdict", leaked["verdict"] == "NULL"
          and leaked["best_feature"] != "trades_in_window")

    print("selftest:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
    return 0 if not fails else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resting-order fill model (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--days", nargs="*", help="YYMMDD; default is everything collected")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.days:
        paths = [os.path.join(FEED_DIR, f"{d}.jsonl") for d in a.days]
    else:
        paths = sorted(os.path.join(FEED_DIR, f) for f in os.listdir(FEED_DIR)
                       if f.endswith(".jsonl"))
    summary = run(paths)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
