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

    # SuperTrend + TEMA 复合策略（从回测代码提取）
    from autobot.strategies.supertrend_tema_strategy import SuperTrendTemaStrategy
    strategy_registry.register(SuperTrendTemaStrategy())

    # ZZS AI预测策略（需要模型文件）
    try:
        from autobot.strategies.zzs_strategy import ZZSStrategy
        strategy_registry.register(ZZSStrategy())
    except ImportError:
        pass  # 如果predict模块不可用则跳过

    # ===== 新增: 双向对冲 + 爆仓联动平仓策略 =====
    from autobot.strategies.hedge_liquidation_strategy import HedgeLiquidationStrategy
    strategy_registry.register(HedgeLiquidationStrategy())
