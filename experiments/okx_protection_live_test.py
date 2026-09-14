"""实盘试验：开 1 张 LONG，验证 algoClOrdId 是否导致保护单挂单失败。

目的：不改任何正式代码的前提下，确认根因假设：
- 测试①：引擎当前格式 `R16_{ts}_L_1`（含下划线）→ 预期 OKX 51000 拒绝
- 测试②：纯字母数字格式 `R16{ts}L1` → 预期成功
最后无条件撤保护单 + 市价平仓，不留裸仓。

运行（在 project 根目录）：
    python3 experiments/okx_protection_live_test.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autobot.config import TradingConfig
from autobot.exchange.okx_trader import OKXTrader
from autobot.strategies.eth_spike_r16_strategy import build_protective_levels

INST = "ETH-USDT-SWAP"
DIRECTION = "long"


def _fmt(res: dict) -> str:
    if res.get("success"):
        return f"SUCCESS algoId={res.get('algo_id', '')}"
    return f"FAIL msg={res.get('message', res.get('data', {}).get('msg', ''))}"


def main() -> None:
    trader = OKXTrader()
    print("=" * 70)
    print(f"[环境] OKX_FLAG={trader.flag}  ({'实盘' if trader.flag == '0' else '模拟盘'})")
    print(f"[环境] INST={INST} POSITION_MODE={TradingConfig.POSITION_MODE} "
          f"MARGIN_MODE={TradingConfig.MARGIN_MODE} TRIGGER={TradingConfig.TP_SL_TRIGGER_TYPE}")
    print("=" * 70)

    balance = trader.get_usdt_balance()
    print(f"[Preflight] USDT 余额 = {balance:.4f}")
    if balance <= 0:
        raise SystemExit("余额获取失败，中止")

    price = trader.get_current_price(INST)
    print(f"[Preflight] 当前价 = {price}")
    if price <= 0:
        raise SystemExit("价格获取失败，中止")

    tick = trader.get_tick_size(INST)
    print(f"[Preflight] tickSz = {tick}")

    detail = trader.get_position_detail(INST)
    for side in ("long", "short"):
        p = detail.get(side)
        if p and float(p.get("size") or 0) > 0:
            raise SystemExit(f"已存在 {side} 持仓 {p.get('size')} 张，为避免干扰试验中止")

    # ---------- 开仓 ----------
    result = trader.open_position(
        side="buy",
        pos_side=DIRECTION,
        current_price=price,
        balance=balance,
        leverage=TradingConfig.DEFAULT_LEVERAGE,
        ratio=TradingConfig.DEFAULT_RATIO,
        margin_mode=TradingConfig.MARGIN_MODE,
        inst_id=INST,
    )
    if not result.get("success"):
        raise SystemExit(f"开仓失败，中止试验: {result.get('message')}")
    print(f"[开仓] 成功 contracts={result.get('contracts')}")

    try:
        detail = trader.get_position_detail(INST)
        pd_ = detail.get(DIRECTION)
        if not pd_:
            raise RuntimeError("开仓后未读到交易所持仓")
        avg_px = float(pd_.get("avg_price") or 0) or price
        size = float(pd_.get("size") or 0)
        print(f"[开仓] 交易所 avgPx={avg_px} size={size}")

        levels = build_protective_levels(1, avg_px, tick)
        tp, sl = levels["take_profit"], levels["stop_loss"]
        print(f"[保护单] TP={tp} SL={sl} (基于 avgPx={avg_px})")

        now_ts = int(time.time())

        # ---------- 测试①：引擎当前格式（含下划线） ----------
        buggy_id = f"R16_{now_ts}_L_1"
        print("-" * 70)
        print(f"[测试①] algoClOrdId = {buggy_id!r}  (当前引擎格式，含下划线)")
        r1 = trader.place_protective_orders(
            INST, DIRECTION, size, tp, sl,
            margin_mode=TradingConfig.MARGIN_MODE,
            algo_cl_ord_id=buggy_id,
        )
        print(f"[测试①] 结果: {_fmt(r1)}")
        if r1.get("success"):
            c = trader.cancel_protective_orders(INST, algo_id=r1.get("algo_id", ""))
            print(f"[测试①] 意外成功，已撤: {_fmt(c)}")

        # ---------- 测试②：纯字母数字格式 ----------
        fixed_id = f"R16{now_ts}L1"
        print("-" * 70)
        print(f"[测试②] algoClOrdId = {fixed_id!r}  (纯字母数字，修复建议)")
        r2 = trader.place_protective_orders(
            INST, DIRECTION, size, tp, sl,
            margin_mode=TradingConfig.MARGIN_MODE,
            algo_cl_ord_id=fixed_id,
        )
        print(f"[测试②] 结果: {_fmt(r2)}")

        # ---------- 验证：查询未完成条件单 ----------
        print("-" * 70)
        algos = trader.get_algo_orders(INST)
        live_ids = []
        if algos.get("success"):
            for a in algos.get("data", []):
                live_ids.append(a.get("algoId", ""))
                print(f"[查询] 驻留条件单 algoId={a.get('algoId')} "
                      f"algoClOrdId={a.get('algoClOrdId', '')} 状态={a.get('state')}")
        else:
            print(f"[查询] 查询条件单失败: {algos.get('message')}")

        # ---------- 撤保护单（完整闭环） ----------
        if r2.get("success"):
            c2 = trader.cancel_protective_orders(INST, algo_id=r2.get("algo_id", ""))
            print(f"[撤单] 测试②保护单: {_fmt(c2)}")

        # ---------- 汇总 ----------
        print("=" * 70)
        print(f"[汇总] 测试①({buggy_id!r}) -> {_fmt(r1)}")
        print(f"[汇总] 测试②({fixed_id!r}) -> {_fmt(r2)}")
        print(f"[汇总] 驻留条件单数量={len(live_ids)}")
        print("=" * 70)
    finally:
        # ---------- 无条件平仓 ----------
        print("-" * 70)
        close = trader.close_position(pos_side=DIRECTION, margin_mode=TradingConfig.MARGIN_MODE, inst_id=INST)
        print(f"[平仓] 结果: success={close.get('success')} data={close.get('data', {}).get('code')}")
        time.sleep(1)
        chk = trader.get_position_detail(INST)
        for side in ("long", "short"):
            p = chk.get(side)
            if p:
                print(f"[确认] {side} 剩余张数 = {p.get('size')}")
        print("[完成] 试验结束，已平仓")


if __name__ == "__main__":
    main()
