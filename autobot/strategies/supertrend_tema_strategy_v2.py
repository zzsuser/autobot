"""
SuperTrend + TEMA 复合策略 v2
完全对齐回测脚本 eth_20260423_3position_16.py

【相对于上一版本的核心变更】
1. S1/S2/S3 开空条件加强，与回测一致
2. L5/L7 新增 price_deviation < 0.02 条件
3. 平仓新增 P1b：多头 1.5% 主动止损（先于强平触发）
4. P6 多头：pnl_ratio > 0（回测）替换旧版 < 0.005
5. P8 多头：新增 pnl_ratio > -0.005 保护条件
6. P9 多头：pnl_ratio > 0.01（回测）替换旧版 < 0.005
7. is_strong_threshold 恢复为 0.6（回测原始值）
8. 新增熔断机制：连续亏损 >= 2 次 → 冷却 288 根 K 线
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, Tuple
from autobot.core.strategy_base import StrategyBase, SignalResult
from autobot.utils.logger import logger


# =====================================================
# 指标计算工具
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
# 策略类
# =====================================================

class SuperTrendTemaStrategy(StrategyBase):
    """
    SuperTrend + TEMA 复合策略 v2（完全对齐回测）

    参数:
        st_period            : SuperTrend ATR 周期 (默认 10)
        st_multiplier        : SuperTrend 乘数 (默认 3)
        liquidation_ratio    : 强平阈值 (默认 0.025 = 2.5%)
        active_stop_loss     : 多头主动止损阈值 (默认 0.015 = 1.5%)  ← 新增
        fixed_take_profit    : 固定止盈 (默认 0.025)
        trend_weak_profit    : 趋势减弱止盈 (默认 0.01)
        st_change_profit     : ST 翻转止盈 (默认 0.005)
        big_take_profit      : 大止盈 (默认 0.04)
        take_profit_multiplier: P5 止盈倍率 (默认 1.5)
        stop_loss_multiplier : P5 止亏倍率 (默认 0.75)
        leverage             : 杠杆，用于 P5 计算 (默认 100)
        min_hold_bars        : 最小持仓根数 (默认 3)
        cooldown_bars        : 平仓后普通冷却根数 (默认 10)
        circuit_break_bars   : 熔断冷却根数 (默认 288 ≈ 24h/5min)  ← 新增
        circuit_break_count  : 触发熔断的连续亏损次数 (默认 2)      ← 新增
        bar_minutes          : 单根 K 线分钟数 (默认 5)
        is_strong_threshold  : 趋势强度阈值 (默认 0.6)
    """

    def __init__(
        self,
        st_period: int = 10,
        st_multiplier: float = 3.0,
        liquidation_ratio: float = 0.025,
        active_stop_loss: float = 0.015,          # ← 新增 P1b
        fixed_take_profit: float = 0.025,
        trend_weak_profit: float = 0.01,
        st_change_profit: float = 0.005,
        big_take_profit: float = 0.04,
        take_profit_multiplier: float = 1.5,
        stop_loss_multiplier: float = 0.75,
        leverage: int = 100,
        min_hold_bars: int = 3,
        cooldown_bars: int = 10,
        circuit_break_bars: int = 288,            # ← 新增熔断
        circuit_break_count: int = 2,             # ← 新增熔断
        bar_minutes: int = 5,
        is_strong_threshold: float = 0.6,         # 回测原始值
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
        self.circuit_break_bars = circuit_break_bars
        self.circuit_break_count = circuit_break_count
        self.bar_minutes = bar_minutes
        self.is_strong_threshold = is_strong_threshold

        # 运行时状态
        self._last_close_ts: Optional[pd.Timestamp] = None
        self._consecutive_loss_count: int = 0
        self._circuit_break_until_ts: Optional[pd.Timestamp] = None

    # ---------- 接口实现 ----------

    @property
    def name(self) -> str:
        return "supertrend_tema_v2"

    @property
    def required_data_length(self) -> int:
        return 600

    def need_stop_check(self) -> bool:
        return True

    def check_stop(self, current_position, entry_price, current_price, **kwargs) -> SignalResult:
        """非信号时间的止盈止损（简化版）"""
        if current_position == 0 or entry_price <= 0:
            return SignalResult(SignalResult.NO_SIGNAL, "无仓位")

        if current_position == 1:
            pnl = (current_price - entry_price) / entry_price
        else:
            pnl = (entry_price - current_price) / entry_price

        loss = -pnl if pnl < 0 else 0

        if loss >= self.liquidation_ratio:
            sig = SignalResult.CLOSE_LONG if current_position == 1 else SignalResult.CLOSE_SHORT
            logger.info(f"[ST+TEMA v2][check_stop] 强平: pnl={pnl*100:.3f}%")
            return SignalResult(sig, f"止损强平: {pnl*100:.2f}%")

        # 多头主动止损 P1b
        if current_position == 1 and loss >= self.active_stop_loss:
            logger.info(f"[ST+TEMA v2][check_stop] 多头主动止损: loss={loss*100:.3f}%")
            return SignalResult(SignalResult.CLOSE_LONG, f"主动止损-{self.active_stop_loss*100:.1f}%: {pnl*100:.2f}%")

        if pnl >= self.fixed_take_profit:
            sig = SignalResult.CLOSE_LONG if current_position == 1 else SignalResult.CLOSE_SHORT
            logger.info(f"[ST+TEMA v2][check_stop] 固定止盈: pnl={pnl*100:.3f}%")
            return SignalResult(sig, f"止盈: {pnl*100:.2f}%")

        return SignalResult(SignalResult.NO_SIGNAL, f"未触发: pnl={pnl*100:.2f}%")

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
                f"[ST+TEMA v2] ===== 信号检查 ===== "
                f"ts={current_ts}, price={current_price:.4f}, "
                f"pos={current_position}, entry={entry_price}"
            )

            # ---------- 有仓位：检查平仓 ----------
            if current_position != 0 and entry_price > 0:
                last_pnl = kwargs.get("last_pnl", None)
                close_result = self._check_close_conditions(
                    df, ind, idx, current_position, entry_price, current_price, entry_index
                )
                if close_result.signal != SignalResult.NO_SIGNAL:
                    # 更新熔断计数
                    if current_position == 1:
                        pnl = (current_price - entry_price) / entry_price
                    else:
                        pnl = (entry_price - current_price) / entry_price
                    self._update_circuit_breaker(pnl, current_ts)
                    self._last_close_ts = current_ts
                    logger.info(f"[ST+TEMA v2] >>> 平仓: {close_result.reason}")
                    return close_result
                else:
                    logger.info(f"[ST+TEMA v2] 持仓未触发平仓: {close_result.reason}")

            # ---------- 无仓位：检查开仓 ----------
            if current_position == 0:
                # 熔断检查（优先级高于普通冷却）
                if self._is_in_circuit_break(current_ts):
                    left = self._circuit_break_bars_left(current_ts)
                    logger.info(f"[ST+TEMA v2] 熔断冷却中，剩余约{left}根")
                    return SignalResult(SignalResult.NO_SIGNAL, f"熔断冷却({left}根)")

                # 普通冷却
                if self._is_in_cooldown(current_ts):
                    left = self._cooldown_bars_left(current_ts)
                    logger.info(f"[ST+TEMA v2] 普通冷却中，剩余约{left}根")
                    return SignalResult(SignalResult.NO_SIGNAL, f"冷却期({left}根)")

                open_result = self._check_open_conditions(df, ind, idx, current_price)
                if open_result.signal != SignalResult.NO_SIGNAL:
                    logger.info(f"[ST+TEMA v2] >>> 开仓: {open_result.reason}")
                else:
                    logger.info(f"[ST+TEMA v2] 未触发开仓: {open_result.reason}")
                return open_result

            return SignalResult(SignalResult.NO_SIGNAL, "持仓中，未触发平仓")

        except Exception as e:
            logger.error(f"[ST+TEMA v2] 策略异常: {e}", exc_info=True)
            return SignalResult(SignalResult.NO_SIGNAL, f"策略异常: {e}")

    # =====================================================
    # 冷却 / 熔断
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

    def _update_circuit_breaker(self, pnl: float, current_ts) -> None:
        """平仓后更新熔断状态"""
        if pnl < 0:
            self._consecutive_loss_count += 1
        else:
            self._consecutive_loss_count = 0

        if self._consecutive_loss_count >= self.circuit_break_count:
            # 触发熔断
            if current_ts is not None:
                self._circuit_break_until_ts = (
                    pd.Timestamp(current_ts)
                    + pd.Timedelta(minutes=self.circuit_break_bars * self.bar_minutes)
                )
            self._consecutive_loss_count = 0
            logger.warning(
                f"[ST+TEMA v2] ⚡ 熔断触发！连续亏损{self.circuit_break_count}次，"
                f"冷却至 {self._circuit_break_until_ts}"
            )

    def _is_in_circuit_break(self, current_ts) -> bool:
        if self._circuit_break_until_ts is None or current_ts is None:
            return False
        try:
            return pd.Timestamp(current_ts) < self._circuit_break_until_ts
        except Exception:
            return False

    def _circuit_break_bars_left(self, current_ts) -> int:
        if self._circuit_break_until_ts is None or current_ts is None:
            return 0
        try:
            remaining_min = (
                self._circuit_break_until_ts - pd.Timestamp(current_ts)
            ).total_seconds() / 60.0
            left = remaining_min / self.bar_minutes
            return max(0, int(np.ceil(left)))
        except Exception:
            return 0

    # =====================================================
    # 指标准备
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
            f"[ST+TEMA v2] 指标来源: TEMA={'DB' if used_db_tema else 'CALC'}, "
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
    # 辅助方法
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
                    ind["tema72"].iloc[ci]  < ind["tema72"].iloc[ci - tp] and   # 已修复
                    ind["tema288"].iloc[ci] < ind["tema288"].iloc[ci - tp]):
                return False
        return True

    # =====================================================
    # 平仓逻辑（完全对齐回测）
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
    ) -> SignalResult:

        hold_bars = (idx - entry_index) if entry_index is not None else 999
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
            f"[ST+TEMA v2][平仓] {'多' if position==1 else '空'} "
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

            # P1b 主动止损 1.5%（回测新增，先于强平触发）
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

            # P5 TEMA72 与 ST 反向
            if tema72_down and st_dir == -1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if price_move_loss >= 0.012 and pnl_ratio < -0.008:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")

            # P6 TEMA144 下穿 TEMA288（回测：pnl > 0，保护盈利）
            if t144_cross_t288_down and pnl_ratio > 0:
                return SignalResult(close_sig, "P6 TEMA144下穿TEMA288")

            # P7 TEMA 康法则强反向（微利或小亏）
            if (cur_t72 - cur_t288 <= -20) and cur_t72 <= prev_t72 and pnl_ratio > -0.005:
                return SignalResult(close_sig,
                                    f"P7 TEMA康法则强反向(差={cur_t72-cur_t288:.2f})")

            # P8 大趋势反转 10 根（回测：pnl > -0.005）
            if self._trend_reversal_confirmed(ind, idx, "down", 10) and pnl_ratio > -0.005:
                return SignalResult(close_sig, "P8 大趋势反转(10根下降)")

            # P9 三线同向下降（回测：pnl > 0.01，有足够盈利才退出）
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

            # P5 TEMA72 与 ST 反向
            if tema72_up and st_dir == 1:
                if pnl_ratio * self.leverage >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向盈利止盈"
                                        f"(杠杆后{pnl_ratio*self.leverage*100:.2f}%)")
                if (price_move_loss >= self.liquidation_ratio * self.stop_loss_multiplier
                        and pnl_ratio < -0.015):
                    return SignalResult(close_sig,
                                        f"P5 TEMA72&ST反向止亏({pnl_ratio*100:.2f}%)")

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
    # 开仓逻辑（完全对齐回测）
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

        # 阈值（与回测完全一致）
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
        is_strong = trend_str > self.is_strong_threshold

        # 价格偏离度（相对 TEMA72）← 回测新增，用于 L5/L7
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
            f"[ST+TEMA v2][开仓] price={current_price:.4f} "
            f"ST={st_dir}(prev={prev_st}) ↑={st_to_bull} ↓={st_to_bear}"
        )
        logger.info(
            f"[ST+TEMA v2][开仓] 斜率 t48={slopes['tema48']:.6f} t72={slopes['tema72']:.6f} "
            f"t144={slopes['tema144']:.6f} t288={slopes['tema288']:.6f}"
        )
        logger.info(
            f"[ST+TEMA v2][开仓] trend_str={trend_str:.3f}(>{self.is_strong_threshold}) "
            f"is_strong={is_strong} is_big={is_big_trend} is_ranging={is_ranging} "
            f"price_dev={price_deviation:.5f}"
        )
        logger.info(
            f"[ST+TEMA v2][开仓] 排列 bull={bull_align} bear={bear_align} | "
            f"交叉 t72↑t144={t72_x_t144_up} t72↓t144={t72_x_t144_down} "
            f"t144↑t288={t144_x_t288_up} t144↓t288={t144_x_t288_down}"
        )

        # ========== 做多 ==========
        long_hits = []

        # L1 TEMA144 上穿 TEMA288 + 强趋势
        if t144_x_t288_up and is_strong:
            long_hits.append("L1 TEMA144上穿TEMA288")

        # L2 TEMA72 上穿 TEMA144（严格版，与回测对齐）
        if (t72_x_t144_up and
                slopes["tema144"] > HIGH and
                slopes["tema288"] > 0 and
                st_dir == 1 and
                self._st_consistency(ind, idx, 1, min_bars=3) and
                bull_align and
                is_strong):
            long_hits.append("L2 TEMA72上穿TEMA144")

        # L3 强上升趋势中 ST 翻转
        if st_to_bull and is_strong_up and bull_align and is_strong:
            long_hits.append("L3 强上升趋势ST翻转")

        # L4 大上升趋势
        if (is_big_trend and st_to_bull and st_dir == 1 and
                slopes["tema144"] > BIG and slopes["tema72"] > BIG and
                slopes["tema288"] > HIGH and bull_align and is_strong):
            long_hits.append("L4 大上升趋势开多")

        # L5 全线向上 + price_deviation（回测新增 price_deviation < 0.02）
        if (not is_ranging and
                t48_up and t72_up and t144_up and t288_up and
                st_dir == 1 and bull_align and
                self._st_consistency(ind, idx, 1, 5) and
                abs(slopes["tema72"]) > STRONG and
                is_strong and
                price_deviation < 0.02):
            long_hits.append("L5 全线向上强劲")

        # L6 康法则背景上升点
        if (cur_t72 >= cur_t288 and
                self._long_background_rise(df, ind, idx) and
                st_dir == 1 and is_strong):
            long_hits.append("L6 康法则背景上升点")

        # L7 三线同向上升 + price_deviation（回测新增 price_deviation < 0.02）
        if (self._three_line_upward(df, ind, idx, slopes["tema72"]) and
                st_dir == 1 and bull_align and
                price_deviation < 0.02):
            long_hits.append("L7 三线同向上升")

        # ========== 做空（回测加强版）==========
        short_hits = []

        # S1 TEMA144 下穿 TEMA288（回测加强：需 ST 空头 + 长周期下降）
        if (t144_x_t288_down and
                is_strong and
                st_dir == -1 and
                slopes["tema288"] < -LOW):
            short_hits.append("S1 TEMA144下穿TEMA288")

        # S2 TEMA72 下穿 TEMA144（回测��强：严格对称多头条件）
        if (t72_x_t144_down and
                slopes["tema144"] < -HIGH and
                slopes["tema288"] < 0 and
                st_dir == -1 and
                self._st_consistency(ind, idx, -1, min_bars=3) and
                bear_align and
                is_strong):
            short_hits.append("S2 TEMA72下穿TEMA144")

        # S3 ST 翻空（回测加强：需 is_strong_down + 空头排列）
        if (st_to_bear and
                is_strong_down and
                bear_align and
                is_strong):
            short_hits.append("S3 ST翻空")

        # S4 下降趋势中（回测原始保留）
        if st_dir == -1 and t72_down and t144_down:
            short_hits.append("S4 下降趋势中")

        # S5 全线向下
        if t48_down and t72_down and t144_down and st_dir == -1:
            short_hits.append("S5 全线向下")

        # S6 背景下降点
        if (cur_t72 < cur_t288 and
                self._short_background_decline(df, ind, idx) and
                st_dir == -1):
            short_hits.append("S6 康法则背景下降点")

        # S7 三线同向下降
        if self._three_line_downward(df, ind, idx, slopes["tema72"]) and st_dir == -1:
            short_hits.append("S7 三线同向下降")

        logger.info(
            f"[ST+TEMA v2][开仓汇总] 多={long_hits or '无'} 空={short_hits or '无'}"
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
