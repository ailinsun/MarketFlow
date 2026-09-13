> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.


# negRisk NO 侧费后净值全库分布 — 层 E 另一半结案

8/5 的[开放场次报告](260805_negrisk-three-dominant-events-open-field-artifact.md)把 YES 侧结干净了,
但在「边界(未做的)」里留了一条: **违约幅度只对三个盘 + World Cup 算了中位, 未做全库分布**。
NO 侧因此只有存在率 (36.2%) 没有幅度, 从未被结案。

本轮补上。**结论: NO 侧比 YES 侧死得更干脆, 且那个 36.2% 本身是恒等式伪影。**

数据 = `unpublished research artifact`,
2026-06-26 → 08-06 共 60,742 次扫描 (COMPLETE 50,845), 只读, 零 money surface。

---

## 一· 先修一个方法错: 「NO 侧 36.2%」是同义反复

8/5 那张四行表里, NO 侧的分子在四个口径下**恒定 9,780**, 而分母从 54,720 缩到 27,008,
于是占比从 17.9% 一路"升"到 36.2%。这不是筛选出了信号, 是恒等式:

```
sum_ask > sum_bid                    (逐腿 ask>bid)
⟹ sum_mid = (sum_ask+sum_bid)/2 > sum_bid
⟹ sum_bid > 1  ⟹  sum_mid > 1 ≥ 0.995
```

**NO 侧违约的定义本身蕴含 Σmid ≥ 0.995**, 所以任何以 Σmid 为筛的口径都不可能剔掉任何一行。
全库实测: `sum_bid>1` 的 9,989 行里, `sum_mid<0.995` 的有 **0 行**。

> 真实基础率 = 9,989 / 50,845 = **19.6%**。36.2% 这个数不该被引用。

「同一个筛既缩分母又不动分子」是 `feedback_overlap_honest_signal_stats` 的同型第四次。
判据: 报占比前先确认分子分母受同一个筛的作用方向。

---

## 二· 费后净值: 40 天 50,845 次扫描里有 2 个正的

`fee_total` 从 timeseries 精确反解 (不是估计): 源码 `polymarket_structural_mispricing.py:329`
定义 `underround_net = 1 − Σask − fee_total` ⟹ `fee_total = 1 − Σask − underround_net`;
`overround_net = (Σbid − 1) − fee_total` (同源第 331 行, 只是没存进 timeseries)。

NO 侧子集 (n = 9,989, 单位 = 分):

| | p05 | p25 | **中位** | p75 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| 毛缺口 `Σbid−1` | 0.10 | 0.40 | **0.80** | 1.70 | 2.40 | 4.40 |
| 费用 `fee_total` | 1.63 | 2.33 | **2.91** | 4.08 | 4.76 | — |
| **费后净** | −3.82 | −2.57 | **−1.90** | −1.45 | −0.81 | **+0.41** |

> **费后净 > 0 = 2 个观测** (占 COMPLETE 的 0.0039%, 占 NO 侧的 0.02%):
>
> | 时刻 | 事件 | 腿 | 毛 | 费 | 净 | 采集器 verdict |
> |---|---|---:|---:|---:|---:|---|
> | 06-26T20:05 | Senegal vs. Iraq | 3 | 0.40 | 0.31 | **+0.09** | EFFICIENT_NO_ARB |
> | 07-06T03:03 | Mexico vs. England · Exact Score | 17 | 2.10 | 1.69 | **+0.41** | EFFICIENT_NO_ARB |
>
> 两个都被采集器判为「无套利机会」, 连候选都没进 —— 与本轮独立计算一致。

按事件看, 八个主要贡献者**无一净中位为正**:

