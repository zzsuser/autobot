"""
SuperTrend + TEMA 复合策略 v3.1
基于 v3 演进，新增 BTC 长周期过滤 L1（BF_slope_L1 方案）

========================================================================
【回测背景 - 与 v3 相同】
========================================================================

数据：BTC + ETH 5min K 线合并，2022-01 ~ 2026-06（49 个月）
基础配置：ATR mult=2.5, min_atr_stop=0, D1 = G 组(0.15/0.25/0.7)

v3 回测（1008 笔）：
  - 总收益: +861.5%
  - 最大回撤: 43.79%
  - 夏普: 1.09

v3.1 回测（BF_slope_L1 组）：
  - 总收益: +1095.9% (vs v3 +234pp)
  - 最大回撤: 43.79% (与 v3 相同)
  - 夏普: 1.26 (vs v3 +0.17)
  - 关键: 44/49 月与 v3 完全一致, 差异集中在 2022-07 (+12.8pp)

========================================================================
【v3.1 相对 v3 的唯一改动】
========================================================================

**只对 L1 加 BTC 长周期方向过滤**

L1 (TEMA144 上穿 TEMA288) 是 v3 里唯一负收益的多头信号:
  - v3 回测: L1 触发 18 次 / 总收益 -2.02% / 胜率 38.9%
  - L1 失败模式: ETH 反弹陷阱 (下跌行情中的假上穿)

v3.1 的改动:
  在 L1 判断处加一个 gate: btc_tema288 的 12 根斜率 > 0
  - btc_slope288 > 0 : L1 允许触发（BTC 长周期上升，ETH 反弹更可能是真的）
  - btc_slope288 ≤ 0 : L1 被过滤（BTC 长周期不涨，ETH 反弹多为假信号）

**L2-L7 完全不动, S1-S7 完全不动, 平仓完全不动**

========================================================================
【BTC 数据接口要求】（重要 - 实盘部署必读）
========================================================================

策略需要 DataFrame df 里包含 BTC 数据。以下任一格式:

方式 A: 预计算 TEMA (推荐, 性能好)
  df 需要包含: 'btc_tema_288'

方式 B: BTC 收盘价 (策略自算 TEMA)
  df 需要包含: 'btc_close'
  策略会在 _prepare_indicators 里自算 btc_tema288

方式 C: 完全不喂 BTC 数据（容错，策略退化到 v3 行为）
  df 中没有任何 btc_* 列 → 策略 warning 日志一次, 之后所有 L1 都放行
  等价于 v3 (+862% 收益)

**推荐**: autobot 数据拉取模块加拉 BTC-USDT-SWAP 5min K 线,
       与 ETH 数据合并到同一 df, 列名 'btc_close'。

DataFrame 结构示例:

  df.columns = [
      'high', 'low', 'close',      # ETH OHLC (v3 需要)
      'tema_48', 'tema_72', 'tema_144', 'tema_288',    # ETH TEMA (可选预计算)
      'supertrend_value', 'supertrend_direction',       # ETH ST (可选预计算)
      'btc_close',                  # BTC 收盘价 (v3.1 需要, 也可用 btc_tema_288 替代)
  ]

========================================================================
【v3 vs v3.1 决策矩阵】
========================================================================

场景 → 用哪个版本

- 你的 autobot 目前只拉 ETH, 加 BTC 拉取比较麻烦 → 先上 v3, 后期升级
- 你能拉 BTC 5min K 线 → 直接上 v3.1
- 你不确定 BTC 数据能否稳定拉取 → 上 v3.1 (容错到 v3 行为), 拉不到 BTC 就退化

v3.1 已内置容错: 找不到 BTC 数据时自动退化 = v3 行为, 不会崩溃。
所以: v3.1 是 v3 的严格超集, 建议直接上 v3.1。

========================================================================
【推荐启动参数（沿用 v3 配置）】
========================================================================

strategy = SuperTrendTemaStrategyV3_1(
    # 阈值（v3 默认, 保持行为一致）
    is_strong_long_threshold=0.6,
    is_strong_short_threshold=0.6,
    
    # 风控（v3 默认）
    liquidation_ratio=0.025,
    active_stop_loss=0.015,
    
    # 止盈（v3 默认）
    fixed_take_profit=0.025,
    trend_weak_profit=0.01,
    st_change_profit=0.005,
    big_take_profit=0.04,
    take_profit_multiplier=1.5,
    stop_loss_multiplier=0.75,
    
    # 其他（v3 默认）
    leverage=100,
    min_hold_bars=3,
    cooldown_bars=10,
    bar_minutes=5,
    st_period=10,
    st_multiplier=3.0,
    
    # v3.1 新增: BTC 过滤开关和参数
    btc_filter_enabled=True,     # 关掉 = 完全等于 v3 行为
    btc_slope_window=12,          # BTC slope 计算窗口 (与 ETH slope 一致)
)

========================================================================
【实盘监控关注点（v3 + v3.1 新增）】
========================================================================

除了 v3 的所有监控项，v3.1 需额外关注：

1. BTC 数据源可用性
   - 每根 K 线日志会显示: [v3.1][BTC] btc_slope288=X.XXX
   - 如果日志出现 [v3.1][BTC] BTC 数据缺失 → 排查数据拉取
   - 数据缺失时策略仍能运行(退化到 v3), 但过滤效果消失

2. L1 被过滤的频率
   - 日志: [v3.1][L1过滤] btc_slope288=-X.XXX < 0
   - 预期: 4.5 年回测里 L1 应被过滤约 5-10 次（占 L1 总触发的 30-50%）
   - 如果实盘 L1 从未被过滤 → 可能 BTC slope 计算有问题

3. L1 触发后的胜率
   - v3 里 L1 胜率 38.9%（含被过滤的失败笔）
   - v3.1 里 L1 剩下的应该胜率 > 50%
   - 累计 1 个月后统计, 低于 30% 说明过滤效果不佳

========================================================================
【回退方案】
========================================================================

v3.1 有 3 层回退保障:

1. 数据层退化: BTC 数据丢失 → 自动降级到 v3 行为 (不过滤)
2. 参数层关闭: btc_filter_enabled=False → 完全等于 v3
3. 代码层切换: 换回 v3 类 → 无差异（v3 文件保留, 未覆盖）

任何异常情况都能安全回到 v3 行为。

"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Tuple
from autobot.core.strategy_base import StrategyBase, SignalResult
from autobot.utils.logger import logger, log_trade


# =====================================================
# 指标计算工具（与 v3 一致）
# =====================================================

def calc_tema(series: pd.Series, period: int) -> pd.Series:
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3


def calc_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
                    ) -> Tuple[pd.Series, pd.Series]:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()

    hl2 = (high + low) / 2
    basic_ub = hl2 + multiplier * atr
    basic_lb = hl2 - multiplier * atr

    final_ub = basic_ub.copy()
    final_lb = basic_lb.copy()

    for i in range(1, len(df)):
        final_ub.iloc[i] = (
            basic_ub.iloc[i]
            if basic_ub.iloc[i] < final_ub.iloc[i - 1] or close.iloc[i - 1] > final_ub.iloc[i - 1]
            else final_ub.iloc[i - 1]
        )
        final_lb.iloc[i] = (
            basic_lb.iloc[i]
            if basic_lb.iloc[i] > final_lb.iloc[i - 1] or close.iloc[i - 1] < final_lb.iloc[i - 1]
            else final_lb.iloc[i - 1]
        )

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    direction.iloc[0] = 1
    supertrend.iloc[0] = final_lb.iloc[0]

    for i in range(1, len(df)):
        if close.iloc[i] > final_ub.iloc[i - 1]:
            direction.iloc[i] = 1
            supertrend.iloc[i] = final_lb.iloc[i]
        elif close.iloc[i] < final_lb.iloc[i - 1]:
            direction.iloc[i] = -1
            supertrend.iloc[i] = final_ub.iloc[i]
        else:
            direction.iloc[i] = direction.iloc[i - 1]
            supertrend.iloc[i] = final_lb.iloc[i] if direction.iloc[i] == 1 else final_ub.iloc[i]

    return supertrend, direction


def calc_slope(series: pd.Series, idx: int, window: int = 12) -> float:
    actual_window = min(window, idx + 1)
    if actual_window < 3:
        if idx >= 1:
            prev = series.iloc[idx - 1]
            return (series.iloc[idx] - prev) / max(1.0, abs(prev))
        return 0.0
    segment = series.iloc[idx - actual_window + 1: idx + 1].values
    return np.polyfit(range(actual_window), segment, 1)[0]


# =====================================================
# 策略类 v3.1
# =====================================================

class SuperTrendTemaStrategyV3_1(StrategyBase):
    """
    SuperTrend + TEMA 复合策略 v3.1

    相对 v3 唯一改动: L1 加 BTC tema288 slope > 0 过滤 (BF_slope_L1 方案)

    参数（除新增外均与 v3 一致）:
        st_period, st_multiplier, liquidation_ratio, active_stop_loss,
        fixed_take_profit, trend_weak_profit, st_change_profit, big_take_profit,
        take_profit_multiplier, stop_loss_multiplier, leverage,
        min_hold_bars, cooldown_bars, bar_minutes,
        is_strong_long_threshold, is_strong_short_threshold : 与 v3 相同

        btc_filter_enabled (v3.1 新增) : 是否启用 BTC 过滤 (默认 True)
        btc_slope_window (v3.1 新增)   : BTC slope 计算窗口 (默认 12, 与 ETH 一致)
    """

    def __init__(
        self,
        st_period: int = 10,
        st_multiplier: float = 3.0,
        liquidation_ratio: float = 0.025,
        active_stop_loss: float = 0.015,
        fixed_take_profit: float = 0.025,
        trend_weak_profit: float = 0.01,
        st_change_profit: float = 0.005,
        big_take_profit: float = 0.04,
        take_profit_multiplier: float = 1.5,
        stop_loss_multiplier: float = 0.75,
        leverage: int = 100,
        min_hold_bars: int = 3,
        cooldown_bars: int = 10,
        bar_minutes: int = 5,
        is_strong_long_threshold: float = 0.6,
        is_strong_short_threshold: float = 0.6,
        # v3.1 新增参数
        btc_filter_enabled: bool = True,
        btc_slope_window: int = 12,
    ):
        self.st_period = st_period
        self.st_multiplier = st_multiplier
        self.liquidation_ratio = liquidation_ratio
        self.active_stop_loss = active_stop_loss
        self.fixed_take_profit = fixed_take_profit
        self.trend_weak_profit = trend_weak_profit
        self.st_change_profit = st_change_profit
        self.big_take_profit = big_take_profit
        self.take_profit_multiplier = take_profit_multiplier
        self.stop_loss_multiplier = stop_loss_multiplier
        self.leverage = leverage
        self.min_hold_bars = min_hold_bars
        self.cooldown_bars = cooldown_bars
        self.bar_minutes = bar_minutes
        self.is_strong_long_threshold = is_strong_long_threshold
        self.is_strong_short_threshold = is_strong_short_threshold
        # v3.1 新增
        self.btc_filter_enabled = btc_filter_enabled
        self.btc_slope_window = btc_slope_window

        # 运行时状态
        self._last_close_ts: Optional[pd.Timestamp] = None
        # BTC 数据缺失只警告一次，避免日志洪水
        self._btc_missing_warned: bool = False

    # ---------- 接口实现 ----------

    @property
    def name(self) -> str:
        return "supertrend_tema_v3_1"

    @property
    def required_data_length(self) -> int:
        return 600

    def need_stop_check(self) -> bool:
        return False

    def check_stop(self, current_position, entry_price, current_price, **kwargs) -> SignalResult:
        return SignalResult(SignalResult.NO_SIGNAL, "策略不使用 check_stop（对齐回测）")

    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs,
    ) -> SignalResult:
        try:
            ind = self._prepare_indicators(df)
            idx = len(df) - 1

            if idx < 300:
                return SignalResult(SignalResult.NO_SIGNAL, f"数据不足({idx+1}根)")

            current_ts = df.index[idx] if hasattr(df.index, '__getitem__') else None
            current_price = df["close"].iloc[idx]
            entry_price = kwargs.get("entry_price", 0)

            logger.info(
                f"[ST+TEMA v3.1] ===== 信号检查 ===== "
                f"ts={current_ts}, price={current_price:.4f}, "
                f"pos={current_position}, entry={entry_price}"
            )

            # ---------- 有仓位：检查平仓 ----------
            if current_position != 0 and entry_price > 0:
                close_result = self._check_close_conditions(
                    df, ind, idx, current_position, entry_price, current_price, entry_index,
                    entry_timestamp=kwargs.get("entry_timestamp", None)
                )
                if close_result.signal != SignalResult.NO_SIGNAL:
                    self._last_close_ts = current_ts
                    log_trade(f"[信号] 平仓: {close_result.reason}")
                    return close_result
                else:
                    logger.info(f"[ST+TEMA v3.1] 持仓未触发平仓: {close_result.reason}")

            # ---------- 无仓位：检查开仓 ----------
            if current_position == 0:
                if self._is_in_cooldown(current_ts):
                    left = self._cooldown_bars_left(current_ts)
                    logger.info(f"[ST+TEMA v3.1] 冷却中，剩余约{left}根")
                    return SignalResult(SignalResult.NO_SIGNAL, f"冷却期({left}根)")

                open_result = self._check_open_conditions(df, ind, idx, current_price)
                if open_result.signal != SignalResult.NO_SIGNAL:
                    log_trade(f"[信号] 开仓: {open_result.reason}")
                else:
                    logger.info(f"[ST+TEMA v3.1] 未触发开仓: {open_result.reason}")
                return open_result

            return SignalResult(SignalResult.NO_SIGNAL, "持仓中，未触发平仓")

        except Exception as e:
            logger.error(f"[ST+TEMA v3.1] 策略异常: {e}", exc_info=True)
            return SignalResult(SignalResult.NO_SIGNAL, f"策略异常: {e}")

    # =====================================================
    # 冷却（与 v3 一致）
    # =====================================================

    def _is_in_cooldown(self, current_ts) -> bool:
        if self._last_close_ts is None or current_ts is None:
            return False
        try:
            elapsed_min = (
                pd.Timestamp(current_ts) - pd.Timestamp(self._last_close_ts)
            ).total_seconds() / 60.0
            return elapsed_min < self.cooldown_bars * self.bar_minutes
        except Exception:
            return False

    def _cooldown_bars_left(self, current_ts) -> int:
        if self._last_close_ts is None or current_ts is None:
            return 0
        try:
            elapsed_min = (
                pd.Timestamp(current_ts) - pd.Timestamp(self._last_close_ts)
            ).total_seconds() / 60.0
            left = self.cooldown_bars - elapsed_min / self.bar_minutes
            return max(0, int(np.ceil(left)))
        except Exception:
            return 0

    # =====================================================
    # 指标准备（v3.1: 新增 BTC 指标读取/计算）
    # =====================================================

    def _prepare_indicators(self, df: pd.DataFrame) -> Dict:
        close = df["close"]

        # ---- ETH 指标（与 v3 一致）----
        used_db_tema = "tema_48" in df.columns
        used_db_st = "supertrend_value" in df.columns and "supertrend_direction" in df.columns

        tema48  = df["tema_48"]  if "tema_48"  in df.columns else calc_tema(close, 48)
        tema72  = df["tema_72"]  if "tema_72"  in df.columns else calc_tema(close, 72)
        tema144 = df["tema_144"] if "tema_144" in df.columns else calc_tema(close, 144)
        tema288 = df["tema_288"] if "tema_288" in df.columns else calc_tema(close, 288)

        if used_db_st:
            st_value     = df["supertrend_value"]
            st_direction = df["supertrend_direction"]
        else:
            st_value, st_direction = calc_supertrend(df, self.st_period, self.st_multiplier)

        # ---- v3.1 新增: BTC 指标 ----
        btc_tema288: Optional[pd.Series] = None
        btc_source = "none"

        if self.btc_filter_enabled:
            if "btc_tema_288" in df.columns:
                # 方式 A: 直接读预计算 TEMA
                btc_tema288 = df["btc_tema_288"]
                btc_source = "precomputed"
            elif "btc_close" in df.columns:
                # 方式 B: 自算 TEMA
                try:
                    btc_tema288 = calc_tema(df["btc_close"], 288)
                    btc_source = "calc_from_close"
                except Exception as e:
                    logger.warning(f"[ST+TEMA v3.1] BTC TEMA 计算失败: {e}")
                    btc_tema288 = None
            else:
                # 方式 C: BTC 数据缺失，容错退化到 v3 行为
                if not self._btc_missing_warned:
                    logger.warning(
                        "[ST+TEMA v3.1] ⚠️ BTC 数据缺失(df 无 btc_close 也无 btc_tema_288). "
                        "策略退化到 v3 行为(不过滤 L1). 该警告只显示一次."
                    )
                    self._btc_missing_warned = True
                btc_tema288 = None
                btc_source = "missing"

        logger.debug(
            f"[ST+TEMA v3.1] 指标来源: TEMA={'DB' if used_db_tema else 'CALC'}, "
            f"ST={'DB' if used_db_st else 'CALC'}, BTC={btc_source}"
        )

        return {
            "tema48":       tema48,
            "tema72":       tema72,
            "tema144":      tema144,
            "tema288":      tema288,
            "st_value":     st_value,
            "st_direction": st_direction,
            "btc_tema288":  btc_tema288,   # v3.1 新增, 可能为 None
            "btc_source":   btc_source,     # v3.1 新增
        }

    # =====================================================
    # 辅助方法（与 v3 一致）
    # =====================================================

    def _get_slopes(self, ind: Dict, idx: int) -> Dict:
        return {
            "tema48":  calc_slope(ind["tema48"],  idx),
            "tema72":  calc_slope(ind["tema72"],  idx),
            "tema144": calc_slope(ind["tema144"], idx),
            "tema288": calc_slope(ind["tema288"], idx),
        }

    def _trend_strength(self, df: pd.DataFrame, idx: int, lookback: int = 20) -> float:
        if idx < lookback:
            return 0
        prices = df["close"].iloc[idx - lookback + 1: idx + 1].values
        up = down = 0.0
        for j in range(1, len(prices)):
            if prices[j] > prices[j - 1]:
                up += prices[j] - prices[j - 1]
            else:
                down += prices[j - 1] - prices[j]
        total = up + down
        return abs(up - down) / total if total > 0 else 0

    def _tema_alignment_bull(self, ind: Dict, idx: int) -> bool:
        return (ind["tema72"].iloc[idx] > ind["tema144"].iloc[idx] >
                ind["tema288"].iloc[idx])

    def _tema_alignment_bear(self, ind: Dict, idx: int) -> bool:
        return (ind["tema72"].iloc[idx] < ind["tema144"].iloc[idx] <
                ind["tema288"].iloc[idx])

    def _st_consistency(self, ind: Dict, idx: int, direction: int, min_bars: int = 5) -> bool:
        if idx < min_bars:
            return False
        for j in range(idx - min_bars + 1, idx + 1):
            if ind["st_direction"].iloc[j] != direction:
                return False
        return True

    def _trend_reversal_confirmed(self, ind: Dict, idx: int,
                                  direction: str, min_bars: int = 10) -> bool:
        if idx < min_bars + 1:
            return False
        for j in range(idx - min_bars + 1, idx + 1):
            d72  = ind["tema72"].iloc[j]  - ind["tema72"].iloc[j - 1]
            d144 = ind["tema144"].iloc[j] - ind["tema144"].iloc[j - 1]
            d288 = ind["tema288"].iloc[j] - ind["tema288"].iloc[j - 1]
            if direction == "down":
                if not (d72 < 0 and d144 < 0 and d288 < 0):
                    return False
            else:
                if not (d72 > 0 and d144 > 0 and d288 > 0):
                    return False
        return True

    def _short_background_decline(self, df: pd.DataFrame, ind: Dict, idx: int) -> bool:
        if idx < 6:
            return False
        cp = df["close"].iloc[idx]
        ct = ind["tema72"].iloc[idx]
        prev_prices = [df["close"].iloc[j] for j in range(idx - 6, idx)]
        prev_temas  = [ind["tema72"].iloc[j] for j in range(idx - 6, idx)]
        return cp >= max(prev_prices) and ct <= min(prev_temas)

    def _long_background_rise(self, df: pd.DataFrame, ind: Dict, idx: int) -> bool:
        if idx < 12:
            return False
        cp = df["close"].iloc[idx]
        ct = ind["tema72"].iloc[idx]
        prev_prices = [df["close"].iloc[j] for j in range(idx - 12, idx)]
        prev_temas  = [ind["tema72"].iloc[j] for j in range(idx - 6, idx)]
        return cp <= min(prev_prices) and ct >= max(prev_temas)

    def _three_line_upward(self, df: pd.DataFrame, ind: Dict,
                           idx: int, tema72_slope: float) -> bool:
        if idx < 12:
            return False
        price_chg = (df["close"].iloc[idx] - df["close"].iloc[idx - 6]) / df["close"].iloc[idx - 6]
        if price_chg < 0.008 or abs(tema72_slope) < 0.0003:
            return False
        for p in range(3):
            ci = idx - p
            tp = 6 + p
            if ci < tp:
                return False
            if not (df["close"].iloc[ci] > df["close"].iloc[ci - tp] and
                    ind["tema48"].iloc[ci]  > ind["tema48"].iloc[ci - tp] and
                    ind["tema72"].iloc[ci]  > ind["tema72"].iloc[ci - tp] and
                    ind["tema288"].iloc[ci] > ind["tema288"].iloc[ci - tp]):
                return False
        return True

    def _three_line_downward(self, df: pd.DataFrame, ind: Dict,
                             idx: int, tema72_slope: float) -> bool:
        if idx < 12:
            return False
        price_chg = (df["close"].iloc[idx] - df["close"].iloc[idx - 6]) / df["close"].iloc[idx - 6]
        if price_chg > -0.008 or abs(tema72_slope) < 0.0003:
            return False
        for p in range(3):
            ci = idx - p
            tp = 6 + p
            if ci < tp:
                return False
            if not (df["close"].iloc[ci] < df["close"].iloc[ci - tp] and
                    ind["tema48"].iloc[ci]  < ind["tema48"].iloc[ci - tp] and
                    ind["tema72"].iloc[ci]  < ind["tema72"].iloc[ci - tp] and
                    ind["tema288"].iloc[ci] < ind["tema288"].iloc[ci - tp]):
                return False
        return True

    # =====================================================
    # v3.1 新增: BTC 过滤判据（仅供 L1 使用）
    # =====================================================

    def _btc_L1_gate(self, ind: Dict, idx: int) -> Tuple[bool, str]:
        """
        BTC 长周期方向过滤 (BF_slope_L1 方案)

        返回:
            (pass, reason): pass=True 允许 L1, pass=False 阻止 L1

        逻辑:
            1. 如果过滤关闭 → 放行, reason='disabled'
            2. 如果 BTC 数据缺失 → 放行, reason='no_data' (容错到 v3 行为)
            3. 如果 idx 不足以算 slope → 放行, reason='idx_early'
            4. 计算 btc_tema288 的 12 根斜率:
               - slope > 0 → 放行 (BTC 长周期上升, L1 可以开)
               - slope ≤ 0 → 阻止 (BTC 长周期不涨, L1 大概率是反弹陷阱)
        """
        if not self.btc_filter_enabled:
            return True, "disabled"

        btc_tema288 = ind.get("btc_tema288")
        if btc_tema288 is None:
            return True, "no_data(fallback_to_v3)"

        if idx < self.btc_slope_window:
            return True, "idx_early"

        try:
            btc_slope288 = calc_slope(btc_tema288, idx, window=self.btc_slope_window)
        except Exception as e:
            logger.warning(f"[ST+TEMA v3.1] BTC slope 计算异常: {e}, 默认放行")
            return True, f"calc_error({e})"

        if btc_slope288 > 0:
            return True, f"btc_slope288={btc_slope288:.6f}>0"
        else:
            return False, f"btc_slope288={btc_slope288:.6f}<=0"

    # =====================================================
    # 平仓逻辑（与 v3 完全一致）
    # =====================================================

    def _check_close_conditions(
        self,
        df: pd.DataFrame,
        ind: Dict,
        idx: int,
        position: int,
        entry_price: float,
        current_price: float,
        entry_index: Optional[int],
        entry_timestamp: Optional[int] = None,
    ) -> SignalResult:

        import time as _time
        if entry_timestamp and entry_timestamp > 0:
            elapsed_sec = _time.time() - entry_timestamp
            hold_bars = int(elapsed_sec / (self.bar_minutes * 60))
        elif entry_index is not None and entry_index != idx:
            hold_bars = idx - entry_index
        else:
            hold_bars = 999

        if hold_bars < self.min_hold_bars:
            return SignalResult(SignalResult.NO_SIGNAL,
                                f"持仓{hold_bars}根，未达最小{self.min_hold_bars}根")

        if position == 1:
            pnl_ratio = (current_price - entry_price) / entry_price
        else:
            pnl_ratio = (entry_price - current_price) / entry_price
        price_move_loss = -pnl_ratio if pnl_ratio < 0 else 0

        slopes   = self._get_slopes(ind, idx)
        st_dir   = ind["st_direction"].iloc[idx]
        prev_st  = ind["st_direction"].iloc[idx - 1] if idx > 0 else st_dir

        tema72_up   = slopes["tema72"] > 0
        tema72_down = slopes["tema72"] <= 0

        cur_t72  = ind["tema72"].iloc[idx]
        cur_t144 = ind["tema144"].iloc[idx]
        cur_t288 = ind["tema288"].iloc[idx]
        prev_t72  = ind["tema72"].iloc[idx - 1]
        prev_t144 = ind["tema144"].iloc[idx - 1]
        prev_t288 = ind["tema288"].iloc[idx - 1]

        t144_cross_t288_down = (prev_t144 >= prev_t288 and cur_t144 < cur_t288)
        t144_cross_t288_up   = (prev_t144 <= prev_t288 and cur_t144 > cur_t288)

        st_to_bear = (prev_st != -1 and st_dir == -1)
        st_to_bull = (prev_st != 1  and st_dir == 1)

        logger.info(
            f"[ST+TEMA v3.1][平仓] {'多' if position==1 else '空'} "
            f"hold={hold_bars} pnl={pnl_ratio*100:+.3f}% loss={price_move_loss*100:.3f}% "
            f"ST={st_dir}(prev={prev_st})"
        )

        close_sig = SignalResult.CLOSE_LONG if position == 1 else SignalResult.CLOSE_SHORT

        # ===== 做多平仓 =====
        if position == 1:
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig, f"P1a强平({price_move_loss*100:.2f}%)")

            if price_move_loss >= self.active_stop_loss:
                return SignalResult(close_sig,
                                    f"P1b主动止损-{self.active_stop_loss*100:.1f}%"
                                    f"({pnl_ratio*100:.2f}%)")

            if pnl_ratio >= self.big_take_profit:
                return SignalResult(close_sig, f"P2a大止盈({pnl_ratio*100:.2f}%)")

            if pnl_ratio >= self.fixed_take_profit and (tema72_down or st_dir == -1):
                return SignalResult(close_sig, f"P2b止盈+趋势转弱({pnl_ratio*100:.2f}%)")

            if pnl_ratio >= self.trend_weak_profit and tema72_down and st_dir == -1:
                return SignalResult(close_sig, f"P3趋势减弱止盈({pnl_ratio*100:.2f}%)")

            if pnl_ratio > self.st_change_profit and st_to_bear:
                return SignalResult(close_sig, f"P4 ST翻空止盈({pnl_ratio*100:.2f}%)")

            if tema72_down and st_dir == -1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if price_move_loss >= 0.012 and pnl_ratio < -0.008:
                    return SignalResult(close_sig, f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")
                return SignalResult(SignalResult.NO_SIGNAL,
                                    "P5外层成立但内部未触发(对齐回测elif独占)")

            if t144_cross_t288_down and pnl_ratio > 0:
                return SignalResult(close_sig, "P6 TEMA144下穿TEMA288")

            if (cur_t72 - cur_t288 <= -20) and cur_t72 <= prev_t72 and pnl_ratio > -0.005:
                return SignalResult(close_sig, f"P7 TEMA康法则强反向(差={cur_t72-cur_t288:.2f})")

            if self._trend_reversal_confirmed(ind, idx, "down", 10) and pnl_ratio > -0.005:
                return SignalResult(close_sig, "P8 大趋势反转(10根下降)")

            if self._three_line_downward(df, ind, idx, slopes["tema72"]) and pnl_ratio > 0.01:
                return SignalResult(close_sig, "P9 三线同向下降平仓")

        # ===== 做空平仓 =====
        elif position == -1:
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig, f"P1止损强平({price_move_loss*100:.2f}%)")

            if pnl_ratio >= self.big_take_profit:
                return SignalResult(close_sig, f"P2a大止盈({pnl_ratio*100:.2f}%)")

            if pnl_ratio >= self.fixed_take_profit and (tema72_up or st_dir == 1):
                return SignalResult(close_sig, f"P2b止盈+趋势转弱({pnl_ratio*100:.2f}%)")

            if pnl_ratio >= self.trend_weak_profit and tema72_up and st_dir == 1:
                return SignalResult(close_sig, f"P3趋势减弱止盈({pnl_ratio*100:.2f}%)")

            if pnl_ratio > self.st_change_profit and st_to_bull:
                return SignalResult(close_sig, f"P4 ST翻多止盈({pnl_ratio*100:.2f}%)")

            if tema72_up and st_dir == 1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if (price_move_loss >= self.liquidation_ratio * self.stop_loss_multiplier
                        and pnl_ratio < -0.015):
                    return SignalResult(close_sig, f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")
                return SignalResult(SignalResult.NO_SIGNAL,
                                    "P5外层成立但内部未触发(对齐回测elif独占)")

            if t144_cross_t288_up and pnl_ratio < 0.005:
                return SignalResult(close_sig, "P6 TEMA144上穿TEMA288")

            if (cur_t72 - cur_t288 >= 20) and cur_t72 >= prev_t72 and pnl_ratio < 0.005:
                return SignalResult(close_sig, f"P7 TEMA康法则强反向(差={cur_t72-cur_t288:.2f})")

            if self._trend_reversal_confirmed(ind, idx, "up", 10):
                return SignalResult(close_sig, "P8 大趋势反转(10根上升)")

            if self._three_line_upward(df, ind, idx, slopes["tema72"]) and pnl_ratio < 0.005:
                return SignalResult(close_sig, "P9 三线同向上升平仓")

        return SignalResult(SignalResult.NO_SIGNAL, "P1~P9 均未命中")

    # =====================================================
    # 开仓逻辑（v3.1: 仅 L1 加 BTC 过滤，其他与 v3 一致）
    # =====================================================

    def _check_open_conditions(
        self,
        df: pd.DataFrame,
        ind: Dict,
        idx: int,
        current_price: float,
    ) -> SignalResult:

        slopes = self._get_slopes(ind, idx)
        st_dir  = ind["st_direction"].iloc[idx]
        prev_st = ind["st_direction"].iloc[idx - 1] if idx > 0 else st_dir

        cur_t48  = ind["tema48"].iloc[idx]
        cur_t72  = ind["tema72"].iloc[idx]
        cur_t144 = ind["tema144"].iloc[idx]
        cur_t288 = ind["tema288"].iloc[idx]
        prev_t72  = ind["tema72"].iloc[idx - 1]
        prev_t144 = ind["tema144"].iloc[idx - 1]
        prev_t288 = ind["tema288"].iloc[idx - 1]

        # 斜率方向
        t48_up   = slopes["tema48"]  > 0
        t48_down = slopes["tema48"]  <= 0
        t72_up   = slopes["tema72"]  > 0
        t72_down = slopes["tema72"]  <= 0
        t144_up  = slopes["tema144"] > 0
        t144_down= slopes["tema144"] <= 0
        t288_up  = slopes["tema288"] > 0
        t288_down= slopes["tema288"] <= 0

        HIGH   = 0.0002
        LOW    = 0.00005
        BIG    = 0.0003
        STRONG = 0.0005

        is_ranging   = (abs(slopes["tema72"])  < LOW and
                        abs(slopes["tema144"]) < LOW and
                        abs(slopes["tema288"]) < LOW)
        is_big_trend = (abs(slopes["tema72"])  > BIG or
                        abs(slopes["tema144"]) > BIG or
                        abs(slopes["tema288"]) > BIG)

        trend_str = self._trend_strength(df, idx)
        is_strong_long  = trend_str > self.is_strong_long_threshold
        is_strong_short = trend_str > self.is_strong_short_threshold

        price_deviation = (current_price - cur_t72) / cur_t72

        t72_x_t144_up    = (prev_t72  <= prev_t144 and cur_t72  > cur_t144)
        t72_x_t144_down  = (prev_t72  >= prev_t144 and cur_t72  < cur_t144)
        t144_x_t288_up   = (prev_t144 <= prev_t288 and cur_t144 > cur_t288)
        t144_x_t288_down = (prev_t144 >= prev_t288 and cur_t144 < cur_t288)

        st_to_bull = (prev_st != 1  and st_dir == 1)
        st_to_bear = (prev_st != -1 and st_dir == -1)

        is_strong_up = (slopes["tema288"] > HIGH and t144_up and
                        cur_t144 > cur_t288 and cur_t72 > cur_t144)
        is_strong_down = (slopes["tema288"] < -HIGH and t144_down and
                          cur_t144 < cur_t288 and cur_t72 < cur_t144)

        bull_align = self._tema_alignment_bull(ind, idx)
        bear_align = self._tema_alignment_bear(ind, idx)

        # v3.1 新增: BTC L1 过滤判据
        btc_L1_pass, btc_reason = self._btc_L1_gate(ind, idx)

        # ---------- 诊断日志 ----------
        logger.info(
            f"[ST+TEMA v3.1][开仓] price={current_price:.4f} "
            f"ST={st_dir}(prev={prev_st}) ↑={st_to_bull} ↓={st_to_bear}"
        )
        logger.info(
            f"[ST+TEMA v3.1][开仓] 斜率 t48={slopes['tema48']:.6f} t72={slopes['tema72']:.6f} "
            f"t144={slopes['tema144']:.6f} t288={slopes['tema288']:.6f}"
        )
        logger.info(
            f"[ST+TEMA v3.1][开仓] trend_str={trend_str:.3f} "
            f"is_strong_long={is_strong_long}(>{self.is_strong_long_threshold}) "
            f"is_strong_short={is_strong_short}(>{self.is_strong_short_threshold}) "
            f"is_big={is_big_trend} is_ranging={is_ranging} "
            f"price_dev={price_deviation:.5f}"
        )
        logger.info(
            f"[ST+TEMA v3.1][开仓] 排列 bull={bull_align} bear={bear_align} | "
            f"交叉 t72↑t144={t72_x_t144_up} t72↓t144={t72_x_t144_down} "
            f"t144↑t288={t144_x_t288_up} t144↓t288={t144_x_t288_down}"
        )
        logger.info(
            f"[ST+TEMA v3.1][BTC] L1过滤={btc_L1_pass} 原因={btc_reason} "
            f"数据源={ind.get('btc_source', 'unknown')}"
        )

        # ========== 做多 ==========
        long_hits = []

        # L1 TEMA144 上穿 TEMA288 + 强趋势
        # v3.1 改动: 加 BTC L1 过滤 (btc_tema288 slope > 0)
        if t144_x_t288_up and is_strong_long:
            if btc_L1_pass:
                long_hits.append("L1 TEMA144上穿TEMA288")
            else:
                # 触发但被 BTC 过滤，记录日志便于复盘
                logger.info(
                    f"[ST+TEMA v3.1][L1过滤] L1 信号被 BTC 过滤: {btc_reason}"
                )
                log_trade(
                    f"[L1过滤] BTC slope 阻止开仓: {btc_reason}"
                )

        # L2 TEMA72 上穿 TEMA144（严格版）
        if (t72_x_t144_up and
                slopes["tema144"] > HIGH and
                slopes["tema288"] > 0 and
                st_dir == 1 and
                self._st_consistency(ind, idx, 1, min_bars=3) and
                bull_align and
                is_strong_long):
            long_hits.append("L2 TEMA72上穿TEMA144")

        # L3 强上升趋势中 ST 翻转
        if st_to_bull and is_strong_up and bull_align and is_strong_long:
            long_hits.append("L3 强上升趋势ST翻转")

        # L4 大上升趋势
        if (is_big_trend and st_to_bull and st_dir == 1 and
                slopes["tema144"] > BIG and slopes["tema72"] > BIG and
                slopes["tema288"] > HIGH and bull_align and is_strong_long):
            long_hits.append("L4 大上升趋势开多")

        # L5 全线向上 + price_deviation
        if (not is_ranging and
                t48_up and t72_up and t144_up and t288_up and
                st_dir == 1 and bull_align and
                self._st_consistency(ind, idx, 1, 5) and
                abs(slopes["tema72"]) > STRONG and
                is_strong_long and
                price_deviation < 0.02):
            long_hits.append("L5 全线向上强劲")

        # L6 康法则背景上升点
        if (cur_t72 >= cur_t288 and
                self._long_background_rise(df, ind, idx) and
                st_dir == 1 and is_strong_long):
            long_hits.append("L6 康法则背景上升点")

        # L7 三线同向上升 + price_deviation
        if (self._three_line_upward(df, ind, idx, slopes["tema72"]) and
                st_dir == 1 and bull_align and
                price_deviation < 0.02):
            long_hits.append("L7 三线同向上升")

        # ========== 做空（与 v3 完全一致）==========
        short_hits = []

        # S1
        if (t144_x_t288_down and
                is_strong_short and
                st_dir == -1 and
                slopes["tema288"] < -LOW):
            short_hits.append("S1 TEMA144下穿TEMA288")

        # S2
        if (t72_x_t144_down and
                slopes["tema144"] < -HIGH and
                slopes["tema288"] < 0 and
                st_dir == -1 and
                self._st_consistency(ind, idx, -1, min_bars=3) and
                bear_align and
                is_strong_short):
            short_hits.append("S2 TEMA72下穿TEMA144")

        # S3
        if (st_to_bear and
                is_strong_down and
                bear_align and
                is_strong_short):
            short_hits.append("S3 ST翻空")

        # S4 (v3 对称化)
        if (st_dir == -1 and t72_down and t144_down and
                is_strong_short):
            short_hits.append("S4 下降趋势中")

        # S5 (v3 对称化)
        if (not is_ranging and
                t48_down and t72_down and t144_down and
                st_dir == -1 and
                bear_align and
                self._st_consistency(ind, idx, -1, 5) and
                abs(slopes["tema72"]) > STRONG and
                is_strong_short and
                price_deviation > -0.02):
            short_hits.append("S5 全线向下")

        # S6 (v3 对称化)
        if (cur_t72 < cur_t288 and
                self._short_background_decline(df, ind, idx) and
                st_dir == -1 and
                is_strong_short):
            short_hits.append("S6 康法则背景下降点")

        # S7 (v3 对称化)
        if (self._three_line_downward(df, ind, idx, slopes["tema72"]) and
                st_dir == -1 and
                bear_align and
                price_deviation > -0.02):
            short_hits.append("S7 三线同向下降")

        logger.info(
            f"[ST+TEMA v3.1][开仓汇总] 多={long_hits or '无'} 空={short_hits or '无'}"
        )

        long_met  = len(long_hits)  > 0
        short_met = len(short_hits) > 0
        long_reason  = long_hits[0]  if long_hits  else ""
        short_reason = short_hits[0] if short_hits else ""

        if long_met and not short_met:
            return SignalResult(SignalResult.LONG, long_reason)

        if short_met and not long_met:
            return SignalResult(SignalResult.SHORT, short_reason)

        if long_met and short_met:
            if st_dir == 1:
                return SignalResult(SignalResult.LONG,  long_reason  + "(多空冲突,ST选多)")
            else:
                return SignalResult(SignalResult.SHORT, short_reason + "(多空冲突,ST选空)")

        return SignalResult(
            SignalResult.NO_SIGNAL,
            f"无开仓信号(多={long_hits or '无'}, 空={short_hits or '无'})"
        )
