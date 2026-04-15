"""仓位管理 - 支持多空同时持仓 + 持久化存储"""
import json
import time
import os
from typing import Optional, Dict, List
from autobot.cache.redis import redis_client
from autobot.config import TradingConfig
from autobot.utils.logger import logger


class SinglePositionInfo:
    """单方向仓位信息（多仓或空仓）"""

    def __init__(
        self,
        direction: str = "",           # "long" / "short" / ""
        has_position: bool = False,
        entry_price: float = 0,
        entry_time: str = "",
        entry_index: Optional[int] = None,
        size: float = 0,               # 合约张数
        margin_mode: str = "cross",    # "cross" / "isolated"
        leverage: int = 100,
        timestamp: int = 0,
        position_id: str = "",         # 唯一标识，用于持久化追踪
        is_liquidated: bool = False,   # 是否已爆仓
    ):
        self.direction = direction
        self.has_position = has_position
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.entry_index = entry_index
        self.size = size
        self.margin_mode = margin_mode
        self.leverage = leverage
        self.timestamp = timestamp
        self.position_id = position_id
        self.is_liquidated = is_liquidated

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "has_position": self.has_position,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "entry_index": self.entry_index,
            "size": self.size,
            "margin_mode": self.margin_mode,
            "leverage": self.leverage,
            "timestamp": self.timestamp,
            "position_id": self.position_id,
            "is_liquidated": self.is_liquidated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SinglePositionInfo":
        return cls(
            direction=data.get("direction", ""),
            has_position=data.get("has_position", False),
            entry_price=float(data.get("entry_price", 0)),
            entry_time=data.get("entry_time", ""),
            entry_index=data.get("entry_index"),
            size=float(data.get("size", 0)),
            margin_mode=data.get("margin_mode", "cross"),
            leverage=int(data.get("leverage", 100)),
            timestamp=int(data.get("timestamp", 0)),
            position_id=data.get("position_id", ""),
            is_liquidated=data.get("is_liquidated", False),
        )


class PositionInfo:
    """
    完整仓位信息 - 同时包含多仓和空仓
    
    替代旧的 position: int (0/1/-1) 设计，
    现在 long 和 short 独立存在，互不干扰。
    """

    def __init__(
        self,
        long: Optional[SinglePositionInfo] = None,
        short: Optional[SinglePositionInfo] = None,
    ):
        self.long = long or SinglePositionInfo(direction="long")
        self.short = short or SinglePositionInfo(direction="short")

    def has_long(self) -> bool:
        return self.long.has_position

    def has_short(self) -> bool:
        return self.short.has_position

    def has_any_position(self) -> bool:
        return self.has_long() or self.has_short()

    def get_direction(self, direction: str) -> SinglePositionInfo:
        """按方向获取仓位"""
        if direction == "long":
            return self.long
        elif direction == "short":
            return self.short
        raise ValueError(f"无效方向: {direction}")

    # === 向后兼容旧代码 ===
    # 旧代码用 pos_info.position (0/1/-1) 和 pos_info.has_position()
    # 这里提供兼容属性，但不推荐新代码使用

    @property
    def position(self) -> int:
        """兼容旧代码: 返回主仓位方向 (优先返回先开的那个)"""
        if self.has_long() and not self.has_short():
            return 1
        elif self.has_short() and not self.has_long():
            return -1
        elif self.has_long() and self.has_short():
            # 两个都有，返回先开的那个
            if self.long.timestamp <= self.short.timestamp:
                return 1
            return -1
        return 0

    @property
    def entry_price(self) -> float:
        """兼容旧代码"""
        if self.position == 1:
            return self.long.entry_price
        elif self.position == -1:
            return self.short.entry_price
        return 0

    @property
    def direction(self) -> str:
        """兼容旧代码"""
        if self.position == 1:
            return "long"
        elif self.position == -1:
            return "short"
        return ""

    @property
    def entry_index(self) -> Optional[int]:
        """兼容旧代码"""
        if self.position == 1:
            return self.long.entry_index
        elif self.position == -1:
            return self.short.entry_index
        return None

    def has_position(self) -> bool:
        """兼容旧代码"""
        return self.has_any_position()

    def to_dict(self) -> dict:
        return {
            "long": self.long.to_dict(),
            "short": self.short.to_dict(),
            # 兼容字段
            "position": self.position,
            "has_any": self.has_any_position(),
        }


