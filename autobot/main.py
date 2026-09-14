"""
FastAPI 主入口 - 自动化交易服务

【架构修复说明】
- B②: lifespan 启动时执行 _startup_reconcile()：
       * 遍历所有任务配置，对账每个仓位
       * 本地有仓但 OKX 无仓 → 标记 liquidated
       * 明确日志（C5）打印每个恢复的仓位详情
- C2: 启动时调用 position_manager.cleanup_orphan_active_positions()
       清理 JSON 里没有对应 Redis 的孤儿仓位
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from autobot.config import ServerConfig, TradingConfig
from autobot.core.engine import trading_engine
from autobot.core.position_manager import position_manager
from autobot.core.strategy_registry import strategy_registry
from autobot.strategies import register_all_strategies
from autobot.task.task_manager import task_manager
from autobot.utils.logger import logger


@asynccontextmanager
async def lifespan(_) -> AsyncGenerator[None, None]:
    """应用生命周期管理"""
    try:
        # 1. 注册策略
        register_all_strategies()
        logger.info(
            f"已注册策略: {[s['name'] for s in strategy_registry.list_strategies()]}"
        )

        # 2. 从 Redis 恢复任务配置
        await task_manager.load_tasks_from_redis()

        # 3. 启动对账 + 孤儿清理（B② + C2 + C5）
        await _startup_reconcile()

        # 3.1 校验账户持仓模式与 POSITION_MODE 配置一致（R16）
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, trading_engine.trader.validate_position_mode)

        # 4. 启动调度器
        asyncio.create_task(task_manager.start_scheduled_tasks())

        print("========= 自动化交易服务启动成功 =========")
        print(f"  保证金模式: {TradingConfig.MARGIN_MODE}")
        print(f"  逐仓仓位比例: {TradingConfig.POSITION_PERCENT*100:.1f}%")
        print(f"  爆仓联动策略: {TradingConfig.LIQUIDATION_ACTION}")
        yield
    except Exception as e:
        print(f"========= 启动错误: {e}")
        raise
    finally:
        print("========= 收到退出信号 =========")
        try:
            await task_manager.save_tasks_to_redis()
            await task_manager.stop_scheduled_tasks()
            print("========= 服务已关闭 =========")
        except Exception as e:
            logger.exception(f"关闭异常: {e}")


async def _startup_reconcile():
    """
    启动时与交易所对账 + 清理孤儿仓位

    1. C5: 把 Redis 里现有的所有仓位明确打到日志
    2. B②: 对每个任务调用 trading_engine.reconcile_with_exchange
    3. C2: 清理 position_history.json 的孤儿 active_positions
    """
    logger.info("=" * 60)
    logger.info("[启动对账] 开始")

    # ---------- 步骤 1: 列出所有恢复的仓位 ----------
    recovered = []
    for timeframe, exchanges in task_manager.tasks.items():
        for exchange, symbols in exchanges.items():
            for symbol, methods in symbols.items():
                for method in methods:
                    pos = position_manager.get_position(exchange, symbol, method)
                    if pos.has_any_position():
                        recovered.append((exchange, symbol, method, pos))

    if not recovered:
        logger.info("[启动对账] Redis 中无残留仓位")
    else:
        logger.warning(f"[启动对账] 从 Redis 恢复了 {len(recovered)} 个仓位:")
        for exchange, symbol, method, pos in recovered:
            if pos.has_long():
                logger.warning(
                    f"  └─ 多仓: {exchange}/{symbol}/{method} "
                    f"entry={pos.long.entry_price} size={pos.long.size} "
                    f"opened_at={pos.long.entry_time} (timestamp={pos.long.timestamp})"
                )
            if pos.has_short():
                logger.warning(
                    f"  └─ 空仓: {exchange}/{symbol}/{method} "
                    f"entry={pos.short.entry_price} size={pos.short.size} "
                    f"opened_at={pos.short.entry_time} (timestamp={pos.short.timestamp})"
                )

    # ---------- 步骤 2: 与交易所对账 ----------
    if recovered:
        logger.info("[启动对账] 与交易所对账中...")
        loop = asyncio.get_event_loop()
        for exchange, symbol, method, _ in recovered:
            try:
                # engine.reconcile_with_exchange 是同步方法，丢线程池
                result = await loop.run_in_executor(
                    None,
                    trading_engine.reconcile_with_exchange,
                    exchange, symbol, method,
                )
                if result.get("long_liquidated") or result.get("short_liquidated"):
                    logger.warning(
                        f"[启动对账] {exchange}/{symbol}/{method} "
                        f"已被自动清理: {result}"
                    )
                else:
                    logger.info(f"[启动对账] {exchange}/{symbol}/{method} 与交易所一致")
            except Exception as e:
                logger.error(f"[启动对账] {exchange}/{symbol}/{method} 对账失败: {e}")

    # ---------- 步骤 3: 清理孤儿 ----------
    orphans = position_manager.cleanup_orphan_active_positions()
    if orphans:
        logger.warning(f"[启动对账] 清理了 {len(orphans)} 个孤儿仓位记录: {orphans}")

    logger.info("[启动对账] 完成")
    logger.info("=" * 60)


app = FastAPI(lifespan=lifespan, title="Autobot Trading Service")


# ==================== 任务管理 ====================

@app.get("/add")
async def add_task(timeframe: str, exchange: str, symbol: str, method: str):
    if not strategy_registry.has(method):
        available = [s["name"] for s in strategy_registry.list_strategies()]
        return {"success": False, "message": f"策略 '{method}' 不存在, 可用: {available}"}
    try:
        await task_manager.add_task(timeframe, exchange, symbol, method)
        return {
            "success": True,
            "message": f"任务添加成功: {timeframe}min {exchange} {symbol} {method}",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/remove")
async def remove_task(timeframe: str, exchange: str, symbol: str, method: str):
    try:
        await task_manager.remove_task(timeframe, exchange, symbol, method)
        return {"success": True, "message": "任务删除成功"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/status")
async def get_status():
    return {
        "success": True,
        "data": {
            "tasks": task_manager.tasks,
            "active_tasks": list(task_manager.active_tasks),
            "task_count": len(task_manager.active_tasks),
            "margin_mode": TradingConfig.MARGIN_MODE,
            "position_percent": TradingConfig.POSITION_PERCENT,
        },
    }


# ==================== 仓位管理 ====================

@app.get("/position")
async def get_position(exchange: str, symbol: str, method: str):
    pos = position_manager.get_position(exchange, symbol, method)
    return {"success": True, "data": pos.to_dict()}


@app.get("/positions")
async def get_all_positions():
    active = position_manager.get_all_active_positions()
    return {"success": True, "data": active, "count": len(active)}


@app.get("/position_history")
async def get_position_history(
    symbol: str = None, direction: str = None, limit: int = 100
):
    history = position_manager.get_position_history(
        symbol=symbol, direction=direction, limit=limit
    )
    return {"success": True, "data": history, "count": len(history)}


@app.get("/reset")
async def reset_position(exchange: str, symbol: str, method: str, direction: str = None):
    try:
        position_manager.clear_position(exchange, symbol, method, direction=direction)
        dir_str = direction or "全部"
        return {
            "success": True,
            "message": f"仓位已重置: {exchange}/{symbol}/{method}/{dir_str}",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/close")
async def force_close(
    exchange: str, symbol: str, method: str, direction: str = None
):
    try:
        success, msg = trading_engine.force_close(
            exchange, symbol, method, direction=direction
        )
        return {"success": success, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/close_all")
async def force_close_all():
    results = []
    for task_key in list(task_manager.active_tasks):
        parts = task_key.split(":")
        if len(parts) == 4:
            _, exchange, symbol, method = parts
            success, msg = trading_engine.force_close(exchange, symbol, method)
            results.append({"task": task_key, "success": success, "message": msg})
    return {"success": True, "data": results}


# ==================== 风险监控 ====================

@app.get("/liquidation_status")
async def get_liquidation_status(symbol: str = None):
    inst_id = symbol or TradingConfig.DEFAULT_INST_ID
    try:
        risk = trading_engine.trader.check_liquidation_risk(inst_id)
        pos_detail = trading_engine.trader.get_position_detail(inst_id)
        recent_liq = position_manager.get_recent_liquidations(hours=24)
        return {
            "success": True,
            "data": {
                "risk": risk,
                "position_detail": pos_detail,
                "recent_liquidations": recent_liq,
                "warn_ratio": TradingConfig.LIQUIDATION_WARN_RATIO,
                "action_on_liquidation": TradingConfig.LIQUIDATION_ACTION,
            },
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


# ==================== 策略 / 配置 ====================

@app.get("/strategies")
async def list_strategies():
    return {"success": True, "data": strategy_registry.list_strategies()}


@app.get("/config")
async def get_config():
    return {
        "success": True,
        "data": {
            "margin_mode": TradingConfig.MARGIN_MODE,
            "position_percent": TradingConfig.POSITION_PERCENT,
            "default_leverage": TradingConfig.DEFAULT_LEVERAGE,
            "default_ratio": TradingConfig.DEFAULT_RATIO,
            "default_balance": TradingConfig.DEFAULT_BALANCE,
            "contract_size": TradingConfig.CONTRACT_SIZE,
            "default_inst_id": TradingConfig.DEFAULT_INST_ID,
            "liquidation_warn_ratio": TradingConfig.LIQUIDATION_WARN_RATIO,
            "liquidation_action": TradingConfig.LIQUIDATION_ACTION,
            "liquidation_reduce_ratio": TradingConfig.LIQUIDATION_REDUCE_RATIO,
        },
    }


# ==================== 健康检查（新增）====================

@app.get("/health")
async def health_check():
    """快速健康检查：Redis + DBReader + 任务调度器"""
    import time as _time
    health = {"status": "ok", "checks": {}}

    # Redis
    try:
        from autobot.cache.redis import redis_client
        health["checks"]["redis"] = "ok" if redis_client.ping() else "fail"
    except Exception as e:
        health["checks"]["redis"] = f"error: {e}"
        health["status"] = "degraded"

    # DBReader
    try:
        health["checks"]["db"] = "ok" if trading_engine.db_reader._connect() else "fail"
    except Exception as e:
        health["checks"]["db"] = f"error: {e}"
        health["status"] = "degraded"

    # 调度器心跳
    stalled = _time.time() - task_manager._last_main_loop_ts
    health["checks"]["scheduler_stall_sec"] = round(stalled, 2)
    if stalled > 60:
        health["status"] = "degraded"
    if stalled > 180:
        health["status"] = "critical"

    health["active_tasks"] = list(task_manager.active_tasks)
    return health


# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    print("========= 启动 Autobot Trading Service =========")
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT)
