# R16 实盘运行手册（Runbook）

ETH 15 分钟插针策略 R16 已接入 autobot 框架，作为**唯一**信号源运行，原 SuperTrend+TEMA 策略已停用（文件保留）。

## 1. 运行状态

- 策略：`eth_spike_r16_15m`（唯一注册）
- 任务：`15min / okx / ETH-USDT-SWAP / eth_spike_r16_15m`
- 保证金模式：`cross`（全仓）
- 每仓：3% 保证金（`DEFAULT_RATIO=0.03`，100x 杠杆 → 名义约 3 倍净值）
- 持仓模式：`long_short_mode`（双向），由 `POSITION_MODE` 参数控制，后续策略可切 `net_mode`
- TP/SL：交易所驻留 OCO 保护单，TP 0.6% / SL 0.9%，触发价类型 `last`（最新价）
- 同向加仓：最多 3 次（`MAX_SAME_DIRECTION_ENTRIES=3`，与 R16 冻结参数一致）
- 信号轮询：每 60s 轮询一次，引擎自动过滤未收盘 K 线并去重，只在检测到新的已收盘 15m K 线时才产生信号
- 环境：`OKX_FLAG=0`（实盘）

## 2. 启动 / 停止

```bash
cd /home/ecs-user/zzs/project
python run.py          # 启动（端口 9002）
```

停止：`Ctrl+C`（lifespan 会自动保存任务、停止调度器）。

## 3. 监控

```bash
# 心跳 + 看门狗日志
tail -f logs/autobot.log | grep -E "HEARTBEAT|WATCHDOG|R16|保护单"

# 健康检查
curl http://localhost:9002/health

# 任务 / 仓位 / 历史
curl "http://localhost:9002/status"
curl "http://localhost:9002/positions"
curl "http://localhost:9002/position_history?symbol=ETH-USDT-SWAP&limit=20"

# 爆仓风险
curl "http://localhost:9002/liquidation_status?symbol=ETH-USDT-SWAP"
```

## 4. 关键日志关键字

| 关键字 | 含义 |
|---|---|
| `[R16] 开long/short @ ... TP=.. SL=.. algo=..` | 开仓 + 已挂保护单 |
| `[R16] 加long/short 第N次` | 同向加仓 + 已改保护单 |
| `[R16] 开...`（反手） | 撤保护→平旧→开反向 |
| `[R16] 保护单挂单失败` / `改保护单失败` | 已触发兜底平仓 |
| `[R16] 保护单缺失，补挂` | 巡检发现裸仓并补挂 |
| `[对账] R16 ... 已平（保护单/外部）` | 检测到仓位被保护单/外部平掉 |
| `[HEARTBEAT]` / `[WATCHDOG]` | 进程存活 / 卡死告警 |

## 5. 配置项（`.passwd` 或环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MARGIN_MODE` | cross | 保证金模式 |
| `DEFAULT_RATIO` | 0.03 | 全仓每仓保证金比例（3%） |
| `DEFAULT_LEVERAGE` | 100 | 杠杆 |
| `POSITION_MODE` | long_short_mode | 持仓模式（long_short_mode=双向 / net_mode=单向） |
| `TP_SL_TRIGGER_TYPE` | last | 保护单触发价类型 |
| `MAX_SAME_DIRECTION_ENTRIES` | 3 | 同向最大入场次数 |
| `PROTECTION_FAIL_ACTION` | close_position | 保护单失败处置 |
| `OKX_FLAG` | 0 | 0=实盘 1=模拟盘 |

## 6. 风险与兜底

- 挂单失败/改单失败 → 立即市价平仓（不会裸仓）。
- 反手必须先确认旧仓归零再开反向，否则中止。
- 保护单巡检每 30s 一次，发现缺失自动补挂。
- 同一根 15m K 线去重（进程内 + Redis 跨重启），防止重复开仓。

## 7. 已知执行差异（与回测）

- 回测在同一 15m 收盘价反手；实盘是「平旧→确认→开新」两步，价格可能不同。
- 回测用 K 线收盘路径；实盘用 `last`（最新价）触发 TP/SL。
- 手续费按交易所实际返回，需人工核对与回测口径（开 0.05% / 平 0.02%）的差异。
