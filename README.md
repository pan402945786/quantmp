# quantmp：策略组合研究与模拟交易

项目核心目标是检验依据当时信息挑选策略组合、持续积累扣除成本后收益的能力。后续开发以 [历史时钟驱动的策略组合研究与模拟实盘](docs/strategy-selection-replay.md) 为主线：受限历史数据 → 研究与内部验证 → 冻结版本 → 向前模拟 → 成熟结果复盘 → 调整组合或补充策略库。

当前已实现历史隔离、连续组合、成熟反馈选择器、有限候选研究，以及冻结协议、状态检查点、追加数据恢复和审计报告。阶段 A 到 E 首版及 F 工程首版已交付；截至 2026-11-12，103 项测试通过，三条冻结运行已续跑至当日 UTC 零点，v2 累积 1 个冻结后新增权益点。13 个统一起点／预算及成本情景审计通过，动态选择器仍缺少收益优势证据；详见 [最新验证](docs/validation-20261112.md) 和 [实现记录](docs/research-system-implementation.md)。独立盈利确认尚未完成。总体背景见 [研究系统设计](docs/research-system-design.md)。

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

仓位以扣费后权益为基数，买卖费用按实际净成交额计提，未投资资金保留为现金；权益序列包含首次决策时的初始资金点。回测要求日期连续的完整日线面板，缺日会报错。实验相同输入、相同结果重复保存不修改历史，结果冲突则拒绝覆盖；当前 `0.7.0` 版本还把实际运行源码摘要纳入实验 ID，保留旧版本记录。

日期 D 的收盘数据在 D+1 日 UTC 零点加发布延迟后释放；策略只收到此时可见的只读历史，不接触未来价格或全量文件摘要。新目标在下一根日线收盘执行，先处理旧订单，再形成新决策。可用时间延迟 `data_delay_seconds` 默认为 0，首版支持 `[0, 86400)` 秒。

`--start`、`--end` 为带时区的可用时间边界，包含边界上的释放事件；起点落在两次释放之间时，从下一次释放开始，起点前数据仅作价格预热。也可在配置中设置 `start_at`、`end_at` 和 `data_delay_seconds`。结束时不补读未来数据，`pending_orders` 表示尚待执行的目标数（含现金目标）。决策快照摘要和订单事件分别追加到 `decision_audit`、`order_events` 表。原单策略及固定组合入口不训练选择器，动态模式见下方。

## 固定策略组合与影子账

```powershell
python research.py --portfolio --config research_mix_config.json --json
python research.py --portfolio --config research_mix_config.json --budget momentum=0.4 --budget relative_reversion=0.4
python research.py --portfolio --config research_mix_config.json --start 2022-01-01T00:00:00Z --end 2022-02-01T00:00:00Z --data-delay-seconds 60
```

示例配置冻结动量与跌幅回归各 50% 的策略预算，默认写入 `research_mix.sqlite3`；这是工程对照方案，未经收益优选。`strategy_budgets` 的总额不得超过 100%，未列出的策略预算为零。重复的 `--budget 策略=比例` 参数会整体替换配置预算；总额小于 100% 时，余额留作现金。策略自身空仓也保留对应预算，不会把资金自动转给其他策略。`--portfolio` 与 `--strategy` 互斥。

每个时点只生成一次各策略信号，按策略预算乘以资产目标权重合并，检查单资产和总仓位，再与实际组合持仓比较形成净订单；策略间相反的调仓先抵消，手续费仅按最终净成交额计算。组合的现金、持仓和净值逐日连续，不把独立影子收益加总为组合收益。

输出中的 `role=shadow` 是各自使用相同初始资金、成本及执行规则的独立假设账户，零预算策略也持续记录；`role=portfolio` 才是固定组合的实际模拟资金账。原单策略入口使用 `role=standalone`。结果提供累计 `total_fees` 和逐日权益点现金比例均值 `average_cash_weight`（含初始点）；不同账户的风险暴露仍可能不同。

`account_roles` 保存账户角色，`allocation_decisions` 追加每次预算、各策略目标、资产权重贡献、现金目标、风险检查和可见证据。权重贡献表示目标暴露，不是收益归因。阶段 C 当轮 51 项测试及 BTC/SOL 对照结果见 [实现记录](docs/research-system-implementation.md)。

## 成熟反馈与动态选择器

```powershell
python research.py --selector --config research_selector_config.json --json
python research.py --selector --config research_selector_config.json --start 2022-01-01T00:00:00Z --end 2023-01-01T00:00:00Z --database research_selector_2022.sqlite3
```

`--selector` 与 `--portfolio`、`--strategy` 互斥，默认示例数据库为 `research_selector.sqlite3`。一次运行同时生成四个 `shadow`、一个 `benchmark` 固定组合和一个 `portfolio` 动态组合；初始资金、执行时间、成本和资产上限一致，资金账各自连续。`--budget` 在动态模式下只覆盖固定对照的预算，选择器预算由配置 `selector.strategy_budget` 控制。