class PositionStore:
    """
    仓位持久化存储 - JSON文件
    
    保存所有开/平仓记录和当前活跃仓位，
    用于爆仓检测后的联动处理和历史回溯。
    """

    def __init__(self, store_path: str = None):
        self.store_path = store_path or TradingConfig.POSITION_STORE_PATH
        self._data = self._load()

    def _load(self) -> dict:
        """从文件加载"""
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"加载仓位记录失败: {e}")
        return {"active_positions": {}, "history": []}

    def _save(self):
        """保存到文件"""
        try:
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存仓位记录失败: {e}")

    def save_open_record(
        self,
        position_id: str,
        exchange: str,
        symbol: str,
        method: str,
        direction: str,
        entry_price: float,
        size: float,
        margin_mode: str,
        leverage: int,
    ):
        """记录开仓"""
        record = {
            "position_id": position_id,
            "exchange": exchange,
            "symbol": symbol,
            "method": method,
            "direction": direction,
            "entry_price": entry_price,
            "size": size,
            "margin_mode": margin_mode,
            "leverage": leverage,
            "open_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "open_timestamp": int(time.time()),
            "status": "open",  # open / closed / liquidated
            "close_price": 0,
            "close_time": "",
            "pnl": 0,
        }
        self._data["active_positions"][position_id] = record
        self._data["history"].append({**record, "action": "open"})
        self._save()
        logger.info(f"[PositionStore] 记录开仓: {position_id} {direction} {symbol}")

    def save_close_record(self, position_id: str, close_price: float, reason: str = ""):
        """记录平仓"""
        active = self._data["active_positions"].get(position_id)
        if not active:
            logger.warning(f"[PositionStore] 未找到活跃仓位: {position_id}")
            return

        # 计算盈亏
        entry_price = active["entry_price"]
        direction = active["direction"]
        size = active["size"]
        if direction == "long":
            pnl = (close_price - entry_price) / entry_price
        else:
            pnl = (entry_price - close_price) / entry_price

        active["status"] = "closed"
        active["close_price"] = close_price
        active["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        active["pnl"] = round(pnl, 6)
        active["close_reason"] = reason

        self._data["history"].append({**active, "action": "close"})
        del self._data["active_positions"][position_id]
        self._save()
        logger.info(f"[PositionStore] 记录平仓: {position_id} pnl={pnl*100:.2f}%")

    def mark_liquidated(self, position_id: str):
        """标记爆仓"""
        active = self._data["active_positions"].get(position_id)
        if active:
            active["status"] = "liquidated"
            active["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._data["history"].append({**active, "action": "liquidated"})
            del self._data["active_positions"][position_id]
            self._save()
            logger.warning(f"[PositionStore] 标记爆仓: {position_id}")

    def get_active_positions(self) -> dict:
        """获取所有活跃仓位"""
        return self._data.get("active_positions", {})

    def get_active_by_symbol(self, symbol: str) -> list:
        """获取指定品种的所有活跃仓位"""
        result = []
        for pos_id, pos in self._data["active_positions"].items():
            if pos["symbol"] == symbol:
                result.append(pos)
        return result

    def get_history(self, symbol: str = None, direction: str = None, limit: int = 100) -> list:
        """查询仓位历史"""
        history = self._data.get("history", [])
        if symbol:
            history = [h for h in history if h.get("symbol") == symbol]
        if direction:
            history = [h for h in history if h.get("direction") == direction]
        return history[-limit:]

    def get_recent_liquidations(self, hours: int = 24) -> list:
        """获取最近N小时内的爆仓记录"""
        cutoff = int(time.time()) - hours * 3600
        return [
            h for h in self._data.get("history", [])
            if h.get("action") == "liquidated" and h.get("open_timestamp", 0) > cutoff
        ]


class PositionManager:
    """
    仓位管理器 - 支持多空同时持仓
    
    Redis key 设计：
      position:{exchange}:{symbol}:{method}:{direction}  -> JSON (SinglePositionInfo)
    
    对比旧版：
      旧: position:{exchange}:{symbol}:{method} -> "0" / "1" / "-1"
      新: 每个方向独立一个 key，存完整 JSON
    """

    KEY_PREFIX = "pos"

    def __init__(self):
        self.store = PositionStore()

    def _build_key(self, exchange: str, symbol: str, method: str, direction: str) -> str:
        return f"{self.KEY_PREFIX}:{exchange}:{symbol}:{method}:{direction}"

    def _generate_position_id(self, exchange: str, symbol: str, method: str, direction: str) -> str:
        """生成唯一仓位ID"""
        return f"{exchange}_{symbol}_{method}_{direction}_{int(time.time())}"

    def get_position(self, exchange: str, symbol: str, method: str) -> PositionInfo:
        """获取完整仓位信息（多+空）"""
        long_pos = self._get_single(exchange, symbol, method, "long")
        short_pos = self._get_single(exchange, symbol, method, "short")
        return PositionInfo(long=long_pos, short=short_pos)

    def get_single_position(self, exchange: str, symbol: str, method: str, direction: str) -> SinglePositionInfo:
        """获取单方向仓位"""
        return self._get_single(exchange, symbol, method, direction)

    def _get_single(self, exchange: str, symbol: str, method: str, direction: str) -> SinglePositionInfo:
        """从Redis读取单方向仓位"""
        key = self._build_key(exchange, symbol, method, direction)
        raw = redis_client.get(key)
        if raw:
            try:
                data = json.loads(raw)
                return SinglePositionInfo.from_dict(data)
            except (json.JSONDecodeError, TypeError):
                pass
        return SinglePositionInfo(direction=direction)

    def save_position(
        self,
        exchange: str,
        symbol: str,
        method: str,
        direction: str,          # "long" / "short"
        price: float,
        size: float = 0,
        margin_mode: str = None,
        leverage: int = None,
        entry_index: Optional[int] = None,
    ):
        """保存开仓信息（指定方向）"""
        if margin_mode is None:
            margin_mode = TradingConfig.MARGIN_MODE
        if leverage is None:
            leverage = TradingConfig.DEFAULT_LEVERAGE

        position_id = self._generate_position_id(exchange, symbol, method, direction)

        pos = SinglePositionInfo(
            direction=direction,
            has_position=True,
            entry_price=price,
            entry_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            entry_index=entry_index,
            size=size,
            margin_mode=margin_mode,
            leverage=leverage,
            timestamp=int(time.time()),
            position_id=position_id,
            is_liquidated=False,
        )

        key = self._build_key(exchange, symbol, method, direction)
        redis_client.set(key, json.dumps(pos.to_dict()))

        # 同时写入持久化文件
        self.store.save_open_record(
            position_id=position_id,
            exchange=exchange,
            symbol=symbol,
            method=method,
            direction=direction,
            entry_price=price,
            size=size,
            margin_mode=margin_mode,
            leverage=leverage,
        )

        logger.info(f"保存仓位: {direction} {symbol} @ {price}, size={size}, mode={margin_mode}")

    def clear_position(
        self,
        exchange: str,
        symbol: str,
        method: str,
        direction: str = None,       # None=清除全部, "long"/"short"=只清指定方向
        close_price: float = 0,
        reason: str = "",
    ):
        """清除仓位信息"""
        directions = [direction] if direction else ["long", "short"]

        for d in directions:
            key = self._build_key(exchange, symbol, method, d)
            # 先读出旧仓位信息，记录平仓
            raw = redis_client.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                    pos_id = data.get("position_id", "")
                    if pos_id and close_price > 0:
                        self.store.save_close_record(pos_id, close_price, reason)
                except (json.JSONDecodeError, TypeError):
                    pass
            # 清除 Redis
            redis_client.delete(key)
            logger.info(f"清除仓位: {exchange}:{symbol}:{method}:{d}")

    def mark_liquidated(self, exchange: str, symbol: str, method: str, direction: str):
        """标记某方向仓位已爆仓"""
        key = self._build_key(exchange, symbol, method, direction)
        raw = redis_client.get(key)
        if raw:
            try:
                data = json.loads(raw)
                pos_id = data.get("position_id", "")
                if pos_id:
                    self.store.mark_liquidated(pos_id)
            except (json.JSONDecodeError, TypeError):
                pass
        redis_client.delete(key)
        logger.warning(f"仓位爆仓: {exchange}:{symbol}:{method}:{direction}")

    def get_all_active_positions(self) -> dict:
        """获取所有活跃仓位（从持久化文件）"""
        return self.store.get_active_positions()

    def get_position_history(self, symbol: str = None, direction: str = None, limit: int = 100) -> list:
        """查询仓位历史"""
        return self.store.get_history(symbol=symbol, direction=direction, limit=limit)

    def get_recent_liquidations(self, hours: int = 24) -> list:
        """获取最近的爆仓记录"""
        return self.store.get_recent_liquidations(hours)


# 全局实例
position_manager = PositionManager()
