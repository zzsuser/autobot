"""交易引擎 - 核心调度逻辑"""
import time
from datetime import datetime, timedelta
from autobot.config import TradingConfig
from autobot.core.strategy_base import SignalResult
from autobot.core.strategy_registry import strategy_registry
from autobot.core.position_manager import position_manager, PositionInfo
from autobot.data.db_reader import DBReader
from autobot.exchange.okx_trader import OKXTrader
from autobot.utils.logger import logger


class TradingEngine:
    """交易引擎 - 接收任务调度，执行策略并下单"""

    def __init__(self):
        self.db_reader = DBReader()
        self.trader = OKXTrader()

    def execute(self, exchange: str, symbol: str, timeframe: str, method: str) -> tuple:
        """
        执行一次交易任务

        Args:
            exchange: 交易所
            symbol: 交易对 (如 ETH-USDT-SWAP)
            timeframe: 时间周期 (分钟数字符串，如 '5')
            method: 策略名称

        Returns:
            (bool, str): (是否成功, 消息)
        """
        logger.info(f"执行交易任务: {exchange}/{symbol}/{timeframe}min/{method}")

        # 1. 获取策略
        strategy = strategy_registry.get(method)
        if not strategy:
            return False, f"策略 '{method}' 未注册"

        # 2. 获取仓位信息
        pos_info = position_manager.get_position(exchange, symbol, method)
        logger.debug(f"当前仓位: {pos_info.to_dict()}")

        # 3. 判断当前是否在周期边界
        now = datetime.now() - timedelta(hours=8)  # 时区调整
        tf_minutes = int(timeframe)
        is_signal_time = now.minute % tf_minutes == 0

        if not is_signal_time:
            # 非信号时间：仅执行止盈止损
            return self._handle_stop_check(strategy, exchange, symbol, method, pos_info)

        # 4. 信号时间：获取数据并生成信号（传入 timeframe）
        return self._handle_signal(strategy, exchange, symbol, timeframe, method, pos_info)

    def _handle_stop_check(
        self, strategy, exchange, symbol, method, pos_info: PositionInfo
    ) -> tuple:
        """处理止盈止损检查"""
        if not strategy.need_stop_check():
            return False, f"策略 {method} 不需要止盈止损检查"

        if not pos_info.has_position():
            return False, "无仓位，无需检查"

        # 获取当前价格
        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        # 调用策略的止盈止损检查
        result = strategy.check_stop(
            current_position=pos_info.position,
            entry_price=pos_info.entry_price,
            current_price=current_price,
        )

        if result.signal == SignalResult.NO_SIGNAL:
            return False, f"未触发止盈止损: {result.reason}"

        # 执行平仓
        return self._execute_close(exchange, symbol, method, pos_info)

    def _handle_signal(
        self, strategy, exchange, symbol, timeframe, method, pos_info: PositionInfo
    ) -> tuple:
        """处理交易信号"""
        # 1. 从数据库获取数据
        data_length = strategy.required_data_length
        # 将symbol转换为数据库格式 (ETH-USDT-SWAP -> ETHUSDT)
        db_symbol = symbol.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")

        # 时间周期映射
        interval_map = {"1": "1min", "5": "5min", "15": "15min", "60": "1h"}
        db_interval = interval_map.get(timeframe, "5min")

        df = self.db_reader.get_data(db_symbol, db_interval, limit=data_length)
        if df is None or df.empty:
            return False, f"获取数据失败: {db_symbol}/{db_interval}"

        logger.debug(f"获取到 {len(df)} 条数据")

        try:
            signal_result = strategy.generate_signal(
                df=df,
                current_position=pos_info.position,
                entry_index=pos_info.entry_index,
                entry_price=pos_info.entry_price,
            )
            logger.info(f"策略信号: {signal_result}")
        except Exception as e:
            logger.error(f"策略执行异常: {e}")
            return False, f"策略执行异常: {e}"

        return self._execute_signal(exchange, symbol, method, pos_info, signal_result, df)

    def _execute_signal(
        self,
        exchange: str,
        symbol: str,
        method: str,
        pos_info: PositionInfo,
        signal: SignalResult,
        df,
    ) -> tuple:
        """根据信号执行交易"""

        if signal.signal == SignalResult.NO_SIGNAL:
            return False, f"无交易信号: {signal.reason}"

        # 平仓信号
        if signal.signal in (SignalResult.CLOSE_LONG, SignalResult.CLOSE_SHORT):
            return self._execute_close(exchange, symbol, method, pos_info)

        # 开多信号
        if signal.signal == SignalResult.LONG:
            return self._execute_open(exchange, symbol, method, pos_info, "long", df)

        # 开空信号
        if signal.signal == SignalResult.SHORT:
            return self._execute_open(exchange, symbol, method, pos_info, "short", df)

        return False, f"未知信号类型: {signal.signal}"

    def _execute_open(
        self, exchange, symbol, method, pos_info: PositionInfo, direction: str, df
    ) -> tuple:
        """执行开仓"""
        side = "buy" if direction == "long" else "sell"
        pos_side = direction

        # 如果有反向仓位，先平仓
        if pos_info.has_position():
            opposite = (direction == "long" and pos_info.position == -1) or \
                       (direction == "short" and pos_info.position == 1)
            if opposite:
                logger.info(f"反向开仓：先平掉当前 {pos_info.direction} 仓位")
                close_pos_side = "long" if pos_info.position == 1 else "short"
                self.trader.close_position(pos_side=close_pos_side)
                self.trader.update_balance(TradingConfig.DEFAULT_BALANCE)
                position_manager.clear_position(exchange, symbol, method)
                time.sleep(0.5)
            elif (direction == "long" and pos_info.position == 1) or \
                 (direction == "short" and pos_info.position == -1):
                return False, f"已有{direction}仓位，无需操作"

        # 获取当前价格
        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        # 执行开仓
        result = self.trader.open_position(
            side=side,
            pos_side=pos_side,
            current_price=current_price,
            balance=TradingConfig.DEFAULT_BALANCE,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            ratio=TradingConfig.DEFAULT_RATIO,
        )

        if result.get("success"):
            position_val = 1 if direction == "long" else -1
            entry_index = len(df) - 1 if df is not None else None
            position_manager.save_position(
                exchange, symbol, method, position_val, current_price, direction, entry_index
            )
            return True, f"开{direction}成功 @ {current_price}"
        else:
            return False, f"开仓失败: {result.get('message', '未知错误')}"

    def _execute_close(self, exchange, symbol, method, pos_info: PositionInfo) -> tuple:
        """执行平仓"""
        if not pos_info.has_position():
            return False, "无仓位可平"

        pos_side = "long" if pos_info.position == 1 else "short"
        result = self.trader.close_position(pos_side=pos_side)
        self.trader.update_balance(TradingConfig.DEFAULT_BALANCE)
        position_manager.clear_position(exchange, symbol, method)

        return True, f"平{pos_side}成功"

    def force_close(self, exchange: str, symbol: str, method: str) -> tuple:
        """强制清仓（API调用）"""
        pos_info = position_manager.get_position(exchange, symbol, method)
        if not pos_info.has_position():
            return False, "无仓位可平"

        return self._execute_close(exchange, symbol, method, pos_info)


# 全局实例
trading_engine = TradingEngine()
