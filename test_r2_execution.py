"""R2 四形态执行层状态机离线测试（mock 交易所/仓位/Redis，不触达真实 Redis/DB/OKX）。

运行:
    python -m unittest -v test_r2_execution.py

覆盖: 收盘挂条件单 / 组合约束(反向/上限)拦截 / 去重 / 超时撤单 / 失效位撤单 /
条件单成交→建仓+保护单 / 加仓→改单+open_entries / F3 下一开盘入场 / 距离过远不入 /
保护单失败兜底平仓。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from autobot.core.engine import TradingEngine, R2_METHOD
from autobot.core.position_manager import PositionInfo, SinglePositionInfo
from autobot.strategies.eth_spike_r2_strategy import (
    RuntimePositionConfig,
    NextOpenEntryIntent,
    PendingEntryIntent,
)


def make_df(n: int = 160) -> pd.DataFrame:
    # 最新一根为“刚收盘”的15m bar（anchor 到 now 前一刻钟），保证 created_at<=now、expiry 在未来
    end = pd.Timestamp.utcnow().floor("15min") - pd.Timedelta(minutes=15)
    idx = pd.date_range(end=end, periods=n, freq="15min")
    close = np.linspace(2000.0, 2160.0, n)
    return pd.DataFrame(
        {"open": close, "high": close + 2, "low": close - 2, "close": close},
        index=idx,
    )


def r2_cfg() -> RuntimePositionConfig:
    return RuntimePositionConfig(
        mode="MARGIN_PCT", value=3.0, leverage=100.0,
        max_same_direction_entries=2, max_total_notional_multiple=6.0,
        contract_value=0.1, contract_step=1.0, min_contracts=1.0, entry_api="INTENTS",
    )


class FakePM:
    """内存版 position_manager（get/update/save/clear/single）"""

    def __init__(self):
        self.store = {"long": None, "short": None}
        self.calls = []

    def _rec(self, direction):
        r = self.store.get(direction)
        if r is None:
            self.store[direction] = {
                "has_position": False, "entry_price": 0.0, "size": 0.0,
                "open_entries": 0, "algo_order_id": "", "entry_time": "",
                "timestamp": 0, "position_id": "", "entry_index": None,
            }
            r = self.store[direction]
        return r

    def get_position(self, exchange, symbol, method):
        long_d = self._rec("long")
        short_d = self._rec("short")
        return PositionInfo(
            long=SinglePositionInfo(
                direction="long", has_position=long_d["has_position"],
                entry_price=long_d["entry_price"], size=long_d["size"],
                open_entries=long_d["open_entries"], algo_order_id=long_d["algo_order_id"],
                position_id=long_d["position_id"],
            ),
            short=SinglePositionInfo(
                direction="short", has_position=short_d["has_position"],
                entry_price=short_d["entry_price"], size=short_d["size"],
                open_entries=short_d["open_entries"], algo_order_id=short_d["algo_order_id"],
                position_id=short_d["position_id"],
            ),
        )

    def get_direction(self, direction):
        return self.get_position(None, None, None).get_direction(direction)

    def save_position(self, **kw):
        self.calls.append(("save", kw))
        direction = kw["direction"]
        rec = self._rec(direction)
        rec.update(
            has_position=True, entry_price=kw["price"], size=kw["size"],
            open_entries=kw.get("open_entries", 1),
            algo_order_id=kw.get("algo_order_id", ""),
            position_id=f"pid-{direction}",
        )

    def update_position(self, exchange, symbol, method, direction, **fields):
        self.calls.append(("update", dict(direction=direction, **fields)))
        rec = self._rec(direction)
        rec["has_position"] = True
        for k, v in fields.items():
            if k == "entry_price":
                rec["entry_price"] = v
            elif k == "size":
                rec["size"] = v
            elif k == "open_entries":
                rec["open_entries"] = v
            elif k == "algo_order_id":
                rec["algo_order_id"] = v
        return self.get_position(None, None, None).get_direction(direction)

    def clear_position(self, exchange, symbol, method, direction=None, close_price=0, reason=""):
        self.calls.append(("clear", dict(direction=direction, reason=reason)))
        for d in (["long", "short"] if direction is None else [direction]):
            self.store[d] = {
                "has_position": False, "entry_price": 0.0, "size": 0.0,
                "open_entries": 0, "algo_order_id": "", "entry_time": "",
                "timestamp": 0, "position_id": "", "entry_index": None,
            }

    def set_local(self, direction, size, open_entries=1, algo_id=""):
        rec = self._rec(direction)
        rec.update(has_position=True, size=size, open_entries=open_entries,
                   algo_order_id=algo_id, entry_price=2000.0, position_id=f"pid-{direction}")


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class FakeTrader:
    """可脚本化交易所 mock（含 R2 用到的所有方法）"""

    def __init__(self):
        self.price = 2000.0
        self.balance = 10_000.0
        self.equity = 10_000.0
        self.tick = 0.01
        self.ctval = 0.1
        self.position_detail = {"long": None, "short": None}
        self.oco_algo_orders = []          # 已驻留 OCO
        self.conditional_algo_orders = []  # 已驻留条件入场单
        self.protection_ok = True
        self.amend_ok = True
        self.entry_ok = True
        self.conditional_ok = True
        self.calls = []
        self.placed_protection = None
        self.amended = None
        self.cancelled = None

    # ---- 报价 ----
    def get_current_price(self, symbol):
        return self.price

    def get_usdt_balance(self):
        return self.balance

    def get_usdt_equity(self):
        return self.equity

    def get_tick_size(self, inst_id=None):
        return self.tick

    def get_contract_value(self, inst_id=None):
        return self.ctval

    # ---- 下单 ----
    def place_stop_entry_order(self, inst_id, side, pos_side, sz, trigger_price,
                               margin_mode=None, algo_cl_ord_id=""):
        self.calls.append(("entry", dict(inst_id=inst_id, side=side, pos_side=pos_side,
                                         sz=sz, trigger=trigger_price, cl=algo_cl_ord_id)))
        if not self.conditional_ok:
            return {"success": False, "message": "mock conditional fail"}
        algo_id = f"cond-{len(self.conditional_algo_orders) + 1}"
        self.conditional_algo_orders.append({"algoId": algo_id, "algoClOrdId": algo_cl_ord_id,
                                             "state": "live", "side": side, "posSide": pos_side})
        return {"success": True, "algo_id": algo_id}

    def open_market_size(self, inst_id, side, pos_side, sz, leverage=None, margin_mode=None):
        self.calls.append(("market", dict(inst_id=inst_id, side=side, pos_side=pos_side, sz=sz)))
        if not self.entry_ok:
            return {"success": False, "message": "mock market fail", "contracts": 0}
        # 成交：让交易所出现对应仓位
        self.position_detail[pos_side] = {
            "avg_price": str(self.price), "size": str(sz), "pos_side": pos_side,
        }
        return {"success": True, "contracts": sz}

    def place_protective_orders(self, inst_id, direction, size, tp_price, sl_price,
                                margin_mode=None, algo_cl_ord_id=""):
        self.placed_protection = dict(direction=direction, size=size, tp=tp_price, sl=sl_price)
        if not self.protection_ok:
            return {"success": False, "message": "mock protection fail"}
        algo_id = f"oco-{len(self.oco_algo_orders) + 1}"
        self.oco_algo_orders.append({"algoId": algo_id, "side": "sell" if direction == "long" else "buy",
                                     "state": "live"})
        return {"success": True, "algo_id": algo_id}

    def amend_protective_orders(self, inst_id, algo_id, new_size, new_tp, new_sl):
        self.amended = dict(algo_id=algo_id, size=new_size, tp=new_tp, sl=new_sl)
        if not self.amend_ok:
            return {"success": False, "message": "mock amend fail"}
        return {"success": True}

    def cancel_protective_orders(self, inst_id, algo_id="", algo_cl_ord_id=""):
        self.cancelled = dict(algo_id=algo_id, algo_cl_ord_id=algo_cl_ord_id)
        self.conditional_algo_orders = [
            a for a in self.conditional_algo_orders if a["algoId"] != algo_id
        ]
        return {"success": True}

    def get_algo_orders(self, inst_id, ord_type="oco"):
        data = self.oco_algo_orders if ord_type == "oco" else self.conditional_algo_orders
        return {"success": True, "data": list(data)}

    def get_position_detail(self, inst_id=None):
        return {"long": self.position_detail.get("long"),
                "short": self.position_detail.get("short")}

    def close_position(self, pos_side="net", ccy="USDT", margin_mode=None, inst_id=None):
        self.calls.append(("close", pos_side))
        self.position_detail[pos_side] = None
        return {"success": True}


def short_pending(signal_bar: pd.Timestamp, close: float, trigger=None, invalidation=None,
                  expiry_bars: int = 3) -> PendingEntryIntent:
    return PendingEntryIntent(
        shape="F2", side=-1, signal_bar_start=signal_bar,
        created_at=signal_bar + pd.Timedelta(minutes=15),
        expires_at=signal_bar + pd.Timedelta(minutes=15 * expiry_bars),
        trigger_price=trigger if trigger is not None else close - 1.0,
        invalidation_price=invalidation if invalidation is not None else close + 1.0,
        signal_close=close, max_distance_pct=0.25,
    )


def f3_next_open(confirm_bar: pd.Timestamp, close: float) -> NextOpenEntryIntent:
    return NextOpenEntryIntent(
        shape="F3", side=1,
        signal_bar_start=confirm_bar - pd.Timedelta(minutes=15),
        confirmation_bar_start=confirm_bar,
        execute_at=confirm_bar + pd.Timedelta(minutes=15),
        signal_close=close, max_distance_pct=0.75,
    )


class FakeStrategy:
    """返回固定意图列表的假策略（可控）"""

    def __init__(self, pending=None, next_open=None):
        self._p = pending or []
        self._n = next_open or []
        self.position_cfg = r2_cfg()

    def pending_entry_intents(self, df):
        return list(self._p)

    def next_open_entry_intents(self, df):
        return list(self._n)


class R2ExecutionTests(unittest.TestCase):
    def setUp(self):
        # 环境: R2 默认档（与交付说明一致）
        old = dict(os.environ)
        os.environ.update({
            "FOURSHAPE_POSITION_MODE": "MARGIN_PCT", "FOURSHAPE_POSITION_VALUE": "3",
            "FOURSHAPE_LEVERAGE": "100", "FOURSHAPE_MAX_ENTRIES": "2",
            "FOURSHAPE_MAX_TOTAL_NOTIONAL_MULTIPLE": "6", "FOURSHAPE_CONTRACT_VALUE": "0.1",
            "FOURSHAPE_CONTRACT_STEP": "1", "FOURSHAPE_MIN_CONTRACTS": "1",
            "FOURSHAPE_ENTRY_API": "INTENTS",
        })
        self.addCleanup(os.environ.clear)
        self.addCleanup(os.environ.update, old)

        self.engine = TradingEngine()
        self.trader = FakeTrader()
        self.engine.trader = self.trader
        self.pm = FakePM()
        self.redis = FakeRedis()
        self.df = make_df()

        p_pm = patch("autobot.core.engine.position_manager", self.pm)
        p_rd = patch("autobot.core.engine.redis_client", self.redis)
        p_reg = patch("autobot.core.engine.strategy_registry")
        self.pm_patcher = p_pm
        self.rd_patcher = p_rd
        p_pm.start()
        p_rd.start()
        self.addCleanup(p_pm.stop)
        self.addCleanup(p_rd.stop)

        # 控制 registry.get
        self.reg_mock = p_reg.start()
        self.addCleanup(p_reg.stop)
        self.reg_mock.get.return_value = FakeStrategy()

    def _set_strategy(self, strategy: FakeStrategy):
        self.reg_mock.get.return_value = strategy

    # ---------- 收盘挂单 ----------

    def test_new_bar_places_conditional_and_persists(self):
        intent = short_pending(self.df.index[-1], float(self.df.iloc[-1].close))
        self._set_strategy(FakeStrategy(pending=[intent]))
        self.trader.price = float(self.df.iloc[-1].close)  # 现价对齐收盘，向下触发价在下方
        ok, msg = self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD,
                                          PositionInfo(), self.df)
        self.assertTrue(ok, msg)
        entry_calls = [c for c in self.trader.calls if c[0] == "entry"]
        self.assertEqual(len(entry_calls), 1)
        key = self.engine._r2_intents_key("okx", "ETH-USDT-SWAP", R2_METHOD)
        stored = json.loads(self.redis.get(key))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["state"], "live")
        self.assertTrue(stored[0]["algo_id"].startswith("cond-"))

    def test_new_bar_dedupes_same_bar(self):
        intent = short_pending(self.df.index[-1], float(self.df.iloc[-1].close))
        self._set_strategy(FakeStrategy(pending=[intent]))
        self.trader.price = float(self.df.iloc[-1].close)
        self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD, PositionInfo(), self.df)
        ok, msg = self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD, PositionInfo(), self.df)
        self.assertFalse(ok)
        entry_calls = [c for c in self.trader.calls if c[0] == "entry"]
        self.assertEqual(len(entry_calls), 1)

    def test_new_bar_blocks_opposite_holding(self):
        # 已持空仓时不允许再开 long 意图(F3)
        pos = PositionInfo(short=SinglePositionInfo(
            direction="short", has_position=True, size=2, open_entries=1))
        f3 = f3_next_open(self.df.index[-1], float(self.df.iloc[-1].close))
        self._set_strategy(FakeStrategy(next_open=[f3]))
        ok, msg = self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD, pos, self.df)
        market = [c for c in self.trader.calls if c[0] == "market"]
        self.assertEqual(len(market), 0)

    def test_new_bar_blocks_when_max_entries_reached(self):
        pos = PositionInfo(long=SinglePositionInfo(
            direction="long", has_position=True, size=2, open_entries=2))
        f3 = f3_next_open(self.df.index[-1], float(self.df.iloc[-1].close))
        self._set_strategy(FakeStrategy(next_open=[f3]))
        self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD, pos, self.df)
        market = [c for c in self.trader.calls if c[0] == "market"]
        self.assertEqual(len(market), 0)

    # ---------- 巡检: 超时 / 失效 / 成交 ----------

    def _seed_intent(self, intent: PendingEntryIntent, algo_id="cond-9"):
        key = self.engine._r2_intents_key("okx", "ETH-USDT-SWAP", R2_METHOD)
        rec = {
            "kind": "pending", "shape": intent.shape, "side": int(intent.side),
            "direction": "short", "signal_bar_start": str(intent.signal_bar_start.isoformat()),
            "created_at": str(intent.created_at.isoformat()),
            "expires_at": str(intent.expires_at.isoformat()),
            "trigger_price": float(intent.trigger_price),
            "invalidation_price": (float(intent.invalidation_price)
                                   if intent.invalidation_price is not None else None),
            "signal_close": float(intent.signal_close),
            "max_distance_pct": float(intent.max_distance_pct),
            "contracts": 5, "algo_id": algo_id, "algo_cl_ord_id": "R2cl",
            "state": "live", "placed_at": "",
        }
        self.redis.set(key, json.dumps([rec]))
        return key

    def test_monitor_expiry_cancels_and_removes(self):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=30)
        intent = short_pending(past, 2000.0, expiry_bars=1)  # 已过期
        self._seed_intent(intent)
        self.trader.conditional_algo_orders.append({"algoId": "cond-9", "state": "live"})
        ok, msg = self.engine._r2_monitor_intents("okx", "ETH-USDT-SWAP", R2_METHOD)
        key = self.engine._r2_intents_key("okx", "ETH-USDT-SWAP", R2_METHOD)
        self.assertEqual(json.loads(self.redis.get(key)), [])
        self.assertIsNotNone(self.trader.cancelled)

    def test_monitor_invalidation_cancels(self):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=1)
        intent = short_pending(past, 2000.0, invalidation=2010.0)  # 反向失效位
        self._seed_intent(intent)
        self.trader.conditional_algo_orders.append({"algoId": "cond-9", "state": "live"})
        self.trader.price = 2011.0  # 已突破失效位
        self.engine._r2_monitor_intents("okx", "ETH-USDT-SWAP", R2_METHOD)
        key = self.engine._r2_intents_key("okx", "ETH-USDT-SWAP", R2_METHOD)
        self.assertEqual(json.loads(self.redis.get(key)), [])
        self.assertEqual(self.trader.cancelled["algo_id"], "cond-9")

    def test_monitor_fill_establishes_position_and_protection(self):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=1)
        intent = short_pending(past, 2000.0, expiry_bars=3)
        self._seed_intent(intent)
        # 条件单已消失 → 交易所出现 short 仓位（触发成交）
        self.trader.position_detail["short"] = {"avg_price": "1999.5", "size": "5"}
        ok, msg = self.engine._r2_monitor_intents("okx", "ETH-USDT-SWAP", R2_METHOD)
        key = self.engine._r2_intents_key("okx", "ETH-USDT-SWAP", R2_METHOD)
        self.assertEqual(json.loads(self.redis.get(key)), [])
        self.assertTrue(self.pm.store["short"]["has_position"])
        self.assertEqual(self.pm.store["short"]["open_entries"], 1)
        self.assertEqual(self.trader.placed_protection["direction"], "short")
        # SL 高于现价(空头), TP 低于现价
        self.assertGreater(self.trader.placed_protection["sl"], self.trader.price)
        self.assertLess(self.trader.placed_protection["tp"], self.trader.price)

    def test_monitor_fill_add_updates_entries_and_amends(self):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=1)
        intent = short_pending(past, 2000.0, expiry_bars=3)
        self._seed_intent(intent)
        self.pm.set_local("short", size=3, open_entries=1, algo_id="oco-1")
        self.trader.oco_algo_orders.append({"algoId": "oco-1", "state": "live"})
        # 成交后总仓 8 张（原3+新5）
        self.trader.position_detail["short"] = {"avg_price": "1999.5", "size": "8"}
        ok, msg = self.engine._r2_monitor_intents("okx", "ETH-USDT-SWAP", R2_METHOD)
        self.assertEqual(self.pm.store["short"]["open_entries"], 2)
        self.assertEqual(self.pm.store["short"]["size"], 8)
        self.assertEqual(self.trader.amended["algo_id"], "oco-1")
        self.assertEqual(self.trader.amended["size"], 8)

    # ---------- F3 下一开盘 ----------

    def test_f3_next_open_enters_market(self):
        close = float(self.df.iloc[-1].close)
        f3 = f3_next_open(self.df.index[-1], close)
        self._set_strategy(FakeStrategy(next_open=[f3]))
        self.trader.price = close * 1.001  # 距信号收盘 0.1%，在 0.75% 内
        ok, msg = self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD,
                                          PositionInfo(), self.df)
        self.assertTrue(ok, msg)
        market = [c for c in self.trader.calls if c[0] == "market"]
        self.assertEqual(len(market), 1)
        self.assertTrue(self.pm.store["long"]["has_position"])
        self.assertEqual(self.pm.store["long"]["open_entries"], 1)
        self.assertIsNotNone(self.trader.placed_protection)

    def test_f3_too_far_skips(self):
        close = float(self.df.iloc[-1].close)
        f3 = f3_next_open(self.df.index[-1], close)
        self._set_strategy(FakeStrategy(next_open=[f3]))
        self.trader.price = close * 1.01  # 1% > 0.75%
        ok, msg = self.engine._r2_new_bar("okx", "ETH-USDT-SWAP", R2_METHOD,
                                          PositionInfo(), self.df)
        market = [c for c in self.trader.calls if c[0] == "market"]
        self.assertEqual(len(market), 0)

    # ---------- 保护单失败兜底 ----------

    def test_establish_protection_fail_closes_and_clears(self):
        past = pd.Timestamp.utcnow() - pd.Timedelta(minutes=1)
        intent = short_pending(past, 2000.0, expiry_bars=3)
        self._seed_intent(intent)
        self.trader.position_detail["short"] = {"avg_price": "1999.5", "size": "5"}
        self.trader.protection_ok = False
        self.engine._r2_monitor_intents("okx", "ETH-USDT-SWAP", R2_METHOD)
        # 保护单失败 → 兜底市价平仓 + 清本地记录
        self.assertTrue(any(c[0] == "close" for c in self.trader.calls))
        self.assertFalse(self.pm.store["short"]["has_position"])
        self.assertTrue(any(c[0] == "clear" for c in self.pm.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
