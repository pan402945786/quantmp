# quantmp：策略组合研究与模拟交易

项目核心目标是检验依据当时信息挑选策略组合、持续积累扣除成本后收益的能力。后续开发以 [历史时钟驱动的策略组合研究与模拟实盘](docs/strategy-selection-replay.md) 为主线：受限历史数据 → 研究与内部验证 → 冻结版本 → 向前模拟 → 成熟结果复盘 → 调整组合或补充策略库。

当前已实现价差监控、BTC/SOL 数据下载、四个策略的独立影子账和固定策略组合。阶段 A、B、C 分别完成资金账务、历史时钟隔离与连续组合；验证范围见 [实现记录](docs/research-system-implementation.md)。下一步是阶段 D 的选择器、成熟反馈和版本生效管理，研究改进循环仍待实现。总体背景见 [研究系统设计](docs/research-system-design.md)。

## 跨交易所现货价差监控

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

按照 [实现记录](docs/research-system-implementation.md)，项目现在包含一个本地、离线、可复现的历史回放入口。默认使用 BTC/SOL 历史数据，也可指定 `--data data/demo_prices.csv` 使用演示面板；比较简单动量、跌幅回归候选（`relative_reversion`）、等权和现金基准，按 `t` 生成信号、在 `t+1` 执行，并扣除换手交易成本。当前回归候选尚未剔除市场影响。

```powershell
python research.py
python research.py --strategy momentum --json
python research.py --start 2022-01-01T00:00:00Z --end 2022-02-01T00:00:00Z --json
python research.py --data-delay-seconds 60 --database research_delay.sqlite3 --json
python -m unittest -v
```

研究结果写入 `research.sqlite3`，其中保存实验配置和数据哈希、每次预测、交易、每日组合权益及指标。该数据库文件默认被忽略，不会提交到 Git。设计依据和当前边界见 [研究系统设计](docs/research-system-design.md) 与 [阶段 1 实现记录](docs/research-system-implementation.md)。

仓位以扣费后权益为基数，买卖费用按实际净成交额计提，未投资资金保留为现金；权益序列包含首次决策时的初始资金点。回测要求日期连续的完整日线面板，缺日会报错。实验相同输入、相同结果重复保存不修改历史，结果冲突则拒绝覆盖；当前 `0.4.0` 版本会生成新的实验 ID，保留旧版本记录。

日期 D 的收盘数据在 D+1 日 UTC 零点加发布延迟后释放；策略只收到此时可见的只读历史，不接触未来价格或全量文件摘要。新目标在下一根日线收盘执行，先处理旧订单，再形成新决策。可用时间延迟 `data_delay_seconds` 默认为 0，首版支持 `[0, 86400)` 秒。

`--start`、`--end` 为带时区的可用时间边界，包含边界上的释放事件；起点落在两次释放之间时，从下一次释放开始，起点前数据仅作预热。也可在配置中设置 `start_at`、`end_at` 和 `data_delay_seconds`。结束时不补读未来数据，`pending_orders` 表示尚待执行的目标数（含现金目标）。决策快照摘要和订单事件分别追加到 `decision_audit`、`order_events` 表。这个入口尚未训练选择器或运行策略扩充循环。

## 固定策略组合与影子账

```powershell
python research.py --portfolio --config research_mix_config.json --json
python research.py --portfolio --config research_mix_config.json --budget momentum=0.4 --budget relative_reversion=0.4
python research.py --portfolio --config research_mix_config.json --start 2022-01-01T00:00:00Z --end 2022-02-01T00:00:00Z --data-delay-seconds 60
```

示例配置冻结动量与跌幅回归各 50% 的策略预算，默认写入 `research_mix.sqlite3`；这是工程对照方案，未经收益优选。`strategy_budgets` 的总额不得超过 100%，未列出的策略预算为零。重复的 `--budget 策略=比例` 参数会整体替换配置预算；总额小于 100% 时，余额留作现金。策略自身空仓也保留对应预算，不会把资金自动转给其他策略。`--portfolio` 与 `--strategy` 互斥。

每个时点只生成一次各策略信号，按策略预算乘以资产目标权重合并，检查单资产和总仓位，再与实际组合持仓比较形成净订单；策略间相反的调仓先抵消，手续费仅按最终净成交额计算。组合的现金、持仓和净值逐日连续，不把独立影子收益加总为组合收益。

输出中的 `role=shadow` 是各自使用相同初始资金、成本及执行规则的独立假设账户，零预算策略也持续记录；`role=portfolio` 才是固定组合的实际模拟资金账。原单策略入口使用 `role=standalone`。结果提供累计 `total_fees` 和逐日权益点现金比例均值 `average_cash_weight`（含初始点）；不同账户的风险暴露仍可能不同。

`account_roles` 保存账户角色，`allocation_decisions` 追加每次预算、各策略目标、资产权重贡献、现金目标、风险检查和可见证据。权重贡献表示目标暴露，不是收益归因。当前 51 项测试及 BTC/SOL 对照结果见 [实现记录](docs/research-system-implementation.md)。

## 接入 BTC 和 SOL 历史数据

使用 Binance 公共现货 K 线接口下载 UTC 日线收盘价。默认从 2021-01-01 下载 `BTCUSDT` 和 `SOLUSDT`，写入 `data/btc_sol_daily.csv`，同时生成同名 `.manifest.json` 记录数据来源、请求区间、行数和 SHA-256。

```powershell
python download_history.py
python research.py --data data/btc_sol_daily.csv --json
```

`research_config.json` 已将研究数据默认切换为 `data/btc_sol_daily.csv`。本仓库包含当前下载的数据文件和 manifest，便于克隆后直接运行；需要刷新数据时重新运行下载脚本，并检查 manifest 中的数据哈希。
