# HANDOFF.md — R2 整体替换 R16（已上线运行）

> 更新: 2026-09-03 19:2x。R2 替换 R16 已在 ETH+BTC 实盘上线并稳定运行。

## 需求（最终）
- R2/R3 入场 = 交易所驻留条件触发单（不做 1 分钟逐 bar 判断）。只做 R2，R3 暂缓。
- **R2 整体替换 R16**，ETH+BTC 统一（用户拍板"BTC 也换 R2"）。
- R2 资金: MARGIN_PCT 3%（equity×3%→100x→名义3x/次，同向≤2，名义≤6x）。
- 不加人为限制；成交后允许继续收新意图（上限由 R2 自身约束）。

## 线上状态（已实测）
- 服务: `python3 -u run.py` pid 98286（端口 9002），OKX_FLAG=0 实盘，MARGIN_MODE=isolated。
- 活跃任务 2: `15:okx:ETH-USDT-SWAP:eth_spike_fourshape_r2_15m` + `15:okx:BTC-USDT-SWAP:eth_spike_fourshape_r2_15m`。
- Redis `schedule_task` = R2×2；Redis 无 pos、无挂单；心跳/巡检正常；重启后无 ERROR。
- 信号轮询每 60s、R2 意图巡检每 30s（日志 `[..] 止损执行: 无R2挂单意图 | 无仓位，无需R2保护巡检`）。

## 关键实现（本会话代码改动）
1. `autobot/strategies/eth_spike_r2_strategy.py`：交付文件入包（与根目录 `eth_spike_r2_strategy.py` 同内容）。
2. `strategies/__init__.py`：注册 R2 + 保留 R16（供仍挂 R16 的任务回退）。
3. `exchange/okx_trader.py`：
   - `place_stop_entry_order`：conditional 入场单，**统一 slTriggerPx**（实盘探针确认: buy 触发须高于现价、sell 须低于现价；tpTriggerPx 用于 buy 向上会报 51277）。
   - `open_market_size`（指定张数市价开）、`get_usdt_equity`。
4. `core/engine.py`：
   - `R2_METHOD`、`PROTECTED_METHODS=(R16,R2)`；reconcile/收盘过滤/去重通用化（R16 保留旧 `r16:lastbar:` key）。
   - `_handle_signal` 对 R2 走 `_r2_new_bar`（INTENTS 下 generate_signal 恒 NO_SIGNAL）。
   - R2 执行层：`_r2_new_bar`(F1/F2/F4 挂条件单+F3 开盘市价入) / `_r2_monitor_intents`(失效/超时/反向占用撤单+成交对账+孤儿条件单清理) /
     `_r2_establish/_r2_add/_r2_market_enter`(成交后真实均价挂/改 TP0.6/SL0.9 OCO) / `_r2_check_protection`(裸仓补挂) / `_r2_stop_check`。
   - 组合约束：持反向仓不挂/撤；同向达上限不挂/撤；无反手。现价已越过触发价不挂。
   - 意图存 `r2:intents:{ex}:{sym}:{method}`，algoClOrdId `R2{ts}{shape}{L|S}` 幂等。
5. `~/.passwd`：清理重复 CONTRACT_SIZE；追加 FOURSHAPE_*（MARGIN_PCT/3/100/2/6/0.1/1/1/INTENTS）。备份 `.passwd.bak_20260903`。
6. 测试 `test_r2_execution.py` 11 用例 + `test_r16_execution.py` 9 用例全部通过。
7. 实盘探针 `experiments/r2_conditional_probe.py` 已跑通（buy/sell 各 1 张极远触发价挂单→可见→撤销），已验证语义。