| 观测数 | 占该事件 | 毛中位 | 费中位 | 净中位 | 事件 |
|---:|---:|---:|---:|---:|---|
| 1,946 | 55.1% | 0.90 | 2.36 | **−1.39** | Brazil Presidential Election |
| 1,691 | 67.1% | 1.10 | 3.69 | **−2.58** | Ballon d'Or Winner 2026 |
| 1,350 | **100%** | 2.20 | 4.10 | **−1.90** | EPL: 2027 Champion |
| 1,212 | 52.5% | 0.40 | 1.66 | **−1.28** | Fed Decision in July? |
| 1,127 | 53.9% | 1.10 | 4.74 | **−3.64** | NBA: 2027 Champion |
| 608 | 70.8% | 0.80 | 2.62 | **−1.90** | Fed Decision in September? |
| 533 | 11.2% | 0.60 | 2.32 | **−1.69** | F1 Drivers' Champion |
| 397 | **100%** | 0.90 | 2.91 | **−2.01** | LALIGA: 2027 Champion |

EPL / LALIGA 两个盘 **100% 的观测都毛违约** —— 存在率满格, 净值仍是 −1.9 / −2.0 分。
**存在率与可交易性零关系**, 这是本表最该带走的一条。

---

## 三· 顺带: YES 侧结案获得第三条独立证据

同一把尺子跑 YES 侧: 费后净 > 0 有 2,606 行, 其中

- `INCOMPLETE_COVERAGE` **2,603** 行 (= 开放场次伪影, 8/5 已判)
- 落在真穷尽集 (Σmid ≥ 0.99) 的 **0 行**

8/5 用事件身份 (2028 大选三连) 定性判掉的东西, 本轮用连续净值口径独立复现: **真穷尽集上
YES 侧费后正净值一行都没有。**

---

## 四· 为什么两侧都死 — 这不是巧合, 是 260804 那条恒等式的结构套利版

费用 = `rate · Σpᵢ(1−pᵢ)`。多腿盘概率分散时 `Σp(1−p) → 1−1/N ≈ 1`, 故
**费用 ≈ rate 本身** (体育盘 `sports_fees_v2` 3% ⟹ 实测费中位 2.91 分 ✓ 吻合)。

而毛缺口来自做市点差的不对称, 实测中位只有 0.8 分。

> **做市商让出的点差不对称 (0.8 分), 比协议费率 (3 分) 小近 4 倍。**

这与 [260804 first-principles](260804_predmkt-first-principles.md) 的
「全市场毛 alpha ±0.5% vs 手续费 1.2%」是**同一个结构在不同维度上的复现**: 无论从
方向性还是从结构性去取, 毛的那一半都比摩擦小一个量级。260626「诚实收敛到 0」不需要
第二种解释, 现在两侧都有了连续净值证据。

---

## 判决

**层 E (negRisk 结构套利) 两侧结案。** 不再占推进位, 不再需要"再看看另一侧"。

外部输入侧同步: PRISM 论文 的 Butterfly
策略 (买语义相似市场的 NO) 在真实 Polymarket 数据上的物理对应就是本文测的 NO 侧;
它在合成语料 + 零交易成本假设下报出的 36.1% 方差降低, 换到真实费表下净中位 **−1.90 分**。
该论文对后续实验 无可提取的实证价值。

---

## 边界 (未做的)

- **价格层结论, 不含容量与滑点**: `optimal_cover` 的 binding-leg capacity 未存进 timeseries;
  即使净值为正也未验证可成交规模。但净值中位 −1.9 分, 容量层无法救回符号
- `fee_total` 按 YES-ask 侧算 (源码口径), NO 侧真实成交价是 `1−bid`, 因 `p(1−p)` 对称
  两者只差一个二阶点差项 (0.0x 分量级), 不改变任何结论; 方向上略微**高估**费用故偏保守
- 采样窗 06-26 → 08-06; `structural_mispricing_watch` 已于 8/5 降频 300s → 3600s (referenced section),
  之后的采样密度与本窗不同
- 缺 bid 的腿按 0 计入 `sum_bid` (源码 `float(l.best_bid or 0.0)`) ⟹ NO 侧毛违约是**保守下界**,
  不会造假阳性
