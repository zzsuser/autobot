"""
策略自动注册

在应用启动时调用 register_all_strategies() 来注册所有可用策略
添加新策略只需:
1. 在 strategies/ 目录下创建新策略文件
2. 继承 StrategyBase
3. 在此文件的 register_all_strategies() 中添加一行注册
"""
from autobot.core.strategy_registry import strategy_registry


def register_all_strategies():
    """注册所有策略到全局注册器"""

    # ===== 主策略: ETH 15分钟四形态策略 R2 (2026-09-03 起替换 R16 作为 ETH 唯一策略) =====
    # R2 执行方式为"交易所驻留条件触发单"：F1/F2/F4 收盘挂 stop-entry，交易所撮合；
    # 引擎只负责失效/超时/反向占用撤单、成交检测、成交后按真实均价挂 TP/SL OCO。
    from autobot.strategies.eth_spike_r2_strategy import EthSpikeR2Strategy
    strategy_registry.register(EthSpikeR2Strategy())

    # ===== 兼容保留: R16 (仅仍挂着 R16 任务的 symbol 使用, 如 BTC-USDT-SWAP; ETH 上已停用) =====
    from autobot.strategies.eth_spike_r16_strategy import EthSpikeR16Strategy
    strategy_registry.register(EthSpikeR16Strategy())

    # ====================== 已停用的旧策略 ======================
    # 以下策略保留文件但不再注册运行。

    # SuperTrend + TEMA 复合策略（从回测代码提取）
    # from autobot.strategies.supertrend_tema_strategy_v2 import SuperTrendTemaStrategy
    # strategy_registry.register(SuperTrendTemaStrategy())

    # v3 - 基于 v2 演进，对称化 L1-L7, is_strong 拆分 long/short 阈值
    # from autobot.strategies.supertrend_tema_strategy_v3 import SuperTrendTemaStrategyV3
    # strategy_registry.register(SuperTrendTemaStrategyV3())

    # ZZS AI预测策略（需要模型文件）
    # try:
    #     from autobot.strategies.zzs_strategy import ZZSStrategy
    #     strategy_registry.register(ZZSStrategy())
    # except ImportError:
    #     pass  # 如果predict模块不可用则跳过

    # ===== 双向对冲 + 爆仓联动平仓策略 =====
    # from autobot.strategies.hedge_liquidation_strategy import HedgeLiquidationStrategy
    # strategy_registry.register(HedgeLiquidationStrategy())
