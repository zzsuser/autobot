"""
双向对冲 + 爆仓联动平仓策略

逻辑:
1. 无仓位 → 同时开多 + 开空（逐仓，各用账户1%资金）
2. 定期检查 → 发现某方向爆仓 → 平掉另一方向
3. 全部平完 → 回到步骤1，循环执行

适用: BTC-USDT-SWAP 逐仓模式
"""
import pandas as pd
from typing import Optional
from autobot.core.strategy_base import StrategyBase, SignalResult, MultiSignalResult
from autobot.utils.logger import logger


class HedgeLiquidationStrategy(StrategyBase):
    """双向对冲 + 爆仓联动平仓"""

    @property
    def name(self) -> str:
        return "hedge_liquidation"

    @property
    def required_data_length(self) -> int:
        # 本策略不依赖K线指标，只需少量数据即可
        return 10

    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs
    ) -> SignalResult:
        """
        单方向信号（兼容旧引擎调用）
        本策略主要使用 generate_multi_signal，这里做基础兼容
        """
        if current_position == 0:
            # 无仓位，发开多信号（但实际应该走 multi_signal 同时开双向）
            return SignalResult(SignalResult.LONG, "对冲策略：无仓位，开多")
        return SignalResult(SignalResult.NO_SIGNAL, "对冲策略：已有仓位，等待检查")

    def generate_multi_signal(
        self,
        df: pd.DataFrame,
        has_long: bool,
        has_short: bool,
        long_entry_price: float = 0,
        long_entry_index: Optional[int] = None,
        short_entry_price: float = 0,
        short_entry_index: Optional[int] = None,
        **kwargs
    ) -> MultiSignalResult:
        """
        双向信号生成 - 核心逻辑

        场景1: 无仓位 → 同时开多+开空
        场景2: 只剩一方 → 说明另一方爆仓了 → 平掉剩余方
        场景3: 双方都在 → 无操作，继续持有
        """
        long_signal = SignalResult(SignalResult.NO_SIGNAL, "无信号")
        short_signal = SignalResult(SignalResult.NO_SIGNAL, "无信号")

        if not has_long and not has_short:
            # ===== 场景1: 双方都没仓位 → 同时开多+开空 =====
            logger.info("[hedge_liquidation] 无仓位，准备双向开仓")
            long_signal = SignalResult(SignalResult.LONG, "对冲策略：同时开多")
            short_signal = SignalResult(SignalResult.SHORT, "对冲策略：同时开空")

        elif has_long and has_short:
            # ===== 场景3: 双方都有仓位 → 正常持有，不操作 =====
            logger.debug("[hedge_liquidation] 多空均持有，继续观察")

        elif has_long and not has_short:
            # ===== 场景2a: 空仓爆仓了 → 平掉多仓 =====
            logger.warning("[hedge_liquidation] ⚠️ 检测到空仓消失（可能爆仓），平掉多仓")
            long_signal = SignalResult(SignalResult.CLOSE_LONG, "空仓爆仓联动：平多")

        elif has_short and not has_long:
            # ===== 场景2b: 多仓爆仓了 → 平掉空仓 =====
            logger.warning("[hedge_liquidation] ⚠️ 检测到多仓消失（可能爆仓），平掉空仓")
            short_signal = SignalResult(SignalResult.CLOSE_SHORT, "多仓爆仓联动：平空")

        return MultiSignalResult(long_signal=long_signal, short_signal=short_signal)

    def need_stop_check(self) -> bool:
        """
        开启非信号周期检查，
        用于在任意时间点检测爆仓并联动平仓
        """
        return True

    def check_stop(
        self,
        current_position: int,
        entry_price: float,
        current_price: float,
        **kwargs
    ) -> SignalResult:
        """
        止盈止损检查 - 本策略不做传统止盈止损，
        爆仓联动在 generate_multi_signal 中处理
        """
        return SignalResult(SignalResult.NO_SIGNAL, "对冲策略不使用传统止盈止损")
