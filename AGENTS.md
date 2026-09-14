# Autobot Trading Service

基于 Python 的加密货币自动化合约交易平台，对接 OKX 交易所，支持多策略、多空双向持仓、爆仓联动保护。

> 当前唯一启用策略：`eth_spike_fourshape_r2_15m`（ETH 四形态插针策略 R2），同时跑 ETH 与 BTC 两个任务。R16 仍注册作兼容回退，但已不挂任务；SuperTrend+TEMA 已停用。R2 为"交易所驻留条件触发单"执行模型（非逐 bar 判断）。详见 `HANDOFF.md`（最新运行状态与修复）与 `R2_R3代码接入与仓位说明.md`。注意 `R16_LIVE_RUNBOOK.md` / `CHANGELOG_R16_LIVE.md` 已过期，勿参照其 R16 任务/流程描述。

## ⚠️ 基础注意事项：内存极小（3.5GB），处理大数据必须流式/分批

本机只有 **3.5GB 内存 + 2 核 CPU + 2GB swap**，且没有内存压力保护（OOM 会直接杀进程，严重时整机卡死、被强制重启，2026-08-24~27 曾多次重启）。

- **任何 Python 脚本禁止一次性把大表/大文件全部加载进内存**（pandas/列表全量读取 = 必卡死）。
- 数据库里最大的是 `kline_data` 1min 表：ETH 315 万行、BTC 319 万行。一次 `SELECT ... WHERE currency_id=..` 不带 LIMIT 取出全部行（哪怕用 `fetchmany`）都会 OOM。
- 正确姿势（见 `recalculate_indicators.py` 的改法）：
  1. 用 SQL `LIMIT` 键集分页（`WHERE id > ? ORDER BY id LIMIT n`），单次 ≤5 万行；
  2. 指标计算带前滚重叠（WARMUP=3000，EMA288/TEMA288 收敛到 ~1e-9），逐块原子提交；
  3. 每块提交后落盘进度（`recalc_progress.json`），被杀/断电后重跑自动续；
  4. 打印每块峰值 RSS，确认 <300MB。
- 启动长任务必须**完全脱离会话**：`setsid nohup python3 -u x.py > log 2>&1 < /dev/null & disown`
  （别用 `(cmd &)` 子 shell，会挂住父管道；也不要与长任务同命令 sleep 监控）。
- 已实测峰值：`recalculate_indicators.py`（CHUNK=50000）处理 1min 大表峰值 RSS ≈ 190MB，安全。
- 磁盘 / 也只剩 ~4GB，注意清理（/var/log、backup 等），别让 MySQL 写爆磁盘。


## 整体架构

```
/home/ecs-user/zzs/
├── database/    ← 数据管道：OKX API → 计算指标 → 写入 MySQL（定时运行）
└── project/     ← 交易系统：读取 MySQL → 策略信号 → OKX 下单（常驻服务）
```

数据流：`OKX Market API` → `database/update_latest_data.py` → `MySQL` → `project/autobot` → `OKX Trade API`

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn（HTTP 服务）
- PyMySQL + pandas（K线数据读取）
- Redis（仓位缓存 + 任务持久化）
- python-okx（OKX 交易所 SDK）
- numpy（策略计算）

## 常用命令

```bash
# 启动交易服务
python run.py

# 运行全部测试
python test_all.py

# 运行单项测试
python test_all.py config
python test_all.py redis
python test_all.py db
python test_all.py strategy
python test_all.py price
python test_all.py position
python test_all.py engine
python test_all.py api

# R2 策略专项测试（当前主策略）
python -m unittest -v test_r2_execution.py             # R2 执行层状态机（mock 交易所/仓位，离线）
python -m unittest -v test_r2_r3_strategy.py          # R2/R3 信号层单测（离线）
# R16 策略专项测试（兼容回退）
python -m unittest -v test_eth_spike_r16_strategy.py   # 信号层单测（离线）
python -m unittest -v test_r16_execution.py            # 执行层状态机（mock 交易所/仓位，离线）

# 数据库更新（在 ../database/ 目录下执行）
python ../database/update_latest_data.py          # 增量更新所有币种所有周期
python ../database/recalculate_indicators.py      # 重新计算全部指标（按行数小→大，断点续跑）
python ../database/recalculate_indicators.py ETHUSDT 5min  # 指定币种和周期
python ../database/recalculate_indicators.py --reset-progress  # 忽略断点，从头算
python ../database/check_database.py              # 数据库状态巡检
```

