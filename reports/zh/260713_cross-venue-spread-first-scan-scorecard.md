> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.


# 260713 跨场馆 (Polymarket ↔ Kalshi) 同事件价差第一刀 scorecard

## 结论交付契约四件套

1. **冻结问题** (单一): 当下 Polymarket 与 Kalshi 的同事件盘, 静态双腿锁定 (两边都吃 taker: 一边买 YES + 另一边买 NO) 的**费后** edge 是否存在、多大。
2. **数据 + 基线**: 2026-07-13 08:59Z 单时点 snapshot。Kalshi 全部 open events (零 auth 公开 API, 过滤 parlay/MVE + volume≥$1k 后 12,234 盘) × Polymarket Gamma top-3000 by volume24hr (volume≥$2k 后数百盘)。基线 = 0 (锁定套利跟自己比, 费后 >0 即肉)。费用: Kalshi taker 0.07·p·(1−p) (通用档, 保守偏高; 部分系列 0.035, **maker 0**), Polymarket taker rate·p·(1−p) (rate 取 feeSchedule, 缺失且 feesEnabled 时 0.03 = sports_fees_v2 档)。
3. **判决: NULL (此形态此时点) — 静态跨场馆 taker 锁定无费后可捕捉肉**。置信档: **探索性** (单时点 / top-of-book 未 sweep 深度 / 真同事件 n=11)。证伪测试 = 本扫描自身 (仪器 selftest 31/31)。
4. **下一步 = continue (换形态, 不在同口径重测)**: ① 跨场馆**反应时差** (事件驱动窗口两边收敛速度差, 需 tick 级双边窗口记录, 对接 `polymarket_stale_window_reaction.py` 模式, 属 NOW.md 源④反应速度); ② **Kalshi maker 0 费侧**做市对冲 (见下 Mbappe 4 分 gap 案例, maker 形态费用 <1 分)。

## 真数字 (人工核对 rules 后的真同事件对, n=11)

| 对 (两边结算语义一致) | 价位区 | mid gap | 费后 edge |
|---|---|---|---|
| England/Argentina to advance (实体翻转对齐) | 0.45 | 0.006 | **−0.030** |
| France/Spain O/U 2.5 | 0.51 | 0.009 | −0.028 |
| France/Spain O/U 1.5 | 0.77 | 0.004 | −0.024 |
| England/Argentina O/U 2.5 | 0.41 | 0.004 | −0.032 |
| Mbappe 全赛事金靴 | 0.58 | **0.040** | +0.001 |
| Kane 全赛事金靴 | 0.04 | 0.011 | −0.001 |
| Bellingham 全赛事金靴 | 0.03 | 0.007 | −0.002 |
| Walz 民主党提名 | 0.003 | 0.004 | +0.003 |
| Pence 共和党提名 | 0.005 | 0.001 | −0.002 |
| Buttigieg 当选总统 | 0.027 | 0.003 | −0.001 |
| Ramaswamy 当选总统 | 0.006 | 0.000 | −0.003 |

- **两平台已被钉在一起**: 真同事件 mid gap p50 ≈ 0.4 分, 高流动对 ≤1 分。跨场馆套利者 (pmxt 类基建的存在) 已把静态价差收干。
- **费用墙 > 价差一个量级**: 高流动对 (价位 0.4–0.8) 双 taker 费 ≈ 2.4–3.2 分, 真实价差 ≤1 分 ⟹ 一致费后 −2.4~−3.2 分。
- 三条 +0.001~+0.003 "正 edge" 全在 ≤0.6 分价位长尾腿 (报价粒度 = tick, 无深度) 或粒度内, 不可执行规模。
- **唯一超粒度真 gap = Mbappe 金靴 4 分** (Kalshi 0.605 vs PM 0.565): taker 形态费后归零 (+0.001), 但 **Kalshi maker 0 费** ⟹ maker 挂单成交 + PM 对冲的形态费用只剩 PM 侧 ~0.7 分, 理论残余 ~3 分 — 代价是 maker 成交不确定 + 双腿时差敞口 + tie-break 条款差异 (PM 有 tie 细则, Kalshi 未写 = oracle basis 真实存在)。此为形态②的实证锚点。

## 仪器 (已落, 复用件)

`unpublished research artifact` — 单文件 read-only 扫描器, selftest 31/31, 一次 `--scan` ~80s 出全量判决。核心层: 双边公开 API 拉取 → informative-token 匹配 (Jaccard + 非年份数字 + 截止日邻近, 全局贪心一对一) → **YES 实体对齐层** (显式实体 / "Will X" 主语提取 / vs-对阵翻转 / 冲突丢弃) → Tier 分层 (A = 实体对齐 + 非年份数字全等 + 无单侧 and/or 复合结构; 只有 A 进 edge 判决) → 双向费后锁定 edge。输出 `unpublished research artifact + matches_*.jsonl`。

## 假匹配形态学 (后续跨场馆匹配工作的教训库)

原始文本匹配的 20 条 "正 edge" 全是假信号, 五种形态 (仪器现已各有闸, 但 **Tier A 仍需人工核对 rules** — 文本层分不出下面第 4 类):

1. **YES 实体错位** (最大宗): 两边报价的是同一事件的不同侧 (England vs Argentina 两边各报一队, 0.455+0.55≈1.005 = 定价一致的幻觉价差) → 实体对齐 + 翻转闸。
2. **阈值/结构错位**: 区间腿 vs above-阈值、set 1 vs 整场 → 非年份数字全等闸 (单字符数字 "1" 必须保留, 曾因 len>1 过滤失效)。
3. **组合盘 vs 单一盘**: "Golden Boot AND Golden Ball"、"Messi OR Mbappé" vs 单一奖项 → and/or 复合结构闸。
4. **同实体不同事件** (文本层无解, 靠人工): 队内最佳射手 vs 全赛事金靴 vs 金球奖; "参选" vs "胜选"; 提名 vs 当选。价格都在低区时 gap 恰好小, 极易漏过 → suspect 标记 (gap>0.30) + Tier A 逐对人工核对 rules。
5. **Unicode 重音**: Mbappé/Dembélé token 切坏跨边不匹配 → NFKD fold。

## Caveats

- 单时点 snapshot ≠ 时序; top-of-book ≠ 深度; 结算条款 basis (tie-break/postponement 细则差异) 文本层未判。
- Kalshi 费按 0.07 通用档保守偏高; PM sports 档 0.03 为 feeSchedule 缺失时的回退。
- 本判决只杀「静态 taker 锁定」这一个形态; 反应时差与 maker 形态未测, 不受本 NULL 约束 (feedback_no_negative_verdict_pollutes_memory: NULL 杀具体假设, 不杀方向)。
