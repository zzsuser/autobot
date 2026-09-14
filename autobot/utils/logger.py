"""日志配置"""
import logging
import sys
from logging.handlers import RotatingFileHandler

from pathlib import Path

_log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
_log_dir.mkdir(exist_ok=True)


def setup_logger(name: str = "autobot", level: str = "INFO") -> logging.Logger:
    _logger = logging.getLogger(name)
    if _logger.handlers:
        return _logger

    _logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    _logger.addHandler(console)

    # 文件输出
    try:
        file_handler = RotatingFileHandler(
            _log_dir / "autobot.log", encoding="utf-8",
            maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)
    except Exception:
        pass

    return _logger


def _setup_trade_logger() -> logging.Logger:
    """独立交易日志：只记录开平仓动作，写入 logs/trade.log"""
    tl = logging.getLogger("autobot.trade")
    if tl.handlers:
        return tl

    tl.setLevel(logging.INFO)
    tl.propagate = False  # 避免重复输出到 autobot.log 和控制台

    trade_formatter = logging.Formatter(
        fmt="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    trade_handler = RotatingFileHandler(
        _log_dir / "trade.log", encoding="utf-8",
        maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    trade_handler.setFormatter(trade_formatter)
    tl.addHandler(trade_handler)
    return tl


logger = setup_logger()
_trade_logger = _setup_trade_logger()


def log_trade(msg: str, level: str = "INFO"):
    """同时写入 autobot.log 和 trade.log"""
    # 写入主日志（autobot.log + 控制台）
    getattr(logger, level.lower())(msg)
    # 写入独立交易日志（trade.log）
    getattr(_trade_logger, level.lower())(msg)
