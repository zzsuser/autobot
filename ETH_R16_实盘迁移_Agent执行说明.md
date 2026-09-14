# ETH 15分钟插针策略R16实盘迁移——Agent详细执行说明

## 0. 任务目标

在现有`autobot`自动交易框架中，保留行情、账户、OKX接口、日志和进程管理能力，**完全替换原SuperTrend + TEMA v3.1的策略逻辑**，接入经过回测审计的ETH 15分钟插针策略R16。

本任务不是在旧策略上增加一个R16过滤器，也不是让两套策略同时决定开平仓。最终运行时只能有一套信号源：

```text
EthSpikeR16Strategy
```

必须使用随本说明提供的文件：

```text
eth_spike_r16_strategy.py
test_eth_spike_r16_strategy.py
```

旧文件`supertrend_tema_strategy_v3_1(1).py`只用于理解`StrategyBase`接口，不得覆盖，必须保留回退副本。

---

# 1. 已确认的旧脚本性质

上传的`supertrend_tema_strategy_v3_1(1).py`不是完整交易机器人，而是`autobot`框架中的策略类。它依赖：

```python
from autobot.core.strategy_base import StrategyBase, SignalResult
from autobot.utils.logger import logger, log_trade
```

文件本身不包含：

- OKX API客户端；
- K线拉取服务；
- 策略注册器；
- 订单执行器；
- 仓位同步；
- 合约规格查询；
- TP/SL或OCO订单管理；
- 进程启动入口；
- 环境变量文件；
- 数据库或持久化状态文件。

因此，不能只修改上传文件就宣称实盘迁移完成。Agent必须在实际部署仓库中找到上述框架代码并完成适配。

---

# 2. R16冻结参数

以下是本轮唯一允许使用的策略参数，不得自行优化、扩大或替换。

| 类别 | 参数 | 冻结值 |
|---|---|---:|
| 周期 | K线周期 | 15分钟 |
| BOLL | 周期 | 18 |
| BOLL | 标准差倍数 | 2.5 |
| BOLL | 当前插针区域 | 严格强区/弱区 |
| 插针 | 实体占整根比例 | ≤20% |
| 插针 | 主影线/实体比例 | 2.0 |
| 插针 | 小实体主影线/对侧影线 | 2.0 |
| 插针 | 突破回看 | 4根 |
| 插针 | 当前K线最小振幅 | 0.3% |
| 前置走势 | 最少趋势根数 | 3根 |
| 前置走势 | 最大总回看 | 4根 |
| 前置走势 | 累计涨跌幅 | ≥0.8% |
| 前置走势 | 末根反向回撤 | ≤0.4% |
| 横盘 | 配置值 | 最多3根 |
| 横盘 | 在当前3～4根窗口下的实际有效上限 | 最多1根 |
| 横盘 | 必须保持BOLL区域 | 是 |
| 横盘 | 不创新高/低 | 是 |
| 方向 | 允许做多/做空 | 都允许 |
| 入场 | 同方向最多入场 | 3次 |
| 退出 | TP | 0.6% |
| 退出 | SL | 0.9% |

## 2.1 BOLL区域的精确定义

做空所需强区：

```text
basis <= close <= upper
```

做多所需弱区：

```text
lower <= close <= basis
```

不能擅自放宽为“收盘价只要在中轨上方/下方”。

## 2.2 横盘参数的真实含义

虽然`max_sideways=3`，但同时存在：

```text
min_pre_move_bars=3
max_pre_lookback_bars=4
```

趋势与横盘共用4根窗口，因此合法组合只有：

```text
4趋势 + 0横盘
3趋势 + 0横盘
3趋势 + 1横盘
```

不得为了“真的允许3根横盘”而把最大回看改为6；那会变成未经本轮验证的新策略。

---

# 3. 旧策略与R16的迁移映射

| 旧策略组件 | 处理方式 | R16对应 |
|---|---|---|
| `calc_tema()` | 删除策略依赖 | 不使用TEMA |
| `calc_supertrend()` | 删除策略依赖 | 不使用SuperTrend |
| BTC TEMA288过滤 | 删除 | R16不需要BTC数据 |
| `_check_open_conditions()` | 整体替换 | `add_r16_signals()` |
| `_check_close_conditions()` | 停用 | 由交易所驻留TP/SL退出 |
| 5分钟K线 | 改为15分钟 | 只处理已收盘15分钟K线 |
| `required_data_length=600` | 改为64 | 64根闭合K线足够且留有缓冲 |
| 仅空仓检查开仓 | 必须修改 | 持仓中仍可同向加仓，最多3次 |
| 冷却10根 | 删除 | R16没有冷却参数 |
| 最小持仓3根 | 删除 | TP/SL挂出后可在任意时刻触发 |
| 原趋势平仓P1～P9 | 全部停用 | 固定TP 0.6%、SL 0.9% |
| 原2.5%止盈等参数 | 全部停用 | 不得混入R16 |