## 运行语义/风险备注
- 条件单触发到引擎同步持仓最多滞后 ≤30s（此窗口裸仓无保护单）——R2 接受项，后续可把 stop_loop 调快。
- F3"下一开盘"用处理时点现价≈开盘价近似（≤60s 延迟），距离上限内才入，超限不入。
- BTC 任务同样跑 R2（四形态是纯 K 线形态，与币种无关；engine `_r2_size` 自动按交易所真实 ctVal 换算张数并告警，见日志）。
- 孤儿条件单安全网：巡检会清理 algoClOrdId 以 R2 开头、不在当前意图集合的驻留单（防重启/Redis 丢失后远期误触发）。
- `test_eth_spike_r16_strategy.py` 单测失败为**既有问题**（import 不存在的 `deliverables` 模块），与本次改动无关。

## 下一步（建议）
1. 观察 1~2 根真实 R2 信号全链路（挂条件单→触发/失效→成交→TP/SL OCO），重点看 30s 巡检日志。
2. 若稳定，把 R2 执行说明补进运行手册（现 R16 相关文档 `R16_LIVE_RUNBOOK.md` 已过期，勿参照其任务/模式描述）。
3. 实盘出现首个成交后，核对 TP/SL 价格取整与持仓张数。

## 2026-09-04：修复「半成品 K 线竞态导致 R2 漏/误判信号」（本会话）
### 现象
- 用户反馈：R2 上线（9-03 19:17）后应有多笔开仓，实际一笔未开。
- 日志佐证：22 小时仅 1 条 R2 记录（9-03 23:45 BTC `F2/short 触发距离过远忽略 0.297%>0.25%`）；Redis 始终无 `r2:intents:*`；无 ERROR。

### 诊断
- Redis `lastbar:*` 与 DB 时间轴核对正常（无卡死；当时 lastbar=13:45 UTC 即最新已收盘 bar）。
- 离线重放 `experiments/r2_replay_audit.py`（DB 权威数据逐收盘 bar 调 `new_pending_entry_intents`/`next_open_entry_intents`）：
  - 引擎上线以来理论意图仅 2 条：ETH 09-04 08:30 UTC F2/short（dist 0.210%<0.25%，**本应挂单**）+ BTC 09-03 ~20:00 UTC F2（dist 0.297% 超限，理论忽略）。
  - 引擎实际只处理 BTC 那条（忽略）；**ETH 08:30 理论意图引擎无任何处理痕迹（漏挂）**。
- 根因（数据管道竞态）：`update_latest_data.py` 每 5min 拉 OKX 最近 bars 并 upsert，**bar 开始数秒即写入半成品（confirm=0），直到 bar 结束后 ~3s 才 upsert 最终值（confirm=1）**。引擎 `_handle_signal` 只用 `open_time+15min <= now` 判断已收盘 → 在 bar 刚结束的 0~3s 窗口会把半成品当已收盘处理并 `set lastbar` 永久标记，之后 cron 写入最终值也不再重试 → 基于半成品 OHLC 的形态判定与实际 bar 不符：ETH 08:30 半成品被判空（漏挂）、BTC 20:00 半成品误成 F2 又因距离忽略（DB 最终值其实不是 F2）。
- 佐证：`kline_data.updated_at` 显示 ETH 08:30 UTC bar created=08:30:02、updated=08:45:03；confirm 列历史干净（每周期仅当前进行中 1 根=0，其余全 1）。

### 修复（改动 1 个文件）
- `autobot/data/db_reader.py`：`get_data()` SQL WHERE 增加 `AND k.confirm = 1`（只返回 OKX 已确认收盘 K 线）+ 注释说明。引擎时间剔除保留作双保险。
- 未改 engine/管道。读取端过滤后，半成品即使入库也不被策略消费；引擎对新收盘 bar 的处理延迟变为「最多等 cron 下一轮 upsert confirm=1」（≈0~5min，平均 ~2.5min），R2 低频可接受。

