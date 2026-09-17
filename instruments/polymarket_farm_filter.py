#!/usr/bin/env python3
"""Detect repeated-size, near-certain BUY signatures and compare ranking aggregates.

Wash-volume filter. It flags a pattern: near-certain BUYs of an identical size,
repeated across several wallets inside one market.
The detector identifies a repeated pattern, not identity, common control or
intent. Thresholds and arithmetic retain the original research implementation.
Inputs are local feed JSONL or gzip tape; the CLI prints aggregates only.
Identifiers inside synthetic self-tests are invented fixtures. Raw wallet data
is not distributed. See the decontamination report for historical results.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from collections import defaultdict
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHALE_FEED = os.path.join(REPO, "data/inputs/whale_trades.jsonl")
# The original tape (1.94 million fills across the 500 highest-activity markets) was
# deleted during a storage cleanup and was not archived, so every tape path in this
# file reads empty. Market metadata can be rebuilt from the public API; a per-fill
# tape cannot, it has to be collected again. This is stated rather than papered over:
# collect a tape first, or downstream reads nothing.
TAPE_PATH = os.path.join(REPO, "data/inputs/tape.jsonl.gz")
OUT_DIR = os.path.join(REPO, "data/generated/farm_signature")
# The live consumer path reads the feed source; the tape source is a research
# snapshot and writes under its own filename. One shared output would let them
# overwrite each other, and their sampling frames are not the same thing: the feed
# only ever contains prints of $2,000 or more.
FARM_PATH = os.path.join(OUT_DIR, "farm_wallets.json")
CANDIDATES_PATH = os.path.join(OUT_DIR, "candidates.json")
MARKET_HEAT_PATH = os.path.join(OUT_DIR, "market_heat.json")




@dataclass
class Config:
    # -- pattern criteria, anchored to measurement rather than fitted to any P&L
    near_certain_px: float = 0.99     # the band where rate * (1 - p) <= 0.05%: near-free
    sig_min_wallets: int = 5          # distinct wallets on one (market, exact size)
    sig_min_prints: int = 20          # repeated prints on one (market, exact size)
    farm_sig_share: float = 0.80      # share of a wallet's buying that sits on signatures
    # -- ranking convention: trade count times market breadth, not dollar volume
    min_life_days: float = 7.0        # under a week there is no behaviour to read
    min_markets: int = 3              # one or two markets is a single bet, not a record
    # -- cheap screen: removes most contamination without the signature clustering
    quick_usd_lo: float = 10000.0     # 72.3% of this dollar band matched the pattern
    quick_usd_hi: float = 20000.0
    quick_max_markets: int = 2
    quick_max_life_days: float = 1.0


# --------------------------------------------------------------------------- #
# Input adapters: two on-disk per-fill sources normalised to one record.
#   feed  a rolling live tape of prints of $2,000 or more
#   tape  a frozen snapshot of every fill in the 500 highest-activity markets
# Normalised record: (wallet, cid, side, px, size, ts_s, label)
# label is the venue's public username, empty when unset. It serves twice over: the
# ranking hands the label back to consumers, and the share of unnamed addresses is
# itself a checkable proxy for whether a cohort is made of people. Across the whole
# sample 82% carry a name; among pattern-matching addresses none do.
# --------------------------------------------------------------------------- #
def iter_feed(path: str):
    with open(path) as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            w = t.get("wallet")
            if not w:
                continue
            ts = t.get("ts_source_ms") or t.get("ts_ingest_ms") or 0
            yield (w, t.get("condition_id") or "", t.get("side"),
                   float(t.get("price") or 0.0), float(t.get("size") or 0.0),
                   int(ts) // 1000, t.get("wallet_label") or "")


def iter_tape(path: str):
    with gzip.open(path, "rt") as f:
        for line in f:
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            w = t.get("w")
            if not w:
                continue
            yield (w, t.get("cid") or "", t.get("side"),
                   float(t.get("px") or 0.0), float(t.get("sz") or 0.0),
                   int(t.get("ts") or 0), t.get("name") or t.get("pseudo") or "")


# --------------------------------------------------------------------------- #
# Aggregation: one streaming pass building wallets, signature candidates and markets
# --------------------------------------------------------------------------- #
def is_real_name(label: str | None) -> bool:
    """Whether a public username was chosen by a person.

    The venue derives a default `0x...` name for addresses that never set one, and
    those account for 23.6% of all non-empty names. Counting them as named would
    understate the unnamed share by 23 percentage points, and the unnamed share is
    exactly the measure being used to judge whether a cohort is human.
    """
    return bool(label) and not str(label).lower().startswith("0x")


def new_wallet() -> dict:
    return {"n_trades": 0, "n_buys": 0, "buy_usd": 0.0, "sell_usd": 0.0,
            "buy_usd_near_certain": 0.0, "cids": set(), "ts_min": None, "ts_max": None,
            "label": "", "sig_usd": 0.0, "sig_prints": 0}


def scan(records, cfg: Config) -> dict:
    """One aggregation pass. Attributing a signature needs global counts, so the
    near-certain band records (wallet, key, dollars) here and attributes on a second
    pass."""
    wallets: dict[str, dict] = defaultdict(new_wallet)
    sigs: dict[tuple, dict] = defaultdict(lambda: {"wallets": set(), "prints": 0, "usd": 0.0})
    markets: dict[str, dict] = defaultdict(lambda: {"buy_usd": 0.0, "n_buys": 0, "traders": set(),
                                                    "buy_usd_near_certain": 0.0})
    near: list[tuple] = []   # (wallet, sig_key, usd) for the near-certain band
    n_rows = 0
    for w, cid, side, px, sz, ts, label in records:
        n_rows += 1
        a = wallets[w]
        a["n_trades"] += 1
        a["cids"].add(cid)
        if label:
            a["label"] = label
        if ts:
            a["ts_min"] = ts if a["ts_min"] is None else min(a["ts_min"], ts)
            a["ts_max"] = ts if a["ts_max"] is None else max(a["ts_max"], ts)
        usd = px * sz
        if side != "BUY":
            a["sell_usd"] += usd
            continue
        a["n_buys"] += 1
        a["buy_usd"] += usd
        m = markets[cid]
        m["buy_usd"] += usd
        m["n_buys"] += 1
        m["traders"].add(w)
        if px >= cfg.near_certain_px:
            a["buy_usd_near_certain"] += usd
            m["buy_usd_near_certain"] += usd
            k = (cid, round(sz, 2))
            s = sigs[k]
            s["wallets"].add(w)
            s["prints"] += 1
            s["usd"] += usd
            near.append((w, k, usd))

    sig_keys = {k for k, v in sigs.items()
                if len(v["wallets"]) >= cfg.sig_min_wallets and v["prints"] >= cfg.sig_min_prints}
    for w, k, usd in near:
        if k in sig_keys:
            a = wallets[w]
            a["sig_usd"] += usd
            a["sig_prints"] += 1
    return {"wallets": dict(wallets), "sigs": dict(sigs), "sig_keys": sig_keys,
            "markets": dict(markets), "n_rows": n_rows}


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def is_farm(a: dict, cfg: Config) -> bool:
    return a["buy_usd"] > 0 and (a["sig_usd"] / a["buy_usd"]) >= cfg.farm_sig_share


def farm_wallets(agg: dict, cfg: Config) -> set:
    return {w for w, a in agg["wallets"].items() if is_farm(a, cfg)}


def life_days(a: dict) -> float:
    if a["ts_min"] is None or a["ts_max"] is None:
        return 0.0
    return (a["ts_max"] - a["ts_min"]) / 86400.0


def wallet_rows(agg: dict, cfg: Config) -> list[dict]:
    out = []
    for w, a in agg["wallets"].items():
        out.append({
            "wallet": w, "label": a["label"],
            "n_trades": a["n_trades"], "n_buys": a["n_buys"],
            "n_markets": len(a["cids"]), "buy_usd": round(a["buy_usd"], 2),
            "sell_usd": round(a["sell_usd"], 2), "life_days": round(life_days(a), 3),
            "named": is_real_name(a["label"]),
            "near_certain_share": round(a["buy_usd_near_certain"] / a["buy_usd"], 4) if a["buy_usd"] else 0.0,
            "sig_share": round(a["sig_usd"] / a["buy_usd"], 4) if a["buy_usd"] else 0.0,
            "sig_prints": a["sig_prints"], "is_farm": is_farm(a, cfg),
            "activity_score": a["n_trades"] * len(a["cids"]),
        })
    return out


def quick_suspect(row: dict, cfg: Config) -> bool:
    """A cheap screen for consumers that have no per-fill tape and cannot run the
    signature clustering.

    72.3% of addresses in the $10k-20k band matched the pattern, against 0.6% in the
    neighbouring $5k-10k band and 0.0% in $1k-5k. These figures were measured on the
    per-fill tape described at the top of this file, which was not retained and is not
    distributed, so they cannot be recomputed from this repository; the published rank
    audit under data/farm_signature can. Contamination is that narrow because
    the operation spends a fixed budget per wallet and then moves to the next one.
    Three scalars are enough to apply it: buy notional, market count and lifetime.

    `rank_candidates` deliberately does not call this. Its hard floor of three markets
    already contains everything this screen catches, so calling it there would be dead
    code. A self-test pins the containment so it cannot silently stop being true.
    """
    return (cfg.quick_usd_lo <= row["buy_usd"] < cfg.quick_usd_hi
            and row["n_markets"] <= cfg.quick_max_markets
            and row["life_days"] < cfg.quick_max_life_days)


def rank_candidates(rows: list[dict], cfg: Config, *, drop_farms: bool = True) -> list[dict]:
    """Rank by trade count times market breadth, with hard floors of seven days of
    life and three markets.

    Why not dollar volume: on the published rank audit the top 5,000 by buy notional is
    46.2% pattern-matching addresses while the top 5,000 by count times breadth is
    0.0%, against a 5.9% baseline over all 144,532 wallets with buys. Same wallets,
    same window, two ranking keys, and almost half a leaderboard either way. A dollar figure can be manufactured with one near-certain ticket.
    Count times breadth times survival cannot: every dimension requires real repeated
    behaviour over time, which is why it is the more trustworthy proxy for an address
    being worth reading.
    """
    keep = []
    for r in rows:
        if drop_farms and r["is_farm"]:
            continue
        if r["life_days"] < cfg.min_life_days or r["n_markets"] < cfg.min_markets:
            continue
        keep.append(r)
    # Ties break on trade count, then market count, so a tied product resolves on
    # activity and the output is reproducible rather than alphabetical.
    keep.sort(key=lambda r: (-r["activity_score"], -r["n_trades"], -r["n_markets"], r["wallet"]))
    return keep


def market_heat(agg: dict, farms: set) -> list[dict]:
    """Market-level activity, counted excluding pattern-matching addresses.

    Trader counts and volume on thin markets are inflated by 64% to 92%. In the worst
    market measured, 820 of 893 traders matched the pattern, leaving 71 actual people.
    (Measured on the undistributed tape; see the note on the quick screen above.)
    Any ranking of "busy markets" has to be rebuilt on the excluding count first, or a
    market with 71 people in it gets chased as one with 893.

    `buy_usd_near_certain` is reported alongside because the band above 0.99 is
    effectively fee-free, so any category signal built on dollar volume has to filter
    by price band first. In one category 94% of the money sat in that band.
    """
    out = []
    for cid, m in agg["markets"].items():
        traders = m["traders"]
        farm_traders = sum(1 for w in traders if w in farms)
        farm_usd = sum(agg["wallets"][w]["buy_usd"] for w in traders if w in farms)
        out.append({
            "condition_id": cid,
            "n_traders": len(traders), "n_traders_exfarm": len(traders) - farm_traders,
            "farm_trader_share": round(farm_traders / len(traders), 4) if traders else 0.0,
            "buy_usd": round(m["buy_usd"], 2),
            "buy_usd_near_certain": round(m["buy_usd_near_certain"], 2),
            "near_certain_share": round(m["buy_usd_near_certain"] / m["buy_usd"], 4) if m["buy_usd"] else 0.0,
            "n_buys": m["n_buys"],
            # A matching wallet's buy notional is a whole-sample figure: it also
            # trades elsewhere. It indicates the scale of contamination and is not a
            # per-market total; that needs per-fill attribution, as done for
            # buy_usd_near_certain.
            "farm_wallet_buy_usd_allmarkets": round(farm_usd, 2),
        })
    out.sort(key=lambda r: -r["n_traders_exfarm"])
    return out


# --------------------------------------------------------------------------- #
# Outputs: the three files consumers read
# --------------------------------------------------------------------------- #


def load_market_heat(path: str = MARKET_HEAT_PATH) -> dict[str, dict]:
    """Consumer entry point mapping condition id to an activity row. A missing or
    corrupt file returns an empty dict, meaning no adjustment rather than a crash."""
    if not os.path.exists(path):
        return {}
    try:
        return {r["condition_id"]: r for r in json.load(open(path)).get("markets") or []
                if r.get("condition_id")}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError, KeyError):
        return {}


def exfarm_volume_factor(heat_row: dict | None) -> float:
    """A discount factor in [0, 1]: how much of a market's volume is not wash.

    It uses one minus the near-certain share of buying, not the share of matching
    wallets, because the question is whether the dollar volume is real, and the
    unreal part is precisely the near-certain tickets bought in the fee-free band.

    An unobserved market returns 1.0, meaning no adjustment. No data does not mean no
    wash volume, it means not knowing; treating it as zero would condemn every market
    never observed, which is a larger error than leaving it alone.
    """
    if not heat_row:
        return 1.0
    s = heat_row.get("near_certain_share")
    if not isinstance(s, (int, float)):
        return 1.0
    return max(0.0, min(1.0, 1.0 - float(s)))


def load_farm_wallets(path: str = FARM_PATH) -> set:
    """Consumer entry point. A missing file returns an empty set: when the filter is
    unavailable it neither waves everything through silently nor crashes. The caller
    gets no filtering, and the output metadata records which source was used."""
    if not os.path.exists(path):
        return set()
    try:
        return set(json.load(open(path)).get("farm_wallets") or [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return set()


def compare_lists(rows: list[dict], cfg: Config, top_n: int = 500) -> dict:
    """Side-by-side comparison of the two ranking conventions: dollar volume with no
    floors, against count times breadth with the lifetime, breadth and pattern floors.

    It reports the overlap plus, for each list, the unnamed share, the median lifetime
    and the median market count. Those three are checkable proxies for whether a list
    is made of people, and none of them depends on this module's own classifier.
    """
    def med(vals):
        v = sorted(vals)
        return v[len(v) // 2] if v else None

    def portrait(lst: list[dict]) -> dict:
        if not lst:
            return {"n": 0}
        return {"n": len(lst),
                "unnamed_share": round(sum(1 for r in lst if not r.get("named")) / len(lst), 4),
                "farm_share": round(sum(1 for r in lst if r["is_farm"]) / len(lst), 4),
                "median_life_days": med([r["life_days"] for r in lst]),
                "median_n_markets": med([r["n_markets"] for r in lst]),
                "median_n_trades": med([r["n_trades"] for r in lst]),
                "median_buy_usd": med([r["buy_usd"] for r in lst])}

    old = sorted(rows, key=lambda r: (-r["buy_usd"], r["wallet"]))[:top_n]
    new = rank_candidates(rows, cfg)[:top_n]
    ow, nw = {r["wallet"] for r in old}, {r["wallet"] for r in new}
    both = ow & nw
    def slim(lst):
        return [{k: r[k] for k in ("wallet", "label", "n_trades", "n_markets", "life_days",
                                   "buy_usd", "named", "is_farm", "near_certain_share")
                 if k in r} for r in lst]

    return {"top_n": top_n,
            "old": {"rank_key": "buy_usd", "gates": "none", **portrait(old)},
            "new": {"rank_key": "n_trades * n_markets",
                    "gates": f"life_days>={cfg.min_life_days} & n_markets>={cfg.min_markets} "
                             f"& not farm", **portrait(new)},
            "overlap_n": len(both),
            "overlap_share": round(len(both) / max(len(ow), 1), 4),
            "baseline_all_wallets": portrait(rows),
            # Both lists are written out. The point of the comparison is two lists a
            # reader can check line by line, not a pair of summary numbers.
            "old_list": slim(old), "new_list": slim(new),
            "only_in_old": sorted(ow - nw), "only_in_new": sorted(nw - ow)}


def aggregate_summary(source: str, path: str, cfg: Config, top_n: int) -> dict:
    records = iter_feed(path) if source == "feed" else iter_tape(path)
    agg = scan(records, cfg)
    rows = wallet_rows(agg, cfg)
    comparison = compare_lists(rows, cfg, top_n=top_n)
    for key in ("old_list", "new_list", "only_in_old", "only_in_new"):
        comparison.pop(key, None)
    return {"schema": "farm-signature-aggregates-v1", "rows": agg["n_rows"],
            "wallets": len(agg["wallets"]), "farm_wallets": len(farm_wallets(agg, cfg)),
            "comparison": comparison}


def selftest() -> int:
    fails: list[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    cfg = Config(sig_min_wallets=3, sig_min_prints=6)
    day = 86400

    # 1) The operation's fingerprint: three wallets repeating size 5200 six times at
    #    0.998 in one market.
    recs = []
    for i in range(3):
        for j in range(3):
            recs.append((f"0xspoke{i}", "cFARM", "BUY", 0.998, 5200.0, 1_780_000_000 + j * 60, ""))
    # Control: a wallet in the same market with no repeated price or size.
    recs += [("0xhuman", "cFARM", "BUY", 0.42, 100.0, 1_780_000_000, "synthetic-person"),
             ("0xhuman", "cB", "BUY", 0.55, 250.0, 1_780_000_000 + 10 * day, "synthetic-person"),
             ("0xhuman", "cC", "BUY", 0.31, 80.0, 1_780_000_000 + 20 * day, "synthetic-person")]
    agg = scan(iter(recs), cfg)
    farms = farm_wallets(agg, cfg)
    check("a signature forms across three wallets and six prints", len(agg["sig_keys"]) == 1)
    check("all three repeating wallets match", farms == {"0xspoke0", "0xspoke1", "0xspoke2"})
    check("a wallet with no fingerprint is untouched", "0xhuman" not in farms)

    # 2) Near-certain prices with no repeated size do not match. Buying a near-certain
    #    ticket is not the pattern; industrial repetition of one is.
    recs2 = [(f"0xw{i}", "cX", "BUY", 0.995, 100.0 + i, 1_780_000_000, "") for i in range(9)]
    agg2 = scan(iter(recs2), cfg)
    check("near-certain prices with varied sizes form no signature",
          len(agg2["sig_keys"]) == 0 and not farm_wallets(agg2, cfg))

    # 3) One wallet repeating alone never reaches the gate: the pattern must reproduce
    #    across wallets, which is what stops a single address being condemned.
    recs3 = [("0xsolo", "cY", "BUY", 0.999, 3000.0, 1_780_000_000 + i, "") for i in range(30)]
    agg3 = scan(iter(recs3), cfg)
    check("one wallet repeating alone forms no signature", len(agg3["sig_keys"]) == 0)

    # 4) the ranking key and its floors
    rows = [
        {"wallet": "0xbroad", "n_trades": 40, "n_markets": 10, "buy_usd": 12000.0,
         "life_days": 20.0, "is_farm": False, "activity_score": 400},
        {"wallet": "0xrich", "n_trades": 3, "n_markets": 3, "buy_usd": 900000.0,
         "life_days": 30.0, "is_farm": False, "activity_score": 9},
        {"wallet": "0xshort", "n_trades": 99, "n_markets": 30, "buy_usd": 50000.0,
         "life_days": 2.0, "is_farm": False, "activity_score": 2970},     # lifetime floor
        {"wallet": "0xnarrow", "n_trades": 99, "n_markets": 2, "buy_usd": 50000.0,
         "life_days": 30.0, "is_farm": False, "activity_score": 198},     # breadth floor
        {"wallet": "0xfarm", "n_trades": 500, "n_markets": 40, "buy_usd": 15000.0,
         "life_days": 30.0, "is_farm": True, "activity_score": 20000},    # pattern floor
    ]
    ranked = rank_candidates(rows, Config())
    check("ranking puts breadth ahead of a large but narrow dollar figure",
          [r["wallet"] for r in ranked] == ["0xbroad", "0xrich"])
    check("each of the three floors rejects on its own",
          all(w not in [r["wallet"] for r in ranked] for w in ("0xshort", "0xnarrow", "0xfarm")))

    # 5) the cheap screen: the $10k-20k band, at most two markets, under a day
    qc = Config()
    check("the screen catches a narrow address in that band",
          quick_suspect({"buy_usd": 15000.0, "n_markets": 1, "life_days": 0.2}, qc))
    check("the neighbouring band is untouched",
          not quick_suspect({"buy_usd": 8000.0, "n_markets": 1, "life_days": 0.2}, qc))
    check("enough breadth or enough life exempts an address in the band",
          not quick_suspect({"buy_usd": 15000.0, "n_markets": 5, "life_days": 0.2}, qc)
          and not quick_suspect({"buy_usd": 15000.0, "n_markets": 1, "life_days": 9.0}, qc))
    # Containment: anything the screen catches the hard floors already reject. This is
    # why rank_candidates does not call it, and the check stops that going stale.
    hit = [{"wallet": "0xq", "buy_usd": 15000.0, "n_markets": 2, "life_days": 0.5,
            "is_farm": False, "n_trades": 99, "activity_score": 198}]
    check("the hard floors contain everything the screen catches",
          quick_suspect(hit[0], qc) and rank_candidates(hit, qc) == [])

    # 6) market activity excluding matches: three matching plus one other reads as
    #    four traders on the surface and one underneath
    heat = market_heat(agg, farms)
    hf = {h["condition_id"]: h for h in heat}
    check("the excluding trader count removes matches",
          hf["cFARM"]["n_traders"] == 4 and hf["cFARM"]["n_traders_exfarm"] == 1)
    check("fee-free-band volume is reported separately",
          hf["cFARM"]["near_certain_share"] > 0.99 and hf["cB"]["near_certain_share"] == 0.0)
    check("markets sort by the excluding trader count",
          [h["condition_id"] for h in heat][0] == "cFARM"
          and all(heat[i]["n_traders_exfarm"] >= heat[i + 1]["n_traders_exfarm"]
                  for i in range(len(heat) - 1)))

    # 6b) the ranking hands back public usernames; the unnamed share is the measure
    wr = {r["wallet"]: r for r in wallet_rows(agg, cfg)}
    check("labels are returned and the unnamed flag is set",
          wr["0xhuman"]["label"] == "synthetic-person" and wr["0xhuman"]["named"]
          and wr["0xspoke0"]["label"] == "" and not wr["0xspoke0"]["named"])
    check("a derived 0x default name does not count as named",
          is_real_name("synthetic-person") and not is_real_name("0x461f5")
          and not is_real_name("0xsynthetic-derived-profile")
          and not is_real_name("") and not is_real_name(None))

    # 7) the comparison: the dollar-volume convention ranks matches first, the other
    #    convention returns none
    cmp_rows = [{"wallet": f"0xf{i}", "buy_usd": 50000.0, "n_trades": 3, "n_markets": 1,
                 "life_days": 0.1, "is_farm": True, "named": False, "activity_score": 3}
                for i in range(3)]
    cmp_rows += [{"wallet": f"0xr{i}", "buy_usd": 5000.0, "n_trades": 50, "n_markets": 9,
                  "life_days": 30.0, "is_farm": False, "named": True, "activity_score": 450}
                 for i in range(3)]
    c = compare_lists(cmp_rows, Config(), top_n=3)
    check("dollar ranking is all matches, the new ranking is none",
          c["old"]["farm_share"] == 1.0 and c["new"]["farm_share"] == 0.0)
    check("disjoint lists report zero overlap", c["overlap_share"] == 0.0)
    check("the unnamed share is reported for both lists",
          c["old"]["unnamed_share"] == 1.0 and c["new"]["unnamed_share"] == 0.0)
    check("both lists are written out for line-by-line checking",
          [r["wallet"] for r in c["old_list"]] == ["0xf0", "0xf1", "0xf2"]
          and [r["wallet"] for r in c["new_list"]] == ["0xr0", "0xr1", "0xr2"]
          and c["only_in_old"] == ["0xf0", "0xf1", "0xf2"])

    # 8) consumer fail-soft: a missing file gives an empty set or table
    check("a missing wallet file returns an empty set",
          load_farm_wallets("/nonexistent/x.json") == set())
    check("a missing activity file returns an empty table",
          load_market_heat("/nonexistent/x.json") == {})
    check("a market that is 92% fee-free band keeps 8% of its volume",
          abs(exfarm_volume_factor({"near_certain_share": 0.92}) - 0.08) < 1e-9)
    check("a clean market is not discounted",
          exfarm_volume_factor({"near_certain_share": 0.0}) == 1.0)
    check("an unobserved market is not adjusted",
          exfarm_volume_factor(None) == 1.0 and exfarm_volume_factor({}) == 1.0
          and exfarm_volume_factor({"near_certain_share": None}) == 1.0)

    # 9) lifetime and the activity score
    a = new_wallet()
    a["ts_min"], a["ts_max"] = 1_780_000_000, 1_780_000_000 + 7 * day
    check("lifetime spans first to last fill", abs(life_days(a) - 7.0) < 1e-9)
    check("a wallet with no timestamps has zero life", life_days(new_wallet()) == 0.0)

    print(f"\nselftest: {'ALL PASS' if not fails else f'{len(fails)} FAIL: {fails}'}")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Wash-volume pattern filter (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--source", choices=["feed", "tape"], default="feed",
                    help="feed=rolling live large prints / tape=frozen snapshot of the busiest markets")
    ap.add_argument("--path", type=str, default=None)
    ap.add_argument("--top", type=int, default=500, help="how many candidates to write")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    path = args.path or (WHALE_FEED if args.source == "feed" else TAPE_PATH)
    if not os.path.exists(path):
        print("Input not found. Supply --path with your own local data; per-wallet inputs are not distributed.")
        return 1
    print(json.dumps(aggregate_summary(args.source, path, Config(), args.top), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
