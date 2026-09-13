> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.

# 260805 每日观测 — negRisk 可执行性的机制解释 + 联邦优先权连败

> 覆盖窗口: 2026-08-04 00:00 → 2026-08-05 08:00 (约 24 小时)。交付人 Scout (情报官, 非安全线)。
> 本文只出观测与判断, 不落码、不碰 money surface、不含任何私域内容。
> 与 260803 每周观测 配套读:
> 那份已判过的 (TWAP 8/7 切换 · 纽约州起诉 · 密歇根 8/12 · 接入面退化四来源 · arXiv 2607.14430)
> 本文不重复, 只报新增与被推翻的部分。

---

## 1. 三句结论

1. **一篇 8 月 1 日的实证论文给了我们 260626 结构套利「诚实收敛到 0」一个机制解释**:
   negRisk 的无套利违约系统性集中在协议**不支持执行**的 YES 侧, 而 adapter 支持的 NO 侧违约
   「既更少见也更短命」。我们扫的正好是被套利者秒杀的那一半。这条**零成本可自查**。
2. **CFTC 的联邦优先权主张一周内在两个联邦法院各败一次**, 8 月 7 日在 SDNY Marrero 法官前还有一次。
   「CFTC 注册 = 州赌博法豁免」这条护城河正在被实测, 且**目前实测结果是负的**。
3. **昨天报的「Polymarket 接入面退化」今天出现第五个来源, 但它与官方状态页冲突** ——
   官方 8/4 记录的是永续合约中断与体育组合盘故障, **没有任何 CLOB 延迟事件**。
   转述说 3–7 秒, 官方说没这回事。按纪律以原文为准, 该数字标 [未核实]。

---

## 2. 研究 — arXiv 2608.00666, 直接接上 P0「结构错价层」

**«Executable Arbitrage and Market Efficiency in Prediction Markets»**
Gebele, J. · Mutzel, T. · Matthes, F. (慕尼黑工业大学), 提交 2026-08-01
http://arxiv.org/abs/2608.00666v1

论文把无套利分成两层, 这个区分正是我们缺的那块:

| 层 | 定义 | 是否可交易 |
|---|---|---|
| 收益空间无套利 (payoff-space) | 由终局收益恒等式推出 | 结算前**未必**可执行 |
| 协议可执行无套利 (protocol-executable) | 取决于协议实际暴露了哪些仓位变换原语 | 这才是能赚到的那层 |

**Polymarket 的 negRisk 让这个区分可观测**: 关联二元盘代表互斥结果, 但 NegRisk Adapter 在结算前
**只实现 NO→YES 一个方向**。

论文的量化结果 (原文摘要, 我们未复现):

- 套利利润合计约 **112 万美元**: 转换器路径 **108.6 万**, 结算篮子路径 **3.2 万**。
- **正向违约集中在不被支持的 YES 侧; adapter 支持的 NO 侧违约显著更少、存续更短。**
- 作者的双向 adapter 原型显示: 若两个方向在结算前都可执行, 剩余套利机会会大幅减少。

**对我们的含义 (推断, 不是论文原话)**: 260626 全结构检查得到的
「negRisk 0 · complementary 0 · coarsening 一致 (<3%)」被记成了「流动盘真高效」。
论文给出第二种解释: 我们的扫描器扫的是**可执行**那一侧, 而那一侧按其实证本来就该是又少又短命的 ——
**收敛到 0 可能是协议设计的必然, 不是市场高效的证据**。这两个解释导向完全不同的下一步:
前者说这条线该结案, 后者说该换观测对象。

**可以零成本分辨**: `structural_mispricing_watch` 自 2026-06-26 起每 5 分钟写效率时间序列。
若该 sidecar 记录了违约发生在哪一侧, 分侧重统计一次即可判。见 referenced section 动作二。

**边界**: 只读了 arXiv 摘要页, 未读正文方法与数据划分, 未复现任何数字。样本期与场馆范围未核。

---

## 3. 监管 — 联邦优先权在三个法院同向失利

按时间顺序, 三条独立裁定 (前两条已核到二手法律媒体, 第三条核到官方新闻稿转述):

| 日期 | 法院 / 法官 | 裁定 |
|---|---|---|
| 2026-07-07 | SDNY · Torres | 驳回 **Kalshi** 的初步禁令申请; 原文称纽约赌博法适用于 Kalshi 体育事件合约**不被 CEA 优先**, Kalshi 未能显示胜诉可能。已上诉第二巡回 |
| 2026-07-28 | 威斯康星联邦地院 | 驳回 **CFTC** 阻止该州执行赌博法的请求; 认定体育事件合约**不太可能构成掉期 (swaps)**, 否定联邦优先权论证 |
| 2026-08-04 | SDNY · Rakoff | 驳回 **CFTC** 的临时限制令申请 (**不影响再次提出**); 认为 CFTC 未显示胜诉可能, 也未显示会遭即时且不可弥补的损害 |

