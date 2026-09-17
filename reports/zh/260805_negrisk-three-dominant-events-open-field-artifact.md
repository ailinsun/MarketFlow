> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.


# 撑起全局的三个盘 = 2028 美国大选三连, 而它们的 YES 侧「违约」是开放场次伪影

维护者 8/5 派活: 查 event 31552 / 31875 / 30829 是什么盘, 判它们是不是同一类结构 —— 若是,
YES 侧长命违约就是可定位的品类特征; 若三个盘毫无共性, 按噪声关掉。

**结论: 是同一类结构, 但共性不是「腿数多的锦标赛盘」, 是「候选人名单不穷尽」。而所谓
YES 侧违约在这三个盘上根本不是违约 —— 是我 8/5 的统计口径绕过了采集器自带的一道 guard。
这条线关掉, 关掉的理由是口径错误, 不是噪声。**

## 一· 三个盘的身份

| event | 盘名 | 腿数 | 观测数 | 采样期 | verdict 众数 |
|---|---|---|---|---|---|
| 31552 | Presidential Election Winner 2028 | 50 | 5,811 | 06-26 → 08-05 | INCOMPLETE_COVERAGE |
| 31875 | Republican Presidential Nominee 2028 | 42 | 5,803 | 06-26 → 08-05 | INCOMPLETE_COVERAGE |
| 30829 | Democratic Presidential Nominee 2028 | 51 | 5,794 | 06-26 → 08-05 | EFFICIENT_NO_ARB |

**2028 美国大选三连** —— 总统胜选 + 共和党提名 + 民主党提名, 同一届选举的三个层面。
它们也是全库唯三横跨整个采样期 (41 天全程在架) 的事件, 这解释了观测数为何独占鳌头:
不是它们违约多, 是它们活得久。世界杯类盘几天就结算, 大选盘还有两年多。

## 二· 共性不是腿数 —— 是 Σmid ≪ 1

按 YES 侧占比排序, 前六名与腿数确实相关, 但真正对齐的是 `sum_mid`:

| Σmid 中位 | YES 侧占比 | 腿数 | 盘 |
|---|---|---|---|
| 0.9535 | 0.95 | 50 | Presidential Election Winner 2028 |
| 0.9640 | 0.72 | 42 | Republican Presidential Nominee 2028 |
| 0.9725 | 0.52 | 51 | Democratic Presidential Nominee 2028 |
| 0.9715 | 0.36 | 41 | Next French Presidential Election |
| 0.9990 | 0.36 | 40 | World Cup Winner |
| 0.9935 | 0.15 | 22 | F1 Drivers' Champion |
| 1.0555 | 0.00 | 20 | EPL: 2027 Champion |
| — | 0.00 | 30 | NBA: 2027 Champion |
| — | 0.00 | 31 | Ballon d'Or Winner 2026 |

腿数被证伪: NBA 30 腿 / Ballon d'Or 31 腿 / EPL 20 腿, YES 侧全部为 0。
真正的分野是**腿集合封闭还是开放**:

- **封闭** (联赛冠军 = 队伍数固定, 精确比分 = 枚举完备): Σmid ≈ 1, YES 侧 ≈ 0
- **开放** (远期选举 = 未列出的候选人仍可能赢): Σmid = 0.95~0.97, YES 侧高

Σmid = 0.9535 的含义是: 已列出的 50 个候选人只占 95.35% 的概率质量, 剩下 4.65% 属于
名单外的人。买齐 50 个 YES 花 0.977 不是稳赚 1 —— 第 51 个人赢的时候全部归零。

## 三· 决定性反转: 采集器早就判掉了, 是我绕过去的

`unpublished research artifact` 有一道 **Exhaustiveness
gate 2 (open-field guard)**, 注释原文:

> a TRULY exhaustive set has Σmid≈1. If Σmid ≪ 1, the listed outcomes leave unpriced
> "field/other" mass (e.g. a far-future election where an unlisted candidate can still
> win) — buying all listed YES is then exposed to that unlisted winner and is NOT risk-free.

举的例子就是这三个盘。8/5 的重统计我直接用 `sum_ask < 1` 定义「YES 侧违约」, 绕过了
`verdict` 字段 —— 把 guard 已经判为「不是套利」的观测, 全部重新算成了违约。

按穷尽性重统计 (全部 coverage=COMPLETE 观测, N=54,720):

