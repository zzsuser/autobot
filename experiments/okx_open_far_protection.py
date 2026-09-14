"""实盘：开 1 张 LONG + 挂远离现价的 TP/SL 保护单（保持持仓，不平仓）。

用于验证保护单驻留 OKX 且不会自动触发；由用户手动平仓后反馈。

运行（project 根目录）：
    python3 experiments/okx_open_far_protection.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autobot.config import TradingConfig
from autobot.exchange.okx_trader import OKXTrader
from autobot.strategies.eth_spike_r16_strategy import _directional_tick_round

INST = "ETH-USDT-SWAP"
DIRECTION = "long"
TP_PCT = 0.05   # 止盈远离现价 5%
SL_PCT = 0.05   # 止损远离现价 5%


def main() -> None:
    trader = OKXTrader()
    print("=" * 70)
    print(f"[环境] OKX_FLAG={trader.flag} ({'实盘' if trader.flag == '0' else '模拟盘'})")
    print("=" * 70)

    balance = trader.get_usdt_balance()
    print(f"[Preflight] USDT 余额 = {balance:.4f}")
    price = trader.get_current_price(INST)
    print(f"[Preflight] 当前价 = {price}")
    if balance <= 0 or price <= 0:
        raise SystemExit("余额/价格获取失败，中止")

    detail = trader.get_position_detail(INST)
    for side in ("long", "short"):
        p = detail.get(side)
        if p and float(p.get("size") or 0) > 0:
            raise SystemExit(f"已有 {side} 持仓 {p.get('size')} 张，中止")

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
        raise SystemExit(f"开仓失败: {result.get('message')}")
    print(f"[开仓] 成功 contracts={result.get('contracts')}")

    detail = trader.get_position_detail(INST)
    pd_ = detail.get(DIRECTION)
    if not pd_:
        raise SystemExit("开仓后未读到交易所持仓")
    avg_px = float(pd_.get("avg_price") or 0) or price
    size = float(pd_.get("size") or 0)
    print(f"[开仓] 交易所 avgPx={avg_px} size={size}")

    tick = trader.get_tick_size(INST)
    raw_tp = avg_px * (1 + TP_PCT)
    raw_sl = avg_px * (1 - SL_PCT)
    tp = _directional_tick_round(raw_tp, 1, "TP", tick)
    sl = _directional_tick_round(raw_sl, 1, "SL", tick)
    print(f"[保护单] TP={tp} (+{TP_PCT*100:.0f}%) SL={sl} (-{SL_PCT*100:.0f}%) 距现价较远，不会自动触发")

    now_ts = int(time.time())
    algo_cl_ord_id = f"R16{now_ts}L1"  # 修复后格式（纯字母数字）
    algo = trader.place_protective_orders(
        INST, DIRECTION, size, tp, sl,
        margin_mode=TradingConfig.MARGIN_MODE,
        algo_cl_ord_id=algo_cl_ord_id,
    )
    if not algo.get("success"):
        print(f"[保护单] 挂单失败: {algo.get('message')} —— 保持仓位已开，请手动处理")
        raise SystemExit(1)
    print(f"[保护单] 挂单成功 algoId={algo.get('algo_id')}")

    algos = trader.get_algo_orders(INST)
    if algos.get("success"):
        for a in algos.get("data", []):
            print(f"[查询] 驻留条件单 algoId={a.get('algoId')} "
                  f"algoClOrdId={a.get('algoClOrdId','')} 状态={a.get('state')}")
    else:
        print(f"[查询] 条件单查询失败: {algos.get('message')}")

    print("=" * 70)
    print("持仓保持 OPEN，保护单已驻留。请手动平仓后告知我，我将核对对账结果。")
    print(f"仓位: {DIRECTION} {size} 张 @ {avg_px} | TP={tp} SL={sl} | algoId={algo.get('algo_id')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