---

# 4. 必须先定位的框架文件

进入真实部署仓库后，先运行：

```bash
rg -n "class StrategyBase|class SignalResult|generate_signal" .
rg -n "create_order|place_order|order-algo|attachAlgo|take.profit|stop.loss|tpTriggerPx|slTriggerPx" .
rg -n "current_position|entry_price|open_entries|position_history|avgPx|posSide|tdMode" .
rg -n "strategy.*registry|strategy.*factory|SuperTrendTemaStrategyV3_1|supertrend_tema_v3_1" .
rg -n "OKX|API_KEY|SECRET|PASSPHRASE|simulated|demo|flag" .
rg -n "5m|bar_minutes|timeframe|candles|confirm" .
```

必须形成文件清单，至少定位：

1. `StrategyBase`和`SignalResult`定义；
2. 调用`generate_signal()`的主循环；
3. 策略工厂或注册配置；
4. OKX下单函数；
5. 查询真实持仓、持仓均价和订单的函数；
6. TP/SL保护单创建、撤销、修改函数；
7. 合约规格和账户模式初始化；
8. 环境变量或配置文件；
9. 持仓状态持久化位置；
10. 启动命令与守护进程配置。

如果缺少任何一项，不得猜测接口。把缺失项记录到`R16_MIGRATION_BLOCKERS.md`并停止实盘启用，但可以继续完成策略层离线Gate。

---

# 5. 环境与账户参数

## 5.1 策略参数与环境参数必须分离

`R16SignalConfig`只保存冻结的信号参数。以下内容必须由实盘环境配置，不得硬编码进策略文件：

- API Key、Secret、Passphrase；
- 模拟盘/实盘开关；
- 交易品种；
- 账户模式；
- 仓位模式；
- 杠杆；
- 单次开仓金额；
- 合约面值、最小下单量、步长、价格tick；
- 网络重试次数和超时；
- 状态数据库路径；
- 告警渠道；
- 保护单失败处置方式。

## 5.2 建议的标准配置名

如果原项目已有等价配置名，优先映射原有名称；不得并存两套冲突配置。

```dotenv
TRADING_ENV=demo
LIVE_TRADING_ENABLED=false

OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=
OKX_INST_ID=ETH-USDT-SWAP
OKX_TD_MODE=isolated
OKX_POSITION_MODE=net_mode

STRATEGY_NAME=eth_spike_r16_15m
STRATEGY_TIMEFRAME=15m
STRATEGY_LOG_TIMEZONE=Asia/Shanghai

LEVERAGE=
ENTRY_SIZING_MODE=fixed_margin
FIXED_ENTRY_MARGIN_CASH=
ENTRY_EQUITY_PCT=
MAX_SAME_DIRECTION_ENTRIES=3

PROTECTION_TIMEOUT_SECONDS=2
PROTECTION_FAIL_ACTION=close_position
STATE_DB_PATH=
```

注意：

- `LEVERAGE`和开仓金额不在本文件中给默认实盘值，必须由用户确认；
- 初始部署必须为`TRADING_ENV=demo`和`LIVE_TRADING_ENABLED=false`；
- 禁止在代码、日志、Markdown或Git中写入真实密钥；
- 日志只能显示Key后4位或完全不显示。

## 5.3 启动时必须从OKX动态读取

每次进程启动后，必须查询并记录：

- `instId`；
- `ctVal`和`ctValCcy`；
- `lotSz`；
- `minSz`；
- `tickSz`；
- 最大允许杠杆；
- 当前账户模式；
- 当前仓位模式；
- 当前该合约杠杆；
- 当前真实持仓；
- 当前未完成普通订单；
- 当前未完成条件单/策略单。

本轮回测曾使用的合约换算值不能直接当作永久常量。若交易所返回值与本地配置不一致，必须停止开仓。

---

# 6. 15分钟行情要求

## 6.1 只计算已收盘K线

`generate_signal()`必须收到：

```python
bar_is_closed=True
```

调用者只有在确认OKX的15分钟K线已经闭合后才能传True。不得用正在形成的K线计算信号。

建议在理论收盘后延迟2～5秒拉取，并检查交易所返回的闭合标志。若数据源没有闭合标志，则至少要求：

```text
当前服务器时间 >= bar_start + 15分钟 + 安全延迟
```

## 6.2 DataFrame契约

