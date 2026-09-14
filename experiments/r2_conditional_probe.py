#!/usr/bin/env python3
"""R2 条件入场单 OKX 实盘受控探针（1 张，远触发价，秒级撤销）。

目的：验证 place_stop_entry_order 使用的 tp/sl 触发字段语义在真实实盘 API 上正确
（buy 用 tpTriggerPx 向上、sell 用 slTriggerPx 向下），并确认 order_algos_list 可查到
conditional 单、可撤销。若语义反了，触发价会被交易所当作另一方向而立即成交 ——
因此触发价设得极远（现价 ×1.5 / ×0.5），张数=1。

用法（需在 service 停止或空闲时人工监督执行）:
    python3 experiments/r2_conditional_probe.py
"""
import sys, time
sys.path.insert(0, "/home/ecs-user/zzs/project")

from autobot.exchange.okx_trader import OKXTrader
from autobot.config import TradingConfig

INST = "ETH-USDT-SWAP"


def main():
    trader = OKXTrader()
    price = trader.get_current_price(INST)
    print(f"current={price}  ctVal={trader.get_contract_value(INST)} tick={trader.get_tick_size(INST)}")
    if price <= 0:
        print("无法获取价格，中止"); return 1

    buy_trigger = round(price * 1.1, 2)   # 向上 10%（buy-stop，OKX: slTriggerPx 须高于现价）
    sell_trigger = round(price * 0.9, 2)  # 向下 10%（sell-stop，slTriggerPx 须低于现价）
    ts = int(time.time())

    r1 = trader.place_stop_entry_order(
        INST, "buy", "long", 1, buy_trigger,
        margin_mode=TradingConfig.MARGIN_MODE, algo_cl_ord_id=f"R2PROBE{ts}B",
    )
    print("buy-stop 挂单:", r1)
    r2 = trader.place_stop_entry_order(
        INST, "sell", "short", 1, sell_trigger,
        margin_mode=TradingConfig.MARGIN_MODE, algo_cl_ord_id=f"R2PROBE{ts}S",
    )
    print("sell-stop 挂单:", r2)
    time.sleep(2)

    algos = trader.get_algo_orders(INST, ord_type="conditional")
    print("conditional 列表 success=", algos.get("success"), "count=", len(algos.get("data", [])))
    for a in algos.get("data", []):
        print("  ", a.get("algoId"), a.get("side"), a.get("posSide"),
              a.get("tpTriggerPx"), a.get("slTriggerPx"), a.get("state"))

    # 秒级撤销（无论成功失败都撤，避免残留）
    for r in (r1, r2):
        aid = r.get("algo_id", "") if r.get("success") else ""
        if aid:
            print("撤销", aid, trader.cancel_protective_orders(INST, algo_id=aid))
    print("探针完成。若 buy-stop/sell-stop 都能挂上且列表可见、可撤，则 place_stop_entry_order 语义正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
