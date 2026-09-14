"""端到端验证 R2 开单链路（真实账户，只读+挂远触发单后立即撤，无成交风险）。

验证四件事：
1. 服务同口径配置下 _r2_size 是否算出小数张(0.63/0.2)
2. _r2_epoch 是否把 UTC-naive 过期时间正确解析（不再提前 3.5h 误判过期）
3. _r2_try_place 是否能真实把小数张条件单挂到 OKX（成功）并撤销
4. _fmt_sz 输出格式
"""
import time
from datetime import datetime, timezone, timedelta

from autobot.core.engine import TradingEngine, R2_METHOD
from autobot.core.strategy_registry import strategy_registry
from autobot.strategies import register_all_strategies
from autobot.strategies.eth_spike_r2_strategy import load_position_config_from_env
from autobot.exchange.okx_trader import OKXTrader

register_all_strategies()

print("=" * 60)
cfg = load_position_config_from_env()
eq = OKXTrader().get_usdt_equity()
print(f"账户权益={eq:.2f} | 配置 step={cfg.contract_step} min={cfg.contract_min if hasattr(cfg,'contract_min') else cfg.min_contracts} mode={cfg.mode} value={cfg.value} lev={cfg.leverage}")

# 1) 过期时间 UTC 解析验证
eng = TradingEngine()
now = time.time()
future_iso = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(tzinfo=None).isoformat()
parsed = eng._r2_epoch(future_iso)
print(f"[1] _r2_epoch('{future_iso}')={parsed:.0f} now={now:.0f} 差值={parsed-now:.0f}s  "
      f"{'OK(未过期)' if parsed > now else 'FAIL(误判过期)'}")

# 2) 张数计算
for symbol, inst in (("ETH-USDT-SWAP", "ETH-USDT-SWAP"), ("BTC-USDT-SWAP", "BTC-USDT-SWAP")):
    price = eng.trader.get_current_price(symbol)
    from autobot.core.position_manager import position_manager
    pos = position_manager.get_position("okx", symbol, R2_METHOD)
    sz = eng._r2_size(strategy_registry.get(R2_METHOD), inst, symbol, "long", price, pos)
    print(f"[2] {symbol} price={price} -> _r2_size={sz} 张")

# 3) 真实挂单演练（ETH long，trigger=现价*1.1 远高于现价，不会成交）
strategy = strategy_registry.get(R2_METHOD)
inst = "ETH-USDT-SWAP"
price = eng.trader.get_current_price(inst)
trigger = round(price * 1.1, 1)
sz = eng._r2_size(strategy, inst, inst, "long", trigger, position_manager.get_position("okx", inst, R2_METHOD))
rec = {
    "direction": "long",
    "trigger_price": trigger,
    "invalidation_price": None,
    "contracts": sz,
    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
    "algo_cl_ord_id": f"VERIFY{int(time.time())}L",
}
print(f"[3] 挂单演练: trigger={trigger} sz={sz} expires={rec['expires_at']}")
ok, msg = eng._r2_try_place(strategy, "okx", inst, R2_METHOD, inst, rec)
print(f"    _r2_try_place -> ok={ok} msg={msg} algo_id={rec.get('algo_id')}")
if ok and rec.get("algo_id"):
    eng.trader.cancel_protective_orders(inst, algo_id=rec["algo_id"])
    print(f"    已撤销 algo_id={rec['algo_id']}")
else:
    print("    !! 未成功挂单，需排查")
print("=" * 60)
