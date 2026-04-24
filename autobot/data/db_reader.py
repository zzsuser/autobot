"""数据库读取器 - 从MySQL读取K线和指标数据"""
import pymysql
import pandas as pd
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
        self.connection = None

    def _connect(self) -> bool:
        """获取数据库连接，支持断线重连"""
        try:
            if self.connection and self.connection.open:
                self.connection.ping(reconnect=True)
                return True
            self.connection = pymysql.connect(**DBConfig.to_dict(), autocommit=True)
            #self.connection = pymysql.connect(**DBConfig.to_dict())
            return True
        except pymysql.Error as e:
            logger.error(f"数据库连接失败: {e}")
            self.connection = None
            return False

    def _safe_close(self):
        """安全关闭连接"""
        try:
            if self.connection:
                self.connection.close()
        except Exception:
            pass
        finally:
            self.connection = None

    def _get_id(self, table: str, value: str):
        """
        查询表的 ID（白名单校验，防止 SQL 注入）

        Args:
            table: 表名，必须在 VALID_LOOKUPS 中
            value: 查询值
        """
        if table not in self.VALID_LOOKUPS:
            raise ValueError(f"不允许查询的表: {table}")

        column = self.VALID_LOOKUPS[table]

        with self.connection.cursor() as cursor:
            # table 和 column 来自白名单硬编码，安全
            sql = f"SELECT id FROM `{table}` WHERE `{column}` = %s LIMIT 1"
            cursor.execute(sql, (value,))
            result = cursor.fetchone()
            return result[0] if result else None

    def get_data(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        """
        获取K线数据和指标

        Args:
            symbol: 币种 (如 'ETHUSDT')
            interval: 周期 (如 '5min', '15min', '1h')
            limit: 最近多少条

        Returns:
            按时间升序排列的DataFrame，失败返回 None
        """
        if not self._connect():
            return None

        try:
            currency_id = self._get_id("currencies", symbol)
            interval_id = self._get_id("time_intervals", interval)

            if not currency_id or not interval_id:
                logger.error(f"未找到: symbol={symbol} 或 interval={interval}")
                return None

            sql = """
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
                WHERE k.currency_id = %s AND k.interval_id = %s
                ORDER BY k.open_time DESC
                LIMIT %s
            """

            df = pd.read_sql(sql, self.connection, params=(currency_id, interval_id, limit))

            if df.empty:
                logger.warning(f"无数据: {symbol}/{interval}")
                return pd.DataFrame()

            # 按时间升序排列并重置索引
            return df.sort_values("open_time").reset_index(drop=True)

        except pymysql.OperationalError as e:
            # 连接级错误（超时、断开等），关闭连接以便下次重连
            logger.error(f"数据库操作异常(将重连): {e}")
            self._safe_close()
            return None
        except pymysql.Error as e:
            logger.error(f"数据库查询异常: {e}")
            return None
        except Exception as e:
            logger.error(f"未预期异常: {e}", exc_info=True)
            return None

    def close(self):
        """外部调用的关闭方法"""
        self._safe_close()
        logger.info("DBReader 连接已关闭")

    def __del__(self):
        """析构时确保连接释放"""
        self._safe_close()