## 项目结构（project/autobot）

```
autobot/
├── main.py                 # FastAPI 应用入口，定义所有 REST API
├── config.py               # 配置管理，从 .passwd 文件加载敏感信息
├── core/
│   ├── engine.py           # 交易引擎：策略调度 → 信号生成 → 下单执行
│   ├── strategy_base.py    # 策略抽象基类（StrategyBase / SignalResult / MultiSignalResult）
│   ├── strategy_registry.py # 策略注册器（单例模式）
│   └── position_manager.py  # 仓位管理（Redis实时 + JSON文件持久化）
├── strategies/
│   ├── __init__.py                      # register_all_strategies() 注册入口
│   ├── eth_spike_r2_strategy.py         # ETH 四形态插针策略 R2（当前唯一启用，ETH+BTC 任务）
│   ├── eth_spike_r16_strategy.py        # ETH 15分钟插针策略 R16（兼容回退，未挂任务）
│   ├── supertrend_tema_strategy_v2.py   # SuperTrend + TEMA 复合策略（已停用）
│   ├── supertrend_tema_strategy_v3.py   # v3 对称化版本（已停用）
│   ├── supertrend_tema_strategy_v3_1.py # v3.1 BTC过滤版本（已停用）
│   └── hedge_liquidation_strategy.py    # 双向对冲 + 爆仓联动策略（已停用）
├── exchange/
│   └── okx_trader.py       # OKX API 封装（开平仓、余额、风控、TP/SL保护单挂/改/撤/查）
├── data/
│   └── db_reader.py        # MySQL 数据读取（SQLAlchemy，K线 + EMA/SMA/TEMA/SuperTrend指标）
├── cache/
│   └── redis.py            # Redis 客户端封装
├── task/
│   └── task_manager.py     # 异步任务调度（按 timeframe 周期触发）
└── utils/
    └── logger.py           # 日志（控制台 + 文件 logs/autobot.log）
```

## 数据管道结构（../database/）

```
database/
├── update_latest_data.py         # 核心：增量更新，自动检测缺口并分页补拉，热身2000根保证TEMA288精度
├── fetch_and_store_okx_data.py   # 批量获取OKX K线并入库（首次初始化用）
├── fetch_historical_data.py      # 历史数据回填
├── recalculate_indicators.py     # 对已有K线重新计算所有指标并upsert（分块+前滚重叠+断点续跑，内存安全）
├── database_manager.py           # 数据库管理工具（检查表、备份、导出JSON、生成报告）
├── check_database.py             # 数据库状态巡检（表结构、数据量、大小）
├── db_reader.py                  # 只读数据读取器
├── test_fetch_okx.py             # OKX API 拉取测试
└── test_reader.py                # 数据读取测试
```

## 数据库表结构

| 表名 | 说明 | 关联 |
|------|------|------|
| `currencies` | 币种表（ETHUSDT、BTCUSDT...） | — |
| `time_intervals` | 周期表（1min、5min、15min、1h、4h、1day） | — |
| `kline_data` | K线主表（open_time, OHLCV） | currency_id + interval_id |
| `ema_indicators` | EMA指标（24/48/60/72/144/288） | kline_id |
| `sma_indicators` | SMA指标（5/10/20/30/60/120/144） | kline_id |
| `tema_indicators` | TEMA指标（24/48/60/72/144/288） | kline_id |
| `supertrend_indicators` | SuperTrend（value/direction/upper/lower） | kline_id |
| `config_summary` | 配置概要 | — |

