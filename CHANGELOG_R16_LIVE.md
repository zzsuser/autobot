# R16 实盘接入变更记录（CHANGELOG）

本记录逐文件说明 R16 接入做了哪些改动、为什么、与回测语义的对应、验证方式。

## 改动总览

| 文件 | 动作 | 说明 |
|---|---|---|
| `autobot/strategies/eth_spike_r16_strategy.py` | 新增（拷贝） | R16 策略适配器，信号层与审计回测逐行一致 |
| `autobot/strategies/__init__.py` | 修改 | 只注册 R16，注释停用旧策略（文件保留） |
| `autobot/config.py` | 修改 | 新增执行层配置项 |
| `autobot/core/position_manager.py` | 修改 | `SinglePositionInfo` 增加 `open_entries/algo_order_id/last_processed_bar` |
| `autobot/exchange/okx_trader.py` | 修改 | 新增保护单挂/改/撤/查 + `tickSz` + 持仓模式参数化 |
| `autobot/core/engine.py` | 修改 | R16 执行路径（OPEN/ADD/REVERSAL + 保护单）、已收盘 K 线过滤、跨重启去重 |
| `autobot/main.py` | 修改 | 启动时校验账户持仓模式 |
| `autobot/task/task_manager.py` | 修改 | 信号循环改为每 60s 轮询，任务实例每分钟对账创建 |
| Redis `schedule_task` | 修改 | 删旧任务 `supertrend_tema_v3(5min)`，写 `eth_spike_r16_15m(15min)` |
| `test_r16_execution.py` | 新增 | 执行层状态机离线测试（9 用例） |
| `R16_LIVE_RUNBOOK.md` | 新增 | 运行手册 |

## 逐文件说明

### autobot/strategies/__init__.py
- 为什么：R16 是唯一信号源，旧策略必须停用。
- 改动：`register_all_strategies()` 只注册 `EthSpikeR16Strategy`，其余 `register(...)` 注释掉。
- 验证：`register_all_strategies()` 后 `list_strategies()` 仅返回 `['eth_spike_r16_15m']`。

### autobot/config.py
- 新增：`POSITION_MODE`、`TP_SL_TRIGGER_TYPE`、`PROTECTION_TIMEOUT_SECONDS`、`PROTECTION_FAIL_ACTION`、`MAX_SAME_DIRECTION_ENTRIES`。
- 为什么：R16 文档要求环境参数与冻结信号参数分离，执行层参数可配置。
- 与回测对应：`MAX_SAME_DIRECTION_ENTRIES=3` 与 R16 冻结的 `max_same_direction_entries=3` 一致（引擎侧防御性兜底，真正上限由策略冻结值保证）。

### autobot/core/position_manager.py
- 为什么：R16 需要持久化「入场批次数」「保护单 ID」「已处理 K 线」，支持同向加仓与重启恢复。
- 改动：`SinglePositionInfo` 增 3 字段 + `to_dict/from_dict`；`save_position` 增可选参数；新增 `update_position()`（不换 position_id 地更新字段，供加仓/补挂用）。

### autobot/exchange/okx_trader.py
- 为什么：R16 退出依赖交易所驻留 TP/SL 条件单，原框架无此能力。
- 新增方法：`get_tick_size`、`validate_position_mode`、`place_protective_orders`（`place_algo_order` OCO）、`amend_protective_orders`、`cancel_protective_orders`、`get_algo_orders`；`_pos_side()` 按 `POSITION_MODE` 决定是否传 `posSide`。
- 与回测对应：TP/SL 价格用 `build_protective_levels` 定向取整（多向上取/空向下取 TP，反之为 SL），触发价类型 `last` 对齐回测 K 线路径。

### autobot/core/engine.py
- 为什么：R16 返回的只有 LONG/SHORT 信号，动作（开/加/反手）由引擎按持仓方向推导；且必须补传 `bar_is_closed` 与 `open_entries`。
- 改动：
  - `_handle_signal`：R16 分支补传 `bar_is_closed=True` + `open_entries`；剔除未收盘 K 线；Redis 跨重启 K 线去重。
  - 新增 `_handle_r16_signal`（OPEN/ADD/REVERSAL 路由）、`_r16_open`、`_r16_add`、`_r16_reverse`、`_r16_check_protection`。
  - `reconcile_with_exchange`：R16 仓位消失按「正常平仓」记录，不误标爆仓。
  - `execute()`：`stop_check` 模式对 R16 路由到保护单巡检。
- 与回测对应：每次入场/加仓后用交易所真实 `avgPx` 重算 TP/SL；反手遵循「撤保护→平旧→确认归零→开新」。

### autobot/main.py
- 改动：启动时调用 `validate_position_mode()` 校验账户持仓模式与配置一致（不一致仅告警）。

### autobot/task/task_manager.py
- 为什么：R16 只处理「已收盘 15m K 线」，不需要只在 15m 整点对齐时才判断；改为每分钟轮询，由引擎内过滤已收盘 K 线并去重，检测到新收盘 K 线才产生信号。
- 改动：新增 `SIGNAL_POLL_INTERVAL_SEC=60`；主循环改为每 60s 对账一次任务实例（不再等 900s 整点边界）；`signal_loop` 改为固定轮询，去掉整点对齐等待。

### test_r16_execution.py
- 覆盖 Gate 4 关键场景：开仓、保护单失败兜底、加仓、达到上限拦截、反手、反手旧仓未归零中止、保护单巡检补挂/跳过。
- 运行：`python -m unittest -v test_r16_execution.py`（离线，mock 交易所与仓位管理）。

## 尚未消除的差异（执行层）

1. 反手为「平旧→确认→开新」两步，非回测的同价原子反手，存在价差。
2. 未区分「保护单触发平仓」与「真实强平」——仓位消失时按正常平仓记录，真实强平不会触发爆仓联动（R16 单边无需联动，但历史标签可能把强平记为平仓）。
3. 启动时若 Redis 有旧格式 `pos:*` 或 `current-position-*` 脏 key，需按 AGENTS.md 已知修复记录手动清理。
