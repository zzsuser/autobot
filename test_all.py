#!/usr/bin/env python3
"""
Autobot 全面测试脚本

测试项目:
1. 配置加载（.passwd文件读取）
2. Redis连接
3. 数据库连接与数据读取
4. 策略注册与信号生成
5. OKX价格获取
6. 仓位管理
7. 交易引擎完整流程（使用模拟数据）
8. FastAPI接口

用法:
    python test_all.py              # 运行所有测试
    python test_all.py config       # 只测配置
    python test_all.py redis        # 只测Redis
    python test_all.py db           # 只测数据库
    python test_all.py strategy     # 只测策略
    python test_all.py price        # 只测价格获取
    python test_all.py position     # 只测仓位管理
    python test_all.py engine       # 只测引擎
    python test_all.py api          # 只测API接口
"""
import sys
import time
import json
import traceback
import numpy as np
import pandas as pd
from typing import List, Tuple


# ============================================================
# 工具函数
# ============================================================

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {Colors.BOLD}{Colors.CYAN}{title}{Colors.END}")
    print(f"{'=' * 60}")


def print_pass(msg: str):
    print(f"  {Colors.GREEN}✓ PASS{Colors.END}  {msg}")


def print_fail(msg: str, error: str = ""):
    print(f"  {Colors.RED}✗ FAIL{Colors.END}  {msg}")
    if error:
        print(f"          {Colors.RED}{error}{Colors.END}")


def print_warn(msg: str):
    print(f"  {Colors.YELLOW}⚠ WARN{Colors.END}  {msg}")


def print_info(msg: str):
    print(f"         {msg}")


