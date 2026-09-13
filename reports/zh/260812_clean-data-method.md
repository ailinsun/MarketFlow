> Archive note: original working report; measurements are unchanged. Unavailable internal references are plain text. See the report index for the surviving public evidence and reproduction limits.

# 260812 Clean data 可复算方法

## 输入与版本

- 输入：`unpublished research artifact`
- 分类器：`public instrument polymarket_farm_filter.py`
- 产物：`farm_wallets.json`、`candidates.json`、`market_heat.json`
- 公网页面数据：`unpublished research artifact`

## 单行链路

`public trades → exact-size signature scan → farm_flag → raw/clean ranking comparison → JSON snapshot → preview page`

## 可复算字段

每个新 `large_print_flow` 行固定带布尔 `farm_flag`。历史行在生成站点快照时，使用同一个带 `generated_at_utc` 的 `farm_wallets.json` 回填该字段。

原始榜按 `buy_usd`；干净榜删除 farm wallet，要求观察寿命至少 7 天、市场数至少 3，再按 `n_trades × n_markets` 排序。`clean_data.json` 同时保留源生成时间、样本行数、钱包数、两份 top list 与 overlap。

## 限制

过滤器只标注公共流水中的重复模式；不推断控制关系、身份或未来表现。feed 只覆盖采集门槛以上的成交打印，因此结论只适用于该样本框。
