"""OKX交易执行封装 - 支持逐仓/全仓 + 多空同时持仓"""
import requests
import pandas as pd
from autobot.config import OKXConfig, TradingConfig
from autobot.utils.logger import logger
import math

try:
    import okx.Trade as Trade
    import okx.Funding as Funding
    import okx.Account as Account
    import okx.MarketData as MarketData
    import okx.PublicData as PublicData
except ImportError:
    logger.warning("okx SDK未安装，交易功能不可用")


class OKXTrader:
    """OKX交易执行器 - 支持逐仓/全仓"""

    def __init__(self):
        self.api_key = OKXConfig.API_KEY
        self.secret_key = OKXConfig.SECRET_KEY
        self.passphrase = OKXConfig.PASSPHRASE
        self.flag = OKXConfig.FLAG

    @staticmethod
    def _pos_side(pos_side: str) -> str:
        """根据 POSITION_MODE 返回应传给 OKX 的 posSide。

        - long_short_mode(双向持仓): 必须传 "long"/"short"
        - net_mode(单向持仓): 不传 posSide，返回空字符串
        """
        if TradingConfig.POSITION_MODE == "net_mode":
            return ""
        return pos_side

    def _get_trade_api(self):
        return Trade.TradeAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag)

    def _get_funding_api(self):
        return Funding.FundingAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag)

    def _get_account_api(self):
        return Account.AccountAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag)

    def _get_public_api(self):
        return PublicData.PublicAPI(flag=self.flag)

    # ==================== 价格和市场信息 ====================

    def get_current_price(self, symbol: str) -> float:
        """获取当前价格"""
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            inst_id = symbol if "-" in symbol else symbol.replace("USDT", "-USDT")
            url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar=1m&limit=1"
            resp = requests.get(url, headers=headers, timeout=10)
            data = resp.json()
            if data.get("data"):
                return float(data["data"][0][4])  # close price
            return 0
        except Exception as e:
            logger.error(f"获取价格失败: {e}")
            return 0

    def get_min_size(self, inst_id: str = None) -> float:
        """查询合约最小下单张数"""
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        try:
            public_api = self._get_public_api()
            result = public_api.get_instruments(instType="SWAP", instId=inst_id)
            if result["code"] == "0" and result["data"]:
                return float(result["data"][0].get("minSz", "1"))
        except Exception as e:
            logger.error(f"查询最小下单量失败: {e}")
        return 1.0

    @staticmethod
    def _fmt_sz(sz) -> str:
        """把张数格式化为 OKX sz 字符串（lotSz=0.01，支持小数张，去掉尾零）"""
        v = round(float(sz), 2)
        return f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))

    def get_contract_value(self, inst_id: str = None) -> float:
        """查询合约面值（每张代表多少币）"""
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        try:
            public_api = self._get_public_api()
            result = public_api.get_instruments(instType="SWAP", instId=inst_id)
            if result["code"] == "0" and result["data"]:
                return float(result["data"][0].get("ctVal", "0.1"))
        except Exception as e:
            logger.error(f"查询合约面值失败: {e}")
        return TradingConfig.CONTRACT_SIZE

    def get_tick_size(self, inst_id: str = None) -> float:
        """查询合约价格最小变动单位(tickSz)，用于保护单价格定向取整"""
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        try:
            public_api = self._get_public_api()
            result = public_api.get_instruments(instType="SWAP", instId=inst_id)
            if result["code"] == "0" and result["data"]:
                return float(result["data"][0].get("tickSz", "0.01"))
        except Exception as e:
            logger.error(f"查询tickSz失败: {e}")
        return 0.01

    def validate_position_mode(self) -> bool:
        """校验账户持仓模式与 POSITION_MODE 配置一致，不一致仅告警"""
        try:
            account_api = self._get_account_api()
            result = account_api.get_account_config()
            if result["code"] == "0" and result["data"]:
                account_pos_mode = result["data"][0].get("posMode", "")
                if account_pos_mode and account_pos_mode != TradingConfig.POSITION_MODE:
                    logger.warning(
                        f"⚠️ 账户持仓模式({account_pos_mode}) 与配置 POSITION_MODE"
                        f"({TradingConfig.POSITION_MODE}) 不一致，下单可能被拒"
                    )
                    return False
                return True
        except Exception as e:
            logger.error(f"校验持仓模式失败: {e}")
        return True

    # ==================== 保证金模式设置 ====================

    def set_margin_mode(
        self,
        margin_mode: str = None,
        inst_id: str = None,
    ) -> dict:
        """
        设置保证金模式

        Args:
            margin_mode: "cross"=全仓, "isolated"=逐仓
            inst_id: 合约ID
        """
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        try:
            account_api = self._get_account_api()
            # OKX 的 set_account_level 控制账户模式，
            # 逐仓/全仓通过 set_leverage 时的 mgnMode 参数来区分
            # 这里我们设置杠杆时同时指定 mgnMode
            logger.info(f"保证金模式将使用: {margin_mode}")
            return {"success": True, "margin_mode": margin_mode}
        except Exception as e:
            logger.error(f"设置保证金模式失败: {e}")
            return {"success": False, "message": str(e)}

    def set_leverage(
        self,
        leverage: int = None,
        margin_mode: str = None,
        inst_id: str = None,
    ) -> dict:
        """
        设置杠杆（同时指定保证金模式）

        Args:
            leverage: 杠杆倍数
            margin_mode: "cross" / "isolated"
            inst_id: 合约ID
        """
        leverage = leverage or TradingConfig.DEFAULT_LEVERAGE
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID

        try:
            account_api = self._get_account_api()
            results_list = []
            for pos_side in ["long", "short"]:
                result = account_api.set_leverage(
                    instId=inst_id,
                    lever=str(leverage),
                    mgnMode=margin_mode,    # 关键: 动态传入
                    posSide=pos_side,
                )
                results_list.append(result)
                if result["code"] != "0":
                    # 可能是单向持仓模式，尝试不指定 posSide
                    result2 = account_api.set_leverage(
                        instId=inst_id,
                        lever=str(leverage),
                        mgnMode=margin_mode,
                    )
                    return result2
            logger.info(f"杠杆设置成功: {leverage}x, 模式={margin_mode}")
            return results_list[0]
        except Exception as e:
            logger.error(f"设置杠杆失败: {e}")
            return {"code": "-1", "msg": str(e)}

    # ==================== 仓位计算 ====================

    def calculate_contracts(
        self,
        current_price: float,
        balance: float = None,
        leverage: int = None,
        ratio: float = None,
        margin_mode: str = None,
        position_percent: float = None,
        inst_id: str = None,
    ) -> float:
        """
        计算开仓合约张数

        全仓模式: contracts = (balance * ratio * leverage) / (current_price * contract_size)
        逐仓模式: contracts = (total_balance * position_percent * leverage) / (current_price * contract_size)
        """
        balance = balance if balance is not None else TradingConfig.DEFAULT_BALANCE
        leverage = leverage or TradingConfig.DEFAULT_LEVERAGE
        ratio = ratio if ratio is not None else TradingConfig.DEFAULT_RATIO
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        position_percent = position_percent if position_percent is not None else TradingConfig.POSITION_PERCENT

        #contract_size = TradingConfig.CONTRACT_SIZE
        contract_size = self.get_contract_value(inst_id)

        if margin_mode == "isolated":
            # 逐仓: 使用总账户余额 * 仓位百分比
            use_amount = balance * position_percent
        else:
            # 全仓: 使用旧逻辑 balance * ratio
            use_amount = balance * ratio

        total_value = use_amount * leverage
        qty = total_value / current_price
        #contracts = round(qty / contract_size, 2)
        min_size = float(self.get_min_size(inst_id))
        raw_contracts = qty / contract_size
        contracts = max(math.ceil(raw_contracts) if raw_contracts >= 0.01 else 0, min_size)

        #contracts = max(int(qty / contract_size), min_size)

        logger.info(
            f"仓位计算: mode={margin_mode}, balance={balance}, "
            f"use_amount={use_amount:.4f}, leverage={leverage}x, "
            f"price={current_price}, contracts={contracts}"
        )
        return contracts

    # ==================== 开仓 ====================

    def open_position(
        self,
        side: str,
        pos_side: str,
        current_price: float,
        balance: float = None,
        leverage: int = None,
        ratio: float = None,
        margin_mode: str = None,
        position_percent: float = None,
        inst_id: str = None,
    ) -> dict:
        """
        开仓

        Args:
            side: 'buy' (做多) 或 'sell' (做空)
            pos_side: 'long' 或 'short'
            current_price: 当前价格
            balance: 可用余额（全仓时使用）或总账户余额（逐仓时使用）
            leverage: 杠杆倍数
            ratio: 资金使用比例（全仓模式）
            margin_mode: "cross"=全仓 / "isolated"=逐仓
            position_percent: 仓位百分比（逐仓模式）
            inst_id: 合约ID

        Returns:
            dict: {success: bool, message: str, data: ..., contracts: float}
        """
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID

        try:
            contracts = self.calculate_contracts(
                current_price=current_price,
                balance=balance,
                leverage=leverage,
                ratio=ratio,
                margin_mode=margin_mode,
                position_percent=position_percent,
                inst_id=inst_id,
            )

            if contracts < 1:
                return {"success": False, "message": f"合约张数过小: {contracts}", "contracts": contracts}

            logger.info(f"开仓: {side}/{pos_side}, {contracts}张 @ {current_price}, mode={margin_mode}")

            # 先设置杠杆（包含保证金模式）
            self.set_leverage(leverage=leverage, margin_mode=margin_mode, inst_id=inst_id)

            trade_api = self._get_trade_api()
            result = trade_api.place_order(
                instId=inst_id,
                tdMode=margin_mode,     # 关键: 动态传入 "cross" 或 "isolated"
                side=side,
                posSide=pos_side,
                ordType="market",
                sz=str(contracts),
            )

            if result["code"] == "0":
                return {
                    "success": True,
                    "message": f"开仓成功: {contracts}张, mode={margin_mode}",
                    "data": result,
                    "contracts": contracts,
                }
            else:
                return {
                    "success": False,
                    "message": f"开仓失败: {result.get('msg', '')}",
                    "data": result,
                    "contracts": contracts,
                }

        except Exception as e:
            return {"success": False, "message": f"开仓异常: {e}", "contracts": 0}

    # ==================== 平仓 ====================

    def open_market_size(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        sz: float,
        leverage: int = None,
        margin_mode: str = None,
    ) -> dict:
        """
        按指定张数市价开仓（R2 F3 下一开盘入场 / 触发成交兜底时用）。

        R2 的张数由策略 calculate_order_size 计算（含 notional 上限/step 取整），
        不走 open_position 的仓位百分比算法，因此这里提供"指定 sz"的入口。
        """
        leverage = leverage or TradingConfig.DEFAULT_LEVERAGE
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        sz = round(float(sz), 2)
        if sz < 0.01:
            return {"success": False, "message": f"张数过小: {sz}", "contracts": 0}
        try:
            self.set_leverage(leverage=leverage, margin_mode=margin_mode, inst_id=inst_id)
            trade_api = self._get_trade_api()
            result = trade_api.place_order(
                instId=inst_id,
                tdMode=margin_mode,
                side=side,
                posSide=self._pos_side(pos_side),
                ordType="market",
                sz=self._fmt_sz(sz),
            )
            if result["code"] == "0":
                logger.info(f"市价开仓成功: {inst_id} {side}/{pos_side} {sz}张 mode={margin_mode}")
                return {"success": True, "message": f"开仓成功: {sz}张", "contracts": sz}
            return {"success": False, "message": f"开仓失败: {result.get('msg','')}", "contracts": 0}
        except Exception as e:
            return {"success": False, "message": f"开仓异常: {e}", "contracts": 0}

    def close_position(
        self,
        pos_side: str = "net",
        ccy: str = "USDT",
        margin_mode: str = None,
        inst_id: str = None,
    ) -> dict:
        """
        市价平仓

        Args:
            pos_side: "long" / "short" / "net"
            ccy: 结算币种
            margin_mode: "cross" / "isolated"
            inst_id: 合约ID
        """
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID

        try:
            trade_api = self._get_trade_api()
            result = trade_api.close_positions(
                instId=inst_id,
                mgnMode=margin_mode,    # 关键: 动态传入
                posSide=pos_side,
                ccy=ccy,
            )
            logger.info(f"平仓结果: pos_side={pos_side}, mode={margin_mode}, result={result}")
            return {"success": result["code"] == "0", "data": result}
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            return {"success": False, "message": str(e)}

    # ==================== 持仓查询（增强版）====================

    def get_positions(self, inst_id: str = None) -> dict:
        """获取当前持仓"""
        try:
            account_api = self._get_account_api()
            if inst_id:
                return account_api.get_positions(instId=inst_id)
            return account_api.get_positions()
        except Exception as e:
            return {"error": str(e)}

    def get_position_detail(self, inst_id: str = None) -> dict:
        """
        获取持仓详情 - 包含保证金率、预估强平价等

        Returns:
            {
                "long": {pos_data} or None,
                "short": {pos_data} or None,
            }
        """
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        result = {"long": None, "short": None}
        try:
            account_api = self._get_account_api()
            pos_result = account_api.get_positions(instId=inst_id)
            if pos_result["code"] == "0":
                for pos in pos_result["data"]:
                    pos_side = pos.get("posSide", "")
                    if float(pos.get("pos", "0")) > 0:
                        detail = {
                            "pos_side": pos_side,
                            "size": pos.get("pos"),
                            "avg_price": pos.get("avgPx"),
                            "upl": pos.get("upl"),                  # 未实现盈亏
                            "upl_ratio": pos.get("uplRatio"),       # 未实现盈亏率
                            "margin": pos.get("margin"),            # 保证金
                            "mgn_ratio": pos.get("mgnRatio"),       # 保证金率
                            "liq_price": pos.get("liqPx"),          # 预估强平价
                            "mark_price": pos.get("markPx"),        # 标记价格
                            "lever": pos.get("lever"),              # 杠杆
                            "mgn_mode": pos.get("mgnMode"),         # 保证金模式
                            "inst_id": pos.get("instId"),
                        }
                        if pos_side == "long":
                            result["long"] = detail
                        elif pos_side == "short":
                            result["short"] = detail
        except Exception as e:
            logger.error(f"获取持仓详情失败: {e}")
        return result

    # ==================== 保护单（TP/SL 条件单） ====================

    def place_stop_entry_order(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        sz: float,
        trigger_price: float,
        margin_mode: str = None,
        algo_cl_ord_id: str = "",
    ) -> dict:
        """
        挂"条件触发开仓单"（R2 四形态 F1/F2/F4 入场意图）。

        OKX conditional 单: 价格触及 trigger 后按市价提交 side/sz 的委托，
        reduceOnly=false → 用于【开仓】而非平仓。

        Args:
            side: "buy"(向上突破开多) / "sell"(向下突破开空)
            pos_side: "long" / "short" / "net"
            sz: 张数（已按策略口径取整）
            trigger_price: 触发价（buy 需高于现价，sell 需低于现价——OKX conditional 校验）
            margin_mode: "cross" / "isolated"
            algo_cl_ord_id: 确定性 clientOrderId（幂等防重复）

        Returns:
            {"success": bool, "algo_id": str, "data": ..., "message": str}

        NOTE(2026-09-03 实盘探针): OKX conditional 的入场触发统一用 slTriggerPx。
        - side=buy : slTriggerPx 必须【高于】现价（向上突破开多，类似 buy-stop）
        - side=sell: slTriggerPx 必须【低于】现价（向下突破开空，类似 sell-stop）
        tpTriggerPx 是镜像语义（buy 只允许 ≤ 现价），不可用于向上触发开多（code 51277）。
        """
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        try:
            trade_api = self._get_trade_api()
            result = trade_api.place_algo_order(
                instId=inst_id,
                tdMode=margin_mode,
                side=side,
                posSide=self._pos_side(pos_side),
                ordType="conditional",
                sz=self._fmt_sz(sz),
                reduceOnly="false",
                slTriggerPx=str(trigger_price),
                slOrdPx="-1",  # 触发后市价开仓
                slTriggerPxType=TradingConfig.TP_SL_TRIGGER_TYPE,
                algoClOrdId=algo_cl_ord_id,
            )
            if result["code"] == "0":
                algo_id = ""
                data = result.get("data") or []
                if data:
                    algo_id = data[0].get("algoId", "")
                logger.info(
                    f"挂条件开仓单成功: {inst_id} {side}/{pos_side} sz={sz} "
                    f"trigger={trigger_price} algoId={algo_id}"
                )
                return {"success": True, "algo_id": algo_id, "data": result}
            logger.error(f"挂条件开仓单失败: {result}")
            return {"success": False, "message": result.get("msg", ""), "data": result}
        except Exception as e:
            logger.error(f"挂条件开仓单异常: {e}")
            return {"success": False, "message": f"挂条件开仓单异常: {e}"}

    def place_protective_orders(
        self,
        inst_id: str,
        direction: str,
        size: float,
        tp_price: float,
        sl_price: float,
        margin_mode: str = None,
        algo_cl_ord_id: str = "",
    ) -> dict:
        """
        挂 OCO 保护单（TP/SL 条件单，真正挂单而非软件平仓）。

        Args:
            inst_id: 合约ID
            direction: 持仓方向 "long" / "short"
            size: 需保护的全部仓位张数
            tp_price: 止盈触发价（已按 tick 定向取整）
            sl_price: 止损触发价（已按 tick 定向取整）
            margin_mode: "cross" / "isolated"
            algo_cl_ord_id: 确定性 clientOrderId（幂等防重复）

        Returns:
            {"success": bool, "algo_id": str, "data": ..., "message": str}
        """
        margin_mode = margin_mode or TradingConfig.MARGIN_MODE
        side = "sell" if direction == "long" else "buy"  # 平仓方向
        pos_side = self._pos_side(direction)

        try:
            trade_api = self._get_trade_api()
            result = trade_api.place_algo_order(
                instId=inst_id,
                tdMode=margin_mode,
                side=side,
                posSide=pos_side,
                ordType="oco",
                sz=self._fmt_sz(size),
                reduceOnly="true",
                tpTriggerPx=str(tp_price),
                tpOrdPx="-1",  # 市价平仓
                slTriggerPx=str(sl_price),
                slOrdPx="-1",
                tpTriggerPxType=TradingConfig.TP_SL_TRIGGER_TYPE,
                slTriggerPxType=TradingConfig.TP_SL_TRIGGER_TYPE,
                algoClOrdId=algo_cl_ord_id,
            )
            if result["code"] == "0":
                algo_id = ""
                data = result.get("data") or []
                if data:
                    algo_id = data[0].get("algoId", "")
                logger.info(
                    f"挂保护单成功: {inst_id} {direction} sz={size} "
                    f"tp={tp_price} sl={sl_price} algoId={algo_id}"
                )
                return {"success": True, "algo_id": algo_id, "data": result}
            logger.error(f"挂保护单失败: {result}")
            return {"success": False, "message": result.get("msg", ""), "data": result}
        except Exception as e:
            logger.error(f"挂保护单异常: {e}")
            return {"success": False, "message": f"挂保护单异常: {e}"}

    def amend_protective_orders(
        self,
        inst_id: str,
        algo_id: str,
        new_size: float,
        new_tp: float,
        new_sl: float,
    ) -> dict:
        """加仓后修改保护单：更新覆盖数量与 TP/SL 价格（基于新均价）"""
        try:
            trade_api = self._get_trade_api()
            result = trade_api.amend_algo_order(
                instId=inst_id,
                algoId=algo_id,
                newSz=self._fmt_sz(new_size),
                newTpTriggerPx=str(new_tp),
                newSlTriggerPx=str(new_sl),
                newTpTriggerPxType=TradingConfig.TP_SL_TRIGGER_TYPE,
                newSlTriggerPxType=TradingConfig.TP_SL_TRIGGER_TYPE,
            )
            if result["code"] == "0":
                logger.info(
                    f"改保护单成功: {inst_id} algoId={algo_id} "
                    f"sz={new_size} tp={new_tp} sl={new_sl}"
                )
                return {"success": True, "data": result}
            logger.error(f"改保护单失败: {result}")
            return {"success": False, "message": result.get("msg", ""), "data": result}
        except Exception as e:
            logger.error(f"改保护单异常: {e}")
            return {"success": False, "message": f"改保护单异常: {e}"}

    def cancel_protective_orders(
        self, inst_id: str, algo_id: str = "", algo_cl_ord_id: str = ""
    ) -> dict:
        """撤销保护单（反手/平仓前调用）"""
        try:
            trade_api = self._get_trade_api()
            item = {"instId": inst_id}
            if algo_id:
                item["algoId"] = algo_id
            if algo_cl_ord_id:
                item["algoClOrdId"] = algo_cl_ord_id
            result = trade_api.cancel_algo_order([item])
            if result["code"] == "0":
                logger.info(f"撤保护单成功: {inst_id} {item}")
                return {"success": True, "data": result}
            logger.error(f"撤保护单失败: {result}")
            return {"success": False, "message": result.get("msg", ""), "data": result}
        except Exception as e:
            logger.error(f"撤保护单异常: {e}")
            return {"success": False, "message": f"撤保护单异常: {e}"}

    def get_algo_orders(self, inst_id: str, ord_type: str = "oco") -> dict:
        """查询未完成的条件单（用于核对保护单是否覆盖仓位）"""
        try:
            trade_api = self._get_trade_api()
            result = trade_api.order_algos_list(instId=inst_id, ordType=ord_type)
            if result["code"] == "0":
                return {"success": True, "data": result.get("data", [])}
            return {"success": False, "message": result.get("msg", ""), "data": []}
        except Exception as e:
            logger.error(f"查询条件单异常: {e}")
            return {"success": False, "message": f"查询条件单异常: {e}", "data": []}

    # ==================== 爆仓风险检测 ====================

    def check_liquidation_risk(self, inst_id: str = None) -> dict:
        """
        检查各仓位的爆仓风险

        Returns:
            {
                "long": {"at_risk": bool, "mgn_ratio": float, "liq_price": float},
                "short": {"at_risk": bool, "mgn_ratio": float, "liq_price": float},
                "has_risk": bool,
                "liquidated_directions": []  # 已经被强平的方向
            }
        """
        inst_id = inst_id or TradingConfig.DEFAULT_INST_ID
        warn_ratio = TradingConfig.LIQUIDATION_WARN_RATIO

        result = {
            "long": {"at_risk": False, "mgn_ratio": 0, "liq_price": 0},
            "short": {"at_risk": False, "mgn_ratio": 0, "liq_price": 0},
            "has_risk": False,
            "liquidated_directions": [],
        }

        try:
            pos_detail = self.get_position_detail(inst_id)

            for direction in ["long", "short"]:
                detail = pos_detail.get(direction)
                if detail is None:
                    continue

                mgn_ratio = float(detail.get("mgn_ratio") or "0")
                liq_price = float(detail.get("liq_price") or "0")

                result[direction]["mgn_ratio"] = mgn_ratio
                result[direction]["liq_price"] = liq_price

                # 保证金率低于阈值 → 有爆仓风险
                if 0 < mgn_ratio < warn_ratio:
                    result[direction]["at_risk"] = True
                    result["has_risk"] = True
                    logger.warning(
                        f"⚠️ 爆仓预警: {direction} {inst_id}, "
                        f"保证金率={mgn_ratio:.4f}, 预估强平价={liq_price}"
                    )

            # 检测已爆仓: 本地有仓位记录但交易所已无仓位
            # 这个逻辑在 engine 层做，此处只返回交易所实际数据

        except Exception as e:
            logger.error(f"检查爆仓风险失败: {e}")

        return result

    # ==================== 余额管理 ====================

    def get_balance(self) -> dict:
        """获取账户余额"""
        try:
            account_api = self._get_account_api()
            return account_api.get_account_balance()
        except Exception as e:
            return {"error": str(e)}

    def get_usdt_balance(self) -> float:
        """获取 USDT 可用余额"""
        try:
            result = self.get_balance()
            if result.get("code") == "0":
                for detail in result["data"][0]["details"]:
                    if detail["ccy"] == "USDT":
                        return float(detail["availBal"])
        except Exception as e:
            logger.error(f"获取USDT余额失败: {e}")
        return 0.0

    def get_usdt_equity(self) -> float:
        """获取 USDT 可用权益（availEq = 可用余额 + 可用保证金，R2 资金口径用）"""
        try:
            result = self.get_balance()
            if result.get("code") == "0":
                for detail in result["data"][0]["details"]:
                    if detail["ccy"] == "USDT":
                        return float(detail.get("availEq") or detail["availBal"])
        except Exception as e:
            logger.error(f"获取USDT可用权益失败: {e}")
        return 0.0

    def update_balance(self, target_amount: float, tolerance: float = 0.01):
        """更新交易账户余额到目标金额"""
        try:
            account_api = self._get_account_api()
            bal_result = account_api.get_account_balance()

            if bal_result["code"] != "0":
                return

            usdt_bal = None
            for detail in bal_result["data"][0]["details"]:
                if detail["ccy"] == "USDT":
                    usdt_bal = float(detail["availBal"])
                    break

            if usdt_bal is None:
                return

            diff = usdt_bal - target_amount
            if abs(diff) <= tolerance:
                return

            funding_api = self._get_funding_api()
            if diff > 0:
                funding_api.funds_transfer(ccy="USDT", amt=str(abs(diff)), from_="18", to="6")
            else:
                funding_api.funds_transfer(ccy="USDT", amt=str(abs(diff)), from_="6", to="18")

        except Exception as e:
            logger.error(f"余额调整失败: {e}")
