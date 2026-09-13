#!/usr/bin/env python3
"""Detect repeated-size, near-certain BUY signatures and compare ranking aggregates.

刷量农场过滤器：同一市场、相同数量的近确定价 BUY 跨多个钱包重复出现时标注模式。
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
# ⚠ 2026-08-05: 这份 tape (194 万笔逐笔, 鲸鱼密集 top-500 市场) 已在 referenced section 的 13.75 GB 清理里
# **删除且无归档**, 本文件的 tape 相关路径因此跑不出结果。不像 meta 可以从 Gamma 重建 ——
# 逐笔要重新采集才有。**照实标注不假装可用**: 要用先重采 tape, 否则下游读到的是空。
TAPE_PATH = os.path.join(REPO, "data/inputs/tape.jsonl.gz")
OUT_DIR = os.path.join(REPO, "data/generated/farm_signature")
# live 消费路径 (profiler 名单口径读它) = feed 源; tape 源是快照研究口径, 另落文件名 —
# 两个源共写一份会互相覆盖, 而它们的样本框根本不同 (feed 只有 ≥$2,000 的打印)。
FARM_PATH = os.path.join(OUT_DIR, "farm_wallets.json")
CANDIDATES_PATH = os.path.join(OUT_DIR, "candidates.json")
MARKET_HEAT_PATH = os.path.join(OUT_DIR, "market_heat.json")




@dataclass
class Config:
    # -- 农场判据 (锚 260803 实测, 非拟合任何 PnL)
    near_certain_px: float = 0.99     # 近确定价档: 费率 rate·(1−p) ≤ 0.05% 的零成本区
    sig_min_wallets: int = 5          # 同一 (市场, 精确 size) 的不同钱包数下界
    sig_min_prints: int = 20          # 同一 (市场, 精确 size) 的重复笔数下界
    farm_sig_share: float = 0.80      # 买入额落在签名上的占比 ≥ 此值 = 农场钱包
    # -- B1 名单口径 (笔数 × 市场广度, 取代按成交额)
    min_life_days: float = 7.0        # 硬门槛: 存活 < 7 天的"鲸鱼"没有可跟的行为历史
    min_markets: int = 3              # 硬门槛: 只碰 1-2 个市场 = 单事件投机或刷量
    # -- 零成本快筛 (不跑签名聚类也能砍掉绝大部分污染)
    quick_usd_lo: float = 10000.0     # 该美元档 72.3% 是农场
    quick_usd_hi: float = 20000.0
    quick_max_markets: int = 2
    quick_max_life_days: float = 1.0


# --------------------------------------------------------------------------- #
# 取数适配 — 两种已落盘的逐笔源归一到同一条记录
#   feed = whale_trades.jsonl (单笔 ≥$2,000 的大额打印, live 滚动)
#   tape = whale_report_260726/tape.jsonl.gz (鲸鱼密集 top-500 市场的全量逐笔, 快照)
# 归一记录: (wallet, cid, side, px, size, ts_s, label)
# label = Polymarket 公开用户名, 空串 = unnamed。它同时当两用: 名单要带回标签给消费层,
# 而 unnamed 率本身是「这批地址是不是真人」的可核验代理 (全样本 82% 有名字, 农场 0%)。
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
# 聚合 — 一趟流式, 同时攒 钱包 / 签名候选 / 市场
# --------------------------------------------------------------------------- #
def is_real_name(label: str | None) -> bool:
    """公开用户名是否是**人取的**。Polymarket 给没设名字的地址派生 `0x…` 形式的默认名,
    实测占全部非空 name/pseudo 的 **23.6%** —— 把它算成「有用户名」会让 unnamed 率整体
    偏低 23 个百分点, 而 unnamed 率正是判「这批地址是不是真人」的验收指标。
    口径与 `whale_report_dataset.scan_contact_fields` 一致 (那里已按此排除)。"""
    return bool(label) and not str(label).lower().startswith("0x")


def new_wallet() -> dict:
    return {"n_trades": 0, "n_buys": 0, "buy_usd": 0.0, "sell_usd": 0.0,
            "buy_usd_near_certain": 0.0, "cids": set(), "ts_min": None, "ts_max": None,
            "label": "", "sig_usd": 0.0, "sig_prints": 0}


def scan(records, cfg: Config) -> dict:
    """一趟聚合。签名归属需要全局计数 ⟹ 近确定档的 (钱包, 键, 金额) 先留痕, 第二趟归因。"""
    wallets: dict[str, dict] = defaultdict(new_wallet)
    sigs: dict[tuple, dict] = defaultdict(lambda: {"wallets": set(), "prints": 0, "usd": 0.0})
    markets: dict[str, dict] = defaultdict(lambda: {"buy_usd": 0.0, "n_buys": 0, "traders": set(),
                                                    "buy_usd_near_certain": 0.0})
    near: list[tuple] = []   # (wallet, sig_key, usd) — 近确定档买入的留痕
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
# 判定
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
    """零成本快筛 — **给拿不到逐笔流水、跑不了签名聚类的消费方用的**独立入口。

    该美元档 (**$10k–20k**) 实测 72.3% 是农场, 而相邻的 $5k–10k 档只有 0.6%、$1k–5k 档 0.0%。
    污染窄到这个程度, 是因为流水线按固定预算铺钱包: 每个钱包刷到十几 k 就换下一个。
    只要有 (买入额, 市场数, 寿命) 三个标量就能用, 一行判完。

    注意 `rank_candidates` **不调用它** —— 那里的硬门槛 `n_markets ≥ 3` 已经把本函数命中的
    (`n_markets ≤ 2`) 全部包住了, 再调一次是死代码。两条判据的关系由 selftest 钉住。"""
    return (cfg.quick_usd_lo <= row["buy_usd"] < cfg.quick_usd_hi
            and row["n_markets"] <= cfg.quick_max_markets
            and row["life_days"] < cfg.quick_max_life_days)


def rank_candidates(rows: list[dict], cfg: Config, *, drop_farms: bool = True) -> list[dict]:
    """B1 口径: 排序键 `n_trades × n_markets`, 硬门槛 life_days≥7 且 n_markets≥3。

    为什么不是 buy_usd: 按 `buy_usd` 取前 5000 有 51.9% 是农场, 按 `n_trades` 取前 5000
    只有 0.1%, 两者仅重叠 19.6% —— 同一个池子, 两种排序键的污染率差 500 倍。
    金额可以用一笔必胜票凭空造出来, 笔数 × 广度 × 存活天数造不出来 (每一维都要真实的
    重复行为), 所以后者才是「这个地址值不值得看」的可信代理。"""
    keep = []
    for r in rows:
        if drop_farms and r["is_farm"]:
            continue
        if r["life_days"] < cfg.min_life_days or r["n_markets"] < cfg.min_markets:
            continue
        keep.append(r)
    # 同分再按笔数、再按市场数 — 让「活跃度」在乘积打平时占先, 结果可复现不靠字典序
    keep.sort(key=lambda r: (-r["activity_score"], -r["n_trades"], -r["n_markets"], r["wallet"]))
    return keep


def market_heat(agg: dict, farms: set) -> list[dict]:
    """B2: 市场级热度的 ex-farm 口径。

    冷门盘的 `n_traders` / `volume` 被农场虚高 64–92% (实测最脏的一个市场 893 个 traders
    里 820 个是农场 → 真实 71 个人)。**做「热门市场发现」前必须按 ex-farm trader 数重排**,
    否则会把 71 个真人的盘当成 893 人的热门盘去追。
    `buy_usd_near_certain` 一并给出: p≥0.99 区是零费区, 任何基于成交额的**品类**信号
    必须先按价格带过滤 (politics 94% 的钱在这个区)。"""
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
            # 农场钱包的买入额是**全样本**口径 (该钱包在别的市场也刷), 只作污染量级参考,
            # 不当"本市场农场成交额"用 —— 精确到市场需按笔归因, 见 buy_usd_near_certain
            "farm_wallet_buy_usd_allmarkets": round(farm_usd, 2),
        })
    out.sort(key=lambda r: -r["n_traders_exfarm"])
    return out


# --------------------------------------------------------------------------- #
# 落盘 — 消费方 (profiler 名单口径 / 市场发现) 读这三份
# --------------------------------------------------------------------------- #


def load_market_heat(path: str = MARKET_HEAT_PATH) -> dict[str, dict]:
    """消费侧入口 — {condition_id: 热度行}。文件缺失/损坏返回空 dict = 不调整 (fail-soft)。"""
    if not os.path.exists(path):
        return {}
    try:
        return {r["condition_id"]: r for r in json.load(open(path)).get("markets") or []
                if r.get("condition_id")}
    except (json.JSONDecodeError, OSError, AttributeError, TypeError, KeyError):
        return {}


def exfarm_volume_factor(heat_row: dict | None) -> float:
    """成交额的去污染折减系数 ∈ [0,1] — 「这个市场的成交额里有多少不是刷出来的」。

    用 `1 − near_certain_share` (p≥0.99 区的买入额占比) 而不是农场钱包数占比:
    热度问的是**成交额**虚不虚, 而虚的那部分正是零费区那些必胜票。
    没有该市场的观测 ⟹ 返 1.0 (不调整) —— **没数据不等于没农场**, 只是不知道,
    这时把它当 0 会把所有没观测过的市场一刀打死, 比不调整错得更狠。"""
    if not heat_row:
        return 1.0
    s = heat_row.get("near_certain_share")
    if not isinstance(s, (int, float)):
        return 1.0
    return max(0.0, min(1.0, 1.0 - float(s)))


def load_farm_wallets(path: str = FARM_PATH) -> set:
    """消费侧入口 — 文件缺失返回空集 (过滤器不可用时**不静默放行也不硬崩**:
    调用方拿到空集就等于没过滤, 但 candidates.json 的 meta 里会写明 source)。"""
    if not os.path.exists(path):
        return set()
    try:
        return set(json.load(open(path)).get("farm_wallets") or [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return set()


def compare_lists(rows: list[dict], cfg: Config, top_n: int = 500) -> dict:
    """验收对照: 旧口径 (按 buy_usd 降序, 无闸) vs 新口径 (笔数×广度 + 寿命/广度闸 + 剔农场)。

    报重叠率与两份名单的 unnamed 率 / 中位 life_days / 中位 n_markets —— 这三个量是
    「名单里是不是真人」的可核验代理, 不依赖任何我们自己的判型器。"""
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
            # 两份名单本体都落盘 — 验收要的是「各出一份」可逐条核对, 不是只看汇总量
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

    # 1) 流水线指纹: 3 个 spoke 钱包在同一市场把 size=5200 重复打 6 笔 @0.998 → 全判农场
    recs = []
    for i in range(3):
        for j in range(3):
            recs.append((f"0xspoke{i}", "cFARM", "BUY", 0.998, 5200.0, 1_780_000_000 + j * 60, ""))
    # 真人对照: 同一市场买了但价位与成交量都不重复
    recs += [("0xhuman", "cFARM", "BUY", 0.42, 100.0, 1_780_000_000, "synthetic-person"),
             ("0xhuman", "cB", "BUY", 0.55, 250.0, 1_780_000_000 + 10 * day, "synthetic-person"),
             ("0xhuman", "cC", "BUY", 0.31, 80.0, 1_780_000_000 + 20 * day, "synthetic-person")]
    agg = scan(iter(recs), cfg)
    farms = farm_wallets(agg, cfg)
    check("签名: (市场,精确size) 跨 3 钱包 6 笔成立", len(agg["sig_keys"]) == 1)
    check("农场: 3 个 spoke 全中", farms == {"0xspoke0", "0xspoke1", "0xspoke2"})
    check("真人不被误伤 (同市场但无指纹)", "0xhuman" not in farms)

    # 2) 近确定价但**不重复成交量** → 不判农场 (买贵票本身不是罪, 是流水线才是)
    recs2 = [(f"0xw{i}", "cX", "BUY", 0.995, 100.0 + i, 1_780_000_000, "") for i in range(9)]
    agg2 = scan(iter(recs2), cfg)
    check("近确定价但成交量各不相同 → 零签名", len(agg2["sig_keys"]) == 0 and not farm_wallets(agg2, cfg))

    # 3) 单钱包自刷够不到签名闸 (钱包数 < gate) —— 要求跨钱包复现, 防单点误判
    recs3 = [("0xsolo", "cY", "BUY", 0.999, 3000.0, 1_780_000_000 + i, "") for i in range(30)]
    agg3 = scan(iter(recs3), cfg)
    check("单钱包重复不成签名 (需跨钱包复现)", len(agg3["sig_keys"]) == 0)

    # 4) B1 排序键与门槛
    rows = [
        {"wallet": "0xbroad", "n_trades": 40, "n_markets": 10, "buy_usd": 12000.0,
         "life_days": 20.0, "is_farm": False, "activity_score": 400},
        {"wallet": "0xrich", "n_trades": 3, "n_markets": 3, "buy_usd": 900000.0,
         "life_days": 30.0, "is_farm": False, "activity_score": 9},
        {"wallet": "0xshort", "n_trades": 99, "n_markets": 30, "buy_usd": 50000.0,
         "life_days": 2.0, "is_farm": False, "activity_score": 2970},     # 寿命闸砍
        {"wallet": "0xnarrow", "n_trades": 99, "n_markets": 2, "buy_usd": 50000.0,
         "life_days": 30.0, "is_farm": False, "activity_score": 198},     # 广度闸砍
        {"wallet": "0xfarm", "n_trades": 500, "n_markets": 40, "buy_usd": 15000.0,
         "life_days": 30.0, "is_farm": True, "activity_score": 20000},    # 农场闸砍
    ]
    ranked = rank_candidates(rows, Config())
    check("B1: 按 n_trades×n_markets 排序, 巨额小笔数排在广度型之后",
          [r["wallet"] for r in ranked] == ["0xbroad", "0xrich"])
    check("B1: life_days<7 / n_markets<3 / 农场 三闸各自生效",
          all(w not in [r["wallet"] for r in ranked] for w in ("0xshort", "0xnarrow", "0xfarm")))

    # 5) 零成本快筛: $10k–20k 且 ≤2 市场 且 <1 天
    qc = Config()
    check("快筛: 命中该美元档的窄寿命窄广度地址",
          quick_suspect({"buy_usd": 15000.0, "n_markets": 1, "life_days": 0.2}, qc))
    check("快筛: 相邻美元档 (0.6% 污染) 不误伤",
          not quick_suspect({"buy_usd": 8000.0, "n_markets": 1, "life_days": 0.2}, qc))
    check("快筛: 同档但广度够 / 寿命够 的不误伤",
          not quick_suspect({"buy_usd": 15000.0, "n_markets": 5, "life_days": 0.2}, qc)
          and not quick_suspect({"buy_usd": 15000.0, "n_markets": 1, "life_days": 9.0}, qc))
    # 两条判据的包含关系 (快筛命中 ⟹ 硬门槛必砍) —— 这是 rank_candidates 不再调用它的依据
    hit = [{"wallet": "0xq", "buy_usd": 15000.0, "n_markets": 2, "life_days": 0.5,
            "is_farm": False, "n_trades": 99, "activity_score": 198}]
    check("快筛命中的地址被硬门槛完全包住 (所以 rank_candidates 不重复调, 非漏判)",
          quick_suspect(hit[0], qc) and rank_candidates(hit, qc) == [])

    # 6) 市场热度 ex-farm: 3 农场 + 1 真人 → 表面 4 人, 真实 1 人
    heat = market_heat(agg, farms)
    hf = {h["condition_id"]: h for h in heat}
    check("B2: ex-farm trader 数剔掉农场",
          hf["cFARM"]["n_traders"] == 4 and hf["cFARM"]["n_traders_exfarm"] == 1)
    check("B2: 零费区 (p≥0.99) 成交额单列, 供品类信号按价格带过滤",
          hf["cFARM"]["near_certain_share"] > 0.99 and hf["cB"]["near_certain_share"] == 0.0)
    check("B2: 按 ex-farm trader 数降序 (热门发现的正确排序)",
          [h["condition_id"] for h in heat][0] == "cFARM"
          and all(heat[i]["n_traders_exfarm"] >= heat[i + 1]["n_traders_exfarm"]
                  for i in range(len(heat) - 1)))

    # 6b) 名单带回公开用户名 (消费层要标签, unnamed 率是验收指标)
    wr = {r["wallet"]: r for r in wallet_rows(agg, cfg)}
    check("label 回传 + unnamed 判定",
          wr["0xhuman"]["label"] == "synthetic-person" and wr["0xhuman"]["named"]
          and wr["0xspoke0"]["label"] == "" and not wr["0xspoke0"]["named"])
    check("0x 派生的默认名不算「有用户名」(占非空名的 23.6%)",
          is_real_name("synthetic-person") and not is_real_name("0x461f5")
          and not is_real_name("0xsynthetic-derived-profile")
          and not is_real_name("") and not is_real_name(None))

    # 7) 验收对照器: 旧口径把农场排在前面, 新口径清零
    cmp_rows = [{"wallet": f"0xf{i}", "buy_usd": 50000.0, "n_trades": 3, "n_markets": 1,
                 "life_days": 0.1, "is_farm": True, "named": False, "activity_score": 3}
                for i in range(3)]
    cmp_rows += [{"wallet": f"0xr{i}", "buy_usd": 5000.0, "n_trades": 50, "n_markets": 9,
                  "life_days": 30.0, "is_farm": False, "named": True, "activity_score": 450}
                 for i in range(3)]
    c = compare_lists(cmp_rows, Config(), top_n=3)
    check("对照: 旧口径 (按金额) 100% 农场 → 新口径 0%",
          c["old"]["farm_share"] == 1.0 and c["new"]["farm_share"] == 0.0)
    check("对照: 两份零重叠时 overlap_share=0", c["overlap_share"] == 0.0)
    check("对照: unnamed 率两侧都报出", c["old"]["unnamed_share"] == 1.0 and c["new"]["unnamed_share"] == 0.0)
    check("对照: 两份名单本体都落盘可逐条核对",
          [r["wallet"] for r in c["old_list"]] == ["0xf0", "0xf1", "0xf2"]
          and [r["wallet"] for r in c["new_list"]] == ["0xr0", "0xr1", "0xr2"]
          and c["only_in_old"] == ["0xf0", "0xf1", "0xf2"])

    # 8) 消费侧 fail-soft: 文件缺失 → 空集/空表 (不静默放行也不硬崩)
    check("load_farm_wallets 缺文件返回空集", load_farm_wallets("/nonexistent/x.json") == set())
    check("load_market_heat 缺文件返回空表", load_market_heat("/nonexistent/x.json") == {})
    check("热度折减: 零费区占 92% 的盘, 成交额只认 8%",
          abs(exfarm_volume_factor({"near_certain_share": 0.92}) - 0.08) < 1e-9)
    check("热度折减: 干净盘不打折", exfarm_volume_factor({"near_certain_share": 0.0}) == 1.0)
    check("热度折减: 没观测过的市场返 1.0 不调整 (没数据 ≠ 没农场, 但也不能一刀打死)",
          exfarm_volume_factor(None) == 1.0 and exfarm_volume_factor({}) == 1.0
          and exfarm_volume_factor({"near_certain_share": None}) == 1.0)

    # 9) life_days 与活跃度分数
    a = new_wallet()
    a["ts_min"], a["ts_max"] = 1_780_000_000, 1_780_000_000 + 7 * day
    check("life_days 按首末成交时间跨度", abs(life_days(a) - 7.0) < 1e-9)
    check("无时间戳的钱包 life=0 (不外推)", life_days(new_wallet()) == 0.0)

    print(f"\nselftest: {'ALL PASS' if not fails else f'{len(fails)} FAIL: {fails}'}")
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket 刷量农场过滤器 (read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--source", choices=["feed", "tape"], default="feed",
                    help="feed=live 大额打印 (滚动) / tape=top-500 市场全量逐笔 (快照)")
    ap.add_argument("--path", type=str, default=None)
    ap.add_argument("--top", type=int, default=500, help="candidates.json 落多少个")
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