### 第二波加固（2026-09-04，防"开不出单且看不见"复发）
用户要求确保不再出现之前"应开仓却开不出、且无痕迹"的问题。对全链路静默失败点复查后新增 2 处加固（`autobot/core/engine.py`，R2 分支）：
1. **每根新收盘 bar 处理结论固定打 info**：`[R2] {symbol} 处理收盘bar {bar}: {结论}`。此前无候选时只写 debug，运维/用户完全看不到引擎是否逐根评估、卡在哪一环 → 之后任何"该开没开"能第一时间在日志定位。
2. **数据管道停滞看门狗**：db_reader 只读 confirm=1 后，若 cron/update_latest_data.py 停摆，引擎将永远等不到新 bar 且无任何报错（lastbar 冻结、60s 轮询全静默 return）。现检测「最新已收盘 K 线距今 > max(2×interval, 3600s)」→ 打 ERROR。
审计其余链路确认无新增静默点：挂单 API 成败均 logger.info/error（okx_trader）；`_r2_size` 张数为 0 / equity 异常走 log_trade 双写可见；条件单消失对账、孤儿条件单清理、裸仓保护补挂兜底均已存在。
- 已知接受项（见上）：条件单触发→引擎对账同步 ≤30s 裸仓窗口；新 bar 处理延迟 ≤1 cron 周期；F3 用处理时点现价≈开盘近似。

### 验证
- `db_reader.get_data(ETH,15min,130)` 现返回 130 根全 confirm=1，df[-1]=最新已收盘（进行中 14:15 UTC 被滤除）。
- `python3 -m unittest test_r2_execution.py test_r16_execution.py` → 20/20 OK（含加固改动后复跑）。
- 重启服务（两次）：pid 98286 → 110251 → **111126**（`logs/run_stdout.log`），启动对账干净、2 任务恢复、0 ERROR、lastbar 正常推进（14:30 UTC）。
- 加固后首个新 bar（15:00 UTC/本地 18:30 收盘）处理时日志应出现 `[R2] ... 处理收盘bar ...`。

### 观察点（修复后）
- BTC 09-04 13:45 UTC F3 候选（probe 确认 candidate=1）：确认窗口 14:00（未突破）+ 14:15 UTC bar（本地 18:00 收盘后由引擎处理，若收盘突破 13:45 bar 实体上沿 → next_open intent → 本地 ~18:15 现价≈开盘距离内市价入场）。
- 之后首个真实 F1/F2/F4 挂单应出现在日志 `[R2] ... 已挂条件单` + `R2意图巡检`，Redis 出现 `r2:intents:*`。
- 保留审计工具：`experiments/r2_replay_audit.py`（可重跑看理论意图）。

## 2026-09-08：真实根因——账户资金不足，所有信号被"张数=0"拦截（第三层）
### 关键时间基准澄清
- 机器时钟在会话中曾跳变（显示 9-04，真实为 9-08）。**以 epoch/DB 为准：当前真实时间 2026-09-08**。DB 数据完整（至 9-08 13:45 UTC bar），研究事件 9-05~9-07 均为真实行情。
### 现象
- 引擎逐根正常处理（日志 9-05~9-07 各 192 条"处理收盘bar"，无遗漏）；信号也一直在产生，但**全部被 `张数为0(资金/名义上限)跳过` 拦截**：
  - 9-05~9-07 共 11 次信号（ETH F1×1/F2×7、BTC F2×3），与研究引擎事件基本吻合；
  - 附带 `策略面值cfg(0.1)!=交易所(0.01)` WARNING（BTC ctVal 为 0.01，引擎已自动改用交易所面值，属正常）。
### 根因
- **账户 USDT equity 仅 32.06**。R2 冻结口径 MARGIN_PCT=3% × 100x：
  - 每笔名义 = 32.06×3%×100 ≈ **96 USDT**；
  - ETH 1 张 = 0.1 ETH×~2450 ≈ **245 USDT 名义** → raw 0.39 张 → floor(step=1)=0 < min_contracts(1) → **0 张**；
  - BTC 1 张 = 0.01 BTC×~77780 ≈ **778 USDT 名义** → 更开不起。