示例规则 `purged-regime-mean-v1` 用当时资产回看收益均值的正负区分两种状态；只用成熟影子标签学习各状态采用动量、跌幅回归还是现金。默认收益期限 5 天、训练 180 天、验证 60 天、每 30 天研究一次，冻结后至少隔 1 天才生效，新决策仍延迟到下一根收盘执行。训练剔除跨验证边界的标签，按固定日历抽取不重叠持有期样本，验证不重新拟合；样本不足时等待，验证失败则冻结现金回退版本。参数是本次工程假设，未按本轮结果调优。

标签描述策略持续影子运行的扣费后区间收益，不是假定单笔买入持有，也不是实际动态组合收益。结束时未成熟标签保持待定，JSON 中的 `selector.pending_feedback` 可查看数量。`selector_events` 追加标签登记／成熟、拒绝研究、版本冻结／生效事件，`allocation_decisions` 保存每次实际采用的已生效版本、状态和预算理由。起点之前只预热价格，不回填影子反馈。

阶段 D 当轮 65 项测试和 BTC/SOL 全区间核验通过。首版动态组合从 100,000 运行至 77,606.27，净收益 **-22.39%**、最大回撤 **-49.68%**、平均现金比例 **93.09%**；同区间固定组合净收益为 388.25%。这组规则未证明优于固定组合或现金，版本与失败记录已保留。内部验证分数只是研究代理，风险预算匹配及冻结后新增数据确认仍待后续完善。

## 有限预算研究与候选确认

```powershell
python research.py --research --config research_loop_config.json --json
python research.py --research --config research_loop_config.json --start 2022-01-01T00:00:00Z --end 2023-01-01T00:00:00Z --database research_loop_2022.sqlite3
```

`--research` 与其他运行模式互斥，示例结果保存到 `research_loop.sqlite3`。输出保留原来的四个 `shadow`，固定组合和原选择器均为 `benchmark`；`adaptive_mix` 是带研究改进的实际模拟资金账。通过内部验证后才新增 `candidate_shadow`，从冻结后的日期起独立记录，不回填历史。

示例计划只研究动量和跌幅回归的 20 日窗口版本，总计最多尝试 2 次、每轮最多 1 次。原选择器连续 60 天留现金且有足够成熟证据时，才在预定研究周期检查是否启动。研究使用当前可见数据的 180 天训练及 60 天验证，记录全部假设和失败；用完预算后不会扩大搜索或重试失败版本。参数和候选定义是事后工程假设，未据本轮收益优选。

候选在训练和验证中达到预设净收益、回撤及相对父策略门槛后冻结，至少隔 1 天开始前向影子运行。默认观察至少 90 天、使用至少 12 个互不重叠的成熟标签，与父策略配对比较；确认失败或超时则拒绝。确认通过后再延迟至少 1 天生效，原选择器选中对应父策略时才替换该位置。原选择器不会重新拟合整个扩充库，其他位置不变；失效候选按预先声明的收益／回撤条件延迟退出。全部交易继续经过资产目标合并和净成交计费。

`research_events` 追加假设、尝试结果、预算耗尽、候选冻结／影子／确认／生效／退出及其证据。`research.internal_validation_rejected` 显示内部验证淘汰数，`effective_replacements` 只列当前生效替换，未确认候选不可出现在实际目标中。影子确认使用的标签仍是研究代理，实际成绩来自 `adaptive_mix` 的连续资金账。

阶段 E 当轮 82 项测试通过，完整替换、退出及资金连续路径由合成算例验证。BTC/SOL 全区间的两个候选均未通过内部验证，没有进入影子确认；组合保持原选择器，净收益仍为 **-22.39%**。失败记录与预算耗尽状态已保存，尚无研究改进带来收益增量的证据。

## 当前优化实验（2026-11-11）

上一版动态选择器在历史回放中长期退回现金，实际组合净收益为 -22.39%。根据分段对照，新增 [paper_v2_config.json](paper_v2_config.json)：采用 20 日动量、50% 动量预算和 50% 现金；选择器只把动量和等权作为候选，单次选中策略的预算上限为 50%。优化依据和分段结果见 [优化记录](docs/optimization-20261111.md)。

启动新的研究纸面运行：

```powershell
New-Item -ItemType Directory -Force runs/paper-v2
python replay.py start --config paper_v2_config.json --mode research --until 2026-11-11T00:00:00Z --checkpoint runs/paper-v2/checkpoint-20261111.json --database runs/paper-v2/frozen.sqlite3 --report-dir runs/paper-v2/reports
```

固定预算组合可作为主控制账户运行：

```powershell
python replay.py start --config paper_v2_config.json --mode portfolio --until 2026-11-11T00:00:00Z --checkpoint runs/paper-v2/portfolio-checkpoint-20261111.json --database runs/paper-v2/portfolio.sqlite3 --report-dir runs/paper-v2/portfolio-reports
```

