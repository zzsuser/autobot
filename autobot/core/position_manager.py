"""仓位管理 - 支持多空同时持仓 + 持久化存储

【架构修复说明】
- C1: save_position 已支持 size 参数，本文件无需改动签名；
       由调用方（engine._execute_open）传入正确的 size。
- C2: PositionStore 新增 cleanup_orphans()，启动时由 main.py 调用，
       清理 active_positions 里没有对应 Redis 记录的孤儿条目，
       同时归档为 history（action=orphaned），便于审计。
- C5: 启动时打印恢复的仓位详情（在 main.py 的 _startup_reconcile 里实现）。
- 历史 direction 字段曾出现 int -1 → 在加载时统一转字符串，防止后续逻辑错乱。
"""
import json
import time
import os
from typing import Optional, Dict, List
from autobot.cache.redis import redis_client
from autobot.config import TradingConfig
from autobot.utils.logger import logger, log_trade


def _norm_direction(d) -> str:
    """direction 历史脏数据兼容：int -1 → 'short', int 1 → 'long'"""
    if isinstance(d, str):
        return d
    if d == 1:
        return "long"
    if d == -1:
        return "short"
    return str(d) if d is not None else ""


class SinglePositionInfo:
    """单方向仓位信息"""

    def __init__(
        self,
        direction: str = "",
        has_position: bool = False,
        entry_price: float = 0,
        entry_time: str = "",
        entry_index: Optional[int] = None,
        size: float = 0,
        margin_mode: str = "cross",
        leverage: int = 100,
        timestamp: int = 0,
        position_id: str = "",
        is_liquidated: bool = False,
        open_entries: int = 0,
        algo_order_id: str = "",
        last_processed_bar: str = "",
    ):
        self.direction = _norm_direction(direction)
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
        # R16 执行层扩展字段
        self.open_entries = open_entries          # 本轮持仓周期已成交入场批次(1/2/3)
        self.algo_order_id = algo_order_id        # 交易所保护单(TP/SL)订单ID
        self.last_processed_bar = last_processed_bar  # 最近已处理的15m收盘K线开始时间(ISO)

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
            "open_entries": self.open_entries,
            "algo_order_id": self.algo_order_id,
            "last_processed_bar": self.last_processed_bar,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SinglePositionInfo":
        return cls(
            direction=_norm_direction(data.get("direction", "")),
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
            open_entries=int(data.get("open_entries", 0)),
            algo_order_id=data.get("algo_order_id", ""),
            last_processed_bar=data.get("last_processed_bar", ""),
        )


class PositionInfo:
    """完整仓位信息 - 同时包含多仓和空仓"""

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
        if direction == "long":
            return self.long
        elif direction == "short":
            return self.short
        raise ValueError(f"无效方向: {direction}")

    # === 向后兼容 ===

    @property
    def position(self) -> int:
        if self.has_long() and not self.has_short():
            return 1
        elif self.has_short() and not self.has_long():
            return -1
        elif self.has_long() and self.has_short():
            if self.long.timestamp <= self.short.timestamp:
                return 1
            return -1
        return 0

    @property
    def entry_price(self) -> float:
        if self.position == 1:
            return self.long.entry_price
        elif self.position == -1:
            return self.short.entry_price
        return 0

    @property
    def direction(self) -> str:
        if self.position == 1:
            return "long"
        elif self.position == -1:
            return "short"
        return ""

    @property
    def entry_index(self) -> Optional[int]:
        if self.position == 1:
            return self.long.entry_index
        elif self.position == -1:
            return self.short.entry_index
        return None

    def has_position(self) -> bool:
        return self.has_any_position()

    def to_dict(self) -> dict:
        return {
            "long": self.long.to_dict(),
            "short": self.short.to_dict(),
            "position": self.position,
            "has_any": self.has_any_position(),
        }


