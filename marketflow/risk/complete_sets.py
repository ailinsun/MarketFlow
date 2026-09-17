#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Completeness gate for mutually exclusive market sets.

**This module exists to refuse one specific false signal.** A multi-candidate
election event priced off mid quotes showed a 3.70% underround — buy every leg for
less than the $1 the set must pay out. Checking real asks and real depth killed it:
of 34 legs, 27 were unquoted placeholders ("Party A" through "Party Z", "Other"),
and their mid was being read as zero. The 7 legs that did have offers carried $1 to
$237 of depth, with the thinnest at $1. The complete set could not actually be
bought, so the arbitrage did not exist.

Any scanner that reads an underround off mid prices will keep manufacturing that
signal. The rule this file enforces is: a set that is not demonstrably closed, and
not demonstrably buyable at a real offer, produces no candidate at all.

Three verdicts:
  INCOMPLETE  the set is not closed (an open-ended leg exists, or some leg has no
              offer, or a leg is too thin to buy) -> never a candidate
  NO_EDGE     the set is closed but real asks sum to >= 1 -> no arbitrage
  CANDIDATE   the set is closed and real asks sum to < 1, reported with the
              thinnest leg's depth, which is the hard ceiling on executable size

Boundaries: read-only public endpoints. It places no orders and touches no arm,
cap or wallet state. It emits shadow candidates only; acting on one is a separate,
explicitly approved decision.

    python3 -m marketflow.risk.complete_sets --scan
    python3 -m marketflow.risk.complete_sets --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

from marketflow.paths import runtime_path

OUT_DIR = runtime_path("risk", "complete_sets")
CAND_LOG = os.path.join(OUT_DIR, "candidates.jsonl")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "marketflow-complete-set/0.1 (read-only)"}

# A leg with one of these names means the set is open: the event admits an outcome
# that is not enumerated, so the legs on offer can never be a complete set.
OPEN_ENDED_RE = re.compile(r"^(other|another|someone else|field|tbd|n/?a)\b|^party [a-z]$", re.I)
MIN_LEG_DEPTH_USD = 20.0        # below this a leg cannot actually be bought


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts else time.time(), tz=timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, tries: int = 2):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.0)
    return None


def _plist(v):
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v) if isinstance(v, str) else []
        return out if isinstance(out, list) else []
    except ValueError:
        return []


# --------------------------------------------------------------------------- #
# pure functions — fully covered by the self-test, no network
# --------------------------------------------------------------------------- #
def leg_name(market: dict) -> str:
    return str(market.get("groupItemTitle") or market.get("question") or "").strip()


def is_open_ended(name: str) -> bool:
    return bool(OPEN_ENDED_RE.match(str(name or "").strip()))


def assess_complete_set(legs: list[dict]) -> dict:
    """Judge one mutually exclusive set. Pure function.

    Each leg is {name, ask, depth_usd}; an ask of None means no offer exists.
    """
    n = len(legs)
    if n < 2:
        return {"verdict": "INCOMPLETE", "reasons": ["too_few_legs"], "n_legs": n}
    reasons = []
    open_legs = [l["name"] for l in legs if is_open_ended(l["name"])]
    if open_legs:
        reasons.append(f"open_ended_legs:{len(open_legs)}")
    noquote = [l["name"] for l in legs if l.get("ask") is None]
    if noquote:
        # This is exactly what produced the false 3.70% underround.
        reasons.append(f"legs_without_quote:{len(noquote)}")
    thin = [l["name"] for l in legs
            if l.get("ask") is not None and float(l.get("depth_usd") or 0) < MIN_LEG_DEPTH_USD]
    if thin:
        reasons.append(f"legs_below_min_depth:{len(thin)}")
    if reasons:
        return {"verdict": "INCOMPLETE", "reasons": reasons, "n_legs": n,
                "n_no_quote": len(noquote), "n_open_ended": len(open_legs),
                "n_thin": len(thin)}
    ask_sum = sum(float(l["ask"]) for l in legs)
    min_depth = min(float(l.get("depth_usd") or 0) for l in legs)
    if ask_sum >= 1.0:
        return {"verdict": "NO_EDGE", "reasons": ["ask_sum_ge_1"], "n_legs": n,
                "ask_sum": round(ask_sum, 4)}
    return {
        "verdict": "CANDIDATE", "reasons": [], "n_legs": n,
        "ask_sum": round(ask_sum, 4),
        "gross_edge_pct": round((1.0 - ask_sum) * 100, 3),
        # The whole set can only be bought in the size of its thinnest leg, which is
        # the hard ceiling on executable size.
        "max_executable_usd": round(min_depth, 2),
        "min_leg_depth_usd": round(min_depth, 2),
    }


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def leg_quote(token_id: str) -> tuple[float | None, float]:
    """(best ask, dollars available at that price). No offer returns (None, 0)."""
    bk = _get(f"{CLOB}/book?token_id={token_id}")
    asks = (bk or {}).get("asks") or []
    if not asks:
        return None, 0.0
    best = min(asks, key=lambda a: float(a["price"]))
    p = float(best["price"])
    return p, float(best["size"]) * p


