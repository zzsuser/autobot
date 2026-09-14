"""
SuperTrend + TEMA 复合策略 v3
基于 v2 演进，专为 m25_s00 回测最优配置实盘部署设计

========================================================================
【回测背景】
========================================================================

数据：BTC + ETH 5min K 线合并，2022-01 ~ 2026-06（49 个月）
配置：ATR mult=2.5, min_atr_stop=0, D1 = G 组(0.15/0.25/0.7)

回测结果（1008 笔交易）：
  - 总收益: +861.5%
  - 最大回撤: 43.79%
  - 夏普: 1.09
  - 胜率: 32.84%
  - 平均盈利: 2.01% / 平均亏损: -0.80% / 盈亏比: 2.51
  - 实际方向: 100% 多头（S 类结构性从未触发，见下）

========================================================================
【v3 相对 v2 的核心改动】
========================================================================

1. S4-S7 对称化（对照 L4-L7 加对称条件）
   - v2 里 S4/S5/S6/S7 明显宽于 L4/L5/L6/L7
   - v3 补齐对称条件
   - 目的: 未来若市场结构改变（长期熊市），S 类被激活时保证质量
   - 回测事实: 2022-2026 ETH 上涨主导, S 类因 TEMA slope 结构条件从未触发
     —— 所以 v3 vs v2 在这段回测里行为完全相同（+861.5% 一致）
     但改动已就位, 未来 BTC/ETH 转熊时会自动激活对称化后的 S 类

2. is_strong 阈值拆分（长/空可独立配置）
   - v2: 单一 is_strong_threshold=0.6
   - v3: is_strong_long_threshold + is_strong_short_threshold
   - 默认都 0.6（保持行为不变）
   - 未来若想让空头更容易触发, 可单独降 short 阈值
   - 诊断证实: 即使 short_threshold=0, S 类也不触发（是 TEMA slope 结构问题, 不是强度门槛问题）

3. 详细开/平仓日志（不变的接口, 更多的信息）
   - 每次开仓写入 L/S 类命中详情
   - 每次平仓写入 P1-P9 触发原因
   - 便于实盘复盘 vs 回测对比

========================================================================
【S4-S7 对称化明细】
========================================================================

S4（下降趋势中）
  v2: ST=-1 + t72↓ + t144↓
  v3: 加 is_strong_short（对照 L4 的 is_strong）

S5（全线向下）
  v2: t48↓ + t72↓ + t144↓ + ST=-1
  v3: 加 6 条件对照 L5:
      + not is_ranging
      + bear_align
      + ST 一致性 (5 根)
      + 强斜率 (|slope_72| > STRONG)
      + is_strong_short
      + price_deviation > -0.02 (价格未下跌太远, 避免追空)

S6（背景下降点 / 康法则）
  v2: t72<t288 + 康法则下降 + ST=-1
  v3: 加 is_strong_short（对照 L6）

S7（三线同向下降）
  v2: 三线下降 + ST=-1
  v3: 加 bear_align + price_deviation > -0.02（对照 L7 的 bull_align + < 0.02）

========================================================================
【不改动的部分（对齐 v2 行为）】
========================================================================

- 所有指标计算（TEMA / SuperTrend / slope）
- L1-L7 开多逻辑
- S1-S3 开空逻辑（已经严格, 无需再改）
- 全部 P1-P9 平仓逻辑
- 冷却机制（10 根 = 50 分钟）
- need_stop_check() 返回 False, K 线内不做额外检查
- 阈值常量 HIGH/LOW/BIG/STRONG（严格与回测一致）
- min_hold_bars / bar_minutes 等运行参数

========================================================================
【推荐启动参数（配套 m25_s00 回测配置）】
========================================================================

strategy = SuperTrendTemaStrategyV3(
    # ATR 相关（回测 m25_s00 = mult=2.5, stop=0）
    # 注意: ATR 相关在这里不用配置, 由平仓 P1a 强平线 liquidation_ratio 承担底线
    #      回测里的 min_atr_stop=0 意味着完全依赖真实 ATR 计算止损
    #      实盘里 P1a (2.5%) + P1b (1.5%) 承担类似角色
    
    # 阈值（v2 默认, 保持对回测行为一致）
    is_strong_long_threshold=0.6,      # 多头强度门槛（回测一致）
    is_strong_short_threshold=0.6,     # 空头强度门槛（回测一致, S 类当前不触发）
    
    # 风控（v2 默认）
    liquidation_ratio=0.025,           # 强平线 2.5%
    active_stop_loss=0.015,            # 主动止损 1.5%
    
    # 止盈（v2 默认）
    fixed_take_profit=0.025,
    trend_weak_profit=0.01,
    st_change_profit=0.005,
    big_take_profit=0.04,
    take_profit_multiplier=1.5,
    stop_loss_multiplier=0.75,
    
    # 其他（v2 默认）
    leverage=100,
    min_hold_bars=3,
    cooldown_bars=10,
    bar_minutes=5,
    
    # SuperTrend（v2 默认）
    st_period=10,
    st_multiplier=3.0,
)

========================================================================
【实盘使用注意】
========================================================================

1. 数据: 需完整 288+ 根历史 K 线（TEMA288 所需, 加缓冲建议 600+）
2. 首笔交易前观察 10 分钟, 确认日志输出正常
3. 第一天重点看:
   - 开仓触发的 L 类信号（预期 L7 三线同向上升占 74.8%）
   - 平仓 P1-P9 触发分布（预期 P1b 主动止损占 30-50%）
   - 冷却期是否合理触发
4. 回撤警戒:
   - 单周 -10% → 观察, 加强监控
   - 单周 -15% → 停机人工介入
   - 累计 -25% → 强制停机
5. 与回测偏离监控:
   - 每周对比 胜率 / 盈亏比 / 平均单笔 vs 回测数字
   - 偏离 > 30% 停机排查

"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Tuple
from autobot.core.strategy_base import StrategyBase, SignalResult
from autobot.utils.logger import logger, log_trade


# =====================================================
# 指标计算工具（与 v2 一致，未做改动）
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
# 策略类 v3
# =====================================================

class SuperTrendTemaStrategyV3(StrategyBase):
    """
    SuperTrend + TEMA 复合策略 v3

    核心特性:
        - 100x 杠杆 3% 仓位, 5min ETH-USDT 永续
        - L1-L7 开多, S1-S7 开空（S 类已对称化, 但 ETH 上涨主导时几乎不触发）
        - P1-P9 分层平仓（强平/主动止损/多档止盈/趋势反转）
        - 10 根冷却期, 无熔断

    v3 vs v2:
        1. S4-S7 加对称条件（对照 L4-L7）
        2. is_strong 阈值拆分成 long / short 两个
        3. name 变更为 supertrend_tema_v3
        4. 顶部详细注释 + 每笔详细日志

    参数（默认值均与 v2 一致, 保持行为不变）:
        st_period                    : SuperTrend ATR 周期 (默认 10)
        st_multiplier                : SuperTrend 乘数 (默认 3)
        liquidation_ratio            : 强平阈值 (默认 0.025 = 2.5%)
        active_stop_loss             : 多头主动止损阈值 (默认 0.015 = 1.5%)
        fixed_take_profit            : 固定止盈 (默认 0.025)
        trend_weak_profit            : 趋势减弱止盈 (默认 0.01)
        st_change_profit             : ST 翻转止盈 (默认 0.005)
        big_take_profit              : 大止盈 (默认 0.04)
        take_profit_multiplier       : P5 止盈倍率 (默认 1.5)
        stop_loss_multiplier         : P5 止亏倍率 (默认 0.75)
        leverage                     : 杠杆 (默认 100)
        min_hold_bars                : 最小持仓根数 (默认 3)
        cooldown_bars                : 冷却根数 (默认 10)
        bar_minutes                  : 单根 K 线分钟数 (默认 5)
        is_strong_long_threshold     : 多头趋势强度阈值 (默认 0.6, v3 新增)
        is_strong_short_threshold    : 空头趋势强度阈值 (默认 0.6, v3 新增)
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
        # v3 新增：拆分成多空两个阈值
        self.is_strong_long_threshold = is_strong_long_threshold
        self.is_strong_short_threshold = is_strong_short_threshold

        # 运行时状态：仅普通冷却，无熔断
        self._last_close_ts: Optional[pd.Timestamp] = None

    # ---------- 接口实现 ----------

    @property
    def name(self) -> str:
        return "supertrend_tema_v3"

    @property
    def required_data_length(self) -> int:
        return 600

    def need_stop_check(self) -> bool:
        """
        完全对齐回测：所有平仓决策只在 K 线收盘时由 generate_signal 中的
        P1~P9 处理，不在 K 线内做额外止盈止损检查。

        返回 False 后，task_manager 不会启动 stop_loop 高频轮询，
        引擎也不会调用 check_stop。
        """
        return False

    def check_stop(self, current_position, entry_price, current_price, **kwargs) -> SignalResult:
        """
        完全对齐回测：此方法保留仅为接口兼容，永远返回 NO_SIGNAL。
        need_stop_check() 已返回 False，引擎不会调用此方法。

        所有平仓判断在 generate_signal() 的 _check_close_conditions 完成。
        """
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
                f"[ST+TEMA v3] ===== 信号检查 ===== "
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
                    logger.info(f"[ST+TEMA v3] 持仓未触发平仓: {close_result.reason}")

            # ---------- 无仓位：检查开仓 ----------
            if current_position == 0:
                # 普通冷却
                if self._is_in_cooldown(current_ts):
                    left = self._cooldown_bars_left(current_ts)
                    logger.info(f"[ST+TEMA v3] 冷却中，剩余约{left}根")
                    return SignalResult(SignalResult.NO_SIGNAL, f"冷却期({left}根)")

                open_result = self._check_open_conditions(df, ind, idx, current_price)
                if open_result.signal != SignalResult.NO_SIGNAL:
                    log_trade(f"[信号] 开仓: {open_result.reason}")
                else:
                    logger.info(f"[ST+TEMA v3] 未触发开仓: {open_result.reason}")
                return open_result

            return SignalResult(SignalResult.NO_SIGNAL, "持仓中，未触发平仓")

        except Exception as e:
            logger.error(f"[ST+TEMA v3] 策略异常: {e}", exc_info=True)
            return SignalResult(SignalResult.NO_SIGNAL, f"策略异常: {e}")

    # =====================================================
    # 冷却（与 v2 一致，未改）
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
    # 指标准备（与 v2 一致，未改）
    # =====================================================

    def _prepare_indicators(self, df: pd.DataFrame) -> Dict:
        close = df["close"]

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

        logger.debug(
            f"[ST+TEMA v3] 指标来源: TEMA={'DB' if used_db_tema else 'CALC'}, "
            f"ST={'DB' if used_db_st else 'CALC'}"
        )

        return {
            "tema48":       tema48,
            "tema72":       tema72,
            "tema144":      tema144,
            "tema288":      tema288,
            "st_value":     st_value,
            "st_direction": st_direction,
        }

    # =====================================================
    # 辅助方法（与 v2 一致，未改）
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
    # 平仓逻辑（与 v2 完全一致，未改动）
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
            hold_bars = 999  # 兜底：无法确定时放行

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
            f"[ST+TEMA v3][平仓] {'多' if position==1 else '空'} "
            f"hold={hold_bars} pnl={pnl_ratio*100:+.3f}% loss={price_move_loss*100:.3f}% "
            f"ST={st_dir}(prev={prev_st})"
        )

        close_sig = SignalResult.CLOSE_LONG if position == 1 else SignalResult.CLOSE_SHORT

        # ===== 做多平仓 =====
        if position == 1:

            # P1a 强平兜底
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig,
                                    f"P1a强平({price_move_loss*100:.2f}%)")

            # P1b 主动止损 1.5%（先于强平触发）
            if price_move_loss >= self.active_stop_loss:
                return SignalResult(close_sig,
                                    f"P1b主动止损-{self.active_stop_loss*100:.1f}%"
                                    f"({pnl_ratio*100:.2f}%)")

            # P2a 大止盈（无条件）
            if pnl_ratio >= self.big_take_profit:
                return SignalResult(close_sig,
                                    f"P2a大止盈({pnl_ratio*100:.2f}%)")

            # P2b 中等止盈 + 趋势转弱
            if pnl_ratio >= self.fixed_take_profit and (tema72_down or st_dir == -1):
                return SignalResult(close_sig,
                                    f"P2b止盈+趋势转弱({pnl_ratio*100:.2f}%)")

            # P3 趋势减弱双重止盈
            if pnl_ratio >= self.trend_weak_profit and tema72_down and st_dir == -1:
                return SignalResult(close_sig,
                                    f"P3趋势减弱止盈({pnl_ratio*100:.2f}%)")

            # P4 ST 翻空止盈
            if pnl_ratio > self.st_change_profit and st_to_bear:
                return SignalResult(close_sig,
                                    f"P4 ST翻空止盈({pnl_ratio*100:.2f}%)")

            # P5 TEMA72 与 ST 反向（elif 独占分支）
            if tema72_down and st_dir == -1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if price_move_loss >= 0.012 and pnl_ratio < -0.008:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")
                return SignalResult(SignalResult.NO_SIGNAL,
                                    "P5外层成立但内部未触发(对齐回测elif独占)")

            # P6 TEMA144 下穿 TEMA288（pnl > 0，保护盈利）
            if t144_cross_t288_down and pnl_ratio > 0:
                return SignalResult(close_sig, "P6 TEMA144下穿TEMA288")

            # P7 TEMA 康法则强反向（微利或小亏）
            if (cur_t72 - cur_t288 <= -20) and cur_t72 <= prev_t72 and pnl_ratio > -0.005:
                return SignalResult(close_sig,
                                    f"P7 TEMA康法则强反向(差={cur_t72-cur_t288:.2f})")

            # P8 大趋势反转 10 根（pnl > -0.005）
            if self._trend_reversal_confirmed(ind, idx, "down", 10) and pnl_ratio > -0.005:
                return SignalResult(close_sig, "P8 大趋势反转(10根下降)")

            # P9 三线同向下降（pnl > 0.01，有足够盈利才退出）
            if self._three_line_downward(df, ind, idx, slopes["tema72"]) and pnl_ratio > 0.01:
                return SignalResult(close_sig, "P9 三线同向下降平仓")

        # ===== 做空平仓 =====
        elif position == -1:

            # P1 强平
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig,
                                    f"P1止损强平({price_move_loss*100:.2f}%)")

            # P2a 大止盈
            if pnl_ratio >= self.big_take_profit:
                return SignalResult(close_sig,
                                    f"P2a大止盈({pnl_ratio*100:.2f}%)")

            # P2b 中等止盈 + 趋势转弱
            if pnl_ratio >= self.fixed_take_profit and (tema72_up or st_dir == 1):
                return SignalResult(close_sig,
                                    f"P2b止盈+趋势转弱({pnl_ratio*100:.2f}%)")

            # P3 趋势减弱双重止盈
            if pnl_ratio >= self.trend_weak_profit and tema72_up and st_dir == 1:
                return SignalResult(close_sig,
                                    f"P3趋势减弱止盈({pnl_ratio*100:.2f}%)")

            # P4 ST 翻多止盈
            if pnl_ratio > self.st_change_profit and st_to_bull:
                return SignalResult(close_sig,
                                    f"P4 ST翻多止盈({pnl_ratio*100:.2f}%)")

            # P5 TEMA72 与 ST 反向（elif 独占分支）
            if tema72_up and st_dir == 1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if (price_move_loss >= self.liquidation_ratio * self.stop_loss_multiplier
                        and pnl_ratio < -0.015):
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")
                return SignalResult(SignalResult.NO_SIGNAL,
                                    "P5外层成立但内部未触发(对齐回测elif独占)")

            # P6 TEMA144 上穿 TEMA288（微利或亏损）
            if t144_cross_t288_up and pnl_ratio < 0.005:
                return SignalResult(close_sig, "P6 TEMA144上穿TEMA288")

            # P7 TEMA 康法则强反向（微利或亏损）
            if (cur_t72 - cur_t288 >= 20) and cur_t72 >= prev_t72 and pnl_ratio < 0.005:
                return SignalResult(close_sig,
                                    f"P7 TEMA康法则强反向(差={cur_t72-cur_t288:.2f})")

            # P8 大趋势反转 10 根（空头无 pnl 过滤，与回测一致）
            if self._trend_reversal_confirmed(ind, idx, "up", 10):
                return SignalResult(close_sig, "P8 大趋势反转(10根上升)")

            # P9 三线同向上升（微利或亏损）
            if self._three_line_upward(df, ind, idx, slopes["tema72"]) and pnl_ratio < 0.005:
                return SignalResult(close_sig, "P9 三线同向上升平仓")

        return SignalResult(SignalResult.NO_SIGNAL, "P1~P9 均未命中")

    # =====================================================
    # 开仓逻辑（v3: is_strong 拆分, S4-S7 对称化）
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

        # 阈值常量（严格与回测一致，未参数化）
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

        # ── v3 改动: is_strong 拆分成多空两个 ──
        trend_str = self._trend_strength(df, idx)
        is_strong_long  = trend_str > self.is_strong_long_threshold
        is_strong_short = trend_str > self.is_strong_short_threshold

        # 价格偏离度（相对 TEMA72）
        # 多头用: price_deviation < 0.02 (不追涨太远)
        # 空头用: price_deviation > -0.02 (不追跌太远, v3 新增对称)
        price_deviation = (current_price - cur_t72) / cur_t72

        # 交叉
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

        # ---------- 诊断日志 ----------
        logger.info(
            f"[ST+TEMA v3][开仓] price={current_price:.4f} "
            f"ST={st_dir}(prev={prev_st}) ↑={st_to_bull} ↓={st_to_bear}"
        )
        logger.info(
            f"[ST+TEMA v3][开仓] 斜率 t48={slopes['tema48']:.6f} t72={slopes['tema72']:.6f} "
            f"t144={slopes['tema144']:.6f} t288={slopes['tema288']:.6f}"
        )
        logger.info(
            f"[ST+TEMA v3][开仓] trend_str={trend_str:.3f} "
            f"is_strong_long={is_strong_long}(>{self.is_strong_long_threshold}) "
            f"is_strong_short={is_strong_short}(>{self.is_strong_short_threshold}) "
            f"is_big={is_big_trend} is_ranging={is_ranging} "
            f"price_dev={price_deviation:.5f}"
        )
        logger.info(
            f"[ST+TEMA v3][开仓] 排列 bull={bull_align} bear={bear_align} | "
            f"交叉 t72↑t144={t72_x_t144_up} t72↓t144={t72_x_t144_down} "
            f"t144↑t288={t144_x_t288_up} t144↓t288={t144_x_t288_down}"
        )

        # ========== 做多（与 v2 一致，仅 is_strong 变量名改成 is_strong_long）==========
        long_hits = []

        # L1 TEMA144 上穿 TEMA288 + 强趋势
        if t144_x_t288_up and is_strong_long:
            long_hits.append("L1 TEMA144上穿TEMA288")

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

        # ========== 做空（v3 对称化: S4-S7 加对照 L4-L7 条件）==========
        short_hits = []

        # S1 TEMA144 下穿 TEMA288（回测加强 + is_strong_short）
        if (t144_x_t288_down and
                is_strong_short and
                st_dir == -1 and
                slopes["tema288"] < -LOW):
            short_hits.append("S1 TEMA144下穿TEMA288")

        # S2 TEMA72 下穿 TEMA144（严格对称多头）
        if (t72_x_t144_down and
                slopes["tema144"] < -HIGH and
                slopes["tema288"] < 0 and
                st_dir == -1 and
                self._st_consistency(ind, idx, -1, min_bars=3) and
                bear_align and
                is_strong_short):
            short_hits.append("S2 TEMA72下穿TEMA144")

        # S3 ST 翻空（is_strong_down + 空头排列）
        if (st_to_bear and
                is_strong_down and
                bear_align and
                is_strong_short):
            short_hits.append("S3 ST翻空")

        # ── v3 S4-S7 对称化 ──

        # S4 下降趋势中（v3: 加 is_strong_short, 对照 L4）
        if (st_dir == -1 and t72_down and t144_down and
                is_strong_short):
            short_hits.append("S4 下降趋势中")

        # S5 全线向下（v3: 加 6 条件, 对照 L5）
        if (not is_ranging and
                t48_down and t72_down and t144_down and
                st_dir == -1 and
                bear_align and
                self._st_consistency(ind, idx, -1, 5) and
                abs(slopes["tema72"]) > STRONG and
                is_strong_short and
                price_deviation > -0.02):
            short_hits.append("S5 全线向下")

        # S6 背景下降点（v3: 加 is_strong_short, 对照 L6）
        if (cur_t72 < cur_t288 and
                self._short_background_decline(df, ind, idx) and
                st_dir == -1 and
                is_strong_short):
            short_hits.append("S6 康法则背景下降点")

        # S7 三线同向下降（v3: 加 bear_align + price_dev > -0.02, 对照 L7）
        if (self._three_line_downward(df, ind, idx, slopes["tema72"]) and
                st_dir == -1 and
                bear_align and
                price_deviation > -0.02):
            short_hits.append("S7 三线同向下降")

        logger.info(
            f"[ST+TEMA v3][开仓汇总] 多={long_hits or '无'} 空={short_hits or '无'}"
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
