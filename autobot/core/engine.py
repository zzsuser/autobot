"""交易引擎 - 核心调度逻辑

【架构修复说明】
- B①: execute() 增加 mode 参数（"signal" / "stop_check"），
       由 Task 调度器明确告知本次调用是信号检查还是止损检查。
       去掉旧的 `is_signal_time = now.minute % tf_minutes == 0 or now.second <= 3` 错误判断，
       连带去掉 WARNING 级别 [时间检查] 日志（C3）。
- B②: 新增 reconcile_with_exchange()，每次 execute 都做对账。
       本地有仓位记录但交易所已无 → mark_liquidated，避免 ghost 仓位卡死策略。
       原 _handle_multi_signal 内的重复对账逻辑被移除，统一走这里。
- C1: _execute_open 调用 save_position 时传入 contracts 作为 size。
- A②: 由于 db_reader 现在返回 DatetimeIndex，策略里的 _last_close_ts 自然变成真实时间戳，
       冷却 / 熔断判断不再永远生效。本文件不改策略，只是说明连锁修复。
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from autobot.config import TradingConfig
from autobot.core.strategy_base import SignalResult
from autobot.core.strategy_registry import strategy_registry
from autobot.core.position_manager import position_manager, PositionInfo
from autobot.cache.redis import redis_client
from autobot.data.db_reader import DBReader
from autobot.exchange.okx_trader import OKXTrader
from autobot.utils.logger import logger, log_trade
from autobot.strategies.eth_spike_r16_strategy import build_protective_levels as r16_build_protective_levels
from autobot.strategies.eth_spike_r2_strategy import (
    build_protective_levels as r2_build_protective_levels,
    calculate_order_size as r2_calc_order_size,
)

# R16 策略方法名（用于引擎识别走保护单执行路径）
R16_METHOD = "eth_spike_r16_15m"
# R2 四形态策略方法名（条件挂单执行路径，2026-09-03 起替换 R16 为唯一启用策略）
R2_METHOD = "eth_spike_fourshape_r2_15m"
# 走"已收盘K线去重 + 交易所驻留保护单"执行路径的策略集合
PROTECTED_METHODS = (R16_METHOD, R2_METHOD)


class TradingEngine:
    """交易引擎 - 接收任务调度，执行策略并下单"""

    def __init__(self):
        self.db_reader = DBReader()
        self.trader = OKXTrader()

    # ==================== 对外入口 ====================

    def execute(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        method: str,
        mode: str = "signal",
    ) -> tuple:
        """
        执行一次交易任务

        Args:
            exchange: 交易所标识（如 "okx"）
            symbol: 合约 ID（如 "ETH-USDT-SWAP"）
            timeframe: K 线周期分钟数（如 "5"）
            method: 策略名（如 "supertrend_tema_v2"）
            mode: "signal" = 完整信号检查（读 K 线 + 策略 + 下单）
                  "stop_check" = 仅止盈止损检查（实时价格触发）

        Returns:
            (success: bool, message: str)
        """
        if mode == "stop_check":
            logger.debug(f"止损检查: {exchange}/{symbol}/{method}")
        else:
            logger.info(f"执行交易任务: {exchange}/{symbol}/{timeframe}min/{method} mode={mode}")

        # 1. 获取策略
        strategy = strategy_registry.get(method)
        if not strategy:
            return False, f"策略 '{method}' 未注册"

        # 2. 每次执行前都与交易所对账（B②）
        #    - 本地有仓但交易所无 → 标记 liquidated（爆仓或被外部平仓）
        #    - 这一步保证策略读到的仓位状态与交易所一致
        self.reconcile_with_exchange(exchange, symbol, method)

        # 3. 重新读取仓位（对账可能改变了状态）
        pos_info = position_manager.get_position(exchange, symbol, method)
        logger.debug(f"当前仓位: {pos_info.to_dict()}")

        # 4. 分支：止损检查模式
        if mode == "stop_check":
            if method == R16_METHOD:
                return self._r16_check_protection(exchange, symbol, method, pos_info)
            if method == R2_METHOD:
                return self._r2_stop_check(exchange, symbol, method, pos_info)
            return self._handle_stop_check(strategy, exchange, symbol, method, pos_info)

        # 5. 分支：信号检查模式
        # 对冲类策略走双向信号路径
        use_multi = hasattr(strategy, "generate_multi_signal") and method == "hedge_liquidation"
        if use_multi:
            return self._handle_multi_signal(strategy, exchange, symbol, timeframe, method, pos_info)

        return self._handle_signal(strategy, exchange, symbol, timeframe, method, pos_info)

    # ==================== B②: 交易所对账 ====================

    def reconcile_with_exchange(self, exchange: str, symbol: str, method: str) -> dict:
        """
        与交易所对账：本地有仓但交易所没仓 → 标记为已平/爆仓。

        R16 策略的退出完全依赖交易所驻留的 TP/SL 保护单，仓位消失通常是保护单触发
        或外部平仓，因此按「正常平仓」记录（而不是误标为爆仓，避免触发爆仓联动）。

        Returns:
            {"long_liquidated": bool, "short_liquidated": bool}
        """
        result = {"long_liquidated": False, "short_liquidated": False}

        local_pos = position_manager.get_position(exchange, symbol, method)
        if not local_pos.has_any_position():
            return result

        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")
        try:
            exchange_pos = self.trader.get_position_detail(inst_id)
        except Exception as e:
            logger.error(f"[对账] 获取交易所仓位失败，跳过: {e}")
            return result

        is_protected = method in PROTECTED_METHODS

        # 多仓对账
        if local_pos.has_long() and exchange_pos.get("long") is None:
            if is_protected:
                close_price = self.trader.get_current_price(symbol)
                position_manager.clear_position(
                    exchange, symbol, method, direction="long",
                    close_price=close_price if close_price > 0 else 0,
                    reason=f"{method}保护单触发/外部平仓",
                )
                log_trade(f"[对账] {method} {symbol} 多仓已平（保护单/外部）", "WARNING")
            else:
                log_trade(
                    f"[风控] 对账: {symbol} 多仓已消失（爆仓或外部平仓） "
                    f"entry={local_pos.long.entry_price}@{local_pos.long.entry_time}",
                    "WARNING",
                )
                position_manager.mark_liquidated(exchange, symbol, method, "long")
                result["long_liquidated"] = True

        # 空仓对账
        if local_pos.has_short() and exchange_pos.get("short") is None:
            if is_protected:
                close_price = self.trader.get_current_price(symbol)
                position_manager.clear_position(
                    exchange, symbol, method, direction="short",
                    close_price=close_price if close_price > 0 else 0,
                    reason=f"{method}保护单触发/外部平仓",
                )
                log_trade(f"[对账] {method} {symbol} 空仓已平（保护单/外部）", "WARNING")
            else:
                log_trade(
                    f"[风控] 对账: {symbol} 空仓已消失（爆仓或外部平仓） "
                    f"entry={local_pos.short.entry_price}@{local_pos.short.entry_time}",
                    "WARNING",
                )
                position_manager.mark_liquidated(exchange, symbol, method, "short")
                result["short_liquidated"] = True

        return result

    # ==================== 信号处理 ====================

    def _handle_signal(
        self, strategy, exchange, symbol, timeframe, method, pos_info: PositionInfo
    ) -> tuple:
        """处理交易信号（单方向策略）"""
        is_protected = method in PROTECTED_METHODS
        data_length = strategy.required_data_length
        db_symbol = symbol.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")
        interval_map = {"1": "1min", "5": "5min", "15": "15min", "60": "1h"}
        db_interval = interval_map.get(timeframe, "5min")

        # 保护单策略会剔除未收盘的最后一根K线，因此多拉 2 根作缓冲，保证过滤后仍有足够收盘K线
        limit = data_length + 2 if is_protected else data_length

        df = self.db_reader.get_data(db_symbol, db_interval, limit=limit)
        if df is None or df.empty:
            return False, f"获取数据失败: {db_symbol}/{db_interval}"

        logger.debug(f"获取到 {len(df)} 条数据")

        if is_protected:
            # 只保留已收盘 K 线：数据库可能已含正在形成的最后一根，必须剔除
            interval_sec = int(timeframe) * 60
            now_ts = int(time.time())
            df = df[df.index.map(
                lambda ts: int(ts.value // 1_000_000_000) + interval_sec <= now_ts
            )]
            if df.empty:
                return False, "无已收盘K线"
            # 过滤后收盘K线不足时先不处理，也不标记去重，等数据补齐后再算
            if len(df) < data_length:
                return False, f"已收盘K线不足: {len(df)}/{data_length}"

        last_bar_key = None
        if is_protected:
            # 跨重启去重：同一根收盘 K 线只处理一次
            # R16 沿用旧前缀避免重启后重复处理当前 bar；R2 使用独立命名空间
            if method == R16_METHOD:
                last_bar_key = f"r16:lastbar:{exchange}:{symbol}:{method}"
            else:
                last_bar_key = f"lastbar:{exchange}:{symbol}:{method}"
            current_bar = str(df.index[-1])
            if redis_client.get(last_bar_key) == current_bar:
                return False, f"{method} 重复K线已处理: {current_bar}"

        # R2 四形态：不走 generate_signal，收盘后把入场意图作为条件挂单交给交易所。
        if method == R2_METHOD:
            # 数据新鲜度看门狗：db_reader 已过滤 confirm=1，最新已收盘 K 线若长时间
            # 未推进说明数据管道(cron/update_latest_data.py)停摆，引擎将永远等不到新 bar。
            try:
                last_ts = int(df.index[-1].value // 1_000_000_000)
                staleness = int(time.time()) - last_ts
                if staleness > max(2 * interval_sec, 3600):
                    logger.error(
                        f"[R2] {symbol} 数据管道停滞: 最新已收盘K线 {current_bar} "
                        f"距今 {staleness}s 未推进，检查 update_latest_data.py/cron"
                    )
            except Exception:
                pass
            try:
                result = self._r2_new_bar(exchange, symbol, method, pos_info, df)
            except Exception as e:
                logger.error(f"R2 策略执行异常: {e}", exc_info=True)
                return False, f"R2 策略执行异常: {e}"
            # 无论是否挂出单，该根K线都视为已处理，防止重复执行
            redis_client.set(last_bar_key, current_bar)
            # 可见性：每根新收盘 bar 的处理结论都留痕（此前无候选时只写 debug，导致
            # "应开仓却没开"无法排障——看不到引擎到底评估了哪根 bar、结论是什么）
            logger.info(f"[R2] {symbol} 处理收盘bar {current_bar}: {result[1]}")
            return result

        try:
            kwargs = {}
            if method == R16_METHOD:
                # R16 只处理已收盘 K 线；open_entries 为当前持仓周期入场批次数
                kwargs["bar_is_closed"] = True
                if pos_info.position == 1:
                    kwargs["open_entries"] = pos_info.long.open_entries
                elif pos_info.position == -1:
                    kwargs["open_entries"] = pos_info.short.open_entries
                else:
                    kwargs["open_entries"] = 0

            signal_result = strategy.generate_signal(
                df=df,
                current_position=pos_info.position,
                entry_index=pos_info.entry_index,
                entry_price=pos_info.entry_price,
                entry_timestamp=(
                    pos_info.long.timestamp if pos_info.position == 1
                    else pos_info.short.timestamp if pos_info.position == -1
                    else 0
                ),
                **kwargs,
            )
            logger.info(f"策略信号: {signal_result}")
            if method == R16_METHOD:
                # 无论有无信号，都标记该根 K 线已处理（对齐策略内 _last_processed_bar）
                redis_client.set(last_bar_key, current_bar)
        except Exception as e:
            logger.error(f"策略执行异常: {e}", exc_info=True)
            return False, f"策略执行异常: {e}"

        if method == R16_METHOD:
            return self._handle_r16_signal(
                strategy, exchange, symbol, method, pos_info, signal_result, df
            )

        return self._execute_signal(exchange, symbol, method, pos_info, signal_result, df)

    # ==================== R16 执行路径（OPEN / ADD / REVERSAL + 保护单） ====================

    @staticmethod
    def _r16_algo_cl_ord_id(df, direction: str, entry_seq: int) -> str:
        """确定性 clientOrderId：同一根 K 线 + 方向 + 入场序号只成功执行一次"""
        try:
            ts = int(df.index[-1].value // 1_000_000_000)
        except Exception:
            ts = int(time.time())
        side_tag = "L" if direction == "long" else "S"
        return f"R16{ts}{side_tag}{entry_seq}"

    def _r16_levels(self, direction: str, avg_px: float, tick: float):
        """计算 TP/SL 并定向取整"""
        side_int = 1 if direction == "long" else -1
        levels = r16_build_protective_levels(side_int, avg_px, tick)
        return levels["take_profit"], levels["stop_loss"]

    def _r16_inst_id(self, symbol: str) -> str:
        return symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")

    def _handle_r16_signal(
        self, strategy, exchange, symbol, method, pos_info: PositionInfo, signal, df
    ) -> tuple:
        """R16 信号执行：按当前持仓 vs 目标方向区分 OPEN / ADD / REVERSAL"""
        if signal.signal == SignalResult.NO_SIGNAL:
            return False, f"无交易信号: {signal.reason}"

        target_side = 1 if signal.signal == SignalResult.LONG else -1
        target_direction = "long" if target_side == 1 else "short"
        cur = pos_info.position

        if cur == 0:
            return self._r16_open(exchange, symbol, method, target_direction, df)
        if cur == target_side:
            return self._r16_add(exchange, symbol, method, target_direction, pos_info, df)
        return self._r16_reverse(exchange, symbol, method, target_direction, pos_info, df)

    def _r16_open(self, exchange, symbol, method, direction, df) -> tuple:
        """开仓第 1 笔 + 挂 TP/SL 保护单"""
        inst_id = self._r16_inst_id(symbol)
        side = "buy" if direction == "long" else "sell"

        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        live_balance = self.trader.get_usdt_balance()
        if live_balance <= 0:
            log_trade(f"[R16] {symbol} 开仓失败: 账户USDT余额不足", "ERROR")
            return False, "账户余额不足"

        result = self.trader.open_position(
            side=side,
            pos_side=direction,
            current_price=current_price,
            balance=live_balance,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            ratio=TradingConfig.DEFAULT_RATIO,
            margin_mode=TradingConfig.MARGIN_MODE,
            inst_id=inst_id,
        )
        if not result.get("success"):
            return False, f"开仓失败: {result.get('message')}"

        # 读交易所真实持仓均价与数量（TP/SL 以 avgPx 为准，而非信号K线收盘价）
        detail = self.trader.get_position_detail(inst_id)
        pos_detail = detail.get(direction)
        avg_px = float(pos_detail.get("avg_price") or 0) if pos_detail else 0.0
        size = float(pos_detail.get("size") or 0) if pos_detail else 0.0
        if avg_px <= 0:
            avg_px = current_price
        if size <= 0:
            size = float(result.get("contracts", 0))

        tick = self.trader.get_tick_size(inst_id)
        tp, sl = self._r16_levels(direction, avg_px, tick)

        algo_cl_ord_id = self._r16_algo_cl_ord_id(df, direction, 1)
        algo = self.trader.place_protective_orders(
            inst_id, direction, size, tp, sl,
            margin_mode=TradingConfig.MARGIN_MODE,
            algo_cl_ord_id=algo_cl_ord_id,
        )
        if not algo.get("success"):
            log_trade(f"[R16] 保护单挂单失败，立即平仓兜底: {algo.get('message')}", "ERROR")
            self.trader.close_position(
                pos_side=direction, margin_mode=TradingConfig.MARGIN_MODE, inst_id=inst_id
            )
            position_manager.clear_position(
                exchange, symbol, method, direction=direction, reason="保护单失败平仓"
            )
            return False, f"保护单挂单失败，已平仓: {algo.get('message')}"

        position_manager.save_position(
            exchange=exchange, symbol=symbol, method=method, direction=direction,
            price=avg_px, size=size, margin_mode=TradingConfig.MARGIN_MODE,
            leverage=TradingConfig.DEFAULT_LEVERAGE, open_entries=1,
            algo_order_id=algo.get("algo_id", ""),
            last_processed_bar=str(df.index[-1]),
        )
        log_trade(
            f"[R16] 开{direction} @ {avg_px} {size}张 TP={tp} SL={sl} algo={algo.get('algo_id')}"
        )
        return True, f"开{direction}成功 @ {avg_px} {size}张 TP={tp} SL={sl}"

    def _r16_add(self, exchange, symbol, method, direction, pos_info: PositionInfo, df) -> tuple:
        """同向加仓（第 2/3 次）+ 修改保护单"""
        single = pos_info.get_direction(direction)
        if single.open_entries >= TradingConfig.MAX_SAME_DIRECTION_ENTRIES:
            return False, f"已达最大同向入场次数({TradingConfig.MAX_SAME_DIRECTION_ENTRIES})"

        inst_id = self._r16_inst_id(symbol)
        side = "buy" if direction == "long" else "sell"

        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        live_balance = self.trader.get_usdt_balance()
        if live_balance <= 0:
            return False, "账户余额不足"

        result = self.trader.open_position(
            side=side,
            pos_side=direction,
            current_price=current_price,
            balance=live_balance,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            ratio=TradingConfig.DEFAULT_RATIO,
            margin_mode=TradingConfig.MARGIN_MODE,
            inst_id=inst_id,
        )
        if not result.get("success"):
            return False, f"加仓失败: {result.get('message')}"

        detail = self.trader.get_position_detail(inst_id)
        pos_detail = detail.get(direction)
        if not pos_detail:
            return False, "加仓后未读到交易所持仓"
        avg_px = float(pos_detail.get("avg_price") or 0)
        size = float(pos_detail.get("size") or 0)
        if avg_px <= 0 or size <= 0:
            return False, "加仓后持仓均价/数量异常"

        tick = self.trader.get_tick_size(inst_id)
        tp, sl = self._r16_levels(direction, avg_px, tick)
        new_entries = single.open_entries + 1

        amend = self.trader.amend_protective_orders(inst_id, single.algo_order_id, size, tp, sl)
        if not amend.get("success"):
            log_trade(f"[R16] 改保护单失败，平仓兜底: {amend.get('message')}", "ERROR")
            self.trader.close_position(
                pos_side=direction, margin_mode=TradingConfig.MARGIN_MODE, inst_id=inst_id
            )
            position_manager.clear_position(
                exchange, symbol, method, direction=direction, reason="改保护单失败平仓"
            )
            return False, f"改保护单失败，已平仓: {amend.get('message')}"

        position_manager.update_position(
            exchange, symbol, method, direction,
            entry_price=avg_px, size=size, open_entries=new_entries,
            last_processed_bar=str(df.index[-1]),
        )
        log_trade(
            f"[R16] 加{direction} 第{new_entries}次 @ {avg_px} 总{size}张 TP={tp} SL={sl}"
        )
        return True, f"加{direction} 第{new_entries}次成功 @ {avg_px} 总{size}张"

    def _r16_reverse(
        self, exchange, symbol, method, target_direction, pos_info: PositionInfo, df
    ) -> tuple:
        """反手：撤保护单 → reduce-only 平旧仓 → 确认归零 → 开反向第 1 笔"""
        old_direction = "long" if pos_info.position == 1 else "short"
        old_single = pos_info.get_direction(old_direction)
        inst_id = self._r16_inst_id(symbol)

        # 1. 撤销旧仓保护单
        if old_single.algo_order_id:
            cancel = self.trader.cancel_protective_orders(inst_id, algo_id=old_single.algo_order_id)
            if not cancel.get("success"):
                log_trade(f"[R16] 撤保护单失败但仍继续反手: {cancel.get('message')}", "WARNING")

        # 2. 平掉旧仓
        self.trader.close_position(
            pos_side=old_direction, margin_mode=TradingConfig.MARGIN_MODE, inst_id=inst_id
        )

        # 3. 确认旧仓归零（未归零不得开反向）
        detail = self.trader.get_position_detail(inst_id)
        remaining = detail.get(old_direction)
        if remaining and float(remaining.get("size") or 0) > 0:
            position_manager.clear_position(
                exchange, symbol, method, direction=old_direction, reason="反手失败旧仓未归零"
            )
            return False, "反手失败: 旧仓未确认归零"

        # 4. 清旧仓本地记录
        position_manager.clear_position(
            exchange, symbol, method, direction=old_direction, reason="反手平旧仓"
        )

        # 5. 开反向新仓
        return self._r16_open(exchange, symbol, method, target_direction, df)

    def _r16_check_protection(
        self, exchange, symbol, method, pos_info: PositionInfo
    ) -> tuple:
        """R16 保护单健康巡检：有仓但保护单缺失/数量不足时告警并补挂"""
        inst_id = self._r16_inst_id(symbol)
        checked = 0
        for direction in ("long", "short"):
            single = pos_info.get_direction(direction)
            if not single.has_position:
                continue
            checked += 1

            detail = self.trader.get_position_detail(inst_id)
            pos_detail = detail.get(direction)
            if not pos_detail:
                continue  # 交易所已无仓，交给 reconcile 处理

            avg_px = float(pos_detail.get("avg_price") or 0)
            size = float(pos_detail.get("size") or 0)
            if avg_px <= 0 or size <= 0:
                continue

            algos = self.trader.get_algo_orders(inst_id)
            covered = False
            if algos.get("success"):
                for a in algos.get("data", []):
                    if a.get("algoId") == single.algo_order_id:
                        covered = True
                        break

            if covered:
                continue

            log_trade(
                f"[R16] 保护单缺失，补挂: {symbol} {direction} sz={size} avg={avg_px}",
                "WARNING",
            )
            tick = self.trader.get_tick_size(inst_id)
            tp, sl = self._r16_levels(direction, avg_px, tick)
            algo = self.trader.place_protective_orders(
                inst_id, direction, size, tp, sl,
                margin_mode=TradingConfig.MARGIN_MODE,
            )
            if algo.get("success"):
                position_manager.update_position(
                    exchange, symbol, method, direction,
                    algo_order_id=algo.get("algo_id", ""),
                )
            else:
                log_trade(
                    f"[R16] 补挂保护单失败: {algo.get('message')}", "ERROR"
                )

        if checked == 0:
            return False, "无仓位，无需保护巡检"
        return True, f"保护巡检完成: 检查{checked}个方向"

    # ==================== R2 四形态执行路径（条件挂单 + 失效撤单 + 成交后保护单） ====================
    # 执行语义：R2 候选在 15m 收盘产生。F1/F2/F4 是价格触发入场 → 直接在交易所挂
    # conditional stop-entry 驻留单（交易所撮合真实逐笔，引擎不做1分钟判断）；引擎只做
    # (a) 失效位触及/超时/反向占用 → 撤单 (b) 检测条件单成交 → 按真实均价挂 TP/SL OCO。
    # F3 是"下一开盘"入场 → 于收盘处理时机用现价≈开盘价做距离检查后市价入场。

    def _r2_intents_key(self, exchange: str, symbol: str, method: str) -> str:
        return f"r2:intents:{exchange}:{symbol}:{method}"

    def _r2_load_intents(self, key: str) -> list:
        raw = redis_client.get(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _r2_save_intents(self, key: str, intents: list) -> None:
        redis_client.set(key, json.dumps(intents))

    @staticmethod
    def _r2_dir(side) -> str:
        return "long" if int(side) == 1 else "short"

    @staticmethod
    def _r2_epoch(iso) -> float:
        """把 UTC-naive ISO 时间串按 UTC 解析为 epoch 秒。

        注意：意图的 created_at/expires_at 来自 df.index（db_reader 用 unit='ms'
        得到的是 UTC-naive），若直接 .timestamp() 会按本机时区(+0330)解释，使
        expires 提前 3.5 小时 → 意图一生成就被误判"已过期"、monitor 提前撤单。
        """
        try:
            dt = datetime.fromisoformat(str(iso))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _r2_cl_ord_id(df, shape: str, side: int) -> str:
        """确定性 clientOrderId：同一根收盘K线 + 形态 + 方向 只成功挂一次"""
        try:
            ts = int(df.index[-1].value // 1_000_000_000)
        except Exception:
            ts = int(time.time())
        tag = "L" if int(side) == 1 else "S"
        return f"R2{ts}{shape}{tag}"

    def _r2_gate_reason(self, strategy, pos_info: PositionInfo, direction: str) -> str:
        """组合层约束（R2 语义）：无反手；同向入场次数受策略上限约束。空串=允许。"""
        cfg = strategy.position_cfg
        if direction == "long":
            if pos_info.has_short():
                return "持反向空仓(无反手)"
            if pos_info.has_long() and pos_info.long.open_entries >= cfg.max_same_direction_entries:
                return f"同向已达上限({cfg.max_same_direction_entries}次)"
        else:
            if pos_info.has_long():
                return "持反向多仓(无反手)"
            if pos_info.has_short() and pos_info.short.open_entries >= cfg.max_same_direction_entries:
                return f"同向已达上限({cfg.max_same_direction_entries}次)"
        return ""

    def _r2_size(self, strategy, inst_id: str, symbol: str, direction: str,
                 order_price: float, pos_info: PositionInfo) -> int:
        """R2 资金口径计算可下单张数（equity 保证金百分比 + 名义上限 + step/min）。"""
        import dataclasses
        cfg = strategy.position_cfg
        equity = self.trader.get_usdt_equity()
        if equity <= 0 or order_price <= 0:
            return 0
        try:
            real_ctval = float(self.trader.get_contract_value(inst_id) or 0)
        except Exception:
            real_ctval = 0.0
        calc_cfg = cfg
        if real_ctval > 0 and abs(real_ctval - cfg.contract_value) > 1e-9:
            log_trade(
                f"[R2] {symbol} 策略面值cfg({cfg.contract_value})!=交易所({real_ctval})，"
                f"按交易所面值计算张数", "WARNING",
            )
            calc_cfg = dataclasses.replace(cfg, contract_value=real_ctval)
        price = self.trader.get_current_price(symbol)
        if price <= 0:
            price = order_price
        single = pos_info.get_direction(direction)
        cur_notional = 0.0
        if single.has_position and single.size > 0:
            cur_notional = float(single.size) * (real_ctval or cfg.contract_value) * price
        size = r2_calc_order_size(equity, order_price, cur_notional, cfg=calc_cfg)
        if size.contracts > 0:
            # 兜底提示：若实际名义明显超出策略目标名义（小资金只能开最小整张），告知用户超配
            req_notional = equity * cfg.value / 100.0 * cfg.leverage
            if size.notional > req_notional * 1.05 + 1e-9:
                log_trade(
                    f"[R2] {symbol} 名义超配提示: 目标名义≈{req_notional:.0f}USDT但最小整张"
                    f"名义≈{size.notional:.0f}USDT(实际{size.contracts}张, 保证金≈"
                    f"{size.notional/cfg.leverage:.2f}USDT)", "WARNING",
                )
        return round(size.contracts, 2)

    def _r2_try_place(self, strategy, exchange, symbol, method, inst_id, rec: dict) -> tuple:
        """给 pending 意图挂条件开仓单；成功返回 (True, msg)，rec 被原地更新 algo_id/state。"""
        direction = rec["direction"]
        side = "buy" if direction == "long" else "sell"
        trigger = float(rec["trigger_price"])
        sz = round(float(rec["contracts"]), 2)
        if sz <= 0:
            return False, "张数为0"
        if self._r2_epoch(rec["expires_at"]) <= time.time():
            return False, "意图已过期"
        price = self.trader.get_current_price(symbol)
        if price > 0:
            inv = rec.get("invalidation_price")
            if inv and direction == "long" and price <= float(inv):
                return False, "价格已触及失效位"
            if inv and direction == "short" and price >= float(inv):
                return False, "价格已触及失效位"
            # OKX conditional 校验: buy 触发须高于现价、sell 须低于现价，已越过则无法挂
            if direction == "long" and price >= trigger:
                return False, "现价已越过向上触发价，不挂"
            if direction == "short" and price <= trigger:
                return False, "现价已越过向下触发价，不挂"
        result = self.trader.place_stop_entry_order(
            inst_id=inst_id, side=side, pos_side=direction, sz=sz,
            trigger_price=trigger, margin_mode=TradingConfig.MARGIN_MODE,
            algo_cl_ord_id=rec.get("algo_cl_ord_id", ""),
        )
        if not result.get("success"):
            return False, f"挂单失败: {result.get('message')}"
        rec["algo_id"] = result.get("algo_id", "")
        rec["state"] = "live"
        rec["placed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return True, f"已挂条件单 trigger={trigger} {sz}张"

    def _r2_place_protection(self, exchange, symbol, method, direction, size, avg_px, old_algo_id: str) -> tuple:
        """挂或改保护单；返回 (ok, algo_id)。旧单不存在则新建。"""
        inst_id = self._r16_inst_id(symbol)
        tick = self.trader.get_tick_size(inst_id)
        side_int = 1 if direction == "long" else -1
        levels = r2_build_protective_levels(side_int, avg_px, tick)
        tp, sl = levels["take_profit"], levels["stop_loss"]

        if old_algo_id:
            amend = self.trader.amend_protective_orders(inst_id, old_algo_id, size, tp, sl)
            if amend.get("success"):
                return True, old_algo_id
            log_trade(f"[R2] 改保护单失败，尝试重挂: {amend.get('message')}", "WARNING")
        algo = self.trader.place_protective_orders(
            inst_id, direction, size, tp, sl,
            margin_mode=TradingConfig.MARGIN_MODE,
        )
        if algo.get("success"):
            return True, algo.get("algo_id", "")
        log_trade(f"[R2] 保护单挂单失败: {algo.get('message')}", "ERROR")
        return False, ""

    def _r2_close_all_fallback(self, exchange, symbol, method, direction, reason: str) -> tuple:
        """保护单失败兜底：市价平仓并清本地记录。"""
        inst_id = self._r16_inst_id(symbol)
        self.trader.close_position(
            pos_side=direction, margin_mode=TradingConfig.MARGIN_MODE, inst_id=inst_id
        )
        position_manager.clear_position(exchange, symbol, method, direction=direction, reason=reason)
        return False, f"{direction} {reason}"

    def _r2_establish(self, exchange, symbol, method, direction, avg_px, size, reason: str) -> tuple:
        """条件单成交且本地无仓 → 建仓 + 挂保护单（open_entries=1）。"""
        ok, algo_id = self._r2_place_protection(exchange, symbol, method, direction, size, avg_px, "")
        if not ok:
            return self._r2_close_all_fallback(exchange, symbol, method, direction, "保护单失败平仓")
        strategy = strategy_registry.get(method)
        position_manager.save_position(
            exchange=exchange, symbol=symbol, method=method, direction=direction,
            price=avg_px, size=size, margin_mode=TradingConfig.MARGIN_MODE,
            leverage=int(strategy.position_cfg.leverage), open_entries=1,
            algo_order_id=algo_id,
        )
        log_trade(f"[R2] {reason}: 开{direction} @ {avg_px} {size}张 algo={algo_id}")
        return True, f"{reason} 开{direction} @ {avg_px} {size}张"

    def _r2_add(self, exchange, symbol, method, direction, avg_px, size, cur_entries: int, reason: str) -> tuple:
        """同向追加成交 → 更新均价/张数/open_entries+1 + 改保护单。"""
        strategy = strategy_registry.get(method)
        cfg = strategy.position_cfg
        new_entries = min(cur_entries + 1, cfg.max_same_direction_entries)
        single = position_manager.get_position(exchange, symbol, method).get_direction(direction)
        ok, algo_id = self._r2_place_protection(
            exchange, symbol, method, direction, size, avg_px, single.algo_order_id or ""
        )
        if not ok:
            return self._r2_close_all_fallback(exchange, symbol, method, direction, "改保护单失败平仓")
        position_manager.update_position(
            exchange, symbol, method, direction,
            entry_price=avg_px, size=size, open_entries=new_entries,
            algo_order_id=algo_id,
        )
        log_trade(f"[R2] {reason}: 加{direction} 第{new_entries}次 @ {avg_px} 总{size}张 algo={algo_id}")
        return True, f"{reason} 加{direction}第{new_entries}次 @ {avg_px} 总{size}张"

    def _r2_market_enter(self, exchange, symbol, method, direction, sz: int, reason: str) -> tuple:
        """F3 下一开盘 / 兜底：按指定张数市价入场，然后同步真实持仓与保护单。"""
        inst_id = self._r16_inst_id(symbol)
        side = "buy" if direction == "long" else "sell"
        strategy = strategy_registry.get(method)
        result = self.trader.open_market_size(
            inst_id=inst_id, side=side, pos_side=direction, sz=sz,
            leverage=int(strategy.position_cfg.leverage),
            margin_mode=TradingConfig.MARGIN_MODE,
        )
        if not result.get("success"):
            return False, f"市价开仓失败: {result.get('message')}"
        detail = self.trader.get_position_detail(inst_id)
        pd_ = detail.get(direction)
        if not pd_:
            return False, f"开仓后未读到{direction}持仓"
        avg_px = float(pd_.get("avg_price") or 0)
        size = float(pd_.get("size") or 0)
        if avg_px <= 0 or size <= 0:
            return False, "开仓后持仓均价/张数异常"
        local = position_manager.get_position(exchange, symbol, method).get_direction(direction)
        if local.has_position:
            return self._r2_add(exchange, symbol, method, direction, avg_px, size,
                                 local.open_entries, reason)
        return self._r2_establish(exchange, symbol, method, direction, avg_px, size, reason)

    def _r2_on_conditional_gone(self, exchange, symbol, method, it: dict) -> tuple:
        """监测到条件单从交易所消失（触发成交或外部撤单）→ 与交易所持仓对账。"""
        direction = it["direction"]
        inst_id = self._r16_inst_id(symbol)
        detail = self.trader.get_position_detail(inst_id)
        pd_ = detail.get(direction)
        ex_size = float(pd_.get("size") or 0) if pd_ else 0.0
        avg_px = float(pd_.get("avg_price") or 0) if pd_ else 0.0
        if ex_size <= 0 or avg_px <= 0:
            log_trade(
                f"[R2] 条件单消失但无对应交易所仓位({it['shape']}/{direction})，"
                f"视为未成交/外部撤单", "WARNING",
            )
            return False, "条件单消失且无仓位"
        local = position_manager.get_position(exchange, symbol, method).get_direction(direction)
        if not local.has_position:
            return self._r2_establish(exchange, symbol, method, direction, avg_px, ex_size,
                                      f"{it['shape']}条件单成交")
        if ex_size > local.size + 1e-9:
            return self._r2_add(exchange, symbol, method, direction, avg_px, ex_size,
                                local.open_entries, f"{it['shape']}条件单成交(加仓)")
        return True, f"{it['shape']}条件单消失，仓位无变化(仅校验保护单)"

    def _r2_monitor_intents(self, exchange, symbol, method) -> tuple:
        """巡检：撤失效/超时/反向占用的条件单；检测成交后同步持仓并上保护单。"""
        key = self._r2_intents_key(exchange, symbol, method)
        intents = self._r2_load_intents(key)
        if not intents:
            return False, "无R2挂单意图"
        strategy = strategy_registry.get(method)
        inst_id = self._r16_inst_id(symbol)
        now = time.time()
        price = self.trader.get_current_price(symbol)
        pos_info = position_manager.get_position(exchange, symbol, method)
        cond = self.trader.get_algo_orders(inst_id, ord_type="conditional")
        live_ids = {}
        if cond.get("success"):
            live_ids = {a.get("algoId"): a for a in cond.get("data", [])}

        keep = []
        msgs = []
        kept_ids = set()
        for it in intents:
            shape = it.get("shape", "?")
            direction = it.get("direction", "")
            algo_id = it.get("algo_id", "")
            # 1) 组合约束（反向占用 / 同向已达上限）→ 撤单
            reason = self._r2_gate_reason(strategy, pos_info, direction)
            if reason:
                if algo_id:
                    self.trader.cancel_protective_orders(inst_id, algo_id=algo_id)
                    msgs.append(f"{shape}/{direction} 撤单({reason})")
                continue
            # 2) 超时 → 撤单
            if self._r2_epoch(it.get("expires_at", "")) <= now:
                if algo_id:
                    self.trader.cancel_protective_orders(inst_id, algo_id=algo_id)
                msgs.append(f"{shape}/{direction} 超时撤单")
                continue
            # 3) 待挂单（上次挂失败）→ 重试一次
            if not algo_id:
                ok, msg = self._r2_try_place(strategy, exchange, symbol, method, inst_id, it)
                if ok:
                    msgs.append(f"{shape}/{direction} 补挂成功")
                    keep.append(it)
                else:
                    msgs.append(f"{shape}/{direction} 补挂失败移除({msg})")
                continue
            # 4) 已挂单：检查是否仍驻留
            if algo_id in live_ids:
                inv = it.get("invalidation_price")
                if inv and price > 0:
                    invalid = (
                        (direction == "long" and price <= float(inv))
                        or (direction == "short" and price >= float(inv))
                    )
                    if invalid:
                        self.trader.cancel_protective_orders(inst_id, algo_id=algo_id)
                        msgs.append(f"{shape}/{direction} 失效位触及撤单")
                        continue
                keep.append(it)  # 仍有效等待触发
                kept_ids.add(algo_id)
            else:
                # 订单已消失：成交 or 外部撤 → 与交易所对账
                ok, msg = self._r2_on_conditional_gone(exchange, symbol, method, it)
                msgs.append(f"{shape}/{direction} 订单消失: {msg}")
                # 成交已同步仓位，意图消费掉（不留存）

        # 安全网：清理属于 R2 引擎、但不在当前意图集合里的孤儿条件单
        # （引擎重启/Redis丢失/异常残留 → 防止远期无人管理的触发单误开仓）
        for a in live_ids.values():
            if a.get("algoClOrdId", "").startswith("R2") and a.get("algoId") not in kept_ids:
                self.trader.cancel_protective_orders(inst_id, algo_id=a.get("algoId"))
                msgs.append(f"孤儿条件单清理({a.get('algoClOrdId')})")

        self._r2_save_intents(key, keep)
        if not msgs:
            msgs.append("无动作")
        return True, "R2意图巡检: " + " | ".join(msgs)

    def _r2_check_protection(self, exchange, symbol, method, pos_info: PositionInfo) -> tuple:
        """保护单健康巡检：有仓但 OCO 缺失/数量不足时告警并补挂（同 R16 语义）。"""
        inst_id = self._r16_inst_id(symbol)
        checked = 0
        for direction in ("long", "short"):
            single = pos_info.get_direction(direction)
            if not single.has_position:
                continue
            checked += 1
            detail = self.trader.get_position_detail(inst_id)
            pd_ = detail.get(direction)
            if not pd_:
                continue  # 交易所已无仓，交给 reconcile
            avg_px = float(pd_.get("avg_price") or 0)
            size = float(pd_.get("size") or 0)
            if avg_px <= 0 or size <= 0:
                continue
            algos = self.trader.get_algo_orders(inst_id)
            covered = False
            if algos.get("success"):
                for a in algos.get("data", []):
                    if a.get("algoId") == single.algo_order_id:
                        covered = True
                        break
            if covered:
                continue
            log_trade(
                f"[R2] 保护单缺失，补挂: {symbol} {direction} sz={size} avg={avg_px}", "WARNING",
            )
            ok, algo_id = self._r2_place_protection(exchange, symbol, method, direction, size, avg_px, "")
            if ok:
                position_manager.update_position(
                    exchange, symbol, method, direction, algo_order_id=algo_id,
                )
            else:
                log_trade(f"[R2] 补挂保护单失败: {direction}", "ERROR")
        if checked == 0:
            return False, "无仓位，无需R2保护巡检"
        return True, f"R2保护巡检完成: 检查{checked}个方向"

    def _r2_new_bar(self, exchange, symbol, method, pos_info: PositionInfo, df) -> tuple:
        """R2 收盘处理：产生 F1/F2/F4 条件挂单意图；F3 下一开盘距离内市价入场。"""
        strategy = strategy_registry.get(method)
        key = self._r2_intents_key(exchange, symbol, method)
        existing = self._r2_load_intents(key)
        inst_id = self._r16_inst_id(symbol)
        msgs = []
        changed = False
        seen = {(it.get("shape"), it.get("signal_bar_start")) for it in existing}

        # ---- F1/F2/F4: 挂条件触发单 ----
        for it in strategy.pending_entry_intents(df):
            direction = self._r2_dir(it.side)
            sig_key = str(it.signal_bar_start.isoformat())
            if (it.shape, sig_key) in seen:
                continue
            reason = self._r2_gate_reason(strategy, pos_info, direction)
            if reason:
                log_trade(f"[R2] {symbol} {it.shape}/{direction} 意图忽略: {reason}")
                continue
            trigger = float(it.trigger_price)
            sig_close = float(it.signal_close)
            dist = abs(trigger - sig_close) / sig_close * 100 if sig_close > 0 else 999.0
            if dist > float(it.max_distance_pct):
                log_trade(
                    f"[R2] {symbol} {it.shape}/{direction} 触发距离过远忽略: "
                    f"{dist:.3f}% > {it.max_distance_pct}%"
                )
                continue
            sz = self._r2_size(strategy, inst_id, symbol, direction, trigger, pos_info)
            if sz <= 0:
                eq = self.trader.get_usdt_equity()
                if eq <= 0:
                    log_trade(
                        f"[R2] {symbol} {it.shape}/{direction} 张数为0: "
                        f"equity获取失败/为0(eq={eq})，检查OKX账户与API权限", "ERROR",
                    )
                else:
                    notional = eq * strategy.position_cfg.value / 100.0 * strategy.position_cfg.leverage
                    ctv = float(self.trader.get_contract_value(inst_id) or strategy.position_cfg.contract_value)
                    per = float(it.trigger_price) * ctv
                    log_trade(
                        f"[R2] {symbol} {it.shape}/{direction} 张数为0(资金不足): "
                        f"equity={eq:.2f} 该口径名义≈{notional:.1f}USDT，"
                        f"需≥1张(≈{per:.0f}USDT/张，min_contracts=1)。"
                        f"账户加金或调整口径才能开仓", "WARNING",
                    )
                continue
            rec = {
                "kind": "pending", "shape": it.shape, "side": int(it.side), "direction": direction,
                "signal_bar_start": sig_key,
                "created_at": str(pd.Timestamp(it.created_at).isoformat()),
                "expires_at": str(pd.Timestamp(it.expires_at).isoformat()),
                "trigger_price": trigger,
                "invalidation_price": (
                    float(it.invalidation_price) if it.invalidation_price is not None else None
                ),
                "signal_close": sig_close,
                "max_distance_pct": float(it.max_distance_pct),
                "contracts": sz,
                "algo_id": "", "algo_cl_ord_id": self._r2_cl_ord_id(df, it.shape, it.side),
                "state": "pending_place", "placed_at": "",
            }
            ok, msg = self._r2_try_place(strategy, exchange, symbol, method, inst_id, rec)
            existing.append(rec)
            changed = True
            msgs.append(f"{it.shape}/{direction}:{msg}")

        # ---- F3: 下一开盘（此刻价≈开盘）距离内即市价入场 ----
        for it in strategy.next_open_entry_intents(df):
            direction = self._r2_dir(it.side)
            if self._r2_gate_reason(strategy, pos_info, direction):
                continue
            sig_close = float(it.signal_close)
            cur = self.trader.get_current_price(symbol)
            if cur <= 0:
                msgs.append(f"{it.shape}/{direction}:现价获取失败跳过")
                continue
            dist = abs(cur - sig_close) / sig_close * 100 if sig_close > 0 else 999.0
            if dist > float(it.max_distance_pct):
                msgs.append(f"{it.shape}/{direction}:开盘距离过远不入场({dist:.3f}%)")
                continue
            sz = self._r2_size(strategy, inst_id, symbol, direction, cur, pos_info)
            if sz <= 0:
                eq = self.trader.get_usdt_equity()
                if eq <= 0:
                    msgs.append(f"{it.shape}/{direction}:张数为0(equity={eq})")
                else:
                    notional = eq * strategy.position_cfg.value / 100.0 * strategy.position_cfg.leverage
                    ctv = float(self.trader.get_contract_value(inst_id) or strategy.position_cfg.contract_value)
                    per = cur * ctv
                    msgs.append(f"{it.shape}/{direction}:张数为0(资金不足 eq={eq:.2f} "
                                f"名义≈{notional:.1f}<1张≈{per:.0f}USDT)")
                continue
            ok, msg = self._r2_market_enter(exchange, symbol, method, direction, sz, f"{it.shape}下一开盘")
            msgs.append(f"{it.shape}/{direction}:{msg}")

        if changed:
            self._r2_save_intents(key, existing)
        if not msgs:
            return False, "R2 本根收盘K线无入场意图"
        return True, "R2收盘处理: " + " | ".join(msgs)

    def _r2_stop_check(self, exchange, symbol, method, pos_info: PositionInfo) -> tuple:
        """R2 stop_check：意图巡检（撤失效/超时、成交同步）+ 保护单补挂巡检。"""
        out = []
        ok, msg = self._r2_monitor_intents(exchange, symbol, method)
        out.append(msg)
        ok2, msg2 = self._r2_check_protection(exchange, symbol, method, pos_info)
        if msg2:
            out.append(msg2)
        return True, " | ".join(out)

    def _handle_multi_signal(
        self, strategy, exchange, symbol, timeframe, method, pos_info: PositionInfo
    ) -> tuple:
        """处理双向策略信号（如对冲策略）

        对账已经在 execute() 入口统一做了，这里不再重复。
        """
        local_has_long = pos_info.has_long()
        local_has_short = pos_info.has_short()

        # 拉 K 线（对冲策略可能不依赖 K 线，但保持兼容）
        data_length = strategy.required_data_length
        db_symbol = symbol.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")
        interval_map = {"1": "1min", "5": "5min", "15": "15min", "60": "1h"}
        db_interval = interval_map.get(timeframe, "5min")

        df = self.db_reader.get_data(db_symbol, db_interval, limit=data_length)
        if df is None or df.empty:
            import pandas as pd
            df = pd.DataFrame()

        try:
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
        except Exception as e:
            logger.error(f"对冲策略执行异常: {e}", exc_info=True)
            return False, f"策略执行异常: {e}"

        messages = []
        if multi_signal.has_long_action:
            sig = multi_signal.long_signal
            if sig.signal == SignalResult.LONG:
                ok, msg = self._execute_open_isolated(exchange, symbol, method, "long")
                messages.append(f"[多仓] {msg}")
            elif sig.signal == SignalResult.CLOSE_LONG:
                ok, msg = self._execute_close_direction(exchange, symbol, method, "long")
                messages.append(f"[平多] {msg}")

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

    def _handle_stop_check(
        self, strategy, exchange, symbol, method, pos_info: PositionInfo
    ) -> tuple:
        """非信号时刻的止盈止损检查"""
        if not strategy.need_stop_check():
            return False, f"策略 {method} 不需要止盈止损检查"

        if not pos_info.has_position():
            return False, "无仓位，无需检查"

        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        result = strategy.check_stop(
            current_position=pos_info.position,
            entry_price=pos_info.entry_price,
            current_price=current_price,
        )

        if result.signal == SignalResult.NO_SIGNAL:
            return False, f"未触发止盈止损: {result.reason}"

        # 触发平仓
        log_trade(f"[平仓触发] {symbol} 止损: {result.reason}")
        return self._execute_close(exchange, symbol, method, pos_info)

    # ==================== 交易执行 ====================

    def _execute_signal(
        self, exchange, symbol, method, pos_info: PositionInfo, signal: SignalResult, df
    ) -> tuple:
        """根据信号执行交易"""
        if signal.signal == SignalResult.NO_SIGNAL:
            return False, f"无交易信号: {signal.reason}"

        if signal.signal in (SignalResult.CLOSE_LONG, SignalResult.CLOSE_SHORT):
            return self._execute_close(exchange, symbol, method, pos_info)

        if signal.signal == SignalResult.LONG:
            return self._execute_open(exchange, symbol, method, pos_info, "long", df)

        if signal.signal == SignalResult.SHORT:
            return self._execute_open(exchange, symbol, method, pos_info, "short", df)

        return False, f"未知信号类型: {signal.signal}"

    def _execute_open(
        self, exchange, symbol, method, pos_info: PositionInfo, direction: str, df
    ) -> tuple:
        """执行开仓（全仓模式）"""
        side = "buy" if direction == "long" else "sell"
        pos_side = direction
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")

        # 反向开仓：先平
        if pos_info.has_position():
            opposite = (
                (direction == "long" and pos_info.position == -1)
                or (direction == "short" and pos_info.position == 1)
            )
            if opposite:
                log_trade(f"[反向平仓] {symbol} 先平 {pos_info.direction} 再开反方向")
                close_pos_side = "long" if pos_info.position == 1 else "short"
                self.trader.close_position(pos_side=close_pos_side)
                position_manager.clear_position(exchange, symbol, method)
                time.sleep(0.5)
            elif (
                (direction == "long" and pos_info.position == 1)
                or (direction == "short" and pos_info.position == -1)
            ):
                return False, f"已有{direction}仓位，无需操作"

        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        live_balance = self.trader.get_usdt_balance()
        if live_balance <= 0:
            log_trade(f"[异常] {symbol} 开仓失败: 账户USDT余额不足", "ERROR")
            return False, "账户余额不足"
        logger.info(f"当前账户USDT可用余额: {live_balance:.4f}")

        result = self.trader.open_position(
            side=side,
            pos_side=pos_side,
            current_price=current_price,
            balance=live_balance,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            ratio=TradingConfig.DEFAULT_RATIO,
            inst_id=inst_id,
        )

        if result.get("success"):
            entry_index = len(df) - 1 if (df is not None and not df.empty) else None
            contracts = result.get("contracts", 0)
            # C1: 传入 size，记录正确的合约张数
            position_manager.save_position(
                exchange=exchange,
                symbol=symbol,
                method=method,
                direction=direction,
                price=current_price,
                size=contracts,
                margin_mode=TradingConfig.MARGIN_MODE,
                leverage=TradingConfig.DEFAULT_LEVERAGE,
                entry_index=entry_index,
            )
            log_trade(
                f"[开仓] {symbol} {direction} @ {current_price} {contracts}张 "
                f"| 本金={live_balance:.2f} | 模式={TradingConfig.MARGIN_MODE}"
            )
            return True, f"开{direction}成功 @ {current_price} ({contracts}张, 本金={live_balance:.2f})"
        else:
            return False, f"开仓失败: {result.get('message', '未知错误')}"

    def _execute_open_isolated(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> tuple:
        """逐仓模式开仓（对冲策略专用）"""
        side = "buy" if direction == "long" else "sell"
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")

        total_balance = self.trader.get_usdt_balance()
        if total_balance <= 0:
            return False, "获取账户余额失败"

        current_price = self.trader.get_current_price(symbol)
        if current_price <= 0:
            return False, "获取当前价格失败"

        logger.info(
            f"逐仓开仓: direction={direction}, balance={total_balance}, "
            f"percent={TradingConfig.POSITION_PERCENT}, price={current_price}"
        )

        result = self.trader.open_position(
            side=side,
            pos_side=direction,
            current_price=current_price,
            balance=total_balance,
            leverage=TradingConfig.DEFAULT_LEVERAGE,
            margin_mode="isolated",
            position_percent=TradingConfig.POSITION_PERCENT,
            inst_id=inst_id,
        )

        if result.get("success"):
            contracts = result.get("contracts", 0)
            # C1: 传 size
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
            log_trade(
                f"[开仓] {symbol} {direction} @ {current_price} {contracts}张 | 逐仓"
            )
            return True, f"开{direction}成功 @ {current_price}, {contracts}张"
        else:
            return False, f"开{direction}失败: {result.get('message', '未知错误')}"

    def _execute_close(self, exchange, symbol, method, pos_info: PositionInfo) -> tuple:
        """执行平仓（全仓模式，平当前主仓位方向）"""
        if not pos_info.has_position():
            return False, "无仓位可平"

        pos_side = "long" if pos_info.position == 1 else "short"
        current_price = self.trader.get_current_price(symbol)

        result = self.trader.close_position(pos_side=pos_side)
        # 不管 OKX 返回如何，本地都清除记录（避免 ghost 仓位卡死）
        position_manager.clear_position(
            exchange, symbol, method,
            close_price=current_price if current_price > 0 else 0,
            reason="策略平仓",
        )

        if result.get("success"):
            log_trade(f"[平仓] {symbol} {pos_side} @ {current_price}")
            return True, f"平{pos_side}成功 @ {current_price}"
        log_trade(f"[平仓] {symbol} {pos_side} 异常: {result}", "WARNING")
        return True, f"平{pos_side}：OKX 返回异常但本地已清除记录 ({result})"

    def _execute_close_direction(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> tuple:
        """平掉指定方向的仓位（双向策略用）"""
        inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT-SWAP")
        current_price = self.trader.get_current_price(symbol)

        result = self.trader.close_position(
            pos_side=direction,
            margin_mode=None,
            inst_id=inst_id,
        )

        if result.get("success"):
            position_manager.clear_position(
                exchange, symbol, method,
                direction=direction,
                close_price=current_price,
                reason="策略平仓",
            )
            log_trade(f"[平仓] {symbol} {direction} @ {current_price}")
            return True, f"平{direction}成功 @ {current_price}"
        else:
            # 失败也清记录，避免本地一直挂着
            position_manager.clear_position(
                exchange, symbol, method,
                direction=direction,
                close_price=current_price,
                reason="平仓失败但清除记录",
            )
            log_trade(f"[平仓] {symbol} {direction} 失败: {result.get('message', '')}", "WARNING")
            return False, f"平{direction}失败: {result.get('message', '')}"

    # ==================== 强平接口（API 用）====================

    def force_close(
        self, exchange: str, symbol: str, method: str, direction: str = None
    ) -> tuple:
        """强制清仓（API 调用）"""
        if direction:
            return self._execute_close_direction(exchange, symbol, method, direction)

        pos_info = position_manager.get_position(exchange, symbol, method)
        if not pos_info.has_position():
            return False, "无仓位可平"
        return self._execute_close(exchange, symbol, method, pos_info)


# 全局实例
trading_engine = TradingEngine()