class PositionStore:
    """仓位持久化存储 - JSON文件"""

    def __init__(self, store_path: str = None):
        self.store_path = store_path or TradingConfig.POSITION_STORE_PATH
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 加载时统一规范化 direction
                for rec in data.get("active_positions", {}).values():
                    rec["direction"] = _norm_direction(rec.get("direction"))
                for rec in data.get("history", []):
                    if "direction" in rec:
                        rec["direction"] = _norm_direction(rec.get("direction"))
                return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"加载仓位记录失败: {e}")
        return {"active_positions": {}, "history": []}

    def _save(self):
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
        record = {
            "position_id": position_id,
            "exchange": exchange,
            "symbol": symbol,
            "method": method,
            "direction": _norm_direction(direction),
            "entry_price": entry_price,
            "size": size,
            "margin_mode": margin_mode,
            "leverage": leverage,
            "open_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "open_timestamp": int(time.time()),
            "status": "open",
            "close_price": 0,
            "close_time": "",
            "pnl": 0,
        }
        self._data["active_positions"][position_id] = record
        self._data["history"].append({**record, "action": "open"})
        self._save()
        log_trade(f"[记录] 开仓: {symbol} {direction} size={size} @ {entry_price}")

    def save_close_record(self, position_id: str, close_price: float, reason: str = ""):
        active = self._data["active_positions"].get(position_id)
        if not active:
            logger.warning(f"[PositionStore] 未找到活跃仓位: {position_id}")
            return

        entry_price = active["entry_price"]
        direction = active["direction"]
        symbol = active.get("symbol", "unknown")
        if direction == "long":
            pnl = (close_price - entry_price) / entry_price if entry_price else 0
        else:
            pnl = (entry_price - close_price) / entry_price if entry_price else 0

        active["status"] = "closed"
        active["close_price"] = close_price
        active["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        active["pnl"] = round(pnl, 6)
        active["close_reason"] = reason

        self._data["history"].append({**active, "action": "close"})
        del self._data["active_positions"][position_id]
        self._save()
        log_trade(f"[记录] 平仓: {symbol} {direction} pnl={pnl*100:.2f}% 原因={reason}")

    def mark_liquidated(self, position_id: str):
        active = self._data["active_positions"].get(position_id)
        if active:
            symbol = active.get("symbol", "unknown")
            direction = active.get("direction", "")
            active["status"] = "liquidated"
            active["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._data["history"].append({**active, "action": "liquidated"})
            del self._data["active_positions"][position_id]
            self._save()
            log_trade(f"[风控] 爆仓: {symbol} {direction}", "WARNING")

    def mark_orphaned(self, position_id: str, reason: str = "启动清理"):
        """标记孤儿仓位（C2）：Redis 没有对应记录"""
        active = self._data["active_positions"].get(position_id)
        if active:
            active["status"] = "orphaned"
            active["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            active["close_reason"] = reason
            self._data["history"].append({**active, "action": "orphaned"})
            del self._data["active_positions"][position_id]
            self._save()
            logger.warning(f"[PositionStore] 标记孤儿: {position_id} ({reason})")

    def get_active_positions(self) -> dict:
        return self._data.get("active_positions", {})

    def get_active_by_symbol(self, symbol: str) -> list:
        return [p for p in self._data["active_positions"].values() if p["symbol"] == symbol]

    def get_history(
        self, symbol: str = None, direction: str = None, limit: int = 100
    ) -> list:
        history = self._data.get("history", [])
        if symbol:
            history = [h for h in history if h.get("symbol") == symbol]
        if direction:
            direction = _norm_direction(direction)
            history = [h for h in history if _norm_direction(h.get("direction")) == direction]
        return history[-limit:]

    def get_recent_liquidations(self, hours: int = 24) -> list:
        cutoff = int(time.time()) - hours * 3600
        return [
            h for h in self._data.get("history", [])
            if h.get("action") == "liquidated" and h.get("open_timestamp", 0) > cutoff
        ]


class PositionManager:
    """仓位管理器 - 支持多空同时持仓"""

    KEY_PREFIX = "pos"

    def __init__(self):
        self.store = PositionStore()

    def _build_key(self, exchange: str, symbol: str, method: str, direction: str) -> str:
        return f"{self.KEY_PREFIX}:{exchange}:{symbol}:{method}:{direction}"

    def _generate_position_id(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> str:
        return f"{exchange}_{symbol}_{method}_{direction}_{int(time.time())}"

    def get_position(self, exchange: str, symbol: str, method: str) -> PositionInfo:
        long_pos = self._get_single(exchange, symbol, method, "long")
        short_pos = self._get_single(exchange, symbol, method, "short")
        return PositionInfo(long=long_pos, short=short_pos)

    def get_single_position(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> SinglePositionInfo:
        return self._get_single(exchange, symbol, method, direction)

    def _get_single(
        self, exchange: str, symbol: str, method: str, direction: str
    ) -> SinglePositionInfo:
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
        direction: str,
        price: float,
        size: float = 0,
        margin_mode: str = None,
        leverage: int = None,
        entry_index: Optional[int] = None,
        open_entries: int = 0,
        algo_order_id: str = "",
        last_processed_bar: str = "",
    ):
        """保存开仓信息"""
        if margin_mode is None:
            margin_mode = TradingConfig.MARGIN_MODE
        if leverage is None:
            leverage = TradingConfig.DEFAULT_LEVERAGE

        direction = _norm_direction(direction)
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
            open_entries=open_entries,
            algo_order_id=algo_order_id,
            last_processed_bar=last_processed_bar,
        )

        key = self._build_key(exchange, symbol, method, direction)
        redis_client.set(key, json.dumps(pos.to_dict()))

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

    def update_position(
        self,
        exchange: str,
        symbol: str,
        method: str,
        direction: str,
        **fields,
    ) -> Optional[SinglePositionInfo]:
        """
        更新已存在仓位的字段（不生成新 position_id，保持历史连贯）。

        用于 R16 加仓（open_entries+1 / 更新均价与张数）和保护单ID、已处理K线等字段。
        返回更新后的 SinglePositionInfo，若仓位不存在返回 None。
        """
        direction = _norm_direction(direction)
        key = self._build_key(exchange, symbol, method, direction)
        raw = redis_client.get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

        pos = SinglePositionInfo.from_dict(data)
        for name, value in fields.items():
            if hasattr(pos, name):
                setattr(pos, name, value)
        pos.timestamp = int(time.time())
        redis_client.set(key, json.dumps(pos.to_dict()))
        logger.debug(f"更新仓位: {exchange}:{symbol}:{method}:{direction} {fields}")
        return pos

    def clear_position(
        self,
        exchange: str,
        symbol: str,
        method: str,
        direction: str = None,
        close_price: float = 0,
        reason: str = "",
    ):
        """清除仓位（同步 Redis + 持久化平仓记录）"""
        directions = [_norm_direction(direction)] if direction else ["long", "short"]

        for d in directions:
            key = self._build_key(exchange, symbol, method, d)
            raw = redis_client.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                    pos_id = data.get("position_id", "")
                    if pos_id and close_price > 0:
                        self.store.save_close_record(pos_id, close_price, reason)
                except (json.JSONDecodeError, TypeError):
                    pass
            redis_client.delete(key)
            logger.info(f"清除仓位: {exchange}:{symbol}:{method}:{d}")

    def mark_liquidated(self, exchange: str, symbol: str, method: str, direction: str):
        """标记某方向仓位已爆仓"""
        direction = _norm_direction(direction)
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
        return self.store.get_active_positions()

    def get_position_history(
        self, symbol: str = None, direction: str = None, limit: int = 100
    ) -> list:
        return self.store.get_history(symbol=symbol, direction=direction, limit=limit)

    def get_recent_liquidations(self, hours: int = 24) -> list:
        return self.store.get_recent_liquidations(hours)

    # ==================== C2: 孤儿清理 ====================

    def cleanup_orphan_active_positions(self) -> List[str]:
        """
        清理 position_history.json 的 active_positions 里没有对应 Redis 记录的孤儿。
        返回被清理的 position_id 列表。
        """
        active = dict(self.store.get_active_positions())  # 拷贝
        orphans = []

        for pid, rec in active.items():
            single = self._get_single(
                rec.get("exchange"),
                rec.get("symbol"),
                rec.get("method"),
                _norm_direction(rec.get("direction")),
            )
            # Redis 没仓 或 Redis 里的 position_id 已经不一样了 → 孤儿
            if (not single.has_position) or (single.position_id and single.position_id != pid):
                orphans.append(pid)

        for pid in orphans:
            self.store.mark_orphaned(pid, reason="启动清理：Redis 无对应记录")

        if orphans:
            logger.warning(f"[启动清理] 清理 {len(orphans)} 个孤儿仓位记录")
        return orphans


# 全局实例
position_manager = PositionManager()
