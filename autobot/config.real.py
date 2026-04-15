"""统一配置管理"""
import os


class DBConfig:
    HOST = os.getenv("DB_HOST", "localhost")
    PORT = int(os.getenv("DB_PORT", 3306))
    USER = os.getenv("DB_USER", "trade")
    PASSWORD = os.getenv("DB_PASSWORD", "mPy7CP222WBdA68t")
    DATABASE = os.getenv("DB_NAME", "trade")
    CHARSET = "utf8mb4"

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


class RedisConfig:
    URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    MAX_CONNECTIONS = 10


class OKXConfig:
    API_KEY = os.getenv("OKX_API_KEY", "e0ce517b-9df1-4f78-9796-9dcf2c7bf5e9")
    SECRET_KEY = os.getenv("OKX_SECRET_KEY", "673D46751725B96C541C1D4F644F7899")
    PASSPHRASE = os.getenv("OKX_PASSPHRASE", "@1Qwertyuiop")
    FLAG = os.getenv("OKX_FLAG", "0")  # 0=实盘, 1=模拟盘


class TradingConfig:
    DEFAULT_LEVERAGE = 100
    DEFAULT_RATIO = 0.03
    DEFAULT_BALANCE = 5
    CONTRACT_SIZE = 0.1  # ETH-USDT-SWAP 每张合约
    DEFAULT_INST_ID = "ETH-USDT-SWAP"
    STOP_PROFIT_PCT = 0.02
    STOP_LOSS_PCT = 0.01


class ServerConfig:
    HOST = "0.0.0.0"
    PORT = 9002