```text
index: DatetimeIndex，表示15分钟K线开始时间
columns: open, high, low, close
排序: 时间升序
重复时间: 0
最近64根NaN: 0
```

`volume`可以存在但不是R16信号必需字段。

## 6.3 时间口径

内部建议统一使用UTC；日志可转换为`Asia/Shanghai`。无论采用哪种方式，必须保证：

- 同一根K线只有一个唯一时间ID；
- 不把K线开始时间和成交时间混为同一字段；
- `signal_bar_start`与`signal_fill_time`分开保存；
- 15分钟收盘成交时间通常等于`signal_bar_start + 15分钟`。

---

# 7. 信号与订单状态机

## 7.1 生成信号时必须传递的运行状态

```python
result = strategy.generate_signal(
    df=closed_15m_df,
    current_position=current_position,  # -1/0/1
    open_entries=open_entries,           # 当前持仓周期已成交入场次数
    bar_is_closed=True,
)
```

`open_entries`不是合约张数，必须是本轮持仓周期已完成的入场批次：1、2或3。

旧框架如果只保存净持仓数量、没有保存入场次数，必须新增持久化字段，不能通过仓位大小反推。

## 7.2 空仓信号

```text
空仓 + LONG  -> 开多第1笔
空仓 + SHORT -> 开空第1笔
```

入场成交后立刻：

1. 查询交易所真实持仓；
2. 读取真实`avgPx`和持仓数量；
3. 根据`avgPx`计算TP/SL；
4. 按交易所`tickSz`定向取整；
5. 创建覆盖全部持仓数量的保护单；
6. 查询订单确认保护单有效；
7. 把入场和保护单ID写入持久化状态。

## 7.3 同方向重复信号

```text
持多 + LONG + open_entries<3  -> 加多
持空 + SHORT + open_entries<3 -> 加空
open_entries>=3                -> 跳过
```

加仓成交后，不能沿用原保护价。必须以交易所更新后的整个仓位`avgPx`重新计算：

做多：

```text
TP = avgPx * (1 + 0.006)
SL = avgPx * (1 - 0.009)
```

做空：

```text
TP = avgPx * (1 - 0.006)
SL = avgPx * (1 + 0.009)
```

保护单数量必须覆盖更新后的全部净持仓。

优先使用交易所支持的原子修改/改单能力。若只能撤旧挂新，必须实现保护超时监控。仓位超过`PROTECTION_TIMEOUT_SECONDS`未被完整保护时，按`PROTECTION_FAIL_ACTION`处理，并立即停止新开仓。

## 7.4 反向信号

回测语义为同一15分钟收盘价反手，但真实交易不能假设两次成交完全原子化。实盘必须按下面状态机：

```text
收到反向信号
  -> 阻止其他新订单
  -> 撤销旧仓保护单
  -> reduce-only平掉旧仓
  -> 查询并确认旧仓为0
  -> 重新查询余额、合约规格和风险限制
  -> 开立新方向第1笔
  -> 查询新仓avgPx
  -> 建立新保护单
  -> 确认保护有效
```

任何一步失败，均不得继续下一步。尤其禁止“旧仓尚未确认平掉就直接开反向仓”。

如果当前框架无法实现上述状态机，第一版可以把反向信号设置为：

```text
只平旧仓，不在同一信号中开新仓
```

但这属于执行差异，必须记录并重新回放评估，不能声称与回测完全一致。

---

# 8. 防重复和重启恢复

## 8.1 每根K线唯一信号ID

建议：

```text
signal_id = ETH-USDT-SWAP|15m|signal_bar_start|LONG/SHORT|R16
```

每笔订单使用确定性的`clientOrderId`，例如信号ID哈希加入场序号：

```text
R16_<bar_hash>_L_1
R16_<bar_hash>_L_2
R16_<bar_hash>_S_1
```

同一`signal_id + entry_sequence`只能成功执行一次。

策略文件内置了进程内重复K线保护，但这不足以覆盖进程重启。执行器必须把以下内容持久化：

- 已处理K线；
- 已生成信号；
- 下单请求；
- 交易所订单ID；
- clientOrderId；
- 成交状态；
- 当前持仓周期ID；
- 当前入场次数；
- 保护单ID；
- 最近一次仓位同步时间。

## 8.2 重启顺序

进程重启后必须先：

1. 从OKX读取真实持仓和订单；
2. 与本地状态对账；
3. 若有仓位，确认保护单覆盖全部数量；
4. 修复或紧急处置未保护仓位；
5. 恢复`open_entries`；
6. 恢复最后已处理K线；
7. 完成对账后才允许处理新信号。

