"""实盘探针：验证 OKX conditional/oco 是否接受小数张(sz=0.01步长)。
挂极远触发价(不会成交)→确认返回code=0→立即撤销。资金占用可忽略。"""
import sys
from autobot.exchange.okx_trader import OKXTrader

t = OKXTrader()
inst = "ETH-USDT-SWAP"
price = t.get_current_price(inst)
trigger = round(price * 1.5, 1)  # 远高于现价，buy-stop 合法但不会触发
sz = 0.01  # 最小步长

# 1) conditional 开仓单（R2 F1/F2/F4 入口）
r = t.place_stop_entry_order(
    inst_id=inst, side="buy", pos_side="long", sz=sz,
    trigger_price=trigger, margin_mode="isolated",
    algo_cl_ord_id=f"PROBE{int(__import__('time').time())}L",
)
print("conditional place:", {k: r.get(k) for k in ("success", "algo_id", "message")})
if r.get("success"):
    aid = r["algo_id"]
    t.cancel_protective_orders(inst, algo_id=aid)
    print("conditional 已撤销 algo_id=", aid)

# 2) oco 保护单（R2 成交后 TP/SL）sz 小数验证——挂一个 0.01 张 oco 再撤
tp = round(price * 0.98, 1)
sl = round(price * 1.02, 1)
r2 = t.place_protective_orders(
    inst_id=inst, direction="long", size=sz, tp_price=tp, sl_price=sl,
    margin_mode="isolated", algo_cl_ord_id=f"PROBE{int(__import__('time').time())}O",
)
print("oco place:", {k: r2.get(k) for k in ("success", "algo_id", "message")})
if r2.get("success"):
    t.cancel_protective_orders(inst, algo_id=r2["algo_id"])
    print("oco 已撤销 algo_id=", r2["algo_id"])