## 配置方式

项目通过 `.passwd` 文件加载敏感配置（不提交到 Git），查找顺序：
1. 项目根目录 `.passwd`
2. 当前工作目录 `.passwd`
3. 用户主目录 `~/.passwd`

格式为 `KEY=VALUE`，支持 `#` 注释。主要配置项：

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME | MySQL 连接 | localhost:3306 |
| REDIS_URL | Redis 连接 | redis://localhost:6379/0 |
| OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE | OKX API 密钥 | （空） |
| OKX_FLAG | 0=实盘, 1=模拟盘 | 0 |
| DEFAULT_LEVERAGE | 杠杆倍数 | 100 |
| DEFAULT_RATIO | 全仓资金使用比例 | 0.03 |
| MARGIN_MODE | cross=全仓, isolated=逐仓 | cross |
| POSITION_PERCENT | 逐仓时单仓占比 | 0.1 |
| STOP_PROFIT_PCT / STOP_LOSS_PCT | 止盈/止损百分比 | 0.02 / 0.01 |
| LIQUIDATION_WARN_RATIO | 爆仓预警保证金率阈值 | 0.15 |
| LIQUIDATION_ACTION | 爆仓联动：reduce/close_all/none | reduce |
| POSITION_MODE | 持仓模式：long_short_mode=双向 / net_mode=单向 | long_short_mode |
| TP_SL_TRIGGER_TYPE | 保护单(TP/SL)触发价类型：last/mark/index | last |
| PROTECTION_TIMEOUT_SECONDS | 保护单挂单超时（秒） | 2 |
| PROTECTION_FAIL_ACTION | 保护单失败处置：close_position/halt | close_position |
| MAX_SAME_DIRECTION_ENTRIES | 同向最多入场次数（旧 R16 冻结 3；R2 改用 `FOURSHAPE_MAX_ENTRIES`） | 3 |

## 交易流程

R2 四形态策略当前流程（执行模型：交易所驻留条件触发单）：

1. `TaskManager` 每 60s 轮询一次信号（`signal_loop`），每 30s 巡检意图/保护单（`stop_loop`）
2. 每个任务调用 `TradingEngine.execute()`，`mode` 区分 "signal" / "stop_check"
3. 引擎从 `PositionManager` 获取当前仓位状态（Redis）
4. 信号周期：从 `DBReader` 读取 15m K 线（**只取 `confirm=1` 的已收盘 K 线**），Redis 跨重启去重（`lastbar:{exchange}:{symbol}:{method}`）
5. `_r2_new_bar()`：F1/F2/F4 在信号 K 收盘时挂**交易所 conditional 条件开仓单**（stop-entry，`slTriggerPx`）；F3 确认后在下一开盘按现价≈开盘市价入场
6. 意图存 Redis `r2:intents:{exchange}:{symbol}:{method}`，`algoClOrdId=R2{ts}{shape}{L|S}`（字母+数字，幂等）
7. 30s 巡检 `_r2_monitor_intents()`：撤失效/超时/反向占用的条件单；检测条件单消失→与交易所持仓对账→成交则建仓/加仓
8. 成交后按交易所真实 `avgPx` 重算 TP/SL（TP 0.6% / SL 0.9%），挂/改交易所驻留 OCO 保护单；裸仓自动补挂，保护单失败立即市价平仓兜底
9. 组合约束：持反向仓不挂/撤（无反手）；同向达上限不挂/撤
10. 仓位口径（`FOURSHAPE_*`）：`MARGIN_PCT` 3% × 100x，同向≤2，总名义≤6x；**张数支持 0.01 步长小数张**（OKX `lotSz=0.01`）

## 策略开发规范