- 回测/研究用大资金口径（如文档示例 10000 USDT → 每笔 1.2 张 ETH）故显示多次开仓；32 USDT 实盘在此口径下**数学上不可能开仓**。非引擎 bug。
### 改动（engine.py，仅日志可观测性）
- `_r2_size` 返回 0 的两处调用点（F1/F2/F4 与 F3）现在会打印明确原因：
  `张数为0(资金不足): equity=32.06 该口径名义≈96.2USDT，需≥1张(≈245USDT/张，min_contracts=1)`；equity≤0 时打 ERROR（区分"API/账户异常"与"资金不足"）。
- 之前笼统 INFO `张数为0(资金/名义上限)跳过` 是误导排障（让人以为引擎/策略故障）的元凶之一。
### 需要用户拍板（未执行）
- 方案 A（推荐）：向 OKX 交易账户加金。按 3%×100x 口径：ETH 至少 ~85 USDT、BTC 至少 ~260 USDT 才够开 1 张；建议 ≥300 USDT 留余量。
- 方案 B：修改 R2 仓位口径（如 FIXED_MARGIN_USDT / 提高 POSITION_VALUE / min_contracts 用小数步长）。**注意 R2 是冻结对照策略，改参数将脱离回测基准，需用户明确同意。**

### 2026-09-08（下）：用户拍板"最小张兜底"——名义不足 1 张时开最小可开张数（已实施）
- 用户明确不接受"计算不足 1 张就跳过"，要求按名义仓位取"能开的最小张数"。
- 修改 `calculate_order_size`（**autobot/strategies/eth_spike_r2_strategy.py 与根目录 eth_spike_r2_strategy.py 同步**）：
  - 当取整后 contracts < min_contracts 时，若 `min_contracts 张名义 ≤ (6x总名义上限-已用)` 且 `其保证金(名义/杠杆) ≤ equity`，则**兜底到 min_contracts 张**；否则仍返回 0。
  - `engine._r2_size` 增加"名义超配提示"WARNING：兜底造成实际名义 > 目标名义（3%×100x）时打印 equity/目标/实际名义与保证金。
- 资金门槛变化（当前账户 52.06 USDT）：
  | equity | ETH(每张≈245名义) | BTC(每张≈778名义) |
  |---|---|---|
  | 52（现在） | **1 张**（4.7x 权益, 保证金 2.45）| 0（15x>6x 上限, 需权益≥130）|
  | 130 | 1 张 | 1 张（6x 权益）|
  | 200 | 2 张 | 1 张 |
- 单测 20/20（含 test_r2_r3_strategy 150 张断言）通过；大资金路径不受兜底影响；服务重启 pid **159761**，0 ERROR。
- 观察：下一 ETH 信号应出现 `名义超配提示` WARNING + `挂条件开仓单成功 ... sz=1` + Redis `r2:intents:*`。

### 2026-09-08（终）：交易所支持 0.01 小数张——全链路去掉 int 截断（已实施+实盘探针验证）
- **用户问"能否开 0.1/0.01 张"→ 能**：OKX ETH/BTC-USDT-SWAP `lotSz=0.01, minSz=0.01`（查 instruments 确认），张数精度两位小数。
- 原代码多处 `int(sz)/str(int(sz))` 把小数截断成整数 → **又一个导致小资金开不出/名义失真的 bug**。已修复：
  - `okx_trader.py`：新增 `_fmt_sz()`（0.01 步长去尾零）；`open_market_size` 去 int、校验 `sz<0.01` 才拒；`place_stop_entry_order`/`place_protective_orders`/`amend_protective_orders` 的 sz 全部走 `_fmt_sz`；`get_min_size` int→float。
  - `engine.py`：`_r2_size` 返回 `round(contracts,2)`；`_r2_try_place` 张数 round 2 位。
  - 策略 `calculate_order_size`：`contract_step/min_contracts` 默认 `1.0→0.01`（root 与包内同步）。上一轮的"最小张兜底"在 min=0.01 下极少触发（raw<0.01 才兜底）。