results: List[Tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    if passed:
        print_pass(f"{name} {detail}")
    else:
        print_fail(name, detail)


# ============================================================
# 测试1: 配置加载
# ============================================================

def test_config():
    print_header("测试1: 配置加载 (.passwd)")

    try:
        from autobot.config import DBConfig, RedisConfig, OKXConfig, TradingConfig, ServerConfig

        # 数据库配置
        assert DBConfig.HOST, "DB_HOST 为空"
        assert DBConfig.PORT > 0, f"DB_PORT 异常: {DBConfig.PORT}"
        assert DBConfig.USER, "DB_USER 为空"
        assert DBConfig.PASSWORD, "DB_PASSWORD 为空（请检查 .passwd 文件）"
        assert DBConfig.DATABASE, "DB_NAME 为空"
        record("DBConfig加载", True, DBConfig.display_safe())

        # Redis配置
        assert RedisConfig.URL, "REDIS_URL 为空"
        record("RedisConfig加载", True, f"URL={RedisConfig.URL}")

        # OKX配置
        if OKXConfig.is_configured():
            record("OKXConfig加载", True, OKXConfig.display_safe())
        else:
            record("OKXConfig加载", True, "API未配置（部分功能不可用）")
            print_warn("OKX API 密钥未配置，交易功能将不可用")

        # 交易参数
        assert TradingConfig.DEFAULT_LEVERAGE > 0
        assert 0 < TradingConfig.DEFAULT_RATIO < 1
        record("TradingConfig加载", True,
               f"杠杆={TradingConfig.DEFAULT_LEVERAGE}x, 比例={TradingConfig.DEFAULT_RATIO}")

        # 服务器配置
        assert ServerConfig.PORT > 0
        record("ServerConfig加载", True,
               f"{ServerConfig.HOST}:{ServerConfig.PORT}")

        # 测试 to_dict
        d = DBConfig.to_dict()
        assert "host" in d and "password" in d
        record("DBConfig.to_dict()", True)

    except Exception as e:
        record("配置加载", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试2: Redis连接
# ============================================================

def test_redis():
    print_header("测试2: Redis连接")

    try:
        from autobot.cache.redis import redis_client

        # Ping
        ok = redis_client.ping()
        record("Redis Ping", ok, "连接正常" if ok else "无法连接")

        if not ok:
            print_warn("Redis未连接，跳过后续Redis测试")
            return

        # Set/Get
        test_key = "__autobot_test__"
        test_val = json.dumps({"test": True, "time": time.time()})
        redis_client.set(test_key, test_val, expiration=60)
        got = redis_client.get(test_key)
        match = got == test_val
        record("Redis Set/Get", match, f"写入并读回验证{'一致' if match else '不一致'}")

        # Delete
        redis_client.delete(test_key)
        after = redis_client.get(test_key)
        record("Redis Delete", after is None, "删除后确认为空")

        # Hash操作
        hash_key = "__autobot_test_hash__"
        redis_client.hset(hash_key, {"a": "1", "b": "2"}, expiration=60)
        val = redis_client.hget(hash_key, "a")
        record("Redis Hash", val is not None, f"hget(a)={val}")
        redis_client.delete(hash_key)

    except Exception as e:
        record("Redis连接", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试3: 数据库连接
# ============================================================

def test_database():
    print_header("测试3: 数据库连接与数据读取")

    try:
        from autobot.data.db_reader import DBReader

        reader = DBReader()

        # 连接测试
        connected = reader._connect()
        record("数据库连接", connected, "连接成功" if connected else "连接失败")

        if not connected:
            print_warn("数据库未连接，跳过数据读取测试")
            return

        # 获取数据
        df = reader.get_data("ETHUSDT", "5min", limit=100)
        if df is not None and not df.empty:
            record("读取K线数据", True, f"获取 {len(df)} 条 ETHUSDT/5min 数据")

            # 检查列
            expected_cols = ["close", "high", "low", "volume"]
            missing = [c for c in expected_cols if c not in df.columns]
            record("数据列完整性", len(missing) == 0,
                   f"列: {list(df.columns[:8])}..." if len(missing) == 0
                   else f"缺少列: {missing}")

            # 检查指标列
            indicator_cols = ["tema_48", "tema_72", "tema_144", "tema_288",
                              "supertrend_value", "supertrend_direction"]
            found = [c for c in indicator_cols if c in df.columns]
            record("指标数据", len(found) > 0,
                   f"已有指标: {found}" if found else "无预计算指标（策略将自行计算）")

            # 检查时间有序
            if "open_time" in df.columns:
                is_sorted = df["open_time"].is_monotonic_increasing
                record("时间排序", is_sorted, "升序排列" if is_sorted else "未排序!")
        else:
            record("读取K线数据", False, "返回空数据（可能表或数据不存在）")

        reader.close()

    except Exception as e:
        record("数据库测试", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试4: 策略注册与信号生成
# ============================================================

def _generate_mock_dataframe(length: int = 600) -> pd.DataFrame:
    """生成模拟K线数据用于策略测试"""
    np.random.seed(42)
    base_price = 2000.0
    returns = np.random.normal(0, 0.003, length)
    prices = base_price * np.cumprod(1 + returns)

    df = pd.DataFrame({
        "open_time": pd.date_range("2025-01-01", periods=length, freq="5min"),
        "open": prices * (1 - np.abs(np.random.normal(0, 0.001, length))),
        "high": prices * (1 + np.abs(np.random.normal(0, 0.002, length))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.002, length))),
        "close": prices,
        "volume": np.random.uniform(100, 10000, length),
    })
    return df


def test_strategy():
    print_header("测试4: 策略注册与信号生成")

    try:
        from autobot.core.strategy_registry import strategy_registry
        from autobot.core.strategy_base import SignalResult

        # 注册策略
        from autobot.strategies import register_all_strategies
        register_all_strategies()

        strategies = strategy_registry.list_strategies()
        record("策略注册", len(strategies) > 0, f"已注册 {len(strategies)} 个策略")
        for s in strategies:
            print_info(f"  - {s['name']} (数据需求={s['required_data_length']}, 止盈止损={s['need_stop_check']})")

        # 测试 SuperTrend+TEMA 策略
        st_strategy = strategy_registry.get("supertrend_tema")
        if st_strategy is None:
            record("SuperTrend+TEMA策略", False, "未找到策略")
            return

        record("获取SuperTrend+TEMA策略", True)

        # 生成模拟数据
        df = _generate_mock_dataframe(600)
        print_info(f"模拟数据: {len(df)} 条, 价格范围 {df['close'].min():.2f} ~ {df['close'].max():.2f}")

        # 测试无仓位时的信号
        t0 = time.time()
        signal = st_strategy.generate_signal(df=df, current_position=0)
        elapsed = (time.time() - t0) * 1000
        record("无仓位信号生成", True,
               f"signal={signal}, 耗时={elapsed:.1f}ms")

        # 测试有多仓时的信号
        entry_price = df["close"].iloc[-50]
        signal_long = st_strategy.generate_signal(
            df=df, current_position=1,
            entry_index=len(df) - 50,
            entry_price=entry_price,
        )
        record("多仓信号生成", True, f"signal={signal_long}")

        # 测试有空仓时的信号
        signal_short = st_strategy.generate_signal(
            df=df, current_position=-1,
            entry_index=len(df) - 50,
            entry_price=entry_price,
        )
        record("空仓信号生成", True, f"signal={signal_short}")

        # 测试止盈止损
        result_stop = st_strategy.check_stop(
            current_position=1,
            entry_price=2000,
            current_price=1930,  # 跌了3.5%，应该触发止损
        )
        is_stop = result_stop.signal != SignalResult.NO_SIGNAL
        record("止损触发测试(跌3.5%)", is_stop, f"{result_stop}")

        result_profit = st_strategy.check_stop(
            current_position=1,
            entry_price=2000,
            current_price=2060,  # 涨了3%，应该触发止盈
        )
        is_profit = result_profit.signal != SignalResult.NO_SIGNAL
        record("止盈触发测试(涨3%)", is_profit, f"{result_profit}")

        result_hold = st_strategy.check_stop(
            current_position=1,
            entry_price=2000,
            current_price=2010,  # 涨了0.5%，应该不触发
        )
        is_hold = result_hold.signal == SignalResult.NO_SIGNAL
        record("持仓不触发测试(涨0.5%)", is_hold, f"{result_hold}")

    except Exception as e:
        record("策略测试", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试5: OKX价格获取
# ============================================================

def test_price():
    print_header("测试5: OKX价格获取")

    try:
        from autobot.exchange.okx_trader import OKXTrader

        trader = OKXTrader()

        # 获取ETH价格（公开API，不需要API key）
        t0 = time.time()
        price = trader.get_current_price("ETH-USDT-SWAP")
        elapsed = (time.time() - t0) * 1000

        if price > 0:
            record("获取ETH价格", True, f"${price:.2f} (耗时{elapsed:.0f}ms)")
        else:
            record("获取ETH价格", False, "返回0（可能网络不通或API限制）")
            print_warn("价格获取失败，请检查网络连接或代理设置")

    except Exception as e:
        record("价格获取", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试6: 仓位管理
# ============================================================

def test_position():
    print_header("测试6: 仓位管理")

    try:
        from autobot.cache.redis import redis_client

        if not redis_client.ping():
            print_warn("Redis未连接，跳过仓位管理测试")
            record("仓位管理", False, "Redis不可用")
            return

        from autobot.core.position_manager import position_manager

        test_exchange = "__test__"
        test_symbol = "TEST-USDT"
        test_method = "test_strategy"

        # 清除可能存在的测试数据
        position_manager.clear_position(test_exchange, test_symbol, test_method)

        # 确认无仓位
        pos = position_manager.get_position(test_exchange, test_symbol, test_method)
        record("初始无仓位", not pos.has_position(), f"position={pos.position}")

        # 保存多仓
        position_manager.save_position(
            test_exchange, test_symbol, test_method,
            position=1, price=2000.5, direction="long", entry_index=100,
        )
        pos = position_manager.get_position(test_exchange, test_symbol, test_method)
        record("保存多仓", pos.position == 1, f"pos={pos.position}, price={pos.entry_price}")
        record("读取开仓价", abs(pos.entry_price - 2000.5) < 0.01, f"entry_price={pos.entry_price}")
        record("读取入场索引", pos.entry_index == 100, f"entry_index={pos.entry_index}")
        record("读取方向", pos.direction == "long", f"direction={pos.direction}")

        # 清除
        position_manager.clear_position(test_exchange, test_symbol, test_method)
        pos = position_manager.get_position(test_exchange, test_symbol, test_method)
        record("清除仓位", not pos.has_position(), f"清除后 position={pos.position}")

    except Exception as e:
        record("仓位管理", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试7: 引擎模拟流程（不实际交易）
# ============================================================

def test_engine():
    print_header("测试7: 交易引擎（模拟）")

    try:
        from autobot.core.strategy_registry import strategy_registry
        from autobot.strategies import register_all_strategies

        # 确保策略已注册
        if not strategy_registry.list_strategies():
            register_all_strategies()

        from autobot.core.engine import TradingEngine

        engine = TradingEngine()

        # 测试策略获取
        strategy = strategy_registry.get("supertrend_tema")
        record("引擎策略获取", strategy is not None)

        # 测试数据库读取（如果可用）
        from autobot.data.db_reader import DBReader
        reader = DBReader()
        if reader._connect():
            df = reader.get_data("ETHUSDT", "5min", limit=600)
            if df is not None and not df.empty:
                record("引擎数据获取", True, f"获取 {len(df)} 条数据")

                # 用真实数据测试策略
                signal = strategy.generate_signal(df=df, current_position=0)
                record("引擎策略执行(真实数据)", True, f"{signal}")
            else:
                record("引擎数据获取", False, "无数据")
                print_info("使用模拟数据测试...")
                df = _generate_mock_dataframe(600)
                signal = strategy.generate_signal(df=df, current_position=0)
                record("引擎策略执行(模拟数据)", True, f"{signal}")
            reader.close()
        else:
            print_warn("数据库未连接，使用模拟数据")
            df = _generate_mock_dataframe(600)
            signal = strategy.generate_signal(df=df, current_position=0)
            record("引擎策略执行(模拟数据)", True, f"{signal}")

    except Exception as e:
        record("引擎测试", False, str(e))
        traceback.print_exc()


# ============================================================
# 测试8: FastAPI接口
# ============================================================

def test_api():
    print_header("测试8: FastAPI接口")

    try:
        from fastapi.testclient import TestClient
        from autobot.main import app

        client = TestClient(app)

        # /strategies
        resp = client.get("/strategies")
        record("GET /strategies", resp.status_code == 200,
               f"status={resp.status_code}, 策略数={len(resp.json().get('data', []))}")

        # /status
        resp = client.get("/status")
        record("GET /status", resp.status_code == 200,
               f"status={resp.status_code}")

        # /position (需要参数)
        resp = client.get("/position", params={
            "exchange": "okx", "symbol": "ETH-USDT-SWAP", "method": "supertrend_tema"
        })
        record("GET /position", resp.status_code == 200,
               f"status={resp.status_code}, data={resp.json().get('data', {}).get('position', '?')}")

        # /add (添加然后删除)
        params = {
            "timeframe": "99",
            "exchange": "test",
            "symbol": "TEST-USDT",
            "method": "supertrend_tema",
        }
        resp = client.get("/add", params=params)
        add_ok = resp.status_code == 200 and resp.json().get("success")
        record("GET /add", add_ok, f"{resp.json().get('message', '')}")

        # /remove
        resp = client.get("/remove", params=params)
        rm_ok = resp.status_code == 200 and resp.json().get("success")
        record("GET /remove", rm_ok, f"{resp.json().get('message', '')}")

        # /add 不存在的策略
        resp = client.get("/add", params={**params, "method": "not_exist"})
        record("GET /add(无效策略)", resp.status_code == 200 and not resp.json().get("success"),
               f"正确拒绝: {resp.json().get('message', '')[:50]}")

    except ImportError:
        print_warn("需要 httpx 库: pip install httpx")
        record("FastAPI测试", False, "缺少 httpx 依赖")
    except Exception as e:
        record("FastAPI测试", False, str(e))
        traceback.print_exc()


# ============================================================
# 主入口
# ============================================================

TEST_MAP = {
    "config": test_config,
    "redis": test_redis,
    "db": test_database,
    "strategy": test_strategy,
    "price": test_price,
    "position": test_position,
    "engine": test_engine,
    "api": test_api,
}


def main():
    print(f"\n{Colors.BOLD}{'=' * 60}")
    print(f"  Autobot 自动化交易平台 - 全面测试")
    print(f"{'=' * 60}{Colors.END}")

    # 选择测试项
    if len(sys.argv) > 1:
        targets = sys.argv[1:]
        for t in targets:
            if t in TEST_MAP:
                TEST_MAP[t]()
            else:
                print(f"\n未知测试项: {t}")
                print(f"可选: {', '.join(TEST_MAP.keys())}")
                return
    else:
        # 运行全部
        for func in TEST_MAP.values():
            func()

    # 打印汇总
    print(f"\n{'=' * 60}")
    print(f"  {Colors.BOLD}测��汇总{Colors.END}")
    print(f"{'=' * 60}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)

    for name, ok, detail in results:
        status = f"{Colors.GREEN}PASS{Colors.END}" if ok else f"{Colors.RED}FAIL{Colors.END}"
        print(f"  [{status}] {name}")

    print(f"\n  总计: {total}  通过: {Colors.GREEN}{passed}{Colors.END}  失败: {Colors.RED}{failed}{Colors.END}")

    if failed == 0:
        print(f"\n  {Colors.GREEN}{Colors.BOLD} 全部测试通过！{Colors.END}\n")
    else:
        print(f"\n  {Colors.YELLOW}请检查失败项，常见原因：{Colors.END}")
        print(f"    1. .passwd 文件不存在或配置错误")
        print(f"    2. Redis/MySQL 服务未启动")
        print(f"    3. 数据库中无 ETHUSDT 数据")
        print(f"    4. 网络无法访问 OKX API\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
