> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.

# 规则错价发现器 首刀验证 — well-powered NULL (方向反转)

**日期** 2026-08-12 · **工具** `unpublished research artifact` · **口径** read-only, 0 触 money surface

## 一句话

把 `settlement_guard` 无方向的 gotcha 摘录升级成"从 auto-resolve fallback 方向读出哪一侧被高估"的
主动 offense —— 拿 532 个已结算政治/事件盘 (带结算前价) 验证, **跟随该信号费后 ROI = −35.32%,
CI95 [−49.76%, −18.54%] (n=159, well-powered), 且方向是反的**。判 NULL, 不接入场。

## 数据与方法

- 样本 = FLB backtest 已结算盘 (politics 267 / mention_culture 235 / other 87 = 589), 补采 gamma
  规则原文 532 个 (`closed=true` 精确匹配 conditionId), 结算前 Yes 价用生命周期中点 nt-0.5 (look-ahead safe)。
- 每盘: 规则原文 → 方向 (auto-No fallback ⟹ 判 Yes 高估 ⟹ 买 No); edge 侧买入价 ∈ [0.03, 0.90]
  才计入 (排 dust 与近确定)。费 = `rate·p·(1−p)` 每股 (rate=0.05, first-principles C3)。
- 213 个有方向信号, 159 个可交易。bootstrap 2000 次 CI。独立盘, 非重叠, 简单重采样合法。

## 结果

| 口径 | n | 费后 ROI | CI95 | win |
|---|---:|---:|---|---:|
| **跟随 rule-edge (买"低估"对侧)** | 159 | **−35.32%** | [−49.76%, −18.54%] | 38.4% |
| 反买 naive 侧 (镜像) | 159 | +14.48% | — | — |
| — triggered_only (样板, 权重2) | 158 | −34.90% | [−49.60%, −17.64%] | 38.6% |
| — auto_resolve (强, 权重3) | **1** | — | 无 power | — |
| — edge=NO | 157 | −36.94% | [−51.93%, −19.81%] | 37.6% |
| — politics | 35 | **−71.43%** | [−93.88%, −41.67%] | 17.1% |
| — mention_culture | 104 | −25.05% | [−44.68%, −3.90%] | 42.3% |

**校准直接反证假设**: No-fallback 组结算前 Yes 价 **0.522** vs 实际胜率 **0.624** → Yes 被**低估** 0.102,
不是我假设的高估。edge 侧买入价中位 0.470 (非 dust, 是真中价位盘在亏)。

## 机制 (为什么反转) — 印证 first-principles

"到期没发生即 resolve to No" 是几乎所有"Will X happen?"盘的**样板条款**, 不是隐藏陷阱。
正则在市场定义上过度触发, 拿一个**不识别真错价**的信号下注 = 纯付摩擦 + 逆选efficient盘的对侧。
这正是 [first-principles](260804_predmkt-first-principles.md) 的会计恒等式在信号层的复现:
**再"聪明"的信号, 只要不携带真错价信息, 费后必负。** naive 侧 +14.48% 是价位×费率几何的镜像
伪影 (买贵favorite), 非验证 edge —— 未做 OOS, 不作任何"买 naive 侧有 edge"的声明。

## 判决与 redirect

- **cheap 版 (fallback-direction 正则) 判 NULL 且反向** —— 不部署为 watch, 不接入场链。
- **NULL = 换路信号, 非死刑**: 规则不对称的真 edge (若存在) 藏在**极稀有的"标题 intent 与规则口径
  真矛盾"**里 (非样板 fallback), 需要**语义级读取** (LLM 比对 headline 意图 vs 规则文本, 判真分歧)
  才能与样板区分 —— 是更难的 build, 不是便宜正则。强信号 auto_resolve 在结算集 n=1、活跃盘约 10/60,
  太稀有, 本刀无法判其死活。
- **规则不对称已验证的用法是防御**: `settlement_guard` 在结算时抓真 gotcha 保护持仓 —— 那条不受本 NULL 影响。
- 保留模块 (read-only 工具 + 验证 harness 可复用); docstring 已记 NULL 防同法重跑。

## 边界

- 样本期 = FLB 260707 抓取窗, top 分层抽样, 政治/事件类。crypto/sports 类未纳入 (方向语义不同)。
- Yes 偏低估可能是这些市场类的已知 favorite/yes-bias, 与本信号无关 —— 本刀只证伪"fallback 方向 = edge"。
- 费用为上界 (BUY=taker 假设), 但价位间相对结构不受影响。
