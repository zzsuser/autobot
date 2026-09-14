"""ETH 15-minute four-shape strategy — frozen R2 live adapter.

This file is standalone and follows the interface of the user's existing
``eth_spike_r16_strategy.py``.  R2 is the frozen audit anchor:

    F1 fixed + F2 MID + old strict F3 + F4 balanced

Important execution distinction from R16
-----------------------------------------
F1/F2/F4 are not immediate market signals.  A closed 15-minute candidate bar
creates an exchange-resident stop-entry intent.  The executor should call
``new_pending_entry_intents`` and monitor each intent with one-minute bars via
``evaluate_pending_intent``.  ``generate_signal`` supports a less exact
15-minute compatibility mode only when FOURSHAPE_ENTRY_API is explicitly set
to CONFIRMED_BAR_COMPAT.

Position sizing is read from environment variables.  With the requested
defaults, 3% means 3% account margin at 100x, i.e. approximately 3x account
equity notional per entry.  This is three times the tested C10_M2 per-entry
notional and is not covered by the R2/R3 return audit.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Optional

import numpy as np
import pandas as pd

from autobot.core.strategy_base import SignalResult, StrategyBase
from autobot.utils.logger import log_trade, logger


VARIANT = "R2"
TICK_FLOOR = 0.01
TP_PCT = 0.60
SL_PCT = 0.90


@dataclass(frozen=True)
class ShapeConfig:
    shape: str
    background: str
    wick: str
    side: int
    trend_n_min: int
    trend_n_max: int
    sideways_max: int
    max_total_context: int
    pre_move_pct: float
    retrace_pct: float
    body_ratio_max: float
    wick_body_min: float
    wick_other_min: float
    amplitude_pct_min: float
    amplitude_atr_min: float
    close_mode: str
    extreme_lookback: int
    boll_length: int
    boll_mult: float
    signal_zone: bool
    outer_touch: bool
    close_reentry: bool
    entry_mode: str
    confirm_expiry_bars: int
    confirm_buffer_atr: float
    confirm_max_distance_pct: float
    confirm_invalidation: str
    custom_structure: str = ""
    engulf_min_body_atr: float = 0.0


def frozen_shape_configs() -> dict[str, ShapeConfig]:
    """Return the exact R2 signal registry used in the completed audit."""

    return {
        "F1": ShapeConfig(
            "F1", "up", "upper", -1, 3, 4, 1, 8, 0.80, 0.40,
            0.20, 2.0, 1.0, 0.30, 0.0, "ANY", 4,
            20, 2.50, True, False, True,
            "BREAK_BODY_EDGE", 3, 0.10, 0.15,
            "OPPOSITE_EXTREME_OR_TIMEOUT",
        ),
        "F2": ShapeConfig(
            "F2", "up", "lower", -1, 2, 5, 3, 8, 0.40, 0.80,
            0.50, 1.0, 0.5, 0.0, 1.0, "MID", 0,
            18, 2.50, True, False, True,
            "BREAK_SIGNAL_EXTREME", 2, 0.0, 0.25,
            "OPPOSITE_EXTREME_OR_TIMEOUT",
        ),
        "F3": ShapeConfig(
            "F3", "down", "upper", 1, 2, 5, 3, 8, 0.40, 0.80,
            0.50, 1.0, 0.5, 0.0, 1.25, "LOW35", 0,
            18, 2.50, False, False, False,
            "CLOSE_BEYOND_BODY_EDGE", 2, 0.0, 0.25,
            "OPPOSITE_EXTREME_OR_TIMEOUT",
        ),
        "F4": ShapeConfig(
            "F4", "down", "lower", 1, 2, 5, 3, 8, 0.40, 0.80,
            0.50, 1.0, 0.5, 0.0, 0.75, "LOW35", 0,
            20, 2.75, True, False, True,
            "BREAK_BODY_EDGE", 3, 0.0, 0.35, "NONE",
        ),
    }


@dataclass(frozen=True)
class RuntimePositionConfig:
    mode: str
    value: float
    leverage: float
    max_same_direction_entries: int
    max_total_notional_multiple: float
    contract_value: float
    contract_step: float
    min_contracts: float
    entry_api: str


@dataclass(frozen=True)
class OrderSize:
    margin_cash: float
    notional: float
    contracts: float
    base_quantity: float


@dataclass(frozen=True)
class PendingEntryIntent:
    shape: str
    side: int
    signal_bar_start: pd.Timestamp
    created_at: pd.Timestamp
    expires_at: pd.Timestamp
    trigger_price: float
    invalidation_price: Optional[float]
    signal_close: float
    max_distance_pct: float


@dataclass(frozen=True)
class PendingEvaluation:
    status: str
    fill_price: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class NextOpenEntryIntent:
    """A close-confirmed entry that may execute only at the next 15m open."""

    shape: str
    side: int
    signal_bar_start: pd.Timestamp
    confirmation_bar_start: pd.Timestamp
    execute_at: pd.Timestamp
    signal_close: float
    max_distance_pct: float


def _env_float(name: str, default: float, *, positive: bool = True) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'valid'}")
    return value


def load_position_config_from_env() -> RuntimePositionConfig:
    mode = os.getenv("FOURSHAPE_POSITION_MODE", "MARGIN_PCT").strip().upper()
    if mode not in {"MARGIN_PCT", "NOTIONAL_PCT", "FIXED_MARGIN_USDT"}:
        raise ValueError(f"unsupported FOURSHAPE_POSITION_MODE={mode!r}")
    entry_api = os.getenv("FOURSHAPE_ENTRY_API", "INTENTS").strip().upper()
    if entry_api not in {"INTENTS", "CONFIRMED_BAR_COMPAT"}:
        raise ValueError(f"unsupported FOURSHAPE_ENTRY_API={entry_api!r}")
    max_entries = int(os.getenv("FOURSHAPE_MAX_ENTRIES", "2"))
    if max_entries < 1:
        raise ValueError("FOURSHAPE_MAX_ENTRIES must be >= 1")
    return RuntimePositionConfig(
        mode=mode,
        value=_env_float("FOURSHAPE_POSITION_VALUE", 3.0),
        leverage=_env_float("FOURSHAPE_LEVERAGE", 100.0),
        max_same_direction_entries=max_entries,
        max_total_notional_multiple=_env_float(
            "FOURSHAPE_MAX_TOTAL_NOTIONAL_MULTIPLE", 6.0
        ),
        contract_value=_env_float("FOURSHAPE_CONTRACT_VALUE", 0.10),
        contract_step=_env_float("FOURSHAPE_CONTRACT_STEP", 0.01),
        min_contracts=_env_float("FOURSHAPE_MIN_CONTRACTS", 0.01),
        entry_api=entry_api,
    )


def calculate_order_size(
    equity: float,
    order_price: float,
    current_notional: float = 0.0,
    cfg: Optional[RuntimePositionConfig] = None,
) -> OrderSize:
    """Convert an environment sizing policy to rounded contract quantity."""

    c = cfg or load_position_config_from_env()
    if equity <= 0 or order_price <= 0 or current_notional < 0:
        raise ValueError("equity/order_price must be positive; current_notional >= 0")
    if c.mode == "MARGIN_PCT":
        requested_margin = equity * c.value / 100.0
        requested_notional = requested_margin * c.leverage
    elif c.mode == "NOTIONAL_PCT":
        requested_notional = equity * c.value / 100.0
        requested_margin = requested_notional / c.leverage
    else:
        requested_margin = c.value
        requested_notional = requested_margin * c.leverage
    total_cap = equity * c.max_total_notional_multiple
    notional = min(requested_notional, max(0.0, total_cap - current_notional))
    raw_contracts = notional / (order_price * c.contract_value)
    contracts = math.floor(raw_contracts / c.contract_step) * c.contract_step
    if contracts + 1e-12 < c.min_contracts:
        # 名义仓位不足以凑 min_contracts 张时不跳过：只要最小可开张数的名义仍在
        # 总名义上限内、且其所需保证金不超过可用权益，就按最小可开张数兜底。
        # （例：权益52 USDT，目标名义156，ETH每张≈245名义 → 理论0.6张 → 开1张）
        floor_notional = c.min_contracts * c.contract_value * order_price
        cap_remain = max(0.0, total_cap - current_notional)
        if floor_notional <= cap_remain + 1e-9 and (floor_notional / c.leverage) <= equity + 1e-9:
            contracts = c.min_contracts
        else:
            return OrderSize(0.0, 0.0, 0.0, 0.0)
    actual_notional = contracts * c.contract_value * order_price
    return OrderSize(
        margin_cash=actual_notional / c.leverage,
        notional=actual_notional,
        contracts=contracts,
        base_quantity=contracts * c.contract_value,
    )


def _validate_ohlc(df: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close"}
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"missing OHLC columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("df.index must be a DatetimeIndex of 15m bar starts")
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("15m timestamps must be unique and ascending")
    if df[list(required)].tail(128).isna().any(axis=None):
        raise ValueError("NaN detected in recent OHLC bars")


def _features(bars: pd.DataFrame) -> pd.DataFrame:
    _validate_ohlc(bars)
    x = bars.copy()
    x["range"] = x.high - x.low
    x["body"] = (x.close - x.open).abs()
    x["upper_wick"] = x.high - x[["open", "close"]].max(axis=1)
    x["lower_wick"] = x[["open", "close"]].min(axis=1) - x.low
    x["valid_bar"] = x["range"] > 0
    denom_body = x.body.clip(lower=TICK_FLOOR)
    x["body_ratio"] = np.where(x.valid_bar, x.body / x["range"], np.nan)
    x["close_location"] = np.where(
        x.valid_bar, (x.close - x.low) / x["range"], np.nan
    )
    x["upper_body_ratio"] = x.upper_wick / denom_body
    x["lower_body_ratio"] = x.lower_wick / denom_body
    x["upper_lower_ratio"] = x.upper_wick / x.lower_wick.clip(lower=TICK_FLOOR)
    x["lower_upper_ratio"] = x.lower_wick / x.upper_wick.clip(lower=TICK_FLOOR)
    x["amplitude_pct_low"] = np.where(
        x.low > 0, x["range"] / x.low * 100.0, np.nan
    )
    prev_close = x.close.shift(1)
    tr = pd.concat(
        [x.high - x.low, (x.high - prev_close).abs(), (x.low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    x["atr14_prev"] = tr.rolling(14, min_periods=14).mean().shift(1)
    x["range_atr"] = x["range"] / x.atr14_prev
    for n in {c.extreme_lookback for c in frozen_shape_configs().values()} - {0}:
        x[f"new_high_{n}"] = x.high > x.high.shift(1).rolling(n, min_periods=n).max()
        x[f"new_low_{n}"] = x.low < x.low.shift(1).rolling(n, min_periods=n).min()
    return x


def _boll(x: pd.DataFrame, length: int, mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    key = f"{length}_{mult:.2f}"
    b, u, l = f"basis_{key}", f"upper_{key}", f"lower_{key}"
    if b not in x:
        x[b] = x.close.rolling(length, min_periods=length).mean()
        dev = x.close.rolling(length, min_periods=length).std(ddof=0)
        x[u] = x[b] + mult * dev
        x[l] = x[b] - mult * dev
    return x[b], x[u], x[l]


def _context_mask(x: pd.DataFrame, cfg: ShapeConfig) -> pd.Series:
    found = np.zeros(len(x), dtype=bool)
    cl, hi, lo = x.close.to_numpy(float), x.high.to_numpy(float), x.low.to_numpy(float)
    for i in range(len(x)):
        for s in range(cfg.sideways_max + 1):
            for n in range(cfg.trend_n_min, cfg.trend_n_max + 1):
                if n + s > cfg.max_total_context:
                    continue
                newest = i - (s + 1)
                base_i = i - (s + n + 1)
                if base_i < 0 or newest < 0 or cl[base_i] <= 0:
                    continue
                base, end = cl[base_i], cl[newest]
                move = (end - base) / base * 100.0 if cfg.background == "up" else (base - end) / base * 100.0
                if i < 2:
                    continue
                retrace = max(cl[i - 2] - cl[i - 1], 0.0) / base * 100.0 if cfg.background == "up" else max(cl[i - 1] - cl[i - 2], 0.0) / base * 100.0
                if move < cfg.pre_move_pct or retrace > cfg.retrace_pct:
                    continue
                trend_slice = slice(i - (s + n), newest + 1)
                trend_high, trend_low = float(np.max(hi[trend_slice])), float(np.min(lo[trend_slice]))
                sideways_ok = True
                for off in range(1, s + 1):
                    j = i - off
                    if cfg.background == "up":
                        sideways_ok &= hi[j] <= trend_high
                    else:
                        sideways_ok &= lo[j] >= trend_low
                if sideways_ok:
                    found[i] = True
                    break
            if found[i]:
                break
    return pd.Series(found, index=x.index)


def _morphology_mask(x: pd.DataFrame, cfg: ShapeConfig) -> pd.Series:
    target_body = x.upper_body_ratio if cfg.wick == "upper" else x.lower_body_ratio
    target_other = x.upper_lower_ratio if cfg.wick == "upper" else x.lower_upper_ratio
    m = (
        x.valid_bar
        & (x.body_ratio <= cfg.body_ratio_max)
        & (target_body >= cfg.wick_body_min)
        & (target_other >= cfg.wick_other_min)
        & (x.amplitude_pct_low >= cfg.amplitude_pct_min)
    )
    if cfg.amplitude_atr_min > 0:
        m &= x.range_atr >= cfg.amplitude_atr_min
    if cfg.close_mode == "LOW35":
        m &= x.close_location <= 0.35
    elif cfg.close_mode == "MID":
        m &= x.close_location.between(0.35, 0.65)
    elif cfg.close_mode != "ANY":
        raise ValueError(f"unsupported close mode {cfg.close_mode}")
    if cfg.extreme_lookback:
        name = f"new_high_{cfg.extreme_lookback}" if cfg.wick == "upper" else f"new_low_{cfg.extreme_lookback}"
        m &= x[name]
    if cfg.signal_zone or cfg.outer_touch or cfg.close_reentry:
        basis, upper, lower = _boll(x, cfg.boll_length, cfg.boll_mult)
        if cfg.signal_zone:
            m &= x.close >= basis if cfg.side == -1 else x.close <= basis
        if cfg.close_reentry:
            m &= x.close <= upper if cfg.side == -1 else x.close >= lower
        if cfg.outer_touch:
            m &= x.high >= upper if cfg.wick == "upper" else x.low <= lower
    return m.fillna(False)


def raw_shape_candidates(bars: pd.DataFrame) -> pd.DataFrame:
    x = _features(bars)
    for shape, cfg in frozen_shape_configs().items():
        x[f"{shape}_candidate"] = _context_mask(x, cfg) & _morphology_mask(x, cfg)
    return x


def _standard_confirmation(x: pd.DataFrame, i: int, cfg: ShapeConfig) -> tuple[Optional[int], Optional[float]]:
    bar = x.iloc[i]
    body_low, body_high = min(bar.open, bar.close), max(bar.open, bar.close)
    atr = float(bar.atr14_prev) if np.isfinite(bar.atr14_prev) else 0.0
    buffer_px = cfg.confirm_buffer_atr * atr
    if cfg.entry_mode == "BREAK_BODY_EDGE":
        trigger = body_low - buffer_px if cfg.side == -1 else body_high + buffer_px
    elif cfg.entry_mode == "BREAK_SIGNAL_EXTREME":
        trigger = bar.low - buffer_px if cfg.side == -1 else bar.high + buffer_px
    else:
        trigger = math.nan
    for k in range(1, cfg.confirm_expiry_bars + 1):
        j = i + k
        if j >= len(x):
            return None, None
        q = x.iloc[j]
        invalid = False
        if cfg.confirm_invalidation != "NONE":
            invalid = q.high >= bar.high if cfg.side == -1 else q.low <= bar.low
        if invalid:
            return None, None
        if cfg.entry_mode in {"BREAK_BODY_EDGE", "BREAK_SIGNAL_EXTREME"}:
            hit = q.low <= trigger if cfg.side == -1 else q.high >= trigger
            if hit:
                raw = min(trigger, q.open) if cfg.side == -1 else max(trigger, q.open)
                distance = abs(raw - bar.close) / bar.close * 100.0
                return (j, float(raw)) if distance <= cfg.confirm_max_distance_pct else (None, None)
        elif cfg.entry_mode == "CLOSE_BEYOND_BODY_EDGE":
            hit = q.close < body_low - buffer_px if cfg.side == -1 else q.close > body_high + buffer_px
            if hit:
                # The audit enters at the next 15m open, not at this close.
                # Distance is therefore checked by evaluate_next_open_intent.
                return j, float(q.close)
        else:
            raise ValueError(cfg.entry_mode)
    return None, None


def _custom_f3_confirmation(x: pd.DataFrame, i: int, cfg: ShapeConfig) -> tuple[Optional[int], Optional[float]]:
    bar = x.iloc[i]
    body_low, body_high = min(bar.open, bar.close), max(bar.open, bar.close)
    atr = float(bar.atr14_prev) if np.isfinite(bar.atr14_prev) else 0.0
    for k in range(1, cfg.confirm_expiry_bars + 1):
        j = i + k
        if j >= len(x):
            return None, None
        q = x.iloc[j]
        if q.low < bar.low:
            return None, None
        body_atr = abs(q.close - q.open) / atr if atr > 0 else 0.0
        engulf = q.close > q.open and q.open <= body_low and q.close >= body_high
        if engulf and q.close > body_high and body_atr >= cfg.engulf_min_body_atr:
            # The audit enters at the next 15m open, not at this close.
            return j, float(q.close)
    return None, None


def add_fourshape_signals(bars: pd.DataFrame) -> pd.DataFrame:
    """Add raw candidates and conservative 15m compatibility confirmations."""

    x = raw_shape_candidates(bars)
    n = len(x)
    x["long_signal"] = False
    x["short_signal"] = False
    x["signal_shape"] = ""
    x["entry_reference_price"] = np.nan
    for shape in ("F1", "F2", "F3", "F4"):
        cfg = frozen_shape_configs()[shape]
        indices = np.flatnonzero(x[f"{shape}_candidate"].to_numpy(bool))
        for i in indices:
            if cfg.custom_structure:
                j, price = _custom_f3_confirmation(x, int(i), cfg)
            else:
                j, price = _standard_confirmation(x, int(i), cfg)
            if j is None:
                continue
            signal_col = "long_signal" if cfg.side == 1 else "short_signal"
            # Research account de-duplicates same-time/same-side events by F1-F4 order.
            if not bool(x.iloc[j][signal_col]):
                x.iat[j, x.columns.get_loc(signal_col)] = True
                x.iat[j, x.columns.get_loc("signal_shape")] = shape
                x.iat[j, x.columns.get_loc("entry_reference_price")] = price
    return x


def new_pending_entry_intents(bars: pd.DataFrame) -> list[PendingEntryIntent]:
    """Return new stop-entry intents created by the latest closed 15m bar."""

    x = raw_shape_candidates(bars)
    i = len(x) - 1
    bar = x.iloc[i]
    signal_start = pd.Timestamp(x.index[i])
    created = signal_start + pd.Timedelta(minutes=15)
    intents: list[PendingEntryIntent] = []
    for shape in ("F1", "F2", "F3", "F4"):
        cfg = frozen_shape_configs()[shape]
        if not bool(bar[f"{shape}_candidate"]):
            continue
        if cfg.entry_mode not in {"BREAK_BODY_EDGE", "BREAK_SIGNAL_EXTREME"}:
            continue
        atr = float(bar.atr14_prev) if np.isfinite(bar.atr14_prev) else 0.0
        buffer_px = cfg.confirm_buffer_atr * atr
        body_low, body_high = min(bar.open, bar.close), max(bar.open, bar.close)
        if cfg.entry_mode == "BREAK_BODY_EDGE":
            trigger = body_low - buffer_px if cfg.side == -1 else body_high + buffer_px
        else:
            trigger = bar.low - buffer_px if cfg.side == -1 else bar.high + buffer_px
        invalidation = None
        if cfg.confirm_invalidation != "NONE":
            invalidation = float(bar.high if cfg.side == -1 else bar.low)
        intents.append(PendingEntryIntent(
            shape=shape,
            side=cfg.side,
            signal_bar_start=signal_start,
            created_at=created,
            expires_at=created + pd.Timedelta(minutes=15 * cfg.confirm_expiry_bars),
            trigger_price=float(trigger),
            invalidation_price=invalidation,
            signal_close=float(bar.close),
            max_distance_pct=cfg.confirm_max_distance_pct,
        ))
    return intents


def new_next_open_entry_intents(bars: pd.DataFrame) -> list[NextOpenEntryIntent]:
    """Return close-confirmed intents that must be checked at the next open.

    R2 uses this path for F3.  Stop-confirmed F1/F2/F4 are handled by
    ``new_pending_entry_intents`` instead.  Multiple F3 candidates confirming
    on the same bar are de-duplicated to one same-side entry intent.
    """

    x = raw_shape_candidates(bars)
    latest = len(x) - 1
    cfg = frozen_shape_configs()["F3"]
    for i in np.flatnonzero(x["F3_candidate"].to_numpy(bool)):
        if i >= latest:
            continue
        if cfg.custom_structure:
            j, _ = _custom_f3_confirmation(x, int(i), cfg)
        else:
            j, _ = _standard_confirmation(x, int(i), cfg)
        if j == latest:
            confirmation_start = pd.Timestamp(x.index[latest])
            return [NextOpenEntryIntent(
                shape="F3",
                side=cfg.side,
                signal_bar_start=pd.Timestamp(x.index[i]),
                confirmation_bar_start=confirmation_start,
                execute_at=confirmation_start + pd.Timedelta(minutes=15),
                signal_close=float(x.iloc[i].close),
                max_distance_pct=cfg.confirm_max_distance_pct,
            )]
    return []


def evaluate_next_open_intent(
    intent: NextOpenEntryIntent,
    bar_start: pd.Timestamp,
    open_price: float,
) -> PendingEvaluation:
    """Validate the real next-open fill and its distance from signal close."""

    when = pd.Timestamp(bar_start)
    if when < intent.execute_at:
        return PendingEvaluation("WAITING")
    if when > intent.execute_at:
        return PendingEvaluation("EXPIRED", reason="next 15m open was missed")
    if open_price <= 0 or not math.isfinite(open_price):
        return PendingEvaluation("INVALID_PRICE", reason="open_price must be positive")
    distance = abs(open_price - intent.signal_close) / intent.signal_close * 100.0
    if distance > intent.max_distance_pct:
        return PendingEvaluation("TOO_FAR", reason=f"distance={distance:.4f}%")
    return PendingEvaluation("TRIGGERED", float(open_price), f"distance={distance:.4f}%")


def evaluate_pending_intent(
    intent: PendingEntryIntent,
    minute_start: pd.Timestamp,
    minute_open: float,
    minute_high: float,
    minute_low: float,
) -> PendingEvaluation:
    """Apply the audited one-minute invalidation-first confirmation rule."""

    when = pd.Timestamp(minute_start)
    if when < intent.created_at:
        return PendingEvaluation("WAITING")
    if when >= intent.expires_at:
        return PendingEvaluation("EXPIRED", reason="confirmation timeout")
    invalid = False
    if intent.invalidation_price is not None:
        invalid = minute_high >= intent.invalidation_price if intent.side == -1 else minute_low <= intent.invalidation_price
    if invalid:
        return PendingEvaluation("INVALIDATED", reason="opposite pin extreme touched first")
    hit = minute_low <= intent.trigger_price if intent.side == -1 else minute_high >= intent.trigger_price
    if not hit:
        return PendingEvaluation("WAITING")
    fill = min(intent.trigger_price, minute_open) if intent.side == -1 else max(intent.trigger_price, minute_open)
    distance = abs(fill - intent.signal_close) / intent.signal_close * 100.0
    if distance > intent.max_distance_pct:
        return PendingEvaluation("TOO_FAR", reason=f"distance={distance:.4f}%")
    return PendingEvaluation("TRIGGERED", float(fill), f"distance={distance:.4f}%")


def _directional_tick_round(raw: float, side: int, role: str, tick: float) -> float:
    if tick <= 0:
        raise ValueError("price_tick must be positive")
    scaled = raw / tick
    if role == "TP":
        ticks = math.ceil(scaled - 1e-10) if side == 1 else math.floor(scaled + 1e-10)
    elif role == "SL":
        ticks = math.floor(scaled + 1e-10) if side == 1 else math.ceil(scaled - 1e-10)
    else:
        raise ValueError(role)
    return float(ticks * tick)


def build_protective_levels(
    position_side: int,
    average_entry_price: float,
    price_tick: float,
) -> dict[str, float]:
    """Build exchange-resident TP/SL after every fill from exchange average."""

    if position_side not in {-1, 1} or average_entry_price <= 0:
        raise ValueError("invalid position side or average entry price")
    if position_side == 1:
        raw_tp = average_entry_price * (1 + TP_PCT / 100.0)
        raw_sl = average_entry_price * (1 - SL_PCT / 100.0)
    else:
        raw_tp = average_entry_price * (1 - TP_PCT / 100.0)
        raw_sl = average_entry_price * (1 + SL_PCT / 100.0)
    return {
        "take_profit_raw": raw_tp,
        "stop_loss_raw": raw_sl,
        "take_profit": _directional_tick_round(raw_tp, position_side, "TP", price_tick),
        "stop_loss": _directional_tick_round(raw_sl, position_side, "SL", price_tick),
    }


class EthSpikeR2Strategy(StrategyBase):
    """R2 signal adapter; exact stop confirmations use the intent API."""

    def __init__(self, *, require_bar_closed_flag: bool = True) -> None:
        self.position_cfg = load_position_config_from_env()
        self.require_bar_closed_flag = require_bar_closed_flag
        self._last_processed_bar: Optional[pd.Timestamp] = None

    @property
    def name(self) -> str:
        return "eth_spike_fourshape_r2_15m"

    @property
    def required_data_length(self) -> int:
        return 128

    def need_stop_check(self) -> bool:
        return False

    def check_stop(self, current_position, entry_price, current_price, **kwargs) -> SignalResult:
        del current_position, entry_price, current_price, kwargs
        return SignalResult(
            SignalResult.NO_SIGNAL,
            "R2 TP/SL must be exchange-resident protective orders",
        )

    def pending_entry_intents(self, df: pd.DataFrame) -> list[PendingEntryIntent]:
        return new_pending_entry_intents(df)

    def next_open_entry_intents(self, df: pd.DataFrame) -> list[NextOpenEntryIntent]:
        return new_next_open_entry_intents(df)

    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs,
    ) -> SignalResult:
        del entry_index
        try:
            _validate_ohlc(df)
            if len(df) < self.required_data_length:
                return SignalResult(SignalResult.NO_SIGNAL, f"R2 insufficient data: {len(df)}/128")
            if current_position not in {-1, 0, 1}:
                raise ValueError("current_position must be -1/0/1")
            if self.require_bar_closed_flag and kwargs.get("bar_is_closed") is not True:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 blocked: closed-bar flag missing")
            bar_start = pd.Timestamp(df.index[-1])
            if self._last_processed_bar == bar_start:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 duplicate closed bar blocked")
            self._last_processed_bar = bar_start

            open_entries_raw = kwargs.get("open_entries")
            if current_position != 0 and open_entries_raw is None:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 blocked: open_entries required")
            open_entries = int(open_entries_raw or 0)

            x = add_fourshape_signals(df)
            row = x.iloc[-1]
            raw_long, raw_short = bool(row.long_signal), bool(row.short_signal)
            if self.position_cfg.entry_api == "INTENTS":
                # Exact delayed execution cannot be represented by a bare
                # LONG/SHORT result.  Fail closed and expose both intent APIs.
                pending = new_pending_entry_intents(df)
                next_open = new_next_open_entry_intents(df)
                stop_shapes = ",".join(i.shape for i in pending) or "-"
                open_shapes = ",".join(i.shape for i in next_open) or "-"
                return SignalResult(
                    SignalResult.NO_SIGNAL,
                    f"R2 intent mode; stop_intents={stop_shapes}; next_open_intents={open_shapes}",
                )
            if raw_long and raw_short:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 simultaneous long/short conflict rejected")
            if not raw_long and not raw_short:
                pending = new_pending_entry_intents(df)
                shapes = ",".join(i.shape for i in pending) or "-"
                return SignalResult(SignalResult.NO_SIGNAL, f"R2 no legacy signal; new_intents={shapes}")
            side = 1 if raw_long else -1
            if current_position == -side:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 opposite signal rejected; no reversal")
            if current_position == side and open_entries >= self.position_cfg.max_same_direction_entries:
                return SignalResult(SignalResult.NO_SIGNAL, "R2 maximum same-direction entries reached")
            signal = SignalResult.LONG if side == 1 else SignalResult.SHORT
            reason = (
                f"variant=R2 shape={row.signal_shape} side={'LONG' if side == 1 else 'SHORT'} "
                f"reference={float(row.entry_reference_price):.4f} entry_api={self.position_cfg.entry_api}"
            )
            log_trade(f"[R2] {reason}")
            return SignalResult(signal, reason)
        except Exception as exc:
            logger.error(f"[R2] strategy error: {exc}", exc_info=True)
            return SignalResult(SignalResult.NO_SIGNAL, f"R2 fail-closed: {exc}")


__all__ = [
    "EthSpikeR2Strategy",
    "NextOpenEntryIntent",
    "OrderSize",
    "PendingEntryIntent",
    "PendingEvaluation",
    "RuntimePositionConfig",
    "ShapeConfig",
    "add_fourshape_signals",
    "build_protective_levels",
    "calculate_order_size",
    "evaluate_pending_intent",
    "evaluate_next_open_intent",
    "frozen_shape_configs",
    "load_position_config_from_env",
    "new_pending_entry_intents",
    "new_next_open_entry_intents",
    "raw_shape_candidates",
]