新策略须：
1. 在 `autobot/strategies/` 下创建文件
2. 继承 `StrategyBase`，实现 `name`、`required_data_length`、`generate_signal()`
3. 如需止盈止损，覆写 `need_stop_check()` 返回 True 并实现 `check_stop()`
4. 如需多空双向信号，覆写 `generate_multi_signal()`
5. 在 `strategies/__init__.py` 的 `register_all_strategies()` 中注册

信号类型：
- `SignalResult.LONG` (1) — 开多
- `SignalResult.SHORT` (-1) — 开空
- `SignalResult.CLOSE_LONG` (2) — 平多
- `SignalResult.CLOSE_SHORT` (-2) — 平空
- `SignalResult.NO_SIGNAL` (0) — 无信号

R2 特殊约定：
- 执行模型为"交易所驻留条件单"，`FOURSHAPE_ENTRY_API=INTENTS`：`generate_signal()` 恒返回 NO_SIGNAL，实际入场由 `pending_entry_intents()` / `next_open_entry_intents()` 产生意图，引擎挂交易所订单
- 四形态：F1(上涨上插针→空) / F2(上涨下插针→空) / F3(下跌上插针→多) / F4(下跌下插针→多)；F1/F2/F4 为条件触发，F3 为下一开盘确认入场
- 退出完全依赖交易所驻留 TP/SL OCO 保护单（非软件 `check_stop()`）
- 形态阈值在 `frozen_shape_configs()` 中冻结，属"冻结对照"策略，**禁止修改**；仓位口径走环境变量 `FOURSHAPE_*`
- R16 兼容约定（已不挂任务）：`generate_signal(bar_is_closed=True, open_entries=...)`，`R16SignalConfig` 冻结

## 仓位管理

- Redis Key 格式: `pos:{exchange}:{symbol}:{method}:{direction}`
- 支持多空独立持仓（PositionInfo 包含 long + short 两个 SinglePositionInfo）
- JSON 文件 `position_history.json` 记录所有开/平仓/爆仓历史
- 爆仓检测：本地有仓位记录但交易所已无仓位 → 标记 liquidated
- R2/R16 字段（`SinglePositionInfo`）：`open_entries`（入场批次）、`algo_order_id`（保护单ID）、`last_processed_bar`（已处理收盘K线）

## API 接口

| 路径 | 功能 |
|------|------|
| GET /add | 添加交易任务（params: timeframe, exchange, symbol, method） |
| GET /remove | 移除交易任务 |
| GET /status | 查看任务状态 |
| GET /position | 查看仓位（多+空） |
| GET /positions | 查看所有活跃仓位 |
| GET /position_history | 仓位历史 |
| GET /reset | 重置仓位记录（不交易） |
| GET /close | 强制平仓（执行交易） |
| GET /close_all | 全部平仓 |
| GET /strategies | 查看可用策略 |
| GET /config | 查看交易配置（脱敏） |
| GET /liquidation_status | 爆仓风险状态 |

## 代码风格

- 中文注释和文档字符串
- 全局单例模式（strategy_registry, position_manager, trading_engine, task_manager, redis_client）
- 类型注解（typing 模块）
- 无 lint/typecheck 工具配置（无 ruff、mypy、flake8）
- 无格式化工具配置（无 black、isort）

## 已知修复记录

### 2026-06-24：db_reader.py 时间戳 bug
- `pd.to_datetime(df["open_time"])` 缺少 `unit='ms'`，导致毫秒时间戳被按纳秒解析，显示为 1970 年
- 修复：加 `unit='ms'` 参数 → `pd.to_datetime(df["open_time"], unit='ms')`
- 影响：开仓后 entry_timestamp 错误 → hold_bars 永远 = 999 → 止损 min_hold_bars 失效

### 2026-06-24：Redis 旧格式 key 清理
- 旧版 `current-position-*`、`position-price-*` key 不会被新版 PositionManager 使用
- 对冲 ghost 仓位（hedge_liquidation，已开 2 个月）需手动清理
- 清理命令：`redis-cli -p 6379 del <key>`