| 口径 | N | YES 侧 |
|---|---|---|
| 全部 | 54,720 | 15,692 (28.7%) |
| 剔除 verdict=INCOMPLETE_COVERAGE | 37,456 | 3,381 (9.0%) |
| 仅 Σmid ≥ 0.99 (真穷尽集) | 30,124 | 758 (2.5%) |
| 仅 Σmid ≥ 0.995 | 27,008 | 151 (0.6%) |

**在真穷尽集上 YES 侧几乎不存在 (0.6%)** —— 与论文 arXiv 2608.00666
「违约系统性集中在协议不支持的 YES 侧」方向相反, 与 8/5 事件聚类口径的符号检验
(z=−5.75, 偏向 NO 侧) 方向一致。两个正确口径互相印证, 错的是那个全局观测口径。

**NO 侧不能放进这张表**: `sum_ask > sum_bid ⟹ sum_bid>1 蕴含 sum_mid>1 ≥ 0.995`, 所以
以 Σmid 为筛的口径**不可能剔掉任何一行** NO 侧观测 (实测 0 行) —— 分子恒定是恒等式,
占比随分母缩小而"上升"是同义反复, 不构成发现。NO 侧的真实基础率 = 19.6%, 其费后净值
分布见 [260806 全库分布](260806_negrisk-no-side-net-verdict.md) (中位 −1.90 分)。

## 四· 附带发现: guard 的容差把两个同病的盘切到了不同 verdict

`field_tolerance = 0.03` ⟹ guard 在 Σmid < 0.97 时触发。

- 31552 (0.9535) / 31875 (0.9640) → 被拦, INCOMPLETE_COVERAGE
- 30829 (0.9725) / 法国大选 79987 (0.9715) → **差 0.25 分漏过**, 落进 EFFICIENT_NO_ARB

同一种病 (开放场次) 被切成两个 verdict。这不构成资金风险 —— EFFICIENT_NO_ARB 是
「无套利机会」, 不触发任何下单。但**做统计时不能用 verdict 当穷尽性判据**, 要直接用
Σmid 阈值。建议分析口径统一取 Σmid ≥ 0.99, 不建议改 guard 的 0.03 (那是实盘入场的
保守容差, 另一件事)。提案不落码。

## 五· World Cup Winner 这一格 (唯一的真边界样本)

40 腿, Σmid = 0.9990 (真穷尽), 却有 36% 观测 Σask < 1。诊断: 中位 Σask = 1.0060,
点差极窄 (Σask−Σbid = 0.0130, 每腿 0.03 分), 即 Σask 在 1 附近抖动, 36% 的时刻略低。
违约时的中位缺口 = **0.6 分**, 而 260713 定的费用墙是 2.4~3.2 分。

这是这批数据里唯一一个「真穷尽集上的真 YES 侧违约」, 而它费后为负 —— 正好落回
260804 first-principles 的会计恒等式: 毛 alpha 量级 < 手续费。

## 判决

层 E 的 **YES 侧**按**开放场次伪影**结案, 不是按噪声结案 (NO 侧的费后净值分布见
[260806 全库分布](260806_negrisk-no-side-net-verdict.md), 中位 −1.90 分, 两侧至此全部结案):

1. 三个主导盘 = 2028 美国大选三连, 同一类结构 = 开放场次不穷尽集
2. 它们的 YES 侧「违约」不是可交易特征, 是未列出候选人的概率质量
3. 真穷尽集上 YES 侧 0.6%, 唯一的真样本 (World Cup) 毛缺口 0.6 分 < 费用墙 2.4 分
4. 论文机制在我们的数据上**不复现**; 260626「诚实收敛到 0」不需要第二种解释

不再占推进位。

## 方法教训

`feedback_overlap_honest_signal_stats` 的第三次出现, 而这次比前两次更该被抓住: **数据里
本来就有一个字段 (`verdict`) 已经标好了答案, 我自己造了个 `sum_ask < 1` 的判据把它绕开了。**
下次在带 verdict / flag 的派生数据上做统计, 先问「采集器为什么给这行打这个标签」,
再决定要不要另起判据。

## 边界 (未做的)

- 未查 Polymarket 侧的 negRisk 标志原文 (gamma-api 今日 403, 与 260805 记录的 UA/代理
  问题同型, 未单独排查)。穷尽性判据全部来自本地 Σmid, 不依赖该接口
- 未按事件重做存续时长 (8/5 报告的边界仍在)