def scan(limit: int = 120, min_vol24: float = 50000.0) -> list[dict]:
    evs = _get(f"{GAMMA}/events?closed=false&limit={limit}&order=volume24hr&ascending=false") or []
    out, checked = [], 0
    for ev in evs:
        if not ev.get("negRisk"):
            continue
        if float(ev.get("volume24hr") or 0) < min_vol24:
            continue
        mks = ev.get("markets") or []
        if len(mks) < 2:
            continue
        checked += 1
        legs = []
        for m in mks:
            toks = _plist(m.get("clobTokenIds"))
            name = leg_name(m)
            if not toks:
                legs.append({"name": name, "ask": None, "depth_usd": 0.0})
                continue
            ask, depth = leg_quote(str(toks[0]))
            legs.append({"name": name, "ask": ask, "depth_usd": depth})
        a = assess_complete_set(legs)
        a.update({"as_of": iso(), "event": ev.get("title"), "slug": ev.get("slug"),
                  "vol24h": round(float(ev.get("volume24hr") or 0))})
        out.append(a)
        v = a["verdict"]
        mark = {"CANDIDATE": "★", "NO_EDGE": " ", "INCOMPLETE": "·"}[v]
        extra = (f"edge {a.get('gross_edge_pct')}% executable ${a.get('max_executable_usd')}"
                 if v == "CANDIDATE" else ",".join(a["reasons"])[:44])
        print(f" {mark} [{v:10}] {str(ev.get('title'))[:48]:48} {a['n_legs']:>3} legs  {extra}")
    cands = [a for a in out if a["verdict"] == "CANDIDATE"]
    print(f"\n[scanner] checked {checked} mutually exclusive events -> CANDIDATE {len(cands)} / "
          f"NO_EDGE {sum(1 for a in out if a['verdict']=='NO_EDGE')} / "
          f"INCOMPLETE {sum(1 for a in out if a['verdict']=='INCOMPLETE')}")
    if cands:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(CAND_LOG, "a", encoding="utf-8") as fh:
            for c in cands:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    return out


def selftest() -> int:
    ok = []

    def chk(n, c):
        ok.append((n, bool(c)))
        print(("  PASS  " if c else "  FAIL  ") + n)

    # The shape actually encountered: a few quoted legs beside many unquoted placeholders.
    russian = ([{"name": "United Russia", "ask": 0.700, "depth_usd": 2.0},
                {"name": "Communist Party", "ask": 0.025, "depth_usd": 25.0},
                {"name": "LDPR", "ask": 0.049, "depth_usd": 2.0}]
               + [{"name": f"Party {c}", "ask": None, "depth_usd": 0.0}
                  for c in "ABCDEFGHIJ"] + [{"name": "Other", "ask": None, "depth_usd": 0.0}])
    a = assess_complete_set(russian)
    chk("a false underround from unquoted legs is refused", a["verdict"] == "INCOMPLETE")
    chk("the count of unquoted legs is reported", a["n_no_quote"] == 11)
    chk("the count of open-ended legs is reported", a["n_open_ended"] >= 11)

    chk("an Other leg alone opens the set",
        assess_complete_set([{"name": "A", "ask": 0.4, "depth_usd": 500},
                             {"name": "Other", "ask": 0.5, "depth_usd": 500}])["verdict"] == "INCOMPLETE")
    chk("placeholder legs are recognised", is_open_ended("Party Z") and is_open_ended("Other")
        and not is_open_ended("United Russia"))

    thin = [{"name": "A", "ask": 0.40, "depth_usd": 5.0},
            {"name": "B", "ask": 0.45, "depth_usd": 900.0}]
    chk("a leg too thin to buy makes the set INCOMPLETE",
        assess_complete_set(thin)["verdict"] == "INCOMPLETE")

    noedge = [{"name": "A", "ask": 0.52, "depth_usd": 900.0},
              {"name": "B", "ask": 0.51, "depth_usd": 900.0}]
    chk("asks summing to at least one give NO_EDGE",
        assess_complete_set(noedge)["verdict"] == "NO_EDGE")

    good = [{"name": "A", "ask": 0.47, "depth_usd": 900.0},
            {"name": "B", "ask": 0.48, "depth_usd": 300.0}]
    g = assess_complete_set(good)
    chk("closed with asks below one gives CANDIDATE", g["verdict"] == "CANDIDATE")
    chk("gross edge arithmetic is right", abs(g["gross_edge_pct"] - 5.0) < 1e-6)
    chk("executable size is the thinnest leg", abs(g["max_executable_usd"] - 300.0) < 1e-6)
    chk("a single-leg event is never a candidate",
        assess_complete_set([{"name": "A", "ask": 0.4, "depth_usd": 900}])["verdict"] == "INCOMPLETE")

    print("\nselftest: " + ("ALL PASS" if all(c for _, c in ok)
                            else f"{sum(1 for _, c in ok if not c)} FAIL"))
    return 0 if all(c for _, c in ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