### 2026-06-24：OKX API Key 失效导致开仓失败
- 现象：日志报 "账户USDT余额为0或获取失败，跳过开仓"，策略有信号但无法开仓
- 根因：OKX API 返回 `code=50119 "API key doesn't exist"`，`get_usdt_balance()` 对非 `code="0"` 响应无额外日志
- 验证：`python3 -c "import okx.Account as Account; api = Account.AccountAPI(key, secret, passphrase, False, '0'); print(api.get_account_balance())"`
- 解决：更新 `.passwd` 中 `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE`，重启服务
- 机器公网 IP：`47.83.167.189`（用于 OKX API Key 白名单）

### 2026-08-16：R16 策略替换（当前唯一信号源）
- 背景：用「ETH 15 分钟插针策略 R16」完全替换原 SuperTrend+TEMA v3.1 信号逻辑
- 改动 9 个文件（新增策略 / 只注册 R16 / okx_trader 加保护单 / engine 加 R16 执行路径 / task_manager 改 60s 轮询等），逐文件说明见 `CHANGELOG_R16_LIVE.md`
- 运行手册：`R16_LIVE_RUNBOOK.md`；执行层状态机离线测试：`test_r16_execution.py`
- 注意：R16 为低频策略（回测约 1 信号/天），长时间无开仓属正常，不代表故障
- 诊断无信号的方法：看日志 `策略信号` 行中的 `amp`（振幅≥0.3%）、`spike`（插针类型）、`move`（前置走势≥0.8%，nan=未命中）三个字段，判断卡在哪一环

### 2026-08-24：algoClOrdId 含下划线导致保护单挂单失败（开仓 1 秒后被平）
- 现象：每次 R16 开仓成功约 1 秒后即被平仓；日志 `[R16] 保护单挂单失败，立即平仓兜底: Parameter algoClOrdId error`
- 根因：`engine.py` 的 `_r16_algo_cl_ord_id()` 生成 `R16_{ts}_{L|S}_{seq}` 含下划线，而 OKX 要求 `algoClOrdId` 只能为字母+数字（≤32 字符），返回 `code=51000`
- 影响：自首个真实信号（08-19）起 6 次开仓全部被兜底平仓，R16 从未成功持仓；`挂保护单成功` 计数始终为 0
- 实盘试验验证：`experiments/okx_protection_live_test.py` 证实旧格式 51000 失败、纯字母数字格式成功（开 1 张 LONG 全闭环后已平仓）
- 修复：`engine.py:253` 改为 `f"R16{ts}{side_tag}{entry_seq}"`（如 `R161787579312L1`），幂等语义不变
- 验证：`python -m unittest -v test_r16_execution.py` 9/9 通过；下一根 15m 信号应出现 `挂保护单成功`

### 2026-08-24：OCO 保护单用 `ordType="conditional"` 导致 TP 腿丢失（只有止损没有止盈）
- 现象：挂保护单返回 `code=0` 成功，但查询驻留条件单只有 `slTriggerPx`，`tpTriggerPx` 为空
- 根因：OKX 文档中 `conditional` 是「单向止损单」，`oco` 才是「一触即撤双向单」；`place_protective_orders` 用 `conditional` + 同时传 TP/SL，OKX 只注册止损腿，止盈永远不会触发
- 实盘试验验证：`experiments/okx_open_far_protection.py` 开 1 张 LONG，用 `conditional` 只挂上 SL（TP 丢失），改用 `ordType="oco"` 后 TP/SL 两腿均驻留（`state=live`），平仓/撤单正常
- 修复：`okx_trader.py:448` `ordType="conditional"` → `ordType="oco"`；`get_algo_orders` 默认 `ord_type="conditional"` → `"oco"`（否则保护巡检找不到 oco 单会重复补挂）；同步改 `test_r16_execution.py` mock 默认值
- 验证：`python -m unittest -v test_r16_execution.py` 9/9 通过；实盘 OCO 两腿确认

