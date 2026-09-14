#!/usr/bin/env python3
"""Local parity and safety checks for the R2/R3 delivery modules."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import types

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN = ROOT / "eth_fourshape_sol_v2/outputs/SOL_FOURSHAPE_R3_20260903T030148ZR1"


def install_autobot_stubs() -> None:
    autobot = types.ModuleType("autobot")
    core = types.ModuleType("autobot.core")
    strategy_base = types.ModuleType("autobot.core.strategy_base")
    utils = types.ModuleType("autobot.utils")
    logger_mod = types.ModuleType("autobot.utils.logger")

    class SignalResult:
        NO_SIGNAL = 0
        LONG = 1
        SHORT = -1

        def __init__(self, signal, reason):
            self.signal = signal
            self.reason = reason

    class StrategyBase:
        pass

    class Logger:
        def error(self, *args, **kwargs):
            pass

    strategy_base.SignalResult = SignalResult
    strategy_base.StrategyBase = StrategyBase
    logger_mod.log_trade = lambda *args, **kwargs: None
    logger_mod.logger = Logger()
    sys.modules.update({
        "autobot": autobot,
        "autobot.core": core,
        "autobot.core.strategy_base": strategy_base,
        "autobot.utils": utils,
        "autobot.utils.logger": logger_mod,
    })


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    install_autobot_stubs()
    r2 = load("delivery_r2", HERE / "eth_spike_r2_strategy.py")
    r3 = load("delivery_r3", HERE / "eth_spike_r3_strategy.py")

    sys.path.insert(0, str(ROOT / "eth_fourshape_sol_v2"))
    import engine as e

    ledger = e.load_15m()
    bars = ledger[["open", "high", "low", "close"]].copy()
    frozen = json.loads((RUN / "P00_DATA_AND_FREEZE/frozen_protocol.json").read_text())
    expected = {
        "r2": {
            "F1": frozen["F1_FIXED"],
            "F2": frozen["F2_MID_CONTROL"],
            "F3": frozen["F3_OLD_DIAGNOSTIC"],
            "F4": frozen["F4_BALANCED_PRIMARY"],
        },
        "r3": {
            "F1": frozen["F1_FIXED"] | {
                "entry_mode": "BREAK_SIGNAL_EXTREME",
                "confirm_buffer_atr": 0.0,
                "confirm_expiry_bars": 3,
                "confirm_max_distance_pct": 0.15,
            },
            "F2": frozen["F2_ANY_USER_DECISION"],
            "F3": frozen["F3_OLD_DIAGNOSTIC"],
            "F4": frozen["F4_BALANCED_PRIMARY"],
        },
    }
    for variant, module in (("r2", r2), ("r3", r3)):
        actual = module.raw_shape_candidates(bars)
        for shape in ("F1", "F2", "F3", "F4"):
            mask, _ = e.signal_mask(ledger.copy(), shape, e.SignalConfig(**expected[variant][shape]))
            got = actual[f"{shape}_candidate"].to_numpy(bool)
            want = mask.to_numpy(bool)
            if not np.array_equal(got, want):
                raise AssertionError(
                    f"{variant}/{shape} raw parity failed: got={got.sum()} want={want.sum()} diff={(got != want).sum()}"
                )
            print(f"raw_parity {variant.upper()} {shape}: PASS count={got.sum()}")

    # R3 custom F3: verify the selected development entry timestamps, including
    # the next-15m-open distance rule, against the frozen evidence table.
    m1 = e.load_1m()
    enriched = r3.raw_shape_candidates(bars)
    f3_cfg = r3.frozen_shape_configs()["F3"]
    got_f3_entries = set()
    for signal_time in enriched.index[enriched.F3_candidate]:
        i = enriched.index.get_loc(signal_time)
        j, _ = r3._custom_f3_confirmation(enriched, i, f3_cfg)
        if j is None:
            continue
        entry_time = enriched.index[j] + pd.Timedelta(minutes=15)
        if entry_time not in m1.index:
            continue
        entry_open = float(m1.loc[entry_time, "open"])
        distance = abs(entry_open - float(enriched.iloc[i].close)) / float(enriched.iloc[i].close) * 100
        if distance <= f3_cfg.confirm_max_distance_pct:
            got_f3_entries.add((pd.Timestamp(signal_time), pd.Timestamp(entry_time)))
    evidence = pd.read_csv(
        RUN / "P04_F3_HTF_AND_HOLDOUT/development_independent_trades.csv.gz"
    )
    evidence = evidence.loc[
        (evidence.config_id == "R3_F3_3c1e1d9c0390e4f3")
        & (evidence.scenario_id == "NORMAL_5_2_SLIP1")
        & (evidence.confirmation_status == "TRIGGERED")
    ]
    want_f3_entries = {
        (pd.to_datetime(a, utc=True), pd.to_datetime(b, utc=True))
        for a, b in zip(evidence.signal_time, evidence.entry_time)
    }
    assert got_f3_entries == want_f3_entries
    print(f"R3_F3_entry_parity: PASS count={len(got_f3_entries)}")

    old_env = dict(os.environ)
    try:
        os.environ.update({
            "FOURSHAPE_POSITION_MODE": "MARGIN_PCT",
            "FOURSHAPE_POSITION_VALUE": "3",
            "FOURSHAPE_LEVERAGE": "100",
            "FOURSHAPE_MAX_TOTAL_NOTIONAL_MULTIPLE": "6",
            "FOURSHAPE_CONTRACT_VALUE": "0.1",
            "FOURSHAPE_CONTRACT_STEP": "1",
            "FOURSHAPE_MIN_CONTRACTS": "1",
        })
        size = r2.calculate_order_size(10_000, 2_000)
        assert size.margin_cash == 300
        assert size.notional == 30_000
        assert size.contracts == 150
        size2 = r2.calculate_order_size(10_000, 2_000, current_notional=30_000)
        assert size2.notional == 30_000
        size3 = r2.calculate_order_size(10_000, 2_000, current_notional=60_000)
        assert size3.notional == 0
        print("position_sizing_3pct_margin: PASS")
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    levels = r2.build_protective_levels(1, 2_000, 0.01)
    assert levels["take_profit"] == 2_012.0 and levels["stop_loss"] == 1_982.0
    levels = r2.build_protective_levels(-1, 2_000, 0.01)
    assert levels["take_profit"] == 1_988.0 and levels["stop_loss"] == 2_018.0
    print("protective_levels: PASS")

    t = r2.PendingEntryIntent(
        "F2", -1, ledger.index[-10], ledger.index[-10] + pd.Timedelta(minutes=15),
        ledger.index[-10] + pd.Timedelta(minutes=45), 2_000.0, 2_020.0, 2_005.0, 0.25,
    )
    result = r2.evaluate_pending_intent(t, t.created_at, 2_010, 2_021, 1_999)
    assert result.status == "INVALIDATED"
    result = r2.evaluate_pending_intent(t, t.created_at, 2_010, 2_019, 1_999)
    assert result.status == "TRIGGERED" and result.fill_price == 2_000.0
    print("pending_invalidation_first: PASS")

    n = r3.NextOpenEntryIntent(
        "F3", 1, ledger.index[-10], ledger.index[-8],
        ledger.index[-8] + pd.Timedelta(minutes=15), 2_000.0, 0.75,
    )
    result = r3.evaluate_next_open_intent(n, n.execute_at, 2_010.0)
    assert result.status == "TRIGGERED" and result.fill_price == 2_010.0
    result = r3.evaluate_next_open_intent(n, n.execute_at, 2_016.0)
    assert result.status == "TOO_FAR"
    result = r3.evaluate_next_open_intent(
        n, n.execute_at + pd.Timedelta(minutes=15), 2_010.0
    )
    assert result.status == "EXPIRED"
    print("next_open_distance_and_expiry: PASS")

    os.environ["FOURSHAPE_ENTRY_API"] = "INTENTS"
    strategy = r2.EthSpikeR2Strategy(require_bar_closed_flag=False)
    signal = strategy.generate_signal(bars.tail(256), 0)
    assert signal.signal == 0 and "intent mode" in signal.reason
    os.environ.pop("FOURSHAPE_ENTRY_API", None)
    print("intent_mode_fail_closed: PASS")
    print("ALL_TESTS_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
