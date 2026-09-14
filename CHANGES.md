# Autobot 架构修复 - 变更说明

本次只修架构问题，**完全不动策略逻辑**。

修复的 5 个文件全部放在 `autobot/` 下，按原项目结构覆盖即可：

```
autobot/
├── main.py                   ← 覆盖
├── core/
│   ├── engine.py             ← 覆盖
│   └── position_manager.py   ← 覆盖
├── data/
│   └── db_reader.py          ← 覆盖
└── task/
    └── task_manager.py       ← 覆盖
```

其他文件（策略 / config / okx_trader / redis / logger / strategy_base / strategy_registry / __init__ / run.py）一行都没动。

## 依赖追加

`db_reader.py` 改用了 SQLAlchemy。如果环境里还没装：

```bash
pip install sqlalchemy
```

pymysql 本身依然作为 driver 被 SQLAlchemy 调用，所以不用动。

## 一对一对照修复点

### A① 「ts=599 永远不变」 → `db_reader.py`

旧代码：
```python
return df.sort_values("open_time").reset_index(drop=True)
```
新代码：
```python
df["open_time"] = pd.to_datetime(df["open_time"])
df = df.set_index("open_time", drop=False)
```
结果：策略里 `df.index[idx]` 直接拿到 `pd.Timestamp`，零改动。

### A② 「平仓后冷却期永远 10 根」 → 由 A① 顺带解决

`_last_close_ts = current_ts` 现在存的是真实时间戳，
`pd.Timestamp(current_ts) - pd.Timestamp(self._last_close_ts)` 才能算出真实经过分钟数。

### A③ 「进程静默挂死 9 小时」 → `task_manager.py`

加了两个协程：

- **HEARTBEAT** —— 每 60s 打一行
  `[HEARTBEAT] 调度器存活, 活跃任务=N, 主循环 X.Xs 前更新`
- **WATCHDOG** —— 检查主循环 `_last_main_loop_ts`，超过 180s 没更新就输出
  `CRITICAL ⚠️ [WATCHDOG] 调度器主循环已 XXXs 未更新心跳！`

⚠️ 同进程协程没法捕获"进程整体冻结"。最终可靠的看门狗必须靠外部：
- systemd 的 `WatchdogSec=120`
- 或独立脚本 `tail -1 logs/autobot.log` 检查最后一行时间戳，超过 N 秒就 `systemctl restart`

新增的 `/health` 端点正是给外部脚本调用的：
```bash
curl http://localhost:9002/health
# {"status":"ok","checks":{"redis":"ok","db":"ok","scheduler_stall_sec":0.34}, ...}
```

### B① 「check_stop 永远不被调用」 → `engine.py` + `task_manager.py`

**Engine 改动**：
- 删除 `is_signal_time = now.minute % tf_minutes == 0 or now.second <= 3`（连带删 WARNING 时间检查日志，即 C3）
- `execute()` 加一个 `mode` 参数（"signal" / "stop_check"），由调用方明确告知

**Task 改动**：每个 Task 现在跑两个独立协程
```
signal_loop  → 严格对齐 timeframe 整点，调 execute(mode="signal")
stop_loop    → 每 30s 心跳，调 execute(mode="stop_check")
```

`STOP_CHECK_INTERVAL_SEC = 30` 在 task_manager.py 顶部，可以按需调。

### B② 「重启不与交易所对账」 → `engine.py` + `main.py`

**Engine 改动**：
- 新增 `reconcile_with_exchange()` 方法
- `execute()` 每次入口都调一次：本地有仓 + 交易所无仓 → `mark_liquidated`
- 原 `_handle_multi_signal` 内重复的对账代码移除

**Main 改动**：lifespan 启动时执行 `_startup_reconcile()`，
对每个已加载任务都做一次对账，并打印恢复仓位详情。

### C1 「size=0」 → `engine.py`

`_execute_open` 和 `_execute_open_isolated` 调 `save_position` 时都补传了 `size=contracts`。

### C2 「13 条孤儿 active_positions」 → `position_manager.py` + `main.py`

- `PositionStore.mark_orphaned(position_id, reason)` —— 把孤儿挪到 history（action=orphaned）
- `PositionManager.cleanup_orphan_active_positions()` —— 遍历 active，比对 Redis，挪走孤儿
- `main.py` 启动时自动调用

启动时你会看到类似日志：
```
[启动清理] 清理 13 个孤儿仓位记录
```

### C3 「WARNING 级别 [时间检查]」 → `engine.py`

整行连带 `is_signal_time` 逻辑一起删了。

### C4 「pandas UserWarning」 → `db_reader.py`

改用 `create_engine(...)` 喂 `pd.read_sql`，warning 消失。
顺便启用了 `pool_pre_ping=True`（自动检测断连）和 `pool_recycle=3600`（避开 MySQL wait_timeout）。

### C5 「启动恢复仓位无明确提示」 → `main.py`

新的启动日志：
```
============================================================
[启动对账] 开始
[启动对账] 从 Redis 恢复了 1 个仓位:
  └─ 空仓: okx/ETH-USDT-SWAP/supertrend_tema_v2 entry=2311.27 size=0
     opened_at=2026-04-24 09:20:01 (timestamp=1777009801)
[启动对账] 与交易所对账中...
[启动对账] okx/ETH-USDT-SWAP/supertrend_tema_v2 已被自动清理: {'long_liquidated': False, 'short_liquidated': True}
[启动清理] 清理 1 个孤儿仓位记录
[启动对账] 完成
============================================================
```

如果 OKX 上确实没那个 short 仓（你的 log 里那个 ghost），现在启动就会自动把它清掉，不会再卡死。

## 历史脏数据兼容

`position_history.json` 里曾经有 `"direction": -1`（整数）这种脏数据。
现在加载和写入时统一过 `_norm_direction()` 转字符串，老文件不用手工改。

## 启动后第一时间观察什么

1. 启动日志里的 `[启动对账]` 段落 —— 确认所有恢复的仓位都正确对账或清理
2. `[HEARTBEAT]` 日志 —— 每 60s 一条，没有就是出问题了
3. `/health` 接口 —— `scheduler_stall_sec` 不应 > 2

## 没修的（你说不动策略，留给你判断）

- B③ 持仓中不评估反向开仓信号（策略 trade-off）
- 策略文件 `supertrend_tema_strategy_v2.py` 一行没动