### 2026-08-28：多币种下 `open_position` 漏传 `inst_id` → BTC 信号误开 ETH 仓、保护单错挂 BTC
- 现象：新增 BTC-USDT-SWAP 任务后，BTC 触发 LONG 信号 → 交易所实际开了 **ETH** 仓（@2491），但保护单（OCO）却挂在 **BTC** instId 上；4 秒后对账发现"本地有 BTC 记录但交易所无 BTC 仓位"→ 标记平仓；ETH 真实仓位成裸仓
- 根因：`engine.py` 的 `_r16_open` / `_r16_add` 调 `trader.open_position(...)` 时**漏传 `inst_id`**，而 `okx_trader.open_position` 内部回退 `inst_id = inst_id or TradingConfig.DEFAULT_INST_ID`（默认 `ETH-USDT-SWAP`）→ 市价单实际下到 ETH；保护单 `place_protective_orders(inst_id=BTC-USDT-SWAP)` 用的却是正确 symbol 转换 → 下单/保护单 instId 错配。单币种（只有 ETH）时默认值恰好一致，从未暴露
- 修复：`engine.py` 三处 `open_position` 调用补传 `inst_id=inst_id`（`_r16_open`、`_r16_add`、`_execute_open` 防御性）；反手 `_r16_reverse` 复用 `_r16_open` 已覆盖
- 清理：撤掉 BTC 孤立 OCO 单、市价平掉误开的 ETH 仓位（账户恢复干净）
- 验证：`python -m unittest -v test_r16_execution.py` 9/9 通过；双任务（ETH+BTC）重启后正常运行
- 注意：**新增多币种任务时，所有经 `open_position` 的下单路径都必须显式传 `inst_id`**，否则回退 DEFAULT_INST_ID

### 2026-09-03：R2 四形态策略替换 R16（当前唯一实盘策略，ETH+BTC）
- R2 为"交易所驻留条件单"执行模型：F1/F2/F4 收盘挂 conditional stop-entry，F3 下一开盘市价入；成交后按真实均价挂 TP0.6%/SL0.9% OCO。详见 `HANDOFF.md` 与 `R2_R3代码接入与仓位说明.md`
- 新增 `autobot/strategies/eth_spike_r2_strategy.py`；`strategies/__init__.py` 注册 R2 + 保留 R16 回退
- 测试 `test_r2_execution.py`（执行层状态机）+ `test_r16_execution.py`

### 2026-09-04：`confirm=0` 半成品 K 线竞态 → R2 漏挂/误判信号
- 现象：R2 上线后应开仓却零成交；离线重放发现理论意图存在但引擎无处理痕迹
- 根因：`update_latest_data.py` 在 bar 开始数秒即 upsert 半成品（`confirm=0`），直到 bar 结束后才写最终值（`confirm=1`）；引擎只用 `open_time+15min<=now` 判断已收盘，会在 bar 刚结束的 0~3s 窗口把半成品当已收盘处理并 `set lastbar` 永久标记，之后最终数据不再重试 → 基于半成品 OHLC 的形态判定与实际不符
- 修复：`autobot/data/db_reader.py` 的 `get_data()` SQL 增加 `AND k.confirm = 1`（只返回 OKX 已确认收盘 K 线）。**注意 `kline_data.confirm` 语义：1=OKX 已确认收盘；每周期当前进行中 bar 为 0**

### 2026-09-04：R2 可观测性加固
- `engine.py` R2 分支：每根新收盘 bar 的处理结论固定打 info（`[R2] <币> 处理收盘bar ...: <结论>`），此前无候选只写 debug 导致"该开没开"无法排障
- 数据管道停滞看门狗：最新已收盘 K 线距今 > max(2×interval, 3600s) 打 ERROR

