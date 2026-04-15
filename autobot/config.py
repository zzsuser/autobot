"""统一配置管理 - 从 .passwd 文件加载敏感信息"""
import os
from pathlib import Path


def _load_passwd_file():
    """
    加载 .passwd 文件中的配置到环境变量
    查找顺序：项目根目录 -> 当前工作目录 -> 用户主目录
    """
    search_paths = [
        Path(__file__).resolve().parent.parent / ".passwd",  # 项目根目录
        Path.cwd() / ".passwd",                               # 当前工作目录
        Path.home() / ".passwd",                               # 用户主目录
    ]

    passwd_file = None
    for p in search_paths:
        if p.is_file():
            passwd_file = p
            break

    if passwd_file is None:
        print("[WARNING] 未找到 .passwd 文件，将使用环境变量或默认值")
        print(f"[WARNING] 搜索路径: {[str(p) for p in search_paths]}")
        return

    print(f"[CONFIG] 从 {passwd_file} 加载配置")
    with open(passwd_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # 仅在环境变量未设置时加载（环境变量优先级更高）
            if key and key not in os.environ:
                os.environ[key] = value


# 模块加载时立即执行
_load_passwd_file()


class DBConfig:
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = int(os.getenv("DB_PORT", "3306"))
    USER = os.getenv("DB_USER", "trade")
    PASSWORD = os.getenv("DB_PASSWORD", "")
    DATABASE = os.getenv("DB_NAME", "trade")
    CHARSET = os.getenv("DB_CHARSET", "utf8mb4")

    @classmethod
    def to_dict(cls):
        return {
            "host": cls.HOST,
            "port": cls.PORT,
            "user": cls.USER,
            "password": cls.PASSWORD,
            "database": cls.DATABASE,
            "charset": cls.CHARSET,
            "connect_timeout": 30,
            "read_timeout": 30,
            "write_timeout": 30,
        }

    @classmethod
    def display_safe(cls):
        """打印脱敏的配置信息（用于调试）"""
        pwd = cls.PASSWORD
        masked = pwd[:2] + "***" + pwd[-2:] if len(pwd) > 4 else "***"
        return f"DB: {cls.USER}@{cls.HOST}:{cls.PORT}/{cls.DATABASE} pwd={masked}"


class RedisConfig:
    URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", "10"))


class OKXConfig:
    API_KEY = os.getenv("OKX_API_KEY", "")
    SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
    PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
    FLAG = os.getenv("OKX_FLAG", "0")  # 0=实盘, 1=模拟盘

    @classmethod
    def is_configured(cls) -> bool:
        """检查OKX API是否已配置"""
        return bool(cls.API_KEY and cls.SECRET_KEY and cls.PASSPHRASE)

    @classmethod
    def display_safe(cls):
        """打印脱敏的API信息"""
        key = cls.API_KEY
        masked = key[:4] + "***" + key[-4:] if len(key) > 8 else "***"
        mode = "实盘" if cls.FLAG == "0" else "模拟盘"
        return f"OKX: key={masked} mode={mode}"

class TradingConfig:
    DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "100"))
    DEFAULT_RATIO = float(os.getenv("DEFAULT_RATIO", "0.03"))
    DEFAULT_BALANCE = float(os.getenv("DEFAULT_BALANCE", "5"))
    CONTRACT_SIZE = float(os.getenv("CONTRACT_SIZE", "0.1"))
    DEFAULT_INST_ID = os.getenv("DEFAULT_INST_ID", "ETH-USDT-SWAP")
    STOP_PROFIT_PCT = float(os.getenv("STOP_PROFIT_PCT", "0.02"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.01"))

    # ===== 新增配置项 =====

    # 保证金模式: "cross"=全仓, "isolated"=逐仓
    MARGIN_MODE = os.getenv("MARGIN_MODE", "cross")

    # 逐仓时，单个仓位占总账户资产的百分比 (0.1 = 10%)
    POSITION_PERCENT = float(os.getenv("POSITION_PERCENT", "0.1"))

    # 仓位记录持久化文件路径
    POSITION_STORE_PATH = os.getenv("POSITION_STORE_PATH", "position_history.json")

    # 爆仓预警: 保证金率低于此值时触发预警 (0.15 = 15%)
    LIQUIDATION_WARN_RATIO = float(os.getenv("LIQUIDATION_WARN_RATIO", "0.15"))

    # 爆仓后联动策略: "reduce"=其他仓位减仓, "close_all"=全部平仓, "none"=不处理
    LIQUIDATION_ACTION = os.getenv("LIQUIDATION_ACTION", "reduce")

    # 爆仓联动减仓比例 (当 LIQUIDATION_ACTION="reduce" 时生效)
    LIQUIDATION_REDUCE_RATIO = float(os.getenv("LIQUIDATION_REDUCE_RATIO", "0.5"))

class ServerConfig:
    HOST = os.getenv("SERVER_HOST", "0.0.0.0")
    PORT = int(os.getenv("SERVER_PORT", "9002"))
