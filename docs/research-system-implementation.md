# 研究系统阶段 1 实现记录

更新时间：2026-11-09

本文件记录 `research-system-design.md` 阶段 1 的实际代码交付。当前实现是本地、离线、确定性的研究闭环，使用 CSV 收盘价数据，不连接交易所，也不执行真实下单。

## 已实现

- `research_system.py`：价格面板加载与校验、数据哈希、动量候选、相对偏离回归候选、等权基准、现金基准、目标仓位、单根数据延迟、换手成本、最大仓位限制、回测指标和 SQLite 记录。
- `research.py`：统一命令行入口，支持单策略或全部策略运行，并输出文本或 JSON 结果。
- `data/demo_prices.csv`：固定的三资产演示数据，包含趋势、反转和横盘阶段。
- `research_config.json`：第一轮实验配置，包括初始资金、5 日回看窗口、60% 单资产上限和 10 bps 交易成本。
- `test_research_system.py`：覆盖矩形数据校验、确定性哈希、延迟成交、成本扣除、数据库追溯和坏数据拒绝。

## 时间和成交口径

策略在日期 `t` 使用截至该日的收盘价计算信号，在日期 `t+1` 的收盘价执行目标仓位。历史数据中没有日内成交信息，因此不会把 `t` 的信号直接按 `t` 的价格成交。交易成本按换手名义金额乘以 `transaction_cost_bps / 10000` 扣除；未配置的资金保留为现金。

当前指标包括期末权益、总收益、年化收益、年化波动率、最大回撤、累计换手和再平衡次数。演示数据很短，年化结果只能用于验证计算链路，不能用于判断真实市场策略有效性。

## 运行

```powershell
python research.py
python research.py --strategy momentum --json
python research.py --data data/demo_prices.csv --config research_config.json --database research.sqlite3
python -m unittest -v
```

默认会把实验写入 `research.sqlite3`。该文件被 `.gitignore` 忽略，实验可以通过 `experiments`、`predictions`、`trades`、`portfolio_daily` 和 `metrics` 表追溯。实验 ID 由代码版本、输入数据哈希、配置和策略列表共同计算；相同输入会得到相同 ID 和相同结果。

## 当前边界

阶段 1 已接入 Binance 公共现货日线数据，当前数据文件包含 `BTCUSDT` 和 `SOLUSDT`，区间为 2021-01-01 至 2026-11-08，共 4,276 行。下载入口是 `download_history.py`，数据清单保存在 `data/btc_sol_daily.manifest.json`，其中记录来源和 SHA-256。下载器会排除当前 UTC 日尚未收盘的 K 线。缺失价格不会被填充，数据必须是完整的日期 × 资产矩形面板。

当前仍未接入基本面指标、复杂市场状态模型、参数搜索、候选版本替换或真实交易。相对偏离回归候选已经加入；下一步应在同一成本与风险约束下比较它与动量策略，并加入固定组合和分段样本对照。

## BTC 和 SOL 数据

```powershell
python download_history.py
python research.py --json
```

下载器使用 UTC 日线 K 线的收盘价，保存为 `date,asset,close` 长表。请求区间的结束日期是开区间；脚本默认请求到明天，因此只会写入接口已经返回的完整日线。重新下载后应检查 manifest 的 `sha256`、`start`、`end` 和 `rows`。
