"""R16 执行层状态机离线测试（mock 交易所与仓位管理，不触达真实 Redis/DB/OKX）。

运行:
    python -m unittest -v test_r16_execution.py

覆盖 Gate 4 的关键场景：开仓 / 保护单失败兜底 / 加仓 / 达到上限 / 反手 / 反手旧仓未归零 /
保护单健康巡检补挂。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from autobot.core.engine import TradingEngine, R16_METHOD
from autobot.core.position_manager import PositionInfo, SinglePositionInfo
from autobot.core.strategy_base import SignalResult


def make_df(n: int = 64) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01 00:00:00", periods=n, freq="15min")
    close = np.linspace(2000.0, 2100.0, n)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close},
        index=idx,
    )


def long_pos(open_entries: int = 1, algo_id: str = "algo-1") -> PositionInfo:
    return PositionInfo(
        long=SinglePositionInfo(
            direction="long", has_position=True, entry_price=2000.0, size=2,
            open_entries=open_entries, algo_order_id=algo_id,
        )
    )


class FakeTrader:
    """可脚本化的交易所 mock，记录调用并返回预设结果。"""

    def __init__(self):
        self.price = 2000.0
        self.balance = 1000.0
        self.tick = 0.01
        self.open_contracts = 2
        self.open_ok = True
        self.protection_ok = True
        self.amend_ok = True
        self.position_detail = {}
        self.algo_orders = []
        self.calls = []
        self.placed = None
        self.amended = None
        self.cancelled = None

    def get_current_price(self, symbol):
        return self.price

    def get_usdt_balance(self):
        return self.balance

    def get_tick_size(self, inst_id=None):
        return self.tick

    def open_position(self, **kw):
        self.calls.append(("open", kw))
        if not self.open_ok:
            return {"success": False, "message": "mock open fail", "contracts": 0}
        return {"success": True, "message": "ok", "contracts": self.open_contracts}

    def get_position_detail(self, inst_id=None):
        return self.position_detail

    def close_position(self, **kw):
        self.calls.append(("close", kw))
        # 平仓后归零
        self.position_detail = {"long": None, "short": None}
        return {"success": True}

    def place_protective_orders(
        self, inst_id, direction, size, tp_price, sl_price,
        margin_mode=None, algo_cl_ord_id="",
    ):
        self.placed = dict(
            direction=direction, size=size, tp=tp_price, sl=sl_price,
            algo_cl_ord_id=algo_cl_ord_id,
        )
        if not self.protection_ok:
            return {"success": False, "message": "mock protection fail"}
        return {"success": True, "algo_id": "algo-new"}

    def amend_protective_orders(self, inst_id, algo_id, new_size, new_tp, new_sl):
        self.amended = dict(algo_id=algo_id, size=new_size, tp=new_tp, sl=new_sl)
        if not self.amend_ok:
            return {"success": False, "message": "mock amend fail"}
        return {"success": True}

    def cancel_protective_orders(self, inst_id, algo_id="", algo_cl_ord_id=""):
        self.cancelled = dict(algo_id=algo_id, algo_cl_ord_id=algo_cl_ord_id)
        return {"success": True}

    def get_algo_orders(self, inst_id, ord_type="oco"):
        return {"success": True, "data": list(self.algo_orders)}


class R16ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = TradingEngine()
        self.trader = FakeTrader()
        self.engine.trader = self.trader
        self.df = make_df()
        # 用 MagicMock 替换全局 position_manager
        self.pm_patcher = patch("autobot.core.engine.position_manager")
        self.pm = self.pm_patcher.start()
        self.addCleanup(self.pm_patcher.stop)

    # ---------- 开仓 ----------

    def test_open_long_places_protection_and_saves(self):
        self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "2"}}
        ok, msg = self.engine._r16_open("okx", "ETH-USDT-SWAP", R16_METHOD, "long", self.df)
        self.assertTrue(ok, msg)
        self.assertEqual(self.trader.placed["direction"], "long")
        self.assertEqual(self.trader.placed["tp"], 2012.0)  # 2000 * 1.006
        self.assertEqual(self.trader.placed["sl"], 1982.0)  # 2000 * 0.991
        self.assertEqual(self.trader.placed["size"], 2)
        save_kwargs = self.pm.save_position.call_args.kwargs
        self.assertEqual(save_kwargs["open_entries"], 1)
        self.assertEqual(save_kwargs["algo_order_id"], "algo-new")

    def test_open_protection_fail_closes_and_clears(self):
        self.trader.protection_ok = False
        self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "2"}}
        ok, msg = self.engine._r16_open("okx", "ETH-USDT-SWAP", R16_METHOD, "long", self.df)
        self.assertFalse(ok)
        self.assertIn("保护单", msg)
        # 兜底平仓
        self.assertTrue(any(c[0] == "close" for c in self.trader.calls))
        self.assertTrue(self.pm.clear_position.called)

    # ---------- 加仓 ----------

    def test_add_success_amends_and_increments(self):
        pos = long_pos(open_entries=1, algo_id="algo-1")
        self.trader.position_detail = {"long": {"avg_price": "2010.0", "size": "4"}}
        ok, msg = self.engine._r16_add("okx", "ETH-USDT-SWAP", R16_METHOD, "long", pos, self.df)
        self.assertTrue(ok, msg)
        self.assertEqual(self.trader.amended["algo_id"], "algo-1")
        self.assertEqual(self.trader.amended["size"], 4)
        # 2010 * 1.006 = 2022.06, 2010 * 0.991 = 1991.91
        self.assertAlmostEqual(self.trader.amended["tp"], 2022.06, places=6)
        self.assertAlmostEqual(self.trader.amended["sl"], 1991.91, places=6)
        up_kwargs = self.pm.update_position.call_args.kwargs
        self.assertEqual(up_kwargs["open_entries"], 2)

    def test_add_at_max_blocks(self):
        pos = long_pos(open_entries=3)
        ok, msg = self.engine._r16_add("okx", "ETH-USDT-SWAP", R16_METHOD, "long", pos, self.df)
        self.assertFalse(ok)
        self.assertIn("最大", msg)
        self.assertFalse(any(c[0] == "open" for c in self.trader.calls))

    def test_add_amend_fail_closes(self):
        pos = long_pos(open_entries=1, algo_id="algo-1")
        self.trader.amend_ok = False
        self.trader.position_detail = {"long": {"avg_price": "2010.0", "size": "4"}}
        ok, msg = self.engine._r16_add("okx", "ETH-USDT-SWAP", R16_METHOD, "long", pos, self.df)
        self.assertFalse(ok)
        self.assertTrue(any(c[0] == "close" for c in self.trader.calls))
        self.assertTrue(self.pm.clear_position.called)

    # ---------- 反手 ----------

    def test_reverse_cancels_closes_and_opens_opposite(self):
        pos = long_pos(open_entries=1, algo_id="algo-1")
        # 反手后旧方向归零，新方向成交
        self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "2"}}
        ok, msg = self.engine._r16_reverse(
            "okx", "ETH-USDT-SWAP", R16_METHOD, "short", pos, self.df
        )
        self.assertTrue(ok, msg)
        self.assertEqual(self.trader.cancelled["algo_id"], "algo-1")
        self.assertTrue(any(c[0] == "close" for c in self.trader.calls))
        self.assertTrue(any(c[0] == "open" for c in self.trader.calls))
        self.assertEqual(self.trader.placed["direction"], "short")

    def test_reverse_old_not_zero_aborts(self):
        pos = long_pos(open_entries=1, algo_id="algo-1")
        # 平仓后仍残留旧仓
        orig_close = self.trader.close_position

        def stubborn_close(**kw):
            self.trader.calls.append(("close", kw))
            self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "1"}}
            return {"success": True}

        self.trader.close_position = stubborn_close
        ok, msg = self.engine._r16_reverse(
            "okx", "ETH-USDT-SWAP", R16_METHOD, "short", pos, self.df
        )
        self.assertFalse(ok)
        self.assertIn("未确认归零", msg)
        self.assertFalse(any(c[0] == "open" for c in self.trader.calls))

    # ---------- 保护单巡检 ----------

    def test_check_protection_replaces_missing(self):
        pos = long_pos(open_entries=1, algo_id="algo-gone")
        self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "2"}}
        self.trader.algo_orders = []  # 无匹配保护单
        ok, msg = self.engine._r16_check_protection(
            "okx", "ETH-USDT-SWAP", R16_METHOD, pos
        )
        self.assertTrue(ok)
        self.assertIsNotNone(self.trader.placed)
        up_kwargs = self.pm.update_position.call_args.kwargs
        self.assertEqual(up_kwargs["algo_order_id"], "algo-new")

    def test_check_protection_skips_when_covered(self):
        pos = long_pos(open_entries=1, algo_id="algo-1")
        self.trader.position_detail = {"long": {"avg_price": "2000.0", "size": "2"}}
        self.trader.algo_orders = [{"algoId": "algo-1"}]
        ok, msg = self.engine._r16_check_protection(
            "okx", "ETH-USDT-SWAP", R16_METHOD, pos
        )
        self.assertTrue(ok)
        self.assertIsNone(self.trader.placed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