本地文件不能凌驾于交易所真实状态。

---

# 9. 保护单价格取整

必须调用：

```python
build_protective_levels(
    position_side=1_or_minus_1,
    average_entry_price=exchange_avg_px,
    price_tick=exchange_tick_sz,
)
```

取整方向已与回测保持一致：

| 仓位 | TP | SL |
|---|---|---|
| 多 | 向上取到有效tick | 向下取到有效tick |
| 空 | 向下取到有效tick | 向上取到有效tick |

不得用Python普通`round()`替代。

触发价、委托价和价格类型（最新价/标记价/指数价）必须在配置和日志中明确。回测基于成交K线路径，实盘若使用标记价触发，会存在执行差异，必须记录。

---

# 10. 分阶段Gate

任何Gate失败，立即停止，不得跳过并继续实盘。

## Gate 0：代码与环境盘点

要求：

- 保留原策略文件；
- 创建独立R16策略文件；
- 输出框架文件清单；
- 输出依赖版本；
- 输出配置映射表；
- 不读取或打印真实密钥。

结果写入：

```text
outputs/r16_live_migration/00_environment_inventory.md
```

## Gate 1：静态与单元测试

运行：

```bash
python -m py_compile path/to/eth_spike_r16_strategy.py
python -m unittest -v path/to/test_eth_spike_r16_strategy.py
```

要求全部通过。

## Gate 2：信号层历史一致性

使用2026年4月1日至7月31日分钟数据重新聚合15分钟K线，与审计参考`backtest_eth_spike_v1_6.add_pine_signals()`比较。

冻结R16应得到：

```text
总原始信号：122
LONG：64
SHORT：58
同根多空冲突：0
```

要求：

- 总数一致；
- 每个信号K线时间一致；
- 每个方向一致；
- 首个差异为0；
- 不允许只比较总数。

结果写入：

```text
outputs/r16_live_migration/01_signal_parity.csv
outputs/r16_live_migration/01_signal_parity_summary.json
```

## Gate 3：实盘行情层回放

用实际部署的数据获取模块拉取至少7天历史15分钟K线，与OKX原始K线或已审计分钟聚合结果逐根比较：

- 时间戳；
- OHLC；
- 是否闭合；
- 缺失；
- 重复；
- 时区。

要求OHLC在交易所精度范围内一致，缺失和重复为0。

## Gate 4：订单状态机离线仿真

使用假交易所客户端覆盖：

1. 空仓开多；
2. 空仓开空；
3. 第2次同向入场；
4. 第3次同向入场；
5. 第4次同向信号被拦截；
6. 多翻空；
7. 空翻多；
8. 部分成交；
9. 下单超时但交易所实际已成交；
10. 保护单创建失败；
11. 保护单撤销失败；
12. 进程在入场成交后、保护单创建前重启；
13. 重复处理同一K线；
14. 本地状态与交易所持仓不一致。

每种情形必须有断言，不能只看日志。

## Gate 5：OKX模拟盘

必须先运行模拟盘：

```text
TRADING_ENV=demo
LIVE_TRADING_ENABLED=false
```

模拟盘至少验证：

- 下单数量合法；
- 逐仓模式正确；
- 多空方向正确；
- 同向加仓计数正确；
- 新均价来自交易所；
- TP/SL价格正确；
- 保护单覆盖全部仓位；
- 平仓后保护单已清理；
- 重启恢复正确；
- 同根K线不会重复开仓。

没有真实市场信号时，可以在测试分支注入人工信号测试执行器，但严禁修改R16策略条件制造信号。人工信号必须打上`TEST_INJECTED`标签，模拟盘完成后删除测试入口或保持默认强制关闭。

## Gate 6：影子运行

连接真实市场行情和账户只读接口，但禁止真实下单。连续运行至少7天或直到捕获不少于5个R16原始信号。

记录：

- 信号时间和方向；
- 理论入场价；
- 理论数量；
- 理论TP/SL；
- 当时账户模式和合约规格；
- 若真实下单预计产生的拒单原因。

## Gate 7：有限实盘

只有用户明确确认杠杆和单次开仓金额后，才允许设置：

```text
TRADING_ENV=live
LIVE_TRADING_ENABLED=true
```

Agent不得自行决定杠杆或资金比例。

---

# 11. 实盘手续费与回测口径

本轮R16实验使用：

```text
开仓手续费：0.05%
平仓手续费：0.02%
```

这只是回测统计口径，不代表当前账户真实费率。实盘必须记录交易所返回的实际手续费和流动性角色。

每笔交易至少保存：

```text
gross_pnl
entry_fee
exit_fee
funding_fee
slippage
net_pnl
maker_or_taker
```

