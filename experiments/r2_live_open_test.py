"""人工实测：按 R2 口径真实开一笔 ETH 仓 + 挂 OCO 保护单（保留，不自动平）。

用途：让用户登录 OKX 查看真实持仓与 TP/SL 挂单。
- 开仓：open_market_size（市价，isolated，100x）
- 保护：engine._r2_establish（挂 TP0.6%/SL0.9% OCO + 登记 position_manager）
"""
from autobot.config import TradingConfig
from autobot.core.engine import TradingEngine, R2_METHOD
from autobot.core.position_manager import position_manager
from autobot.strategies import register_all_strategies
from autobot.strategies.eth_spike_r2_strategy import load_position_config_from_env
from autobot.exchange.okx_trader import OKXTrader
from autobot.core.strategy_registry import strategy_registry

register_all_strategies()
EXCHANGE, SYMBOL, INST = "okx", "ETH-USDT-SWAP", "ETH-USDT-SWAP"

t = OKXTrader()
eng = TradingEngine()
strat = strategy_registry.get(R2_METHOD)

# 0) 安全检查：当前无仓、无挂单
pos0 = position_manager.get_position(EXCHANGE, SYMBOL, R2_METHOD)
d0 = t.get_position_detail(INST)
print("开仓前: 本地仓=", pos0.position, "| 交易所 long=", (d0.get("long") or {}).get("size"))

# 1) 按 R2 口径算张数
price = t.get_current_price(INST)
sz = eng._r2_size(strat, INST, SYMBOL, "long", price, pos0)
print(f"计划: 市价开 long {sz} 张 (现价={price}, 口径 value={strat.position_cfg.value}% lev={strat.position_cfg.leverage}x)")

# 2) 市价开仓
r = t.open_market_size(
    inst_id=INST, side="buy", pos_side="long", sz=sz,
    leverage=int(strat.position_cfg.leverage), margin_mode=TradingConfig.MARGIN_MODE,
)
print("开仓返回:", r)
if not r.get("success"):
    raise SystemExit("开仓失败，终止")

# 3) 读取交易所真实持仓
pd_ = (t.get_position_detail(INST) or {}).get("long") or {}
avg, size = float(pd_["avg_price"]), float(pd_["size"])
print(f"交易所真实持仓: long {size} 张 @ avg={avg}")

# 4) 走 R2 建立仓位路径：挂 OCO 保护单 + 登记
ok, msg = eng._r2_establish(EXCHANGE, SYMBOL, R2_METHOD, "long", avg, size, "人工实测开仓")
print("建立仓位:", ok, msg)

# 5) 展示最终状态
p = position_manager.get_position(EXCHANGE, SYMBOL, R2_METHOD)
print("本地仓位记录:", p.to_dict())
algos = t.get_algo_orders(INST)
for a in algos.get("data", []):
    print("驻留保护单:", {k: a.get(k) for k in ("algoId", "ordType", "sz", "tpTriggerPx", "slTriggerPx", "state")})
print("完成：仓位与保护单已保留，未平仓。")
