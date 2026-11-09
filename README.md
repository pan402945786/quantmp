# 跨交易所现货价差监控

策略研究系统的设计与实施交接见 [quantmp：策略研究、组合运行与持续改进设计](docs/research-system-design.md)。该文档记录后续系统规划，下面描述当前已实现的价差监控工具。

这是一个只读的研究工具，通过公开 REST 订单簿比较 Binance、Kraken 和 Coinbase Exchange 的现货价格。无需 API 密钥，也不会下单。适用于 Python 3.10 及以上，不需要安装第三方库。

## 快速开始

在项目目录 PowerShell 中运行：

```powershell
python arbitrage.py --demo --once
python -m unittest -v
python arbitrage.py --once
python arbitrage.py
```

`--demo` 使用固定虚构盘口和内存数据库，用于无网络检查程序，不会写入真实记录。真实模式需要能够访问各交易所公开 API；某些地区或网络可能不可用。按 Ctrl+C 停止。可以用 `--config 路径` 指定其他配置。

## 配置

修改 `config.json`：`base`、`quote` 是记录用的资产名称；`symbol` 必须由使用者核实代表相同的资产和计价币，程序不会验证。每所的 `taker_fee` 是你账户的单边吃单费率，例如 `0.001` 为 0.1%；示例费率只是占位值，不代表交易所当前收费。`quantity` 是基础资产数量；`rebalance_cost_quote` 是每次机会分摊的库存再平衡成本，单位为计价币；`min_net_profit_quote` 是信号最小净收益。默认数据库是当前工作目录中的 `arbitrage.sqlite3`。

每次并发获取盘口，按数量逐档计算买卖均价，扣除两边手续费及再平衡预估，计算净利润。结果包括亏损方向；只有净利润为正且达到门槛才显示 `SIGNAL`。数据库 `observations` 保存所有通过新鲜度和深度检查的方向、费用和时间指标，可用 SQLite 工具分析。

`max_age_seconds` 限制收到响应后到比较时的本地时间，同时限制每次请求耗时；`max_skew_seconds` 限制两份响应收到时间之差。请求延迟单独记录；它们不能证明交易所数据的生成时间同步，也不能保证行情在下单时仍然有效。部分市场或交易对可能不支持、暂停、存在地区限制或使用不同名称；失败会打印 WARN，其他交易所仍可比较。

## 使用边界

这是盘口快照估算，不是真实成交或可实现利润。模型假设两边可以同时按快照全部吃单、卖出侧预先持币，未验证库存、最小下单量、数量精度、充提状态、额外市场冲击、单边成交、资金占用、税费和交易所信用风险。`rebalance_cost_quote` 必须按实际情况估算；币价和库存风险不一定能用固定成本表示。价格中的 USDT 与 USD 等不同计价币不能直接比较。不要基于信号直接实盘。

## 研究闭环（阶段 1）

按照 [研究系统阶段 1 实现记录](docs/research-system-implementation.md)，项目现在包含一个本地、离线、可复现的研究入口。它使用 `data/demo_prices.csv` 的矩形价格数据，比较简单动量、相对偏离回归、等权和现金基准，按 `t` 生成信号、在 `t+1` 执行，并扣除换手交易成本。

```powershell
python research.py
python research.py --strategy momentum --json
python -m unittest -v
```

研究结果写入 `research.sqlite3`，其中保存实验配置和数据哈希、每次预测、交易、每日组合权益及指标。该数据库文件默认被忽略，不会提交到 Git。设计依据和当前边界见 [研究系统设计](docs/research-system-design.md) 与 [阶段 1 实现记录](docs/research-system-implementation.md)。

## 接入 BTC 和 SOL 历史数据

使用 Binance 公共现货 K 线接口下载 UTC 日线收盘价。默认从 2021-01-01 下载 `BTCUSDT` 和 `SOLUSDT`，写入 `data/btc_sol_daily.csv`，同时生成同名 `.manifest.json` 记录数据来源、请求区间、行数和 SHA-256。

```powershell
python download_history.py
python research.py --data data/btc_sol_daily.csv --json
```

`research_config.json` 已将研究数据默认切换为 `data/btc_sol_daily.csv`。本仓库包含当前下载的数据文件和 manifest，便于克隆后直接运行；需要刷新数据时重新运行下载脚本，并检查 manifest 中的数据哈希。
