"""策略基类 - 所有策略必须继承此类"""
from abc import ABC, abstractmethod
import pandas as pd
from typing import Dict, Any, Optional, List


class SignalResult:
    """交易信号结果"""

    LONG = 1       # 开多
    SHORT = -1     # 开空
    CLOSE_LONG = 2   # 平多
    CLOSE_SHORT = -2  # 平空
    NO_SIGNAL = 0  # 无信号

    def __init__(self, signal: int, reason: str = "", details: Optional[Dict] = None):
        self.signal = signal
        self.reason = reason
        self.details = details or {}

    def __repr__(self):
        signal_names = {1: "LONG", -1: "SHORT", 2: "CLOSE_LONG", -2: "CLOSE_SHORT", 0: "NO_SIGNAL"}
        return f"SignalResult(signal={signal_names.get(self.signal, self.signal)}, reason={self.reason})"


class MultiSignalResult:
    """
    多方向信号结果 - 支持同时返回多仓和空仓的信号
    
    用于同时持有多空仓位时，分别生成各自的信号。
    """

    def __init__(
        self,
        long_signal: Optional[SignalResult] = None,
        short_signal: Optional[SignalResult] = None,
    ):
        self.long_signal = long_signal or SignalResult(SignalResult.NO_SIGNAL, "无信号")
        self.short_signal = short_signal or SignalResult(SignalResult.NO_SIGNAL, "无信号")

    @property
    def has_long_action(self) -> bool:
        return self.long_signal.signal != SignalResult.NO_SIGNAL

    @property
    def has_short_action(self) -> bool:
        return self.short_signal.signal != SignalResult.NO_SIGNAL

    def __repr__(self):
        return f"MultiSignalResult(long={self.long_signal}, short={self.short_signal})"


class StrategyBase(ABC):
    """
    策略基类

    所有策略必须实现:
    - name: 策略名称
    - required_data_length: 需要的历史数据条数
    - generate_signal(): 生成交易信号
    
    可选实现:
    - generate_multi_signal(): 同时为多/空仓位生成信号（多空同时持仓时使用）
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称，用于注册和识别"""
        pass

    @property
    @abstractmethod
    def required_data_length(self) -> int:
        """策略需要的最少历史数据条数"""
        pass

    @abstractmethod
    def generate_signal(
        self,
        df: pd.DataFrame,
        current_position: int,
        entry_index: Optional[int] = None,
        **kwargs
    ) -> SignalResult:
        """
        生成交易信号（单方向）

        Args:
            df: 包含K线和指标数据的DataFrame
            current_position: 当前仓位 (0=无, 1=多, -1=空)
            entry_index: 开仓时的数据索引
            **kwargs: 额外参数 (entry_price 等)

        Returns:
            SignalResult: 交易信号
        """
        pass

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
        为多/空仓位分别生成信号（多空同时持仓时使用）
        
        默认实现: 分别调用两次 generate_signal()
        策略子类可以覆盖此方法实现更精细的逻辑。

        Args:
            df: K线数据
            has_long: 是否持有多仓
            has_short: 是否持有空仓
            long_entry_price: 多仓入场价
            long_entry_index: 多仓入场索引
            short_entry_price: 空仓入场价
            short_entry_index: 空仓入场索引

        Returns:
            MultiSignalResult: 包含多仓信号和空仓信号
        """
        long_signal = SignalResult(SignalResult.NO_SIGNAL, "无信号")
        short_signal = SignalResult(SignalResult.NO_SIGNAL, "无信号")

        # 1. 检查多仓的平仓/持仓信号
        if has_long:
            long_signal = self.generate_signal(
                df=df,
                current_position=1,
                entry_index=long_entry_index,
                entry_price=long_entry_price,
            )
            # 只保留平多信号，忽略开仓信号
            if long_signal.signal not in (SignalResult.CLOSE_LONG, SignalResult.NO_SIGNAL):
                long_signal = SignalResult(SignalResult.NO_SIGNAL, "多仓持有中，忽略非平仓信号")

        # 2. 检查空仓的平仓/持仓信号
        if has_short:
            short_signal = self.generate_signal(
                df=df,
                current_position=-1,
                entry_index=short_entry_index,
                entry_price=short_entry_price,
            )
            # 只保留平空信号，忽略开仓信号
            if short_signal.signal not in (SignalResult.CLOSE_SHORT, SignalResult.NO_SIGNAL):
                short_signal = SignalResult(SignalResult.NO_SIGNAL, "空仓持有中，忽略非平仓信号")

        # 3. 无仓位时检查开仓信号
        if not has_long and not has_short:
            # 两边都没有仓位，正常生成信号
            open_signal = self.generate_signal(df=df, current_position=0)
            if open_signal.signal == SignalResult.LONG:
                long_signal = open_signal
            elif open_signal.signal == SignalResult.SHORT:
                short_signal = open_signal

        elif not has_long:
            # 只有空仓，检查是否可以加开多仓
            open_signal = self.generate_signal(df=df, current_position=0)
            if open_signal.signal == SignalResult.LONG:
                long_signal = open_signal

        elif not has_short:
            # 只有多仓，检查是否可以加开空仓
            open_signal = self.generate_signal(df=df, current_position=0)
            if open_signal.signal == SignalResult.SHORT:
                short_signal = open_signal

        return MultiSignalResult(long_signal=long_signal, short_signal=short_signal)

    def need_stop_check(self) -> bool:
        """是否需要止盈止损检查（非信号周期时）"""
        return False

    def check_stop(
        self,
        current_position: int,
        entry_price: float,
        current_price: float,
        **kwargs
    ) -> SignalResult:
        """
        止盈止损检查（默认不实现，子类可覆盖）

        Returns:
            SignalResult: 平仓信号或无信号
        """
        return SignalResult(SignalResult.NO_SIGNAL, "策略未实现止盈止损")
