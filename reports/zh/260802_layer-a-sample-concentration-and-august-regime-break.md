> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.

# 260802 层 A 采样集中度 + 8 月机制断点 — 观测触发的一次自查

> 触发源: 全球观测层 2026-08-01/02 采到 Polymarket 8 月加密盘补贴加码 + 结算规则换 TWAP。
> 顺着这条外部信号回查我们自己的层 A 采样池, 查出一个**外部信号无关但更硬的问题**: 样本集中度。
> 冻结问题 = 「8 月底那个层 A 判决, 现在能不能当底仓判据用?」
> 交付人 Scout (情报官, 非安全线)。本文只出观测与判断, 不落码、不碰 money surface。

---

## 1. 一句话

**8 月底的层 A 判决 (referenced section 判据: 费后奖励年化 ≥15% 底仓成立 / <5% 降级) 当前样本里,
84.1% 的奖励权重压在两个事件族上, 其中单一族占 64.9% —— 算出来的年化不能区分
「层 A 机制成立」和「2026 年 7–8 月中东盘特别好赚」。**

---

## 2. First-hand 数字 (本机数据, 2026-08-02)

数据源 `unpublished research artifact` (采样器 LIVE, 最后写入 08-02 10:09):

| 口径 | 值 |
|---|---|
| 采样盘数 | 30 |
| 采样盘日奖励率合计 | $11,026 / day |
| 占官方全站日奖励池 ($42,425, referenced section 实测) | **26.0%** |
| 以色列–伊朗事件族 (15 盘, 滚动停火日期盘) | $7,159 = **64.9%** |
| 美联储利率族 (3 盘) | $2,116 = 19.2% |
| 两族合计 | **84.1%** |
| 加密类盘 | **0 个** |

两点直接读出来的结论:

1. **有效独立样本远小于 30**。"Israel x Iran ceasefire continues through August 2 / 3 / 4 / 9 / 15"
   是同一事件的滚动日期盘, 停火崩或续会同时改变 65% 的奖励权重样本。referenced section 已经给过观测期护栏
   (不足 25 天一律 PRELIMINARY), 但护栏管的是**时间长度**, 没管**横截面独立性**。
2. **外部效度只覆盖「地缘政治高奖励盘」这一类**, 不能外推成"层 A 底仓年化 X%"。占官方池 26%
   也说明采样覆盖面本身是子集, 不是全站。

---

## 3. 外部信号 (触发本次自查的那条) 与它的真实影响面

**已核官方原文** (https://docs.polymarket.com/market-data/chainlink-twap):
- Polymarket RTDS 定于 **2026-08-04** 上线, WebSocket `wss://ws-live-data.polymarket.com`。
- TWAP 窗口参数 `windowSeconds` ∈ {30, 60}。
- 开发者陷阱原文: 上线前订阅返回 `topic not found`, 且**被拒的预上线订阅在已开的 socket 上可能不会重试**。

**多源二手一致, 未在 Polymarket 官方 changelog 核到原文** (docs.polymarket.com/changelog 最新条目停在 07-17):
- 结算 **2026-08-07 00:00 UTC** 从最后一秒快照切 TWAP; 5 分钟盘用 30 秒窗, 15 分钟与 4 小时盘用 60 秒窗。
- 8 月投放 **$100 万**流动性奖励到相关加密盘。
- 来源: kucoin.com / coincu.com / cryptotimes.io / predictionnews.com 四家转述同一批 Polymarket 开发者贴文。

**对我们的实际影响面 (已逐条核过, 不是推测)**:

| 面 | 结论 | 依据 |
|---|---|---|
| 层 A 采样池被 8 月加密补贴污染 | **否** | 采样 30 盘零加密盘 (referenced section) |
| 我们的实时价格 ws 受 RTDS 上线影响 | **否** | `unpublished research artifact` 用 `ws-subscriptions-clob.polymarket.com`, 与 RTDS 是两条链路 |
| Kalshi 移除错误响应 `service` 字段影响我们 | **否** | `kalshi_macro.py` / `prediction_market_kalshi_universe.py` 零引用该字段 |

**推断 (非事实, 未验证)**: 平台把额外奖励投向加密盘, 可能把做市资金从地缘政治盘吸走,
使我们采样的这批盘 8 月竞争变松、Q 份额与年化偏高。方向与"补贴抬高"相反, 但同样是偏差。
证伪路径 = 判决时看 8/7 前后我们采样盘的 Q 份额有无系统性抬升。

---

## 4. 研究面 — 一个能补 referenced section 已知空洞的公开数据集

referenced section/referenced section 留下的最大空洞: 无人区盘 (13/56 盘 Q 份额 ≥5%, 贡献 41% 奖励) 到底是免费午餐还是
毒性补偿 —— 当时只有 4 个已结算盘, n=4 什么都证明不了。

**arXiv 2606.04217v2 "Polymarket-v1 Database"** (http://arxiv.org/abs/2606.04217v2):
Polymarket 一代 CTF Exchange 全链上成交档案, 2022-11-21 → 2026-04-28, **12.0 亿笔成交 /
130 万个盘 / $610 亿名义额**, 定义性特征 = **100% 真值 aggressor 方向** (从链上结算层导出,
不是启发式推断)。

- **能补什么**: 用真值方向直接算"谁在吃谁", 对同类无人区盘做逆选择/毒性的外部对照, 正面回答
  那 41% 的性质。
- **补不了什么 (诚实边界)**: 它是**链上成交档案, 没有盘口深度快照**, 所以验不了我们的
  `queue_ahead` AUC 0.914 —— 队列结构仍然只有自采的 3 天 36,792 观测这一个来源。

**另两篇同期论文, 与我们已有判决互证**:
- arXiv 2606.31675 结算操纵实证: Polymarket 5 分钟 BTC 盘结算时点现货流激增 + 结算后大幅反转,
  操纵者利润主要来自散户; **15 分钟盘几乎没有** —— 拉长合约期就压住了。这与 8/7 上 TWAP 是
  同一件事的两面。
- arXiv 2607.26245 OpenMarket: 独立第三方拿 Binance 订单流对 Polymarket BTC 15 分钟盘做
  43 个微结构特征的走步逻辑回归, **样本外打不赢 Polymarket 自己盘口的隐含概率**, 模拟交易
  每笔 −0.116 归一化收益。这是我们 260621「crypto 阈值盘贴着期权面, 判 NULL」的一次
  **方法与数据都不同的独立复刻** ⟹ 这条不用重开。

---

## 5. 本次没有覆盖到的面

- **私域观测一律未读**: 程序层已硬拒, 本文不含任何私域内容。
- **未核到 Polymarket 官方对 8/7 结算切换与 $100 万奖励的第一手公告** (官方 changelog 停在 07-17,
  帮助中心未查)。所有相关数字标为二手多源一致。
- **未量化 8 月奖池的品类再分配对全站的影响** —— 需要跨品类奖励率快照, 我们的采样器只覆盖 top-30。
- **未验证 PREDICT 会议 (10/6–7, 纽约 Marriott Marquis) 的早鸟 8/7 截止说法**, 唯一来源是赞助商推文,
  官网未查到价目表。标 [未核实]。
- **未读层 A 采样器代码本身** (只读了它产出的数据), 所以"top-30 healthy 如何选盘"的选择逻辑
  没有独立核过 —— 事件族集中是不是选盘规则的必然产物, 待查。
