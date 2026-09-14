"""ETH 15-minute spike strategy — R16 live strategy adapter.

This module replaces the signal layer of ``SuperTrendTemaStrategyV3_1`` while
keeping the same ``StrategyBase.generate_signal`` integration shape.  It does
not submit exchange orders.  Position sizing, same-direction additions,
reversals, and protective TP/SL orders remain responsibilities of the autobot
execution layer.

R16 signal parameters are frozen to the audited V1.8 experiment:

* Bollinger: length 18, multiplier 2.5, close in the strict half-band.
* Pre-move: 3 to 4 bars, cumulative move >= 0.8%.
* Sideways: configured as 3, but only 0 or 1 is feasible when the total
  lookback is 4 and at least 3 trend bars are required.
* Current spike amplitude >= 0.25%.
* Last-bar retracement <= 0.4%.
* Maximum same-direction entries: 3.
* Protective exit: TP 0.6%, SL 0.9%, recalculated from the exchange-reported
  position average after every entry/addition.

The signal implementation intentionally mirrors ``backtest_eth_spike_v1_6``.
Do not simplify or vector-rewrite it without passing the parity gate described
in the accompanying deployment guide.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

import numpy as np
import pandas as pd

from autobot.core.strategy_base import SignalResult, StrategyBase
from autobot.utils.logger import log_trade, logger


@dataclass(frozen=True)
class R16SignalConfig:
    """Frozen R16 signal and exit parameters.

    Runtime/exchange settings such as API keys, account mode, leverage,
    contract value, lot size, and position sizing do not belong here.
    """

    wick_body_ratio: float = 2.0
    small_body_pct: float = 20.0
    small_wick_opp_ratio: float = 2.0
    break_lookback: int = 4
    enable_break_rule: bool = True
    enable_current_spike_amplitude: bool = True
    min_current_spike_amplitude_pct: float = 0.25

    use_boll_filter: bool = True
    current_spike_midline_only: bool = False
    bb_length: int = 18
    bb_mult: float = 2.5

    min_pre_move_bars: int = 3
    max_pre_lookback_bars: int = 4
    min_pre_move_pct: float = 0.80
    enable_last_bar_retracement: bool = True
    max_last_bar_retracement_pct: float = 0.40
    trend_end_must_stay_in_zone: bool = True
    max_sideways: int = 3
    sideways_must_stay_in_zone: bool = True
    sideways_no_new_extreme: bool = True
    sideways_tolerance_pct: float = 0.0

    max_same_direction_entries: int = 3
    allow_long: bool = True
    allow_short: bool = True

    take_profit_pct: float = 0.60
    stop_loss_pct: float = 0.90


def assert_frozen_r16(cfg: R16SignalConfig) -> None:
    """Reject accidental parameter drift from the audited R16 candidate."""

    expected = R16SignalConfig()
    if cfg != expected:
        changed = []
        for name in expected.__dataclass_fields__:
            before = getattr(expected, name)
            after = getattr(cfg, name)
            if before != after:
                changed.append(f"{name}: expected={before!r}, actual={after!r}")
        raise ValueError("R16 frozen-parameter violation: " + "; ".join(changed))


def _validate_ohlc(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close"}
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"missing OHLC columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("df.index must be a pandas.DatetimeIndex of 15m bar starts")
    if not df.index.is_monotonic_increasing:
        raise ValueError("15m bars must be sorted in ascending timestamp order")
    if df.index.has_duplicates:
        raise ValueError("duplicate 15m bar timestamps detected")
    if df[list(required)].tail(64).isna().any(axis=None):
        raise ValueError("NaN detected in recent OHLC bars")


def add_r16_signals(bars: pd.DataFrame, cfg: R16SignalConfig) -> pd.DataFrame:
    """Add exact V1.6/R16 signal columns to 15-minute closed bars."""

    _validate_ohlc(bars)
    d = bars.copy()

    d["basis"] = d["close"].rolling(
        cfg.bb_length, min_periods=cfg.bb_length
    ).mean()
    d["dev"] = d["close"].rolling(
        cfg.bb_length, min_periods=cfg.bb_length
    ).std(ddof=0)
    d["upper"] = d["basis"] + cfg.bb_mult * d["dev"]
    d["lower"] = d["basis"] - cfg.bb_mult * d["dev"]
    d["strict_strong_zone"] = (
        (d["close"] >= d["basis"]) & (d["close"] <= d["upper"])
    )
    d["strict_weak_zone"] = (
        (d["close"] >= d["lower"]) & (d["close"] <= d["basis"])
    )
    d["strong_zone"] = (
        d["close"] >= d["basis"]
        if cfg.current_spike_midline_only
        else d["strict_strong_zone"]
    )
    d["weak_zone"] = (
        d["close"] <= d["basis"]
        if cfg.current_spike_midline_only
        else d["strict_weak_zone"]
    )

    body = (d["close"] - d["open"]).abs()
    bar_range = d["high"] - d["low"]
    upper_wick = d["high"] - pd.concat(
        [d["open"], d["close"]], axis=1
    ).max(axis=1)
    lower_wick = pd.concat(
        [d["open"], d["close"]], axis=1
    ).min(axis=1) - d["low"]
    body_pct = pd.Series(
        np.where(bar_range > 0, body / bar_range * 100.0, 0.0),
        index=d.index,
    )
    small = body_pct <= cfg.small_body_pct
    bull = d["close"] > d["open"]
    bear = d["close"] < d["open"]

    previous_highest = d["high"].shift(1).rolling(
        cfg.break_lookback, min_periods=cfg.break_lookback
    ).max()
    previous_lowest = d["low"].shift(1).rolling(
        cfg.break_lookback, min_periods=cfg.break_lookback
    ).min()
    new_high = d["high"] > previous_highest
    new_low = d["low"] < previous_lowest

    upper_spike_bull = (
        (~small) & bull & (upper_wick >= cfg.wick_body_ratio * body)
    )
    upper_spike_bear = (
        (~small) & bear & (body <= upper_wick / cfg.wick_body_ratio)
    )
    lower_spike_bull = (
        (~small) & bull & (body <= lower_wick / cfg.wick_body_ratio)
    )
    lower_spike_bear = (
        (~small) & bear & (lower_wick >= cfg.wick_body_ratio * body)
    )
    small_upper = small & (upper_wick > lower_wick) & (
        (upper_wick >= cfg.small_wick_opp_ratio * lower_wick)
        | (cfg.enable_break_rule & new_high)
    )
    small_lower = small & (lower_wick > upper_wick) & (
        (lower_wick >= cfg.small_wick_opp_ratio * upper_wick)
        | (cfg.enable_break_rule & new_low)
    )

    d["upper_spike_bull"] = upper_spike_bull
    d["upper_spike_bear"] = upper_spike_bear
    d["small_upper_spike"] = small_upper
    d["lower_spike_bull"] = lower_spike_bull
    d["lower_spike_bear"] = lower_spike_bear
    d["small_lower_spike"] = small_lower
    d["upper_spike"] = upper_spike_bull | upper_spike_bear | small_upper
    d["lower_spike"] = lower_spike_bull | lower_spike_bear | small_lower
    d["body"] = body
    d["bar_range"] = bar_range
    d["body_pct_of_range"] = body_pct
    d["upper_wick"] = upper_wick
    d["lower_wick"] = lower_wick
    d["new_high"] = new_high
    d["new_low"] = new_low

    upper_span = d["upper"] - d["basis"]
    lower_span = d["basis"] - d["lower"]
    d["boll_position_normalized"] = np.where(
        d["close"] >= d["basis"],
        np.where(
            upper_span > 0,
            (d["close"] - d["basis"]) / upper_span,
            np.nan,
        ),
        np.where(
            lower_span > 0,
            (d["close"] - d["basis"]) / lower_span,
            np.nan,
        ),
    )
    d["current_spike_amplitude_pct"] = np.where(
        d["low"] > 0, bar_range / d["low"] * 100.0, 0.0
    )
    d["current_spike_amplitude_pass"] = (
        pd.Series(True, index=d.index)
        if not cfg.enable_current_spike_amplitude
        else d["current_spike_amplitude_pct"]
        >= cfg.min_current_spike_amplitude_pct
    )

    count = len(d)
    up_context = np.zeros(count, dtype=bool)
    down_context = np.zeros(count, dtype=bool)
    diagnostic = {
        name: np.full(count, np.nan)
        for name in [
            "up_sideways",
            "down_sideways",
            "up_trend_bars",
            "down_trend_bars",
            "up_move",
            "down_move",
            "up_retrace",
            "down_retrace",
        ]
    }
    highs = d["high"].to_numpy()
    lows = d["low"].to_numpy()
    closes = d["close"].to_numpy()
    strict_strong = d["strict_strong_zone"].to_numpy(dtype=bool)
    strict_weak = d["strict_weak_zone"].to_numpy(dtype=bool)
    min_move_bars = min(cfg.min_pre_move_bars, cfg.max_pre_lookback_bars)

    for i in range(count):
        for sideways_bars in range(cfg.max_sideways + 1):
            max_move_bars = cfg.max_pre_lookback_bars - sideways_bars
            if max_move_bars < min_move_bars:
                continue
            for trend_bars in range(min_move_bars, max_move_bars + 1):
                newest_offset = sideways_bars + 1
                base_offset = sideways_bars + trend_bars + 1
                if i < max(base_offset, 2):
                    continue
                newest = i - newest_offset
                base = i - base_offset
                base_close = closes[base]
                end_close = closes[newest]
                if base_close <= 0:
                    continue

                window_high = float(
                    np.max(highs[i - (sideways_bars + trend_bars) : newest + 1])
                )
                window_low = float(
                    np.min(lows[i - (sideways_bars + trend_bars) : newest + 1])
                )
                up_move = (end_close - base_close) / base_close * 100.0
                down_move = (base_close - end_close) / base_close * 100.0
                up_retrace = (
                    max(closes[i - 2] - closes[i - 1], 0.0)
                    / base_close
                    * 100.0
                )
                down_retrace = (
                    max(closes[i - 1] - closes[i - 2], 0.0)
                    / base_close
                    * 100.0
                )

                up_zone_ok = (
                    (not cfg.use_boll_filter)
                    or (not cfg.trend_end_must_stay_in_zone)
                    or bool(strict_strong[newest])
                )
                down_zone_ok = (
                    (not cfg.use_boll_filter)
                    or (not cfg.trend_end_must_stay_in_zone)
                    or bool(strict_weak[newest])
                )
                up_side_ok = True
                down_side_ok = True
                for offset in range(1, sideways_bars + 1):
                    j = i - offset
                    if cfg.use_boll_filter and cfg.sideways_must_stay_in_zone:
                        up_side_ok = up_side_ok and bool(strict_strong[j])
                        down_side_ok = down_side_ok and bool(strict_weak[j])
                    if cfg.sideways_no_new_extreme:
                        up_side_ok = up_side_ok and bool(
                            highs[j]
                            <= window_high
                            * (1 + cfg.sideways_tolerance_pct / 100.0)
                        )
                        down_side_ok = down_side_ok and bool(
                            lows[j]
                            >= window_low
                            * (1 - cfg.sideways_tolerance_pct / 100.0)
                        )

                up_ok = (
                    up_move >= cfg.min_pre_move_pct
                    and (
                        (not cfg.enable_last_bar_retracement)
                        or up_retrace <= cfg.max_last_bar_retracement_pct
                    )
                    and up_zone_ok
                    and up_side_ok
                )
                down_ok = (
                    down_move >= cfg.min_pre_move_pct
                    and (
                        (not cfg.enable_last_bar_retracement)
                        or down_retrace <= cfg.max_last_bar_retracement_pct
                    )
                    and down_zone_ok
                    and down_side_ok
                )

                if not up_context[i] and up_ok:
                    up_context[i] = True
                    diagnostic["up_sideways"][i] = sideways_bars
                    diagnostic["up_trend_bars"][i] = trend_bars
                    diagnostic["up_move"][i] = up_move
                    diagnostic["up_retrace"][i] = up_retrace
                if not down_context[i] and down_ok:
                    down_context[i] = True
                    diagnostic["down_sideways"][i] = sideways_bars
                    diagnostic["down_trend_bars"][i] = trend_bars
                    diagnostic["down_move"][i] = down_move
                    diagnostic["down_retrace"][i] = down_retrace

    d["up_context"] = up_context
    d["down_context"] = down_context
    d["up_sideways_bars"] = diagnostic["up_sideways"]
    d["down_sideways_bars"] = diagnostic["down_sideways"]
    d["up_trend_bars"] = diagnostic["up_trend_bars"]
    d["down_trend_bars"] = diagnostic["down_trend_bars"]
    d["up_move_pct"] = diagnostic["up_move"]
    d["down_move_pct"] = diagnostic["down_move"]
    d["up_final_retracement_pct"] = diagnostic["up_retrace"]
    d["down_final_retracement_pct"] = diagnostic["down_retrace"]

    upper_boll_pass = (
        pd.Series(True, index=d.index)
        if not cfg.use_boll_filter
        else d["strong_zone"]
    )
    lower_boll_pass = (
        pd.Series(True, index=d.index)
        if not cfg.use_boll_filter
        else d["weak_zone"]
    )
    d["short_structure_pass"] = (
        d["upper_spike"]
        & d["current_spike_amplitude_pass"]
        & upper_boll_pass
        & d["up_context"]
    )
    d["long_structure_pass"] = (
        d["lower_spike"]
        & d["current_spike_amplitude_pass"]
        & lower_boll_pass
        & d["down_context"]
    )
    d["short_signal"] = d["short_structure_pass"]
    d["long_signal"] = d["long_structure_pass"]
    d["short_spike_type"] = np.select(
        [d["small_upper_spike"], d["upper_spike_bull"], d["upper_spike_bear"]],
        ["small_upper", "bull_upper", "bear_upper"],
        default="",
    )
    d["long_spike_type"] = np.select(
        [d["small_lower_spike"], d["lower_spike_bull"], d["lower_spike_bear"]],
        ["small_lower", "bull_lower", "bear_lower"],
        default="",
    )
    return d


def _directional_tick_round(
    raw_price: float,
    position_side: int,
    role: str,
    price_tick: float,
) -> float:
    if price_tick <= 0:
        raise ValueError("price_tick must be positive")
    scaled = raw_price / price_tick
    eps = 1e-10
    if role == "TP":
        ticks = (
            math.ceil(scaled - eps)
            if position_side == 1
            else math.floor(scaled + eps)
        )
    elif role == "SL":
        ticks = (
            math.floor(scaled + eps)
            if position_side == 1
            else math.ceil(scaled - eps)
        )
    else:
        raise ValueError(f"unsupported bracket role: {role}")
    return float(ticks * price_tick)


def build_protective_levels(
    position_side: int,
    average_entry_price: float,
    price_tick: float,
    cfg: Optional[R16SignalConfig] = None,
) -> dict[str, float]:
    """Build R16 TP/SL from the post-fill exchange average price."""

    config = cfg or R16SignalConfig()
    if position_side not in {-1, 1}:
        raise ValueError("position_side must be 1 (long) or -1 (short)")
    if average_entry_price <= 0:
        raise ValueError("average_entry_price must be positive")
    tp_ratio = config.take_profit_pct / 100.0
    sl_ratio = config.stop_loss_pct / 100.0
    if position_side == 1:
        raw_tp = average_entry_price * (1 + tp_ratio)
        raw_sl = average_entry_price * (1 - sl_ratio)
    else:
        raw_tp = average_entry_price * (1 - tp_ratio)
        raw_sl = average_entry_price * (1 + sl_ratio)
    return {
        "take_profit_raw": raw_tp,
        "stop_loss_raw": raw_sl,
        "take_profit": _directional_tick_round(
            raw_tp, position_side, "TP", price_tick
        ),
        "stop_loss": _directional_tick_round(
            raw_sl, position_side, "SL", price_tick
        ),
    }


class EthSpikeR16Strategy(StrategyBase):
    """Autobot strategy-layer adapter for the audited R16 signal rules.

    Required ``generate_signal`` keyword arguments:

    * ``bar_is_closed=True`` after the exchange confirms the 15m candle closed.
    * ``open_entries`` when a position exists; it is the number of entry lots in
      the current position cycle, not the exchange position quantity.

    The executor must support LONG/SHORT while already holding the same side as
    an addition.  It must also implement close-confirm-open reversal handling.
    """

    def __init__(
        self,
        cfg: Optional[R16SignalConfig] = None,
        *,
        require_bar_closed_flag: bool = True,
        enforce_frozen_parameters: bool = True,
    ) -> None:
        self.cfg = cfg or R16SignalConfig()
        if enforce_frozen_parameters:
            assert_frozen_r16(self.cfg)
        self.require_bar_closed_flag = require_bar_closed_flag
        self._last_processed_bar: Optional[pd.Timestamp] = None

    @property
    def name(self) -> str:
        return "eth_spike_r16_15m"

    @property
    def required_data_length(self) -> int:
        return 64

    def need_stop_check(self) -> bool:
        # R16 exits must be exchange-resident protective orders, not a candle-
        # close software check.
        return False

    def check_stop(
        self,
        current_position,
        entry_price,
        current_price,
        **kwargs,
    ) -> SignalResult:
        return SignalResult(
            SignalResult.NO_SIGNAL,
            "R16 TP/SL must be managed by exchange-resident protective orders",
        )

    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs,
    ) -> SignalResult:
        del entry_index  # R16 has no candle-close discretionary exit.
        try:
            _validate_ohlc(df)
            if len(df) < self.required_data_length:
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    f"R16 insufficient data: {len(df)}/{self.required_data_length}",
                )
            if current_position not in {-1, 0, 1}:
                raise ValueError(
                    f"current_position must be -1/0/1, got {current_position!r}"
                )
            if self.require_bar_closed_flag and kwargs.get("bar_is_closed") is not True:
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    "R16 blocked: bar_is_closed=True was not supplied",
                )

            bar_start = pd.Timestamp(df.index[-1])
            if self._last_processed_bar == bar_start:
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    f"R16 duplicate closed bar blocked: {bar_start.isoformat()}",
                )

            open_entries_raw = kwargs.get("open_entries")
            if current_position != 0 and open_entries_raw is None:
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    "R16 blocked: open_entries is required for an existing position",
                )
            open_entries = int(open_entries_raw or 0)
            if open_entries < 0:
                raise ValueError("open_entries cannot be negative")

            enriched = add_r16_signals(df, self.cfg)
            row = enriched.iloc[-1]
            raw_long = bool(row["long_signal"])
            raw_short = bool(row["short_signal"])
            self._last_processed_bar = bar_start

            if not raw_long and not raw_short:
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    self._reason(row, "NO_SIGNAL", current_position, open_entries),
                )

            # Exact V1.6 precedence: LONG wins if an exceptional candle matches
            # both directions.
            target_side = 1 if raw_long else -1
            if target_side == 1 and not self.cfg.allow_long:
                return SignalResult(SignalResult.NO_SIGNAL, "R16 LONG disabled")
            if target_side == -1 and not self.cfg.allow_short:
                return SignalResult(SignalResult.NO_SIGNAL, "R16 SHORT disabled")

            if (
                current_position == target_side
                and open_entries >= self.cfg.max_same_direction_entries
            ):
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    self._reason(
                        row,
                        "MAX_SAME_DIRECTION_ENTRIES",
                        current_position,
                        open_entries,
                    ),
                )

            if current_position == 0:
                action = "OPEN"
            elif current_position == target_side:
                action = f"ADD_{open_entries + 1}"
            else:
                action = "REVERSAL_CLOSE_CONFIRM_OPEN"

            signal = SignalResult.LONG if target_side == 1 else SignalResult.SHORT
            reason = self._reason(row, action, current_position, open_entries)
            log_trade(f"[R16] {reason}")
            return SignalResult(signal, reason)
        except Exception as exc:
            logger.error(f"[R16] strategy error: {exc}", exc_info=True)
            return SignalResult(
                SignalResult.NO_SIGNAL,
                f"R16 fail-closed strategy error: {exc}",
            )

    @staticmethod
    def _number(row: pd.Series, name: str) -> float:
        value = row.get(name, np.nan)
        return float(value) if pd.notna(value) else math.nan

    def _reason(
        self,
        row: pd.Series,
        action: str,
        current_position: int,
        open_entries: int,
    ) -> str:
        side = "LONG" if bool(row["long_signal"]) else (
            "SHORT" if bool(row["short_signal"]) else "NONE"
        )
        if side == "LONG":
            spike_type = row.get("long_spike_type", "")
            move = self._number(row, "down_move_pct")
            trend_bars = self._number(row, "down_trend_bars")
            sideways = self._number(row, "down_sideways_bars")
            retrace = self._number(row, "down_final_retracement_pct")
        else:
            spike_type = row.get("short_spike_type", "")
            move = self._number(row, "up_move_pct")
            trend_bars = self._number(row, "up_trend_bars")
            sideways = self._number(row, "up_sideways_bars")
            retrace = self._number(row, "up_final_retracement_pct")
        return (
            f"action={action} side={side} pos={current_position} entries={open_entries} "
            f"spike={spike_type or '-'} amp={self._number(row, 'current_spike_amplitude_pct'):.4f}% "
            f"move={move:.4f}% trend_bars={trend_bars:g} sideways={sideways:g} "
            f"retrace={retrace:.4f}% close={self._number(row, 'close'):.4f} "
            f"basis={self._number(row, 'basis'):.4f} upper={self._number(row, 'upper'):.4f} "
            f"lower={self._number(row, 'lower'):.4f}"
        )


__all__ = [
    "EthSpikeR16Strategy",
    "R16SignalConfig",
    "add_r16_signals",
    "assert_frozen_r16",
    "build_protective_levels",
]
