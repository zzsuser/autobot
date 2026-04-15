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
        """执行一次交易任务"""
        logger.info(f"执行交易任务: {exchange}/{symbol}/{timeframe}min/{method}")

        # 1. 获取策略
        strategy = strategy_registry.get(method)
        if not strategy:
            return False, f"策略 '{method}' 未注册"

        # 2. 获取仓位信息
        pos_info = position_manager.get_position(exchange, symbol, method)
        logger.debug(f"当前仓位: {pos_info.to_dict()}")

        # ===== 新增: 检查是否需要用多方向信号模式 =====
        # 如果策略实现了 generate_multi_signal 且是对冲类策略，使用双向模式
        use_multi = hasattr(strategy, 'generate_multi_signal') and method == "hedge_liquidation"

        if use_multi:
            # 双向模式：先检查爆仓联动，再处理信号
            return self._handle_multi_signal(strategy, exchange, symbol, timeframe, method, pos_info)

        # 3. 原有逻辑: 判断当前是否在周期边界
        now = datetime.now() - timedelta(hours=8)
        tf_minutes = int(timeframe)
        is_signal_time = now.minute % tf_minutes == 0

        if not is_signal_time:
            return self._handle_stop_check(strategy, exchange, symbol, method, pos_info)

        return self._handle_signal(strategy, exchange, symbol, timeframe, method, pos_info)

    # ===== 新增方法: 双向信号处理 =====
    def _handle_multi_signal(
        self, strategy, exchange, symbol, timeframe, method, pos_info: PositionInfo
    ) -> tuple:
        """
        处理双向策略信号（如对冲策略）

        1. 先同步交易所实际仓位，检测爆仓
        2. 调用 generate_multi_signal 获取多空两个方向的信号
        3. 分别执行
        """
        # 1. 检查交易所实际仓位，与本地记录对比，检测爆仓
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")
        exchange_positions = self.trader.get_position_detail(inst_id)

        local_has_long = pos_info.has_long()
        local_has_short = pos_info.has_short()

        # 交易所实际是否有仓位
        exchange_has_long = exchange_positions.get("long") is not None
        exchange_has_short = exchange_positions.get("short") is not None

        # 爆仓检测: 本地有记录但交易所已无仓位
        if local_has_long and not exchange_has_long:
            logger.warning(f"[爆仓检测] 多仓已消失（爆仓）: {symbol}")
            position_manager.mark_liquidated(exchange, symbol, method, "long")
            local_has_long = False

        if local_has_short and not exchange_has_short:
            logger.warning(f"[爆仓检测] 空仓已消失（爆仓）: {symbol}")
            position_manager.mark_liquidated(exchange, symbol, method, "short")
            local_has_short = False

        # 2. ��取K线数据（对冲策略可能不需要，但保持兼容）
        data_length = strategy.required_data_length
        db_symbol = symbol.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")
        interval_map = {"1": "1min", "5": "5min", "15": "15min", "60": "1h"}
        db_interval = interval_map.get(timeframe, "5min")
        df = self.db_reader.get_data(db_symbol, db_interval, limit=data_length)
        if df is None or df.empty:
            # 对冲策略可以不依赖K线，创建一个空DataFrame
            import pandas as pd
            df = pd.DataFrame()

        # 3. 调用双向信号生成
        multi_signal = strategy.generate_multi_signal(
            df=df,
            has_long=local_has_long,
            has_short=local_has_short,
            long_entry_price=pos_info.long.entry_price if local_has_long else 0,
            long_entry_index=pos_info.long.entry_index if local_has_long else None,
            short_entry_price=pos_info.short.entry_price if local_has_short else 0,
            short_entry_index=pos_info.short.entry_index if local_has_short else None,
        )

        logger.info(f"双向信号: {multi_signal}")

        messages = []

        # 4. 执行多仓信号
        if multi_signal.has_long_action:
            sig = multi_signal.long_signal
            if sig.signal == SignalResult.LONG:
                ok, msg = self._execute_open_isolated(exchange, symbol, method, "long")
                messages.append(f"[多仓] {msg}")
            elif sig.signal == SignalResult.CLOSE_LONG:
                ok, msg = self._execute_close_direction(exchange, symbol, method, "long")
                messages.append(f"[平多] {msg}")

        # 5. 执行空仓信号
        if multi_signal.has_short_action:
            sig = multi_signal.short_signal
            if sig.signal == SignalResult.SHORT:
                ok, msg = self._execute_open_isolated(exchange, symbol, method, "short")
                messages.append(f"[空仓] {msg}")
            elif sig.signal == SignalResult.CLOSE_SHORT:
                ok, msg = self._execute_close_direction(exchange, symbol, method, "short")
                messages.append(f"[平空] {msg}")

        if messages:
            return True, " | ".join(messages)
        return False, "双向策略：无操作"

    def _execute_open_isolated(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> tuple:
        """
        逐仓模式开仓（对冲策略专用）
        使用总账户余额的 POSITION_PERCENT（如1%）作为保证金
        """
        side = "buy" if direction == "long" else "sell"
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")

        # 获取账户总余额
        total_balance = self.trader.get_usdt_balance()
        if total_balance <= 0:
            return False, "获取账户余额失败"

        # 获取当前价格
        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        logger.info(
            f"逐仓开仓: direction={direction}, balance={total_balance}, "
            f"percent={TradingConfig.POSITION_PERCENT}, price={current_price}"
        )

        # 执行开仓（逐仓模式）
        result = self.trader.open_position(
            side=side,
            pos_side=direction,
            current_price=current_price,
            balance=total_balance,                   # 传入总余额
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            margin_mode="isolated",                  # 强制逐仓
            position_percent=TradingConfig.POSITION_PERCENT,  # 使用百分比
            inst_id=inst_id,
        )

        if result.get("success"):
            contracts = result.get("contracts", 0)
            position_manager.save_position(
                exchange=exchange,
                symbol=symbol,
                method=method,
                direction=direction,
                price=current_price,
                size=contracts,
                margin_mode="isolated",
                leverage=TradingConfig.DEFAULT_LEVERAGE,
            )
            return True, f"开{direction}成功 @ {current_price}, {contracts}张"
        else:
            return False, f"开{direction}失败: {result.get('message', '未知错误')}"

    def _execute_close_direction(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> tuple:
        """平掉指定方向的仓位"""
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")

        # 获取当前价格用于记录
        current_price = self.trader.get_current_price(symbol)

        result = self.trader.close_position(
            pos_side=direction,
            margin_mode="isolated",
            inst_id=inst_id,
        )

        if result.get("success"):
            position_manager.clear_position(
                exchange, symbol, method,
                direction=direction,
                close_price=current_price,
                reason="爆仓联动平仓"
            )
            return True, f"平{direction}成功 @ {current_price}"
        else:
            # 可能交易所也已无仓位，直接清记录
            position_manager.clear_position(
                exchange, symbol, method,
                direction=direction,
                close_price=current_price,
                reason="平仓失败但清除记录"
            )
            return False, f"平{direction}失败: {result.get('message', '')}"

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
    def force_close(self, exchange: str, symbol: str, method: str, direction: str = None) -> tuple:
        """强制清仓"""
        if direction:
            return self._execute_close_direction(exchange, symbol, method, direction)
        pos_info = position_manager.get_position(exchange, symbol, method)
        if not pos_info.has_position():
            return False, "无仓位可平"
        return self._execute_close(exchange, symbol, method, pos_info)


# 全局实例
trading_engine = TradingEngine()