资金费不得漏记。

---

# 12. 日志字段

每根闭合15分钟K线至少记录：

- `signal_bar_start`；
- `signal_fill_time`；
- OHLC；
- BOLL中轨、上轨、下轨；
- 插针类型；
- 插针振幅；
- 前置涨跌幅；
- 趋势根数；
- 横盘根数；
- 末根回撤；
- LONG/SHORT结构是否通过；
- 当前持仓方向；
- 当前入场次数；
- 最终动作和跳过原因。

每次订单操作至少记录：

- `signal_id`；
- `clientOrderId`；
- 交易所订单ID；
- 请求数量；
- 成交数量；
- 请求价格；
- 成交均价；
- 交易所`avgPx`；
- TP/SL原始值和取整值；
- 保护单ID；
- 订单状态；
- 错误码；
- 重试次数；
- 延迟；
- 手续费。

---

# 13. 强制停止条件

出现以下任一情况，必须阻止新开仓：

1. 当前K线未确认闭合；
2. K线缺失、重复或乱序；
3. 合约规格读取失败；
4. 本地持仓与交易所不一致；
5. 有仓位但`open_entries`未知；
6. 有仓位但保护单不存在或数量不足；
7. 同一K线订单状态不明确；
8. API返回超时且无法确认订单是否已成交；
9. 保护单创建或更新失败；
10. 账户模式、仓位模式或杠杆与启动配置不一致；
11. 发现同一信号重复成交；
12. 出现强平或强制减仓；
13. 实盘参数与冻结R16参数不一致。

必须做到“失败时不新开仓”，不能在异常时继续按默认值交易。

---

# 14. 禁止事项

Agent不得：

- 修改R16信号参数；
- 把R16与SuperTrend/TEMA条件混合；
- 继续使用BTC过滤；
- 继续使用原策略P1～P9平仓；
- 把15分钟改回5分钟；
- 使用未收盘K线；
- 因框架不支持就取消第2、3次同向入场而不报告；
- 因实现方便就取消反向信号；
- 用普通`round()`处理保护价格；
- 把合约面值和步长永久硬编码；
- 把API密钥写入代码或日志；
- Gate失败后继续模拟盘或实盘；
- 未经用户确认自行设置真实杠杆和仓位。

---

# 15. Agent最终交付物

完成后必须提交：

```text
1. 新R16策略文件
2. 修改后的策略注册/启动配置
3. 修改后的行情调用代码
4. 修改后的订单与保护单状态机
5. 持久化状态结构或迁移脚本
6. 单元测试与集成测试
7. .env.example（不得包含真实密钥）
8. 00_environment_inventory.md
9. 01_signal_parity.csv
10. 01_signal_parity_summary.json
11. 02_market_data_gate.md
12. 03_order_state_machine_tests.md
13. 04_demo_run_summary.md
14. 05_shadow_run_summary.md
15. R16_LIVE_RUNBOOK.md
16. CHANGELOG_R16_LIVE.md
```

`CHANGELOG_R16_LIVE.md`必须逐文件记录：

- 修改了什么；
- 为什么修改；
- 与回测语义的对应关系；
- 验证方法；
- 是否存在尚未消除的差异。

---

# 16. 最终验收标准

只有同时满足以下条件，才可以向用户报告“已具备有限实盘条件”：

- R16参数哈希或逐字段校验通过；
- 2026年4—7月122个原始信号逐笔一致；
- 15分钟闭合K线Gate通过；
- 同向最多3次入场已实现；
- 第4次同向信号被正确阻止；
- 反向状态机已实现或明确记录执行差异；
- 每次入场/加仓后根据交易所真实均价更新TP/SL；
- TP/SL覆盖全部仓位；
- 重启恢复和订单幂等测试通过；
- OKX模拟盘通过；
- 影子运行通过；
- 杠杆和开仓金额由用户明确确认；
- 未发现未保护仓位和重复成交；
- 所有未解决风险已在Runbook列出。

如果只完成策略文件接入，不得报告“实盘部署完成”，只能报告：

```text
R16策略层迁移完成，执行层仍待Gate验证。
```

---

# 17. 当前已完成的参考验证

随本说明提供的`eth_spike_r16_strategy.py`已经在当前工作环境完成：

```text
Python语法检查：通过
基础单元测试：通过
2026-04—07信号逐根一致性：通过
R16原始信号：122
LONG：64
SHORT：58
多空同根冲突：0
```

该验证只证明策略计算层与审计回测一致，不证明用户另一台机器上的行情、订单、账户和保护单实现已经正确。Agent仍必须从Gate 0开始执行并保存证据。
