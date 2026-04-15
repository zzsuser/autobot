"""策略注册器 - 管理所有可用策略"""
from typing import Dict, Type, Optional
from autobot.core.strategy_base import StrategyBase
from autobot.utils.logger import logger


class StrategyRegistry:
    """策略注册器，使用单例模式"""

    _instance = None
    _strategies: Dict[str, StrategyBase] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def register(self, strategy: StrategyBase):
        """注册一个策略实例"""
        name = strategy.name
        self._strategies[name] = strategy
        logger.info(f"策略已注册: {name}")

    def unregister(self, name: str) -> bool:
        """注销一个策略"""
        if name in self._strategies:
            del self._strategies[name]
            logger.info(f"策略已注销: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[StrategyBase]:
        """获取策略实例"""
        return self._strategies.get(name)

    def list_strategies(self) -> list:
        """列出所有已注册策略"""
        return [
            {
                "name": s.name,
                "required_data_length": s.required_data_length,
                "need_stop_check": s.need_stop_check(),
            }
            for s in self._strategies.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._strategies


# 全局单例
strategy_registry = StrategyRegistry()