- **实盘探针**（`experiments/okx_fractional_sz_probe.py`，远触发价挂撤，无风险）：conditional sz=0.01 挂单成功、oco 0.01 张挂单成功，均立即撤销。
- 资金档位实测（3%×100x 精确匹配）：
  | equity | ETH(每张≈245名义) | BTC(每张≈778名义) |
  |---|---|---|
  | 52（当前）| **0.63 张**(名义154) | **0.2 张**(名义156) |
  | 100 | 1.22 张 | 0.38 张 |
  | 200 | 2.44 张 | 0.77 张 |
- 单测 20/20；语法 OK；服务重启 pid **170024**，2 任务、无仓、0 ERROR。
- **此后 52 USDT 账户 ETH/BTC 信号即可按 3% 名义精确挂条件单（0.63/0.2 张），无需加金。**

## 2026-09-11：过去 7 天对账 + 第四根因（.passwd 覆盖 + 时区过期 bug）
### 关键教训
- 上轮"小数张修复"只在交互脚本验证，**服务从未生效**：`config.py` 会把 `.passwd` 注入 `os.environ`（`if key not in os.environ`），而 `.passwd` 显式 `FOURSHAPE_CONTRACT_STEP=1 / FOURSHAPE_MIN_CONTRACTS=1` 覆盖了代码默认 0.01。
### 过去 7 天（9-04~9-11）实盘 vs 研究对账（研究事件 ETH 9 + BTC 4，实盘 0 单）
| 研究事件(UTC) | 实盘处理结果 | 原因 |
|---|---|---|
| ETH F1 9-05 17:30 | 9-05 21:16 F1/short 张数为0 | 资金+整数张 |
| ETH F2 9-05 22:00 | 9-06 01:46 张数为0 | 同上 |
| ETH F2 9-06 02:00 | 9-06 05:46 张数为0 | 同上 |
| ETH F2 9-06 05:45 | 9-06 08:01 张数为0 | 同上 |
| ETH F2 9-07 13:15 | 9-07 17:00 张数为0 | 同上 |
| ETH F2 9-07 13:45 | 9-07 17:30 张数为0 | 同上 |
| ETH F2 9-09 04:45 | 9-09 08:31 **意图已过期** | **时区 bug** |
| ETH F3 9-10 13:00 | 9-10 17:01 开盘距离过远0.34% | 策略正常过滤 |
| ETH F4 9-10 14:00 | 9-10 17:46 **意图已过期** | **时区 bug** |
| BTC F3 9-04 13:45 | 无日志 | 半成品竞态(修复前) |
| BTC F2 9-05 17:30 | 9-05 21:16 张数为0 | 资金+整数张 |
| BTC F2 9-07 00:00 | 9-07 03:46/04:01 张数为0 | 同上 |
| BTC F4 9-10 14:00 | 9-10 17:46 张数为0 | 同上 |
### 第四根因：`_r2_epoch` 时区错配
- 意图的 created/expires 来自 df.index（UTC-naive）；`datetime.fromisoformat(...).timestamp()` 按本机 +0330 解释 → expires 提前 3.5h → **能算出张数的意图一挂就"意图已过期"**，monitor 也会提前撤单。
- 修复：`_r2_epoch` 对 naive 时间 `replace(tzinfo=timezone.utc)`；engine 增加 `timezone` import。
### 本轮修复
1. `.passwd`：`FOURSHAPE_CONTRACT_STEP 1→0.01`、`FOURSHAPE_MIN_CONTRACTS 1→0.01`（备份 `.passwd.bak_20260911`）。**这是让小数张真正生效的关键。**
2. `engine._r2_epoch` UTC 解析修复 + `timezone` import。
3. "张数为0"日志每张名义改用交易所真实面值（此前 BTC 误显示 7714 USDT/张，实为 ~778）。
### 验证
- 经 `.passwd` 注入后实测 `cfg.contract_step=0.01, min_contracts=0.01`；`_r2_epoch('2026-09-09T05:30:00')` = 正确 UTC epoch。
- 单测 20/20；服务重启 pid **191078**（监听 9002），2 任务、无仓、0 ERROR。
- 预期：下一信号应出现 `挂条件开仓单成功 ... sz=0.63`(ETH)/`sz=0.2`(BTC) → `r2:intents:*` → 触发 → OCO。
### 教训（防复发）
- 改配置默认值前必须确认 `.passwd`/环境是否有显式覆盖；验证必须在**服务进程同口径**（经 config 注入）下做，不能只用裸 shell 脚本。

