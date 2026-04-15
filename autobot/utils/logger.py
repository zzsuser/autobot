"""日志配置"""
import logging
import sys

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
        from pathlib import Path
        log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "autobot.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)
    except Exception:
        pass

    return _logger

logger = setup_logger()