**下一个硬日期: 8 月 7 日** —— CFTC 可在 **Marrero 法官**前重新提出。

来源: https://www.cryptotimes.io/2026/08/04/federal-judge-rejects-cftc-bid-to-block-new-yorks-kalshi-case/ ·
https://www.gamblinginsider.com/news/179752/cftc-setback-wisconsin-prediction-market-case ·
https://www.jdsupra.com/legalnews/sdny-rejects-bid-to-block-states-from-7453875/

**一处必须校正的转述**: X 上 8/4 有转述称「纽约州法院驳回了**州方**的初步禁令申请」。
核验后不成立 —— 8/4 被驳回的是 **CFTC** 的临时限制令, 不是州方的申请;
被驳回初步禁令申请的是 **Kalshi**, 时间是 7/7。方向刚好反了。

**对我们的影响面**: 自营只读 Polymarket 不受直接影响。真正相关的是 **Guardian 托管栈** ——
它是唯一面向外部用户、可能跨州的表面。「受 CFTC 监管即可豁免州法」这个前提, 现在有三个联邦法院的
反向实测。见 referenced section 动作三。

---

## 4. 竞品与机制变化 (已核到官方原文的部分)

### 4.1 Kalshi 官方 changelog — 两个日期

来源: https://docs.kalshi.com/changelog (今日第一手核)

- **8 月 6 日**: 已弃用的 multivariate REST 查询端点与 multivariate WebSocket 频道**移除**;
  REST 错误响应体不再返回已弃用的 `service` 字段; 订单与成交执行报告新增 `LastMkt`。
- **8 月 17 日**: 组合 (multivariate) 市场从 `deci_cent` ($0.001 最小价位) 切到
  `center_centi_edge_centi_cent`, 全区间统一 **$0.0001 (0.01¢)** 最小价位。
  官方明确要求: 从 `*_dollars` 字段读价 (整数分字段无法表示次分价格), 并按市场 `price_ranges` 里的
  `step` 对齐下单与询价, **不要按结构名硬编码**。

**已自查, 我们不受 8/6 影响**: 两个 Kalshi 消费者
`unpublished research artifact` 与
`unpublished research artifact` 都只用 `trade-api/v2` 基础端点;
后者 `:129` 显式跳过 parlay/multivariate 集合。**无 multivariate 端点调用, 8/6 不断线。**

**8/17 那条与昨天的价位表达恒等式相关**: 打平门槛 `rate·(1−p)` 说极端价位表达最便宜,
而最小价位精度正是极端价位**能否被表达**的物理约束。$0.001 → $0.0001 把 Kalshi 组合盘的
可表达分辨率提高一个数量级。当前我们不在 Kalshi 下单, 所以这是结构认识不是行动项。

### 4.2 七月成交量 — 我们这个场馆在缩

来源: The Block 数据面板, https://www.theblock.co/post/410382/kalshi-polymarket-volume-july

| 场馆 | 7 月成交额 | 环比 |
|---|---:|---:|
| Kalshi | $37.7B | +14% |
| **Polymarket 主站** | **$7.9B** | **−26%** |
| Polymarket US | $5.0B | +54% |
| 合计 | $50.59B | +7.8% |

The Block 归因: Polymarket US 五月取消候补名单后, 美国用户从主站转到美国站。

**校正**: X 上流传的「Kalshi $41.05B / Polymarket $12.75B」与 The Block 口径不符
(疑为名义成交额 notional 与撮合额口径差)。本文采用 The Block 数字。

**对我们的含义 (推断)**: 我们的执行与层 A 采样都在**主站**, 而主站是三者里唯一环比下滑的。
这对层 D (执行差) 与层 A (做市补贴) 的样本代表性是个提醒 —— 世界杯窗口 (6/11–7/19) 结束叠加
用户分流, 8 月的盘口深度可能与 7 月样本不同分布。

### 4.3 Kalshi × Comply — 内幕交易监控扩到事件合约

CNBC 2026-08-04: https://www.cnbc.com/2026/08/04/kalshi-makes-partnership-with-comply-compliance-tech-company.html
Comply (服务 5,000+ 家以金融机构为主的客户) 把 Kalshi 的事件合约成交数据接入其合规软件,
使企业能看到员工的事件合约交易并做预清算与规则配置。承接 6 月与 StarCompliance 的同类合作。
Kalshi 称 2026 年 Q1 做了 150+ 次调查、拦下 100+ 起疑似内幕交易。

**为什么记这条**: 这是「事件合约被当作受监管金融产品对待」的基础设施化证据,
方向与 referenced section 的司法结果相反。两股力同时在走, 别只看一边。

