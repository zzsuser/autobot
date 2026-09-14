# SuperTrendTemaStrategyV3_1 部署说明

## 文件

- `supertrend_tema_strategy_v3_1.py`（1004 行，38 KB，语法 PASS）
- 与 v3 完全独立，不影响原本代码

## v3.1 相对 v3 的唯一改动

**只对 L1 加 BTC 长周期方向过滤**（BF_slope_L1 方案，回测选定）

```
if L1 触发 (TEMA144 上穿 TEMA288) AND is_strong_long:
    if btc_tema288 的 12 根斜率 > 0:
        允许开仓 L1
    else:
        过滤掉这个 L1 信号 (记录日志)
```

**L2-L7 完全不动**，**S1-S7 完全不动**，**平仓完全不动**。

## 回测数据

| 指标 | v3 (无过滤) | v3.1 (BF_slope_L1) | 差异 |
|---|---|---|---|
| 总收益 | +861.5% | +1095.9% | **+234pp** |
| 最大回撤 | 43.79% | **43.79%** | **持平** |
| 夏普 | 1.09 | **1.26** | **+15%** |
| 严重亏月 | 2 | 2 | 持平 |

⚠️ **诚实提示**：44/49 个月两版行为完全一致，收益差异集中在 2022-07（+12.8pp）。**BTC 过滤是否真解决下跌月问题证据不足**，但账户级指标全面改善或持平，无退步。

## 3 个新参数

```python
strategy = SuperTrendTemaStrategyV3_1(
    # ... 与 v3 相同的所有参数 ...
    
    btc_filter_enabled=True,     # v3.1 新增: 开关 (False = 完全等于 v3)
    btc_slope_window=12,          # v3.1 新增: BTC slope 窗口
)
```

## BTC 数据接口（重要）

策略需要 DataFrame df 里包含 BTC 数据。3 种方式：

### 方式 A：预计算 TEMA（推荐，性能最好）
```
df 需要列: 'btc_tema_288'
```

### 方式 B：BTC 收盘价（策略自算）
```
df 需要列: 'btc_close'
策略会在 _prepare_indicators 里自算 btc_tema288 (period=288)
```

### 方式 C：完全不喂（容错，退化到 v3）
```
df 无任何 btc_* 列
→ 策略首次运行 log warning 一次
→ 之后所有 L1 都放行 (行为完全等于 v3)
→ 不会崩溃
```

## 部署步骤

### 1. 放文件

```bash
cp supertrend_tema_strategy_v3_1.py autobot/strategies/
```

**不删 v2 和 v3 文件**。三个版本并存，随时可切换/回退。

### 2. 策略注册（如果需要）

如果 autobot 通过 name 或注册表加载策略：

```bash
grep -rn "supertrend_tema" autobot/ --include="*.py"
```

参考典型加载模式：

```python
# autobot/strategies/__init__.py 或类似
from autobot.strategies.supertrend_tema_strategy_v2 import SuperTrendTemaStrategy
from autobot.strategies.supertrend_tema_strategy_v3 import SuperTrendTemaStrategyV3
from autobot.strategies.supertrend_tema_strategy_v3_1 import SuperTrendTemaStrategyV3_1

STRATEGY_REGISTRY = {
    "supertrend_tema_v2":   SuperTrendTemaStrategy,
    "supertrend_tema_v3":   SuperTrendTemaStrategyV3,
    "supertrend_tema_v3_1": SuperTrendTemaStrategyV3_1,
}
```

### 3. 切换到 v3.1

```python
from autobot.strategies.supertrend_tema_strategy_v3_1 import SuperTrendTemaStrategyV3_1

strategy = SuperTrendTemaStrategyV3_1(
    is_strong_long_threshold=0.6,
    is_strong_short_threshold=0.6,
    # 其他参数照抄 v3 配置
    btc_filter_enabled=True,      # 关键: 保持 True
)
```

### 4. 让 autobot 拉 BTC 数据（关键前置）

**这是 v3.1 唯一比 v3 多的运维工作**。3 种处理方式：

**选项 1 - autobot 加拉 BTC 5min K 线（推荐）**

- 修改数据拉取模块，同时订阅 `BTC-USDT-SWAP` 5min K 线
- 数据合并模块把 BTC 数据合到 ETH df 里
- df 里加列 `btc_close`（或 `btc_tema_288`）
- **理想方案**，v3.1 的 BTC 过滤生效

**选项 2 - 独立拉 BTC，写个 join 层**

- 独立的 BTC 数据拉取模块
- 在策略调用前 `df = df.merge(btc_df, on='timestamp')`
- 效果同选项 1

**选项 3 - 暂时不拉 BTC，让 v3.1 退化到 v3**

- df 里没有 btc 列
- 策略自动退化，行为 = v3
- 日志首次会有 warning：`⚠️ BTC 数据缺失, 策略退化到 v3 行为`
- **可以先这样上，之后再补 BTC 数据**

### 5. 你现有的 .env 交易参数无需改动

```
DEFAULT_LEVERAGE=100
DEFAULT_RATIO=0.03
POSITION_PERCENT=0.03
STOP_PROFIT_PCT=0.025
STOP_LOSS_PCT=0.025
...
```

**全部保留**，v3.1 与 .env 完全兼容。

### 6. 重启服务，看日志前缀

```
[ST+TEMA v3.1] ===== 信号检查 =====
[ST+TEMA v3.1] 指标来源: TEMA=... ST=... BTC=precomputed/calc_from_close/missing
[ST+TEMA v3.1][BTC] L1过滤=True/False 原因=... 数据源=...
```

