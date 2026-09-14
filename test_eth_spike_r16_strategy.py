"""Offline tests for the R16 live strategy adapter.

Run from the repository root:

    python -m unittest -v deliverables/test_eth_spike_r16_strategy.py

The full parity test runs when ``backtest_eth_spike_v1_6.py`` and the four
2026-04..07 one-minute CSV files are present.  The framework is stubbed because
the uploaded reference contained only the strategy class, not autobot itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


class _SignalResult:
    NO_SIGNAL = 0
    LONG = 1
    SHORT = -1
    CLOSE_LONG = 2
    CLOSE_SHORT = -2

    def __init__(self, signal, reason):
        self.signal = signal
        self.reason = reason


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _install_autobot_stubs() -> None:
    autobot = types.ModuleType("autobot")
    core = types.ModuleType("autobot.core")
    strategy_base = types.ModuleType("autobot.core.strategy_base")
    utils = types.ModuleType("autobot.utils")
    logger_module = types.ModuleType("autobot.utils.logger")
    strategy_base.StrategyBase = object
    strategy_base.SignalResult = _SignalResult
    logger_module.logger = _Logger()
    logger_module.log_trade = lambda *args, **kwargs: None
    sys.modules.update(
        {
            "autobot": autobot,
            "autobot.core": core,
            "autobot.core.strategy_base": strategy_base,
            "autobot.utils": utils,
            "autobot.utils.logger": logger_module,
        }
    )


_install_autobot_stubs()
from deliverables.eth_spike_r16_strategy import (  # noqa: E402
    EthSpikeR16Strategy,
    R16SignalConfig,
    add_r16_signals,
    assert_frozen_r16,
    build_protective_levels,
)


class R16UnitTests(unittest.TestCase):
    def test_frozen_config(self):
        assert_frozen_r16(R16SignalConfig())
        with self.assertRaises(ValueError):
            assert_frozen_r16(R16SignalConfig(bb_length=20))

    def test_bracket_levels_and_tick_rounding(self):
        long_levels = build_protective_levels(1, 2_000.0, 0.01)
        self.assertEqual(long_levels["take_profit"], 2_012.0)
        self.assertEqual(long_levels["stop_loss"], 1_982.0)
        short_levels = build_protective_levels(-1, 2_000.0, 0.01)
        self.assertEqual(short_levels["take_profit"], 1_988.0)
        self.assertEqual(short_levels["stop_loss"], 2_018.0)

    def test_partial_bar_is_fail_closed(self):
        idx = pd.date_range("2026-01-01", periods=64, freq="15min", tz="UTC")
        close = np.linspace(2_000, 2_100, len(idx))
        frame = pd.DataFrame(
            {
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
            },
            index=idx,
        )
        result = EthSpikeR16Strategy().generate_signal(frame, 0)
        self.assertEqual(result.signal, _SignalResult.NO_SIGNAL)
        self.assertIn("bar_is_closed", result.reason)


class R16ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference_path = ROOT / "backtest_eth_spike_v1_6.py"
        cls.data_paths = [
            ROOT / "upload" / "eth_usdt_swap_1m_20260401_to_20260501(2).csv",
            ROOT / "upload" / "eth_usdt_swap_1m_20260501_to_20260601(2).csv",
            ROOT / "upload" / "eth_usdt_swap_1m_20260601_to_20260701(2).csv",
            ROOT / "upload" / "eth_usdt_swap_1m_20260701_to_20260801(2).csv",
        ]

    def test_signal_parity_with_audited_reference(self):
        if not self.reference_path.exists() or not all(
            path.exists() for path in self.data_paths
        ):
            self.skipTest("audited reference or 2026-04..07 CSV files not present")

        spec = importlib.util.spec_from_file_location("r16_reference", self.reference_path)
        reference = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = reference
        spec.loader.exec_module(reference)

        frames = [pd.read_csv(path) for path in self.data_paths]
        raw = pd.concat(frames, ignore_index=True).sort_values("timestamp")
        raw = raw.drop_duplicates("timestamp", keep="last")
        raw.index = pd.DatetimeIndex(
            pd.to_datetime(raw["timestamp"], unit="ms", utc=True).dt.tz_convert(
                "Asia/Shanghai"
            )
        )
        bars = reference.resample_to_15m(raw)

        reference_cfg = reference.Config(
            bb_length=18,
            bb_mult=2.5,
            min_pre_move_bars=3,
            max_pre_lookback_bars=4,
            min_pre_move_pct=0.80,
            max_sideways=3,
            min_current_spike_amplitude_pct=0.25,
            max_last_bar_retracement_pct=0.40,
            take_profit_pct=0.60,
            stop_loss_pct=0.90,
            max_same_direction_entries=3,
        )
        expected = reference.add_pine_signals(bars, reference_cfg)
        actual = add_r16_signals(bars, R16SignalConfig())

        pd.testing.assert_series_equal(
            actual["long_signal"], expected["long_signal"], check_names=False
        )
        pd.testing.assert_series_equal(
            actual["short_signal"], expected["short_signal"], check_names=False
        )
        self.assertEqual(int(actual["long_signal"].sum()), int(expected["long_signal"].sum()))
        self.assertEqual(int(actual["short_signal"].sum()), int(expected["short_signal"].sum()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
