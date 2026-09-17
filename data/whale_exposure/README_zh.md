> Public archive: address and label fields are omitted; numerical rows are unchanged. The historical instrument and editorial materials named below are not distributed.

# 巨鲸敞口名次 v1↔v2 对照版 (260813 技术裁定批落地)

- 数据: [260813_whale-exposure-v1v2-comparison.json](whale_exposure_v1v2_comparison_2026-08-13.json)
  (= `unpublished aggregate source` 的 research 落点, 298 行)
- 仪器: `historical holding-exposure instrument (not distributed)` (selftest 15 项; B4 用它独立重实现敞口公式,
  与 v1 名次 Spearman ρ=0.835)
- 裁定 (260813 技术裁定批 D 节): **出对照版, 不替换绝对名次** —— v1 的敞口公式随 session
  scratchpad 消失, 本对照是重新实现的; 干净可信的是**同一公式下只换封口规则的新旧对照**,
  不是绝对名次。300 人的入选资格完全不受影响 (资格由闸定, 不由排序键定)。

## 唯一变量与结果

唯一变量 = 持仓封口时间: v1 用 `endDate` (被远期占位值与提前结算双向污染, `closedTime − endDate`
中位 0.0 天但尾部 −638 ~ +245 天), v2 用真实结算时间 (`closedTime`/`umaEndDate`, 不早于最后成交)。

| 量 | 值 |
|---|---|
| 敞口变小 / 变大 | 248 / 49 (中位比值 0.556) |
| 名次挪动中位 | 25 名 |
| 挪动 ≥50 名 | 31.5% |
| 新旧 top-10 重叠 | 4 |

敞口是积分量, 尾巴主导排序 —— 两条尾巴 (469 个市场提前 ≥30 天结算; 199 个延后 ≥7 天) 就是
名次大挪动的全部来源。

## 引用纪律

对外引用本对照时: 数字必须与「资格不受影响 / top 段仍是真实交易者」的对偶面同句;
更正稿 draft 见验收包 260812_positioning-alignment-audit.md (unpublished editorial material),
发不发由维护者决定。