---

## 5. 行动建议 (三条, 各带截止时间与放弃条件)

### 动作一 — 层 A 前期基线的冻结时点提前到 8/6 04:00 UTC 前

**为什么**: 周报动作一定的是「8/6 24:00 UTC 前冻结」。今日核到 Polymarket 官方状态页新增一条
**8 月 6 日 04:30 UTC 计划维护 (price history service)**
(https://status.polymarket.com/history/1)。若 `polymarket_maker_book_sampler` 的取数路径与
price history 服务同源, 冻结窗口的尾段会落在维护窗口里。

**具体**: (a) 先确认采样器是否依赖 price history 服务; (b) 无论结论如何, 把前期快照的截止时点
提前到 **8/6 04:00 UTC**, 维护窗口之后的采样单独标段。

**验证条件**: 8/6 之后检查快照在 04:30–06:00 UTC 有无采样缺口。有缺口 → 判决里显式排除该窗口。
**放弃条件**: 若确认采样器与 price history 不同源且 8/6 采样连续 → 按原计划 8/6 24:00 UTC 冻结,
本条作废不再提。

### 动作二 — 按 YES / NO 侧重读 negRisk 违约时间序列 (今天可做, 零采集成本)

**为什么**: referenced section 给了 260626「收敛到 0」第二种解释, 且这两种解释导向相反的下一步。
`structural_mispricing_watch` 自 6/26 起已经在写效率时间序列, 不需要新建任何采集。

**具体**: 把已有 sidecar 按违约方向 (YES 侧 / NO 侧) 分组重统计一次频次与存续时长。
**只读历史文件, 不改扫描器, 不碰执行链。**

**验证条件**: 若 YES 侧违约频次显著高于 NO 侧且存续更长 → 论文机制在我们样本上复现,
层 E 可以按「协议不暴露该方向为可执行原语」正式结案, 不再以「继续攒料」的名义占推进位。
若两侧对称 → 论文结论在我们样本上不复现, 层 E 维持现状, 这条线关掉。
**放弃条件**: 若 sidecar 根本没记方向信息 → **不为此新建采集**, 直接记「无法验证」结案。

### 动作三 — 8/8 查 8/7 听证结果, 决定 Guardian 地区红线是否要按州重画

**为什么**: referenced section 三个联邦法院同向, 且 8/7 有下一个节点。Guardian 是我们唯一面向外部用户的资金表面,
它的地区限制假设如果建立在「CFTC 注册即豁免州法」上, 那个前提正在被实测为假。

**具体**: 8/8 核 SDNY docket 或法律媒体确认 Marrero 法官的裁定方向; 若同样驳回 CFTC,
把「州级地区围栏」列入 Guardian 开放注册的前置条件清单, 送 Lex 做红线地图输入。

**验证条件**: 8/8 能拿到明确裁定方向 (驳回 / 批准 / 延期)。
**放弃条件**: 若 Marrero 批准 CFTC 的申请 → 优先权方向反转, 本条不动, 按原计划走。

---

## 6. 本次没有覆盖到的面

- **私域观测一律未读**: 程序层已硬拒, 本文不含任何私域内容。
- **arXiv 2608.00666 未复现**, 只读摘要页; 未核样本期、场馆范围、违约判定阈值与费用假设。
- **「CLOB 延迟 3–7 秒」未核实且与官方状态页冲突** —— 官方 8/4 只记录永续合约中断
  (08:30–10:12 UTC) 与体育组合盘故障 (14:55–15:35 UTC), 无 CLOB 事件。该数字不进任何行动建议。
  同一条转述里的「日经/恒生开盘时间错用美股时段」「天气盘次分档切换阈值 4¢→1¢ 后切换逻辑坏」
  同样未核, 未在 Polymarket changelog 或状态页找到对应记录。
- **未核 ICE 20 亿美元投资 Polymarket 的「独家数据分销」当前状态**: 投资本身与分销安排在 2025-10
  至 2026-03 的公开材料中可核 (ICE 投资者关系页), 但 X 上「6 月 1 日起机构客户须持 ICE 许可才能
  消费 Polymarket 数据」这一具体说法**未在官方材料核到**, 标 [未核实]。
- **未确认 `polymarket_maker_book_sampler` 的取数路径**是否依赖 price history 服务 (动作一 (a) 的前提)。
- **未读 Guardian 当前的地区限制实现** —— 动作三只提出问题, 未自行判断现状。
- **PREDICT 会议 (早鸟 8/7 到期) 与拉斯维加斯 Prediction Markets Conference (11/3–5) 未作参会价值判断**;
  Frontier Forecast 黑客松 (旧金山) 页面未核报名截止日。
