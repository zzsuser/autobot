"""
SuperTrend + TEMA 复合策略
从回测代码 eth_20251219_claude.py 中提取的实盘版本

开仓逻辑：基于 TEMA 交叉/排列/斜率 + SuperTrend 方向综合判断
平仓逻辑：9级优先级（止损 > 固定止盈 > 趋势减弱止盈 > ST翻转 > ...）

【本版本修复点】
1. 修复 _three_line_downward 中 tema72 比较方向错误（> 改为 <）
2. 在 _check_open_conditions / _check_close_conditions 增���大量诊断日志
3. 放宽 is_strong 门槛 (0.6 -> 0.4)，并去掉 L1/L3/L4 中的 is_strong 强制要求
4. 移植回测中的 cooldown_bars 冷却逻辑（基于时间戳）
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
    """计算三重指数移动平均线 TEMA"""
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3


def calc_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
                    ) -> Tuple[pd.Series, pd.Series]:
    """
    计算 SuperTrend 指标

    Returns:
        (supertrend_value, direction)  direction: 1=多头, -1=空头
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
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
            supertrend.iloc[i] = (
                final_lb.iloc[i] if direction.iloc[i] == 1 else final_ub.iloc[i]
            )

    return supertrend, direction


def calc_slope(series: pd.Series, idx: int, window: int = 12) -> float:
    """计算指定位置的线性回归斜率"""
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
    SuperTrend + TEMA 复合策略

    参数（可通过构造函数自定义）:
        st_period:           SuperTrend ATR周期 (默认10)
        st_multiplier:       SuperTrend 乘数 (默认3)
        liquidation_ratio:   止损阈值 (默认0.03, 即3%)
        fixed_take_profit:   固定止盈 (默认0.025, 即2.5%)
        trend_weak_profit:   趋势减弱止盈阈值 (默认0.01)
        st_change_profit:    ST翻转止盈阈值 (默认0.005)
        min_hold_bars:       最少持仓K线数 (默认3)
        cooldown_bars:       平仓后冷却K线数 (默认10)
        bar_minutes:         单根K线分钟数，用于冷却时间换算 (默认5)
        is_strong_threshold: 趋势强度阈值 (默认0.4，原回测/旧版为0.6)
    """

    def __init__(
        self,
        st_period: int = 10,
        st_multiplier: float = 3.0,
        liquidation_ratio: float = 0.03,
        fixed_take_profit: float = 0.025,
        trend_weak_profit: float = 0.01,
        st_change_profit: float = 0.005,
        take_profit_multiplier: float = 1.5,
        stop_loss_multiplier: float = 0.75,
        min_hold_bars: int = 3,
        cooldown_bars: int = 10,
        bar_minutes: int = 5,
        is_strong_threshold: float = 0.4,
    ):
        self.st_period = st_period
        self.st_multiplier = st_multiplier
        self.liquidation_ratio = liquidation_ratio
        self.fixed_take_profit = fixed_take_profit
        self.trend_weak_profit = trend_weak_profit
        self.st_change_profit = st_change_profit
        self.take_profit_multiplier = take_profit_multiplier
        self.stop_loss_multiplier = stop_loss_multiplier
        self.min_hold_bars = min_hold_bars
        self.cooldown_bars = cooldown_bars
        self.bar_minutes = bar_minutes
        self.is_strong_threshold = is_strong_threshold

        # 运行时状态
        self._indicators: Optional[Dict] = None
        # 冷却用：上次平仓的时间戳（仅做诊断与冷却判断；策略实例需在引擎中保持单例）
        self._last_close_ts: Optional[pd.Timestamp] = None

    # ---------- 接口实现 ----------

    @property
    def name(self) -> str:
        return "supertrend_tema"

    @property
    def required_data_length(self) -> int:
        # TEMA288 需要足够的预热数据；回测代码用 max(288, len//10)
        return 600

    def need_stop_check(self) -> bool:
        return True

    def check_stop(self, current_position, entry_price, current_price, **kwargs) -> SignalResult:
        """
        非信���时间的止盈止损检查（简化版，仅检查固定阈值）
        完整的多级止盈在 generate_signal 中处理
        """
        if current_position == 0 or entry_price <= 0:
            return SignalResult(SignalResult.NO_SIGNAL, "无仓位")

        if current_position == 1:
            pnl = (current_price - entry_price) / entry_price
        else:
            pnl = (entry_price - current_price) / entry_price

        loss = -pnl if pnl < 0 else 0

        # 止损
        if loss >= self.liquidation_ratio:
            sig = SignalResult.CLOSE_LONG if current_position == 1 else SignalResult.CLOSE_SHORT
            logger.info(f"[ST+TEMA][check_stop] 触发止损: pnl={pnl*100:.3f}%, 阈值={self.liquidation_ratio*100:.2f}%")
            return SignalResult(sig, f"止损触发: {pnl * 100:.2f}%")

        # 固定止盈
        if pnl >= self.fixed_take_profit:
            sig = SignalResult.CLOSE_LONG if current_position == 1 else SignalResult.CLOSE_SHORT
            logger.info(f"[ST+TEMA][check_stop] 触发固定止盈: pnl={pnl*100:.3f}%, 阈值={self.fixed_take_profit*100:.2f}%")
            return SignalResult(sig, f"止盈触发: {pnl * 100:.2f}%")

        return SignalResult(SignalResult.NO_SIGNAL, f"未触发: pnl={pnl * 100:.2f}%")

    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs,
    ) -> SignalResult:
        """
        核心信号生成

        Args:
            df: 数据库读取的DataFrame, 需要 close/high/low/volume 列
                也可能包含预计算的 tema_48/tema_72/tema_144/tema_288/
                supertrend_value/supertrend_direction 等列
            current_position: 0=无仓, 1=多, -1=空
            entry_index: 开仓时的df索引
        """
        try:
            ind = self._prepare_indicators(df)
            idx = len(df) - 1  # 最新一根K线

            if idx < 300:
                logger.warning(f"[ST+TEMA] 数据不足，当前 {idx+1} 根，需要至少 301 根")
                return SignalResult(SignalResult.NO_SIGNAL, "数据不足以计算指标")

            # 当前K线时间
            current_ts = df.index[idx] if hasattr(df.index, '__getitem__') else None
            current_price = df["close"].iloc[idx]
            entry_price = kwargs.get("entry_price", 0)

            logger.info(
                f"[ST+TEMA] ===== 信号检查开始 ===== "
                f"K线时间={current_ts}, 价格={current_price:.4f}, "
                f"当前仓位={current_position}, entry_price={entry_price}, entry_index={entry_index}"
            )

            # ---------- 有仓位：检查平仓 ----------
            if current_position != 0 and entry_price > 0:
                close_result = self._check_close_conditions(
                    df, ind, idx, current_position, entry_price, current_price, entry_index
                )
                if close_result.signal != SignalResult.NO_SIGNAL:
                    # 记录平仓时间戳，用于后续冷却
                    self._last_close_ts = current_ts
                    logger.info(f"[ST+TEMA] >>> 触发平仓: {close_result.reason}")
                    return close_result
                else:
                    logger.info(f"[ST+TEMA] 持仓中未触发平仓: {close_result.reason}")

            # ---------- 无仓位：检查开仓 ----------
            if current_position == 0:
                # 冷却检查
                if self._is_in_cooldown(current_ts):
                    bars_left = self._cooldown_bars_left(current_ts)
                    logger.info(
                        f"[ST+TEMA] 处于冷却期，剩余约 {bars_left} 根K线 "
                        f"(上次平仓时间={self._last_close_ts}, cooldown_bars={self.cooldown_bars})"
                    )
                    return SignalResult(SignalResult.NO_SIGNAL,
                                        f"冷却期内（剩余{bars_left}根）")

                open_result = self._check_open_conditions(df, ind, idx, current_price)
                if open_result.signal != SignalResult.NO_SIGNAL:
                    logger.info(f"[ST+TEMA] >>> 触发开仓: {open_result.reason}")
                else:
                    logger.info(f"[ST+TEMA] 未触发开仓: {open_result.reason}")
                return open_result

            return SignalResult(SignalResult.NO_SIGNAL, "持仓中，未触发平仓")

        except Exception as e:
            logger.error(f"SuperTrend+TEMA策略异常: {e}", exc_info=True)
            return SignalResult(SignalResult.NO_SIGNAL, f"策略异常: {e}")

    # =====================================================
    # 冷却判断
    # =====================================================

    def _is_in_cooldown(self, current_ts) -> bool:
        if self._last_close_ts is None or current_ts is None:
            return False
        try:
            elapsed_min = (pd.Timestamp(current_ts) - pd.Timestamp(self._last_close_ts)).total_seconds() / 60.0
            return elapsed_min < self.cooldown_bars * self.bar_minutes
        except Exception:
            return False

    def _cooldown_bars_left(self, current_ts) -> int:
        if self._last_close_ts is None or current_ts is None:
            return 0
        try:
            elapsed_min = (pd.Timestamp(current_ts) - pd.Timestamp(self._last_close_ts)).total_seconds() / 60.0
            elapsed_bars = elapsed_min / self.bar_minutes
            left = self.cooldown_bars - elapsed_bars
            return max(0, int(np.ceil(left)))
        except Exception:
            return 0

    # =====================================================
    # 指标准备
    # =====================================================

    def _prepare_indicators(self, df: pd.DataFrame) -> Dict:
        """准备所有需要的技术指标，优先使用数据库已有的"""
        close = df["close"]

        used_db_tema = "tema_48" in df.columns
        used_db_st = "supertrend_value" in df.columns and "supertrend_direction" in df.columns

        # TEMA - 优先数据库，否则计算
        tema48 = df["tema_48"] if "tema_48" in df.columns else calc_tema(close, 48)
        tema72 = df["tema_72"] if "tema_72" in df.columns else calc_tema(close, 72)
        tema144 = df["tema_144"] if "tema_144" in df.columns else calc_tema(close, 144)
        tema288 = df["tema_288"] if "tema_288" in df.columns else calc_tema(close, 288)

        # SuperTrend - 优先数据库
        if used_db_st:
            st_value = df["supertrend_value"]
            st_direction = df["supertrend_direction"]
        else:
            st_value, st_direction = calc_supertrend(df, self.st_period, self.st_multiplier)

        logger.debug(
            f"[ST+TEMA] 指标来源: TEMA={'DB' if used_db_tema else 'CALC'}, "
            f"SuperTrend={'DB' if used_db_st else 'CALC'}"
        )

        return {
            "tema48": tema48,
            "tema72": tema72,
            "tema144": tema144,
            "tema288": tema288,
            "st_value": st_value,
            "st_direction": st_direction,
        }

    # =====================================================
    # 辅助计算
    # =====================================================

    def _get_slopes(self, ind: Dict, idx: int) -> Dict:
        """计算当前位置各TEMA的斜率"""
        return {
            "tema48": calc_slope(ind["tema48"], idx),
            "tema72": calc_slope(ind["tema72"], idx),
            "tema144": calc_slope(ind["tema144"], idx),
            "tema288": calc_slope(ind["tema288"], idx),
        }

    def _trend_strength(self, df: pd.DataFrame, idx: int, lookback: int = 20) -> float:
        """计算趋势强度 (0~1)"""
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
        """TEMA多头排列: 72 > 144 > 288"""
        return (ind["tema72"].iloc[idx] > ind["tema144"].iloc[idx] >
                ind["tema288"].iloc[idx])

    def _tema_alignment_bear(self, ind: Dict, idx: int) -> bool:
        """TEMA空头排列: 72 < 144 < 288"""
        return (ind["tema72"].iloc[idx] < ind["tema144"].iloc[idx] <
                ind["tema288"].iloc[idx])

    def _st_consistency(self, ind: Dict, idx: int, direction: int, min_bars: int = 5) -> bool:
        """检查SuperTrend是否连续min_bars根保持某个方向"""
        if idx < min_bars:
            return False
        st_dir = ind["st_direction"]
        for j in range(idx - min_bars + 1, idx + 1):
            if st_dir.iloc[j] != direction:
                return False
        return True

    def _trend_reversal_confirmed(self, ind: Dict, idx: int, direction: str, min_bars: int = 6) -> bool:
        """
        检查TEMA72/144/288是否连续min_bars根同向变化

        direction: 'down' 表示全部下降, 'up' 表示全部上升
        """
        if idx < min_bars + 1:
            return False
        for j in range(idx - min_bars + 1, idx + 1):
            d72 = ind["tema72"].iloc[j] - ind["tema72"].iloc[j - 1]
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
        """做空背景下降点: 价格>=前6根最高 且 TEMA72<=前6根最低"""
        if idx < 6:
            return False
        cp = df["close"].iloc[idx]
        ct = ind["tema72"].iloc[idx]
        prev_prices = [df["close"].iloc[j] for j in range(idx - 6, idx)]
        prev_temas = [ind["tema72"].iloc[j] for j in range(idx - 6, idx)]
        return cp >= max(prev_prices) and ct <= min(prev_temas)

    def _long_background_rise(self, df: pd.DataFrame, ind: Dict, idx: int) -> bool:
        """做多背景上升点: 价格<=前12根最低 且 TEMA72>=前6根最高"""
        if idx < 12:
            return False
        cp = df["close"].iloc[idx]
        ct = ind["tema72"].iloc[idx]
        prev_prices = [df["close"].iloc[j] for j in range(idx - 12, idx)]
        prev_temas = [ind["tema72"].iloc[j] for j in range(idx - 6, idx)]
        return cp <= min(prev_prices) and ct >= max(prev_temas)

    def _three_line_upward(self, df: pd.DataFrame, ind: Dict, idx: int, tema72_slope: float) -> bool:
        """三线同向上升: 价格涨幅>0.8% + TEMA48/72/288连续三周期上升"""
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
            cur_c = df["close"].iloc[ci]
            prev_c = df["close"].iloc[ci - tp]
            if not (cur_c > prev_c and
                    ind["tema48"].iloc[ci] > ind["tema48"].iloc[ci - tp] and
                    ind["tema72"].iloc[ci] > ind["tema72"].iloc[ci - tp] and
                    ind["tema288"].iloc[ci] > ind["tema288"].iloc[ci - tp]):
                return False
        return True

    def _three_line_downward(self, df: pd.DataFrame, ind: Dict, idx: int, tema72_slope: float) -> bool:
        """
        三线同向下降: 价格跌幅>0.8% + TEMA48/72/288连续三周期下降
        【修复 #1】tema72 比较方向修正为 '<'，与回测保持一致
        """
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
            cur_c = df["close"].iloc[ci]
            prev_c = df["close"].iloc[ci - tp]
            if not (cur_c < prev_c and
                    ind["tema48"].iloc[ci] < ind["tema48"].iloc[ci - tp] and
                    ind["tema72"].iloc[ci] < ind["tema72"].iloc[ci - tp] and  # ✅ 修复：原来是 >
                    ind["tema288"].iloc[ci] < ind["tema288"].iloc[ci - tp]):
                return False
        return True

    # =====================================================
    # 平仓逻辑 - 9级优先级
    # =====================================================

    def _check_close_conditions(
        self,
        df: pd.DataFrame,
        ind: Dict,
        idx: int,
        position: int,    # 1=多, -1=空
        entry_price: float,
        current_price: float,
        entry_index: Optional[int],
    ) -> SignalResult:
        """
        检查平仓条件，按优先级从高到低
        """
        # 持仓K线数检查
        hold_bars = (idx - entry_index) if entry_index is not None else 999
        if hold_bars < self.min_hold_bars:
            return SignalResult(SignalResult.NO_SIGNAL,
                                f"持仓{hold_bars}根，未达最小{self.min_hold_bars}")

        # 计算盈亏比
        if position == 1:
            pnl_ratio = (current_price - entry_price) / entry_price
        else:
            pnl_ratio = (entry_price - current_price) / entry_price

        price_move_loss = -pnl_ratio if pnl_ratio < 0 else 0

        # 斜率和方向
        slopes = self._get_slopes(ind, idx)
        st_dir = ind["st_direction"].iloc[idx]
        prev_st_dir = ind["st_direction"].iloc[idx - 1] if idx > 0 else st_dir

        tema72_up = slopes["tema72"] > 0
        tema72_down = slopes["tema72"] <= 0
        tema144_up = slopes["tema144"] > 0

        cur_t72 = ind["tema72"].iloc[idx]
        cur_t144 = ind["tema144"].iloc[idx]
        cur_t288 = ind["tema288"].iloc[idx]
        prev_t72 = ind["tema72"].iloc[idx - 1]
        prev_t144 = ind["tema144"].iloc[idx - 1]
        prev_t288 = ind["tema288"].iloc[idx - 1]

        # TEMA交叉
        t144_cross_t288_down = (prev_t144 >= prev_t288 and cur_t144 < cur_t288)
        t144_cross_t288_up = (prev_t144 <= prev_t288 and cur_t144 > cur_t288)

        # ST翻转
        st_to_bear = (prev_st_dir != -1 and st_dir == -1)
        st_to_bull = (prev_st_dir != 1 and st_dir == 1)

        # ===== 诊断日志 =====
        logger.info(
            f"[ST+TEMA][平仓检查] 方向={'多' if position == 1 else '空'}, "
            f"持仓={hold_bars}根, pnl={pnl_ratio*100:+.3f}%, loss={price_move_loss*100:.3f}% | "
            f"ST方向={st_dir}(prev={prev_st_dir}), st_to_bear={st_to_bear}, st_to_bull={st_to_bull}"
        )
        logger.info(
            f"[ST+TEMA][平仓检查] 斜率: t72={slopes['tema72']:.6f}, "
            f"t144={slopes['tema144']:.6f}, t288={slopes['tema288']:.6f} | "
            f"TEMA: t72={cur_t72:.4f}, t144={cur_t144:.4f}, t288={cur_t288:.4f} (差={cur_t72 - cur_t288:.4f})"
        )
        logger.info(
            f"[ST+TEMA][平仓检查] 交叉: t144↑t288={t144_cross_t288_up}, t144↓t288={t144_cross_t288_down}"
        )

        close_sig = SignalResult.CLOSE_LONG if position == 1 else SignalResult.CLOSE_SHORT

        # ===== 做多平仓 =====
        if position == 1:
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig, f"P1止损/强平 (跌幅{price_move_loss * 100:.2f}%)")

            if pnl_ratio >= self.fixed_take_profit:
                return SignalResult(close_sig, f"P2固定止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if pnl_ratio >= self.trend_weak_profit and (tema72_down or st_dir == -1):
                return SignalResult(close_sig,
                                    f"P3趋势减弱止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if pnl_ratio > self.st_change_profit and st_to_bear:
                return SignalResult(close_sig, f"P4 SuperTrend翻空止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if tema72_down and st_dir == -1:
                if pnl_ratio >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72与ST反向盈利止盈 (盈利{pnl_ratio * 100:.2f}%)")
                if price_move_loss >= self.liquidation_ratio * self.stop_loss_multiplier and pnl_ratio < -0.015:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72与ST反向止亏 (亏损{pnl_ratio * 100:.2f}%)")

            if t144_cross_t288_down:
                return SignalResult(close_sig, "P6 TEMA144下穿TEMA288")

            if (cur_t72 - cur_t288 <= -20) and cur_t72 <= prev_t72:
                return SignalResult(close_sig, f"P7 TEMA康法则强反向 (差={cur_t72 - cur_t288:.2f})")

            if self._trend_reversal_confirmed(ind, idx, "down", min_bars=8):
                return SignalResult(close_sig, "P8 大趋势反转(连续8根下降)")

            if self._three_line_downward(df, ind, idx, slopes["tema72"]):
                return SignalResult(close_sig, "P9 三线同向下降平仓")

        # ===== 做空平仓 =====
        elif position == -1:
            if price_move_loss >= self.liquidation_ratio:
                return SignalResult(close_sig, f"P1止损/强平 (涨幅{price_move_loss * 100:.2f}%)")

            if pnl_ratio >= self.fixed_take_profit:
                return SignalResult(close_sig, f"P2固定止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if pnl_ratio >= self.trend_weak_profit and (tema72_up or st_dir == 1):
                return SignalResult(close_sig,
                                    f"P3趋势减弱止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if pnl_ratio > self.st_change_profit and st_to_bull:
                return SignalResult(close_sig, f"P4 SuperTrend翻多止盈 (盈利{pnl_ratio * 100:.2f}%)")

            if tema72_up and st_dir == 1:
                if pnl_ratio >= self.liquidation_ratio * self.take_profit_multiplier:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72与ST反向盈利止盈 (盈利{pnl_ratio * 100:.2f}%)")
                if price_move_loss >= self.liquidation_ratio * self.stop_loss_multiplier and pnl_ratio < -0.015:
                    return SignalResult(close_sig,
                                        f"P5 TEMA72与ST反向止亏 (亏损{pnl_ratio * 100:.2f}%)")

            if t144_cross_t288_up:
                return SignalResult(close_sig, "P6 TEMA144上穿TEMA288")

            if (cur_t72 - cur_t288 >= 20) and cur_t72 >= prev_t72:
                return SignalResult(close_sig, f"P7 TEMA康法则强反向 (差={cur_t72 - cur_t288:.2f})")

            if self._trend_reversal_confirmed(ind, idx, "up", min_bars=6):
                return SignalResult(close_sig, "P8 大趋势反转(连续6根上升)")

            if self._three_line_upward(df, ind, idx, slopes["tema72"]):
                return SignalResult(close_sig, "P9 三线同向上升平仓")

        return SignalResult(SignalResult.NO_SIGNAL, "未触发平仓条件(P1~P9均未命中)")

    # =====================================================
    # 开仓逻辑 - 7种做多 + 7种做空
    # =====================================================

    def _check_open_conditions(
        self, df: pd.DataFrame, ind: Dict, idx: int, current_price: float
    ) -> SignalResult:
        """检查开仓条件"""

        slopes = self._get_slopes(ind, idx)
        st_dir = ind["st_direction"].iloc[idx]
        prev_st_dir = ind["st_direction"].iloc[idx - 1] if idx > 0 else st_dir

        cur_t48 = ind["tema48"].iloc[idx]
        cur_t72 = ind["tema72"].iloc[idx]
        cur_t144 = ind["tema144"].iloc[idx]
        cur_t288 = ind["tema288"].iloc[idx]
        prev_t72 = ind["tema72"].iloc[idx - 1]
        prev_t144 = ind["tema144"].iloc[idx - 1]
        prev_t288 = ind["tema288"].iloc[idx - 1]

        # 斜率方向
        t48_up = slopes["tema48"] > 0
        t48_down = slopes["tema48"] <= 0
        t72_up = slopes["tema72"] > 0
        t72_down = slopes["tema72"] <= 0
        t144_up = slopes["tema144"] > 0
        t144_down = slopes["tema144"] <= 0
        t288_up = slopes["tema288"] > 0
        t288_down = slopes["tema288"] <= 0

        # 阈值
        HIGH = 0.0002
        LOW = 0.00005
        BIG = 0.0003
        STRONG = 0.0005

        t72_low_slope = abs(slopes["tema72"]) < LOW
        t144_low_slope = abs(slopes["tema144"]) < LOW
        t288_low_slope = abs(slopes["tema288"]) < LOW
        is_ranging = t72_low_slope and t144_low_slope and t288_low_slope

        is_big_trend = (abs(slopes["tema72"]) > BIG or
                        abs(slopes["tema144"]) > BIG or
                        abs(slopes["tema288"]) > BIG)

        trend_str = self._trend_strength(df, idx)
        # 【修复 #3】放宽 is_strong 阈值（默认 0.4，可通过参数调整）
        is_strong = trend_str > self.is_strong_threshold

        # 交叉
        t72_x_t144_up = (prev_t72 <= prev_t144 and cur_t72 > cur_t144)
        t72_x_t144_down = (prev_t72 >= prev_t144 and cur_t72 < cur_t144)
        t144_x_t288_up = (prev_t144 <= prev_t288 and cur_t144 > cur_t288)
        t144_x_t288_down = (prev_t144 >= prev_t288 and cur_t144 < cur_t288)

        st_to_bull = (prev_st_dir != 1 and st_dir == 1)
        st_to_bear = (prev_st_dir != -1 and st_dir == -1)

        is_strong_up = (slopes["tema288"] > HIGH and t144_up and
                        cur_t144 > cur_t288 and cur_t72 > cur_t144)
        is_strong_down = (slopes["tema288"] < -HIGH and t144_down and
                          cur_t144 < cur_t288 and cur_t72 < cur_t144)

        bull_align = self._tema_alignment_bull(ind, idx)
        bear_align = self._tema_alignment_bear(ind, idx)

        # ========== 诊断日志：所有指标值/布尔状态 ==========
        logger.info(
            f"[ST+TEMA][开仓诊断] price={current_price:.4f}, "
            f"ST方向={st_dir}(prev={prev_st_dir}), st↑bull={st_to_bull}, st↓bear={st_to_bear}"
        )
        logger.info(
            f"[ST+TEMA][开仓诊断] 斜率: t48={slopes['tema48']:.6f}, t72={slopes['tema72']:.6f}, "
            f"t144={slopes['tema144']:.6f}, t288={slopes['tema288']:.6f}"
        )
        logger.info(
            f"[ST+TEMA][开仓诊断] TEMA: t48={cur_t48:.4f}, t72={cur_t72:.4f}, "
            f"t144={cur_t144:.4f}, t288={cur_t288:.4f}"
        )
        logger.info(
            f"[ST+TEMA][开仓诊断] 趋势状态: trend_str={trend_str:.3f} (阈值={self.is_strong_threshold}), "
            f"is_strong={is_strong}, is_big_trend={is_big_trend}, is_ranging={is_ranging}, "
            f"is_strong_up={is_strong_up}, is_strong_down={is_strong_down}"
        )
        logger.info(
            f"[ST+TEMA][开仓诊断] 排列: bull_align={bull_align}, bear_align={bear_align} | "
            f"交叉: t72↑t144={t72_x_t144_up}, t72↓t144={t72_x_t144_down}, "
            f"t144↑t288={t144_x_t288_up}, t144↓t288={t144_x_t288_down}"
        )
        logger.info(
            f"[ST+TEMA][开仓诊断] 方向(↑/↓): t48={'↑' if t48_up else '↓'}, t72={'↑' if t72_up else '↓'}, "
            f"t144={'↑' if t144_up else '↓'}, t288={'↑' if t288_up else '↓'}"
        )

        # ========== 做多条件 ==========
        long_met = False
        long_reason = ""
        long_hits = []

        # L1: 【修复 #3】TEMA144上穿TEMA288 （去掉 is_strong 强制）
        if t144_x_t288_up:
            long_met, long_reason = True, "L1 TEMA144上穿TEMA288开多"
            long_hits.append("L1")

        # L2: TEMA72上穿TEMA144 + TEMA144斜率向上 + ST多头
        if t72_x_t144_up and slopes["tema144"] > HIGH and st_dir == 1:
            long_met, long_reason = True, "L2 TEMA72上穿TEMA144开多"
            long_hits.append("L2")

        # L3: 【修复 #3】强上升趋势中ST翻转 （去掉 is_strong 强制）
        if st_to_bull and is_strong_up and bull_align:
            long_met, long_reason = True, "L3 强上升趋势中ST翻转开多"
            long_hits.append("L3")

        # L4: 【修复 #3】大上升趋势 （去掉 is_strong 强制）
        if (is_big_trend and st_to_bull and st_dir == 1 and
                slopes["tema144"] > BIG and slopes["tema72"] > BIG and
                slopes["tema288"] > HIGH and bull_align):
            long_met, long_reason = True, "L4 大上升趋势中开多"
            long_hits.append("L4")

        # L5: 全线向上 + 强劲（保留 is_strong）
        if (not is_ranging and t48_up and t72_up and t144_up and t288_up and
                st_dir == 1 and bull_align and
                self._st_consistency(ind, idx, 1, 5) and
                abs(slopes["tema72"]) > STRONG and is_strong):
            long_met, long_reason = True, "L5 全线向上且趋势强劲开多"
            long_hits.append("L5")

        # L6: 康交易法则背景上升点（保留 is_strong）
        if (cur_t72 >= cur_t288 and
                self._long_background_rise(df, ind, idx) and
                st_dir == 1 and is_strong):
            long_met, long_reason = True, "L6 康交易法则背景上升点开多"
            long_hits.append("L6")

        # L7: 三线同向上升
        if (self._three_line_upward(df, ind, idx, slopes["tema72"]) and
                st_dir == 1 and bull_align):
            long_met, long_reason = True, "L7 三线同向上升趋势开多"
            long_hits.append("L7")

        # ========== 做空条件 ==========
        short_met = False
        short_reason = ""
        short_hits = []

        # S1: TEMA144下穿TEMA288
        if t144_x_t288_down:
            short_met, short_reason = True, "S1 TEMA144下穿TEMA288开空"
            short_hits.append("S1")

        # S2: TEMA72下穿TEMA144
        if t72_x_t144_down and slopes["tema144"] < 0:
            short_met, short_reason = True, "S2 TEMA72下穿TEMA144开空"
            short_hits.append("S2")

        # S3: ST翻空
        if st_to_bear:
            short_met, short_reason = True, "S3 SuperTrend翻空开空"
            short_hits.append("S3")

        # S4: 下降趋势中
        if st_dir == -1 and t72_down and t144_down:
            short_met, short_reason = True, "S4 下降趋势中开空"
            short_hits.append("S4")

        # S5: 全线向下
        if t48_down and t72_down and t144_down and st_dir == -1:
            short_met, short_reason = True, "S5 全线向下开空"
            short_hits.append("S5")

        # S6: 背景下降点
        if (cur_t72 < cur_t288 and
                self._short_background_decline(df, ind, idx) and st_dir == -1):
            short_met, short_reason = True, "S6 背景下降点开空"
            short_hits.append("S6")

        # S7: 三线同向下降（受益于修复#1）
        if (self._three_line_downward(df, ind, idx, slopes["tema72"]) and st_dir == -1):
            short_met, short_reason = True, "S7 三线同向下降开空"
            short_hits.append("S7")

        # ========== 命中汇总 ==========
        logger.info(
            f"[ST+TEMA][开仓汇总] 多头命中={long_hits if long_hits else '无'}, "
            f"空头命中={short_hits if short_hits else '无'}"
        )

        # ========== 决策 ==========
        if long_met and not short_met:
            return SignalResult(SignalResult.LONG, long_reason)

        if short_met and not long_met:
            return SignalResult(SignalResult.SHORT, short_reason)

        if long_met and short_met:
            # 多空冲突，以SuperTrend方向为准
            if st_dir == 1:
                return SignalResult(SignalResult.LONG, long_reason + "(多空冲突,ST选多)")
            else:
                return SignalResult(SignalResult.SHORT, short_reason + "(多空冲突,ST选空)")

        return SignalResult(SignalResult.NO_SIGNAL,
                            f"无开仓信号 (多={long_hits or '无'}, 空={short_hits or '无'})")