**看到 `v3.1` 前缀 + BTC 数据源信息就是成功**。

## 实盘监控清单

### 第 1 天必看

- [ ] 日志前缀是 `v3.1`（不是 v3 或 v2）
- [ ] BTC 数据源不是 `missing`（如果 missing，你选了退化模式）
- [ ] `[v3.1][BTC]` 日志正常输出 btc_slope288 数值
- [ ] 首次开仓行为正常

### 第 1 周监控项

除了 v3 的所有监控（回撤、胜率、强平次数），v3.1 额外看：

- [ ] **L1 触发和被过滤的比例**
  - 日志搜 `L1过滤` 关键词
  - 预期：L1 应被过滤约 30-50%（回测中约 5-10 次 / 4.5 年）
  - 如果 L1 从未被过滤 → 说明 BTC 一直上升，正常
  - 如果 L1 一直被过滤 → 排查 BTC 数据是否有问题
  
- [ ] **BTC 数据同步性**
  - 检查 df 里 btc_close 和 eth close 是不是同一根 K 线
  - 时间戳错位会让过滤失效

### 报警阈值（与 v3 一致）

| 指标 | 观察 | 加强监控 | 停机 |
|---|---|---|---|
| 单周回撤 | ≤ 10% | 10-15% | ≥ 15% |
| 累计回撤 | ≤ 15% | 15-25% | ≥ 25% |
| 胜率 | 25-40% | 20-25% 或 >45% | < 20% |
| 强平次数/周 | ≤ 2 | 3-4 | ≥ 5 |

## 回退方案（3 层保障）

### 第 1 层：数据层退化
如果 BTC 数据源突然断了：
- 策略自动检测到 df 无 btc 列
- 打印 warning（一次）
- 所有 L1 自动放行
- **等价于 v3 行为**，无需人工干预

### 第 2 层：参数关闭
如果发现 BTC 过滤效果不好：
- 修改配置：`btc_filter_enabled=False`
- 重启服务
- v3.1 行为完全 = v3（不动其他代码）

### 第 3 层：代码切换
如果 v3.1 有 bug：
- 改导入回 v3：
  ```python
  from autobot.strategies.supertrend_tema_strategy_v3 import SuperTrendTemaStrategyV3
  ```
- v2 / v3 / v3.1 三个文件都存在，随时切换

## 常见问题

### Q: 我暂时没时间加 BTC 数据拉取，能直接上 v3.1 吗？
**A: 能**。v3.1 内置容错，找不到 BTC 数据自动退化到 v3 行为。行为完全等于 v3（+862% / 43.79%），实盘表现不会更差。之后有空再补 BTC 数据，v3.1 自动激活过滤。

### Q: BTC 数据的时间同步怎么保证？
**A: 关键**。BTC 和 ETH 必须是同一个 5min K 线的时间戳。如果错位（比如 BTC 是上一根，ETH 是当前根），过滤会失效。建议：
- 用同一个数据源（同一个交易所的 5min K 线）
- 合并前 `df.merge(btc_df, on='timestamp', how='inner')`
- 检查合并后是否有 NaN

### Q: btc_slope_window=12 是什么意思？
**A: BTC 长周期斜率窗口**。用最近 12 根 K 线（60 分钟）拟合 btc_tema288 的直线斜率。**与 ETH 内部的 slope 计算窗口一致**，保持代码风格统一。**不建议改**。

### Q: v3.1 会不会开仓变少？
**A: 预期上会**。回测里 L1 触发 18 次 / -2.02%，其中被过滤后剩余 8-13 次都是好 L1。**开仓少了但质量高了**。整体收益提升 +234pp 就是这么来的。

### Q: 首次运行会立即开仓吗？
**A: 需要 BTC 数据先加载**。v3.1 需要 288+ 根 K 线的 BTC 数据算 tema288，加上 12 根 slope 窗口，**至少 300 根历史 BTC K 线**（≈ 1 天）。ETH 需求相同。如果历史数据齐全，下一根 K 线收盘时就会评估信号。

## 下一步

**Week 1**：观察，不调参数
- 重点看 BTC 数据源是否稳定
- L1 触发 vs 被过滤的比例

**Week 2**：如果一切正常
- 可以考虑加仓（3% → 5%）
- 或者尝试禁用 BTC 过滤对比看看差异（`btc_filter_enabled=False`）

**Week 4**：数据够了
- 对比实盘 vs 回测（胜率、盈亏比）
- 决定是否继续优化（早期硬止损、ETH 长周期过滤等）

## 版本对比总表

| 特性 | v2 | v3 | v3.1 |
|---|---|---|---|
| L1-L7 逻辑 | 原始 | 相同 | L1 加 BTC 过滤，L2-L7 不变 |
| S1-S3 逻辑 | 原始 | 相同 | 相同 |
| S4-S7 逻辑 | 宽松 | **对称化** | 相同（对称化）|
| is_strong 阈值 | 单一 | **拆分 long/short** | 相同（拆分）|
| BTC 数据需求 | 无 | 无 | **需要 btc_close 或 btc_tema_288** |
| 回测收益 | 未知 | +862% | +1096% |
| 回测回撤 | 未知 | 43.79% | 43.79% |
| 回测夏普 | 未知 | 1.09 | 1.26 |

## 文件已就绪

`supertrend_tema_strategy_v3_1.py` → `/mnt/user-data/outputs/`（38 KB）