### 2026-09-08/11：小数张（0.01 步长）全链路修复 + 两个隐蔽陷阱
- OKX ETH/BTC-USDT-SWAP `lotSz=minSz=0.01`，**支持小数张**；原代码多处 `int(sz)`/`str(int(sz))` 把小数张截断成整数
- 修复：`okx_trader._fmt_sz()` 统一格式化；`open_market_size`/`place_stop_entry_order`/`place_protective_orders`/`amend_protective_orders` 全改小数张；`engine._r2_size` 返回 `round(contracts,2)`、`_r2_try_place` round 2 位；策略 `contract_step/min_contracts` 默认 `1.0→0.01`
- **陷阱①（`.passwd` 覆盖）**：`config.py` 会把 `.passwd` 注入 `os.environ`（`if key not in os.environ`），`.passwd` 中显式 `FOURSHAPE_CONTRACT_STEP=1`/`FOURSHAPE_MIN_CONTRACTS=1` 会覆盖代码默认值。已改 `.passwd` 为 0.01（备份 `.passwd.bak_20260911`）。**改任何配置默认值前必须先查 `.passwd` 是否显式设置，且验证要在服务同口径（经 config 注入）下做**
- **陷阱②（`_r2_epoch` 时区）**：意图的 created/expires 来自 df.index（UTC-naive），`datetime.fromisoformat(...).timestamp()` 会按本机 +0330 解释 → expires 提前 3.5h → 意图一挂就"已过期"、monitor 提前撤单。已修为 naive 时间 `replace(tzinfo=timezone.utc)`
- `calculate_order_size` 增加"最小张兜底"：名义不足 min 张但仍在名义上限内且保证金够时，按 min 张下（小资金保护）

### 2026-09-11：R2 全链路人工实盘验证（通过）
- `experiments/verify_r2_order_path.py`：服务同口径配置、`_r2_epoch` 过期判定、`_r2_size`（ETH 0.63/BTC 0.2 张）、`_r2_try_place` 真实挂小数张条件单并撤销，全部成功
- `experiments/r2_live_open_test.py`：真实市价开 ETH long 0.63 张 → 挂 OCO(TP+0.6%/SL-0.9%) → 服务接管巡检 → 用户手动平仓 → 服务对账清仓，闭环跑通

## 注意事项

- 绝不提交 `.passwd` 文件
- `OKX_FLAG=0` 为实盘交易，修改配置需极度谨慎
- `database/` 中的脚本硬编码了数据库密码，仅限本机使用
- Redis 必须可用，否则仓位管理和任务持久化失效
- `update_latest_data.py` 使用 2000 根热身数据保证 TEMA288 指标精度
- 数据库更新脚本应通过 cron 或 systemd timer 定时执行
- `db_reader.py` 中 `pd.to_datetime()` 必须带 `unit='ms'`，否则时间戳解析错误
- R2 是低频策略，长时间无开仓信号属预期，不代表策略故障（诊断方法：看日志 `[R2] <币> 处理收盘bar ...: <结论>`，及 `张数为0/触发距离过远/意图已过期` 等）
- **改 `FOURSHAPE_*` 等配置默认值前，先查 `.passwd` 是否显式覆盖**（`config.py` 会把 `.passwd` 注入 `os.environ`）；验证必须在服务同口径下做
- **OKX 永续支持 0.01 步长小数张**（`lotSz=minSz=0.01`），所有下单路径禁止 `int(sz)` 截断，统一走 `okx_trader._fmt_sz()`
- **时间统一用 UTC-naive**（db_reader 的 `open_time` 是 UTC）：任何 `datetime.fromisoformat(...).timestamp()` 都要先 `replace(tzinfo=timezone.utc)`，否则按本机 +0330 解释导致过期/撤单/比较错误
- 排障顺序：交易所 API（有无仓/挂单）→ Redis（`pos:*`、`r2:intents:*`、`lastbar:*`）→ 日志（`处理收盘bar`/`挂条件开仓单`/`张数为0`）