后续每日更新数据后，从相应检查点使用 `replay.py resume`，每次写入新的检查点文件。

## 继续验证（2026-11-12）

已更新至 2026-11-11 完整日线，并核实此前价格不变。最新固定组合检查点为 `runs/paper-v2/portfolio-checkpoint-20261112.json`，研究组合为 `runs/paper-v2/checkpoint-20261112.json`。原始 v2 历史权益分别为 636,939.79、158,264.37，首个冻结后新增日收益均为 0；这两个余额包含历史回放，并非今天新建的 100,000 账户。30 日候选已失败且尝试预算耗尽，不会自动进入 90 日确认。后台每日调度尚未配置。

新增统一起点和 50% 策略预算的验证工具，比较 5 日动量、20 日动量、等权、动态研究及现金，并检查 10／25／50 bps 成本：

```powershell
python validate_paper.py --end 2026-11-12T00:00:00Z --output runs/validation-new
```

输出目录必须为空或不存在。完整报告留在本机，Git 中保留 [验证记录](docs/validation-20261112.md) 与 [指标和协议摘要](docs/validation-20261112.json)。同起点下动态组合仍亏损；20 日动量在 25 bps 下最近一段也转为亏损。相同预算尚不等于实际波动匹配，所有历史比较均为事后诊断，当前保持冻结配置继续积累新数据。

## 冻结协议、保存与恢复

```powershell
python replay.py start --config research_loop_config.json --until 2022-01-01T00:00:00Z --checkpoint runs/checkpoint-2022.json
python replay.py resume --from runs/checkpoint-2022.json --checkpoint runs/checkpoint-latest.json
python replay.py report --from runs/checkpoint-latest.json
```

`start` 在处理决策前冻结解析后的参数、执行／选择／研究规则、源码 SHA-256、Python 环境、资产池及初始数据指纹。默认 `--mode research`，也支持 `standalone`、`portfolio`、`selector`；独立模式可重复指定 `--strategy`。`--until` 只控制暂停点，不修改协议中的起止参数，且不能越过真实时钟处理尚未可用的日线。

检查点保存现金、持仓、未成交目标、未成熟反馈、选择器版本、候选状态、研究预算及已有日志。`resume` 直接恢复这些状态，只重建已释放价格视图，不重算过去的策略决策。恢复时校验代码、Python 环境、参数和先前已知的全部价格；既有日期、资产或价格变化会拒绝继续，只接受同一资产池追加完整日线。数据放到新路径时指定 `--data 路径`，之后检查点记住该路径。

检查点及报告不可覆盖，续跑请使用新文件名；同一数据库也会拒绝从一个父检查点悄悄生成两条不同分支，应接着最新检查点运行。没有新事件时保持原检查点和日志不变。默认使用独立的 `research_frozen.sqlite3`，以不可变修订追加完整快照，保留已有实验记录。JSON 是受限类型的状态格式，不加载 pickle 或任意类。

每次保存同时在检查点旁归档九个运行源码文件。以后修改代码会使当前程序拒绝恢复旧运行，可使用归档的 `engine-摘要/replay.py` 和原 Python 环境继续旧版本。归档不包含价格文件，数据文件仍须按指纹保留。`runs/` 已被 Git 忽略。

审计报告位于 `runs/reports`（可用 `--report-dir` 修改），包括 Markdown、JSON 与完整权益 CSV。报告独立核对成交时序、费用、持仓、现金、收益指标、标签成熟及版本可用时间，并提供逐月收益、回撤／亏损持续天数、切换次数、资产和版本的账务贡献。账务贡献不等于反事实策略优势。

BTC/SOL 从 2021 年末检查点补入后续 1,773 天历史数据，恢复结果与完整回放逐项一致；8,508 个成熟标签及 24 个待定标签通过审计。报告当前仍为 `awaiting_post_freeze_rows`：只有初始快照之外、且可用时间晚于真实冻结时间的已处理新行，才进入单独的新增数据区间。重新下载旧日期、截断历史再补回和恢复运行都不构成独立盈利确认；CSV 的真实发布、采集和修订历史仍未接入。

## 接入 BTC 和 SOL 历史数据

使用 Binance 公共现货 K 线接口下载 UTC 日线收盘价。默认从 2021-01-01 下载 `BTCUSDT` 和 `SOLUSDT`，写入 `data/btc_sol_daily.csv`，同时生成同名 `.manifest.json` 记录数据来源、请求区间、行数和 SHA-256。

```powershell
python download_history.py
python research.py --data data/btc_sol_daily.csv --json
```

`research_config.json` 已将研究数据默认切换为 `data/btc_sol_daily.csv`。本仓库包含当前下载的数据文件和 manifest，便于克隆后直接运行；需要刷新数据时重新运行下载脚本，并检查 manifest 中的数据哈希。
