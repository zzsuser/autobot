"""数据库读取器 - 从MySQL读取K线和指标数据

【架构修复说明】
- A①: 返回的 DataFrame 使用 open_time 作为 DatetimeIndex，
       策略代码里 df.index[idx] 自然得到 pd.Timestamp，不需要修改策略
- C4: 改用 SQLAlchemy engine 喂 pd.read_sql，消除 pandas UserWarning
       同时启用 pool_pre_ping，自动检测断连并重连
"""
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from autobot.config import DBConfig
from autobot.utils.logger import logger


class DBReader:
    """数据库行情读取器"""

    # 白名单：只允许查询这些表和列
    VALID_LOOKUPS = {
        "currencies": "symbol",
        "time_intervals": "interval",
    }

    def __init__(self):
        self._engine: Engine = None

    def _get_engine(self) -> Engine:
        """惰性创建 SQLAlchemy engine"""
        if self._engine is not None:
            return self._engine

        c = DBConfig.to_dict()
        url = (
            f"mysql+pymysql://{c['user']}:{c['password']}"
            f"@{c['host']}:{c['port']}/{c['database']}?charset={c['charset']}"
        )
        self._engine = create_engine(
            url,
            pool_pre_ping=True,         # 每次取连接前 ping，自动检测断连
            pool_recycle=3600,          # 1 小时强制回收，避开 MySQL wait_timeout
            pool_size=5,
            max_overflow=5,
            connect_args={
                "connect_timeout": 30,
                "read_timeout": 30,
                "write_timeout": 30,
            },
        )
        logger.info(f"DBReader: SQLAlchemy engine 已创建 ({c['user']}@{c['host']}:{c['port']}/{c['database']})")
        return self._engine

    def _connect(self) -> bool:
        """兼容旧接口：测试连接是否可用（test_all.py 等会调）"""
        try:
            engine = self._get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as e:
            logger.error(f"数据库连接失败: {e}")
            return False

    def _safe_close(self):
        """销毁 engine（下次访问会重建）"""
        try:
            if self._engine:
                self._engine.dispose()
        except Exception:
            pass
        finally:
            self._engine = None

    def _get_id(self, table: str, value: str):
        """白名单查询表 ID（防 SQL 注入）"""
        if table not in self.VALID_LOOKUPS:
            raise ValueError(f"不允许查询的表: {table}")

        column = self.VALID_LOOKUPS[table]
        engine = self._get_engine()

        with engine.connect() as conn:
            sql = text(f"SELECT id FROM `{table}` WHERE `{column}` = :val LIMIT 1")
            result = conn.execute(sql, {"val": value})
            row = result.fetchone()
            return row[0] if row else None

    def get_data(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        """
        获取K线数据和指标

        Args:
            symbol: 币种 (如 'ETHUSDT')
            interval: 周期 (如 '5min', '15min', '1h')
            limit: 最近多少条

        Returns:
            按时间升序排列的 DataFrame，index 为 DatetimeIndex（open_time）。
            同时保留 open_time 列。
            失败返回 None；无数据返回空 DataFrame。
        """
        try:
            currency_id = self._get_id("currencies", symbol)
            interval_id = self._get_id("time_intervals", interval)

            if not currency_id or not interval_id:
                logger.error(f"未找到: symbol={symbol} 或 interval={interval}")
                return None

            sql = text("""
                SELECT
                    k.open_time, k.open, k.high, k.low, k.close, k.volume,
                    e.ema_24, e.ema_48, e.ema_60, e.ema_72, e.ema_144, e.ema_288,
                    s.sma_5, s.sma_10, s.sma_20, s.sma_30, s.sma_60, s.sma_120, s.sma_144,
                    t.tema_24, t.tema_48, t.tema_60, t.tema_72, t.tema_144, t.tema_288,
                    st.supertrend_value, st.supertrend_direction, st.upper_band, st.lower_band
                FROM kline_data k
                LEFT JOIN ema_indicators e ON k.id = e.kline_id
                LEFT JOIN sma_indicators s ON k.id = s.kline_id
                LEFT JOIN tema_indicators t ON k.id = t.kline_id
                LEFT JOIN supertrend_indicators st ON k.id = st.kline_id
                WHERE k.currency_id = :cid AND k.interval_id = :iid
                  -- 【2026-09-04 修复】只返回 OKX 已确认(confirm=1)的收盘 K 线。
                  -- 数据管道在 bar 开始数秒即 upsert 半成品(confirm=0)，直到 bar 结束后才
                  -- 更新为最终值(confirm=1)；若引擎把半成品当已收盘处理会漏/误判形态信号，
                  -- 故读取端强制过滤，配合引擎端 open_time 时间剔除作双保险。
                  AND k.confirm = 1
                ORDER BY k.open_time DESC
                LIMIT :lim
            """)

            engine = self._get_engine()
            df = pd.read_sql(
                sql, engine,
                params={"cid": currency_id, "iid": interval_id, "lim": limit},
            )

            if df.empty:
                logger.warning(f"无数据: {symbol}/{interval}")
                return pd.DataFrame()

            # 升序排列
            df = df.sort_values("open_time")
            # open_time 转 datetime 类型
            # unit='ms'：数据库 open_time 是毫秒级时间戳，必须显式指定单位
            # 否则 pandas 默认按纳秒解析，产生 1970-01-01 等错误时间戳
            df["open_time"] = pd.to_datetime(df["open_time"], unit='ms')
            # 设为 DatetimeIndex，但保留 open_time 列（drop=False）
            # —— 这样策略里 df.index[idx] 拿到的是 Timestamp，而 df["open_time"] 也能用
            df = df.set_index("open_time", drop=False)
            df.index.name = None  # 避免列名冲突

            return df

        except SQLAlchemyError as e:
            logger.error(f"数据库操作异常: {e}")
            self._safe_close()
            return None
        except Exception as e:
            logger.error(f"未预期异常: {e}", exc_info=True)
            return None

    def close(self):
        """外部调用的关闭方法"""
        self._safe_close()
        logger.info("DBReader 连接已关闭")

    def __del__(self):
        self._safe_close()