### 端到端真实验证（2026-09-11，`experiments/verify_r2_order_path.py`）
不再只做代码审查，用真实账户走通挂单链路（远触发价、挂后立即撤、无成交）：
```
账户权益=52.06 | 配置 step=0.01 min=0.01 mode=MARGIN_PCT value=3.0 lev=100.0
[1] _r2_epoch(UTC+30min) 与 now 差=1800s  OK(未过期)      # 时区修复生效
[2] ETH price=2457 -> _r2_size=0.63 张 | BTC -> 0.2 张     # 小数张生效
[3] _r2_try_place -> 挂条件开仓单成功: ETH buy/long sz=0.57 trigger=2703.2 algoId=3912528312536055808 -> 已撤销
```
- 交易所确认无仓位、无 conditional/oco 残留挂单。
- 已真实验证：服务同口径配置、过期判定、小数张计算、真实条件单挂单/撤销。
- 尚未验证（需等真实信号）：完整流水线「信号 bar 处理→自动挂单→触发成交→OCO 保护单」，以及 monitor 成交通道。各独立环节已分别验证，但未在实盘串跑一次。

## 2026-09-11：人工实测真实开仓+挂单（⚠️ 当前有持仓，未平）
用户要求人工实测一次真实开仓/挂单以便查看。已用 `experiments/r2_live_open_test.py` 完成：
- **持仓**：ETH-USDT-SWAP **long 0.63 张 @ 均价 2459.66**（100x isolated，保证金≈1.55 USDT，名义≈155 USDT）
- **保护单**：OCO algoId=`3912531496885207040`，sz=0.63，TP=2474.42(+0.6%)，SL=2437.52(-0.9%)，state=live
- **服务已接管**：09:05:35 起日志 `R2保护巡检完成: 检查1个方向`；`/position` 与交易所状态一致（position_manager 已登记 open_entries=1、algo_order_id）。
- 这笔为**人工测试仓**（非策略信号触发），`_r2_establish` 路径已实盘验证成功。
- **待用户决定**：保留观察或平仓。平仓方式：`curl "localhost:9002/close?exchange=okx&symbol=ETH-USDT-SWAP&method=eth_spike_fourshape_r2_15m&direction=long"`（或让 Agent 执行；平仓后服务对账会与交易所同步）。
- **结算（2026-09-11 09:07）**：用户已在 OKX 手动平掉该测试仓。服务对账记录 `[对账] eth_spike_fourshape_r2_15m ETH-USDT-SWAP 多仓已平（保护单/外部）`；交易所无仓、无 conditional/oco 残留挂单，本地记录已清。人工实测闭环完成：**市价开仓(小数张)→读真实均价→挂 OCO→服务接管巡检→手动平仓→服务对账清仓**，全链路实盘跑通且状态干净。
### 验证
- `test_r2_execution.py` + `test_r16_execution.py` 20/20；语法 OK；服务重启 pid **157943**，2 任务、无仓、0 ERROR。
- 加金前任何 R2 信号仍会以清晰日志被拦截（现在原因可见）。

## 关键命令
- 状态: `curl -s localhost:9002/status`、`curl -s localhost:9002/strategies`、`curl -s localhost:9002/positions`
- 日志: `tail -f logs/autobot.log | grep -E "R2|HEARTBEAT|保护单|意图"`
- 测试: `python3 -m unittest test_r2_execution.py test_r16_execution.py`
- 重启: 先 `kill <pid>`（注意 lifespan 会把内存任务写回 Redis——若手工改过 schedule_task 需在进程退出后重设），再 `setsid nohup python3 -u run.py > logs/run_stdout.log 2>&1 < /dev/null & disown`
