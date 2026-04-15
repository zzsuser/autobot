"""
FastAPI 主入口 - 自动化交易服务

功能:
- /add       添加交易任务
- /remove    移除交易任务
- /status    查看任务状态
- /reset     重置仓位
- /close     强制清仓（支持指定方向）
- /strategies 查看可用策略
- /position  查看仓位信息（多+空）
- /positions 查看所有活跃仓位
- /position_history 查看仓位历史
- /liquidation_status 查看爆仓风险
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from autobot.config import ServerConfig, TradingConfig
from autobot.cache.redis import redis_client
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
        # 注册所有策略
        register_all_strategies()
        logger.info(f"已注册策略: {[s['name'] for s in strategy_registry.list_strategies()]}")

        # 从Redis恢复任务
        await task_manager.load_tasks_from_redis()
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


app = FastAPI(lifespan=lifespan, title="Autobot Trading Service")


# ==================== 任务管理 ====================

@app.get("/add")
async def add_task(timeframe: str, exchange: str, symbol: str, method: str):
    """添加交易任务"""
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
    """移除交易任务"""
    try:
        await task_manager.remove_task(timeframe, exchange, symbol, method)
        return {"success": True, "message": f"任务删除成功"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/status")
async def get_status():
    """查看当前任务状态"""
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


# ==================== 仓位管理（改造）====================

@app.get("/position")
async def get_position(exchange: str, symbol: str, method: str):
    """查看仓位信息（多仓 + 空仓）"""
    pos = position_manager.get_position(exchange, symbol, method)
    return {"success": True, "data": pos.to_dict()}


@app.get("/positions")
async def get_all_positions():
    """查看所有活跃仓位（从持久化存储）"""
    active = position_manager.get_all_active_positions()
    return {"success": True, "data": active, "count": len(active)}


@app.get("/position_history")
async def get_position_history(
    symbol: str = None, direction: str = None, limit: int = 100
):
    """查看仓位历史记录"""
    history = position_manager.get_position_history(
        symbol=symbol, direction=direction, limit=limit
    )
    return {"success": True, "data": history, "count": len(history)}


@app.get("/reset")
async def reset_position(exchange: str, symbol: str, method: str, direction: str = None):
    """
    重置仓位信息（仅清除记录，不执行交易）
    
    Args:
        direction: "long"/"short"/不传=清除全部
    """
    try:
        position_manager.clear_position(exchange, symbol, method, direction=direction)
        dir_str = direction or "全部"
        return {"success": True, "message": f"仓位已重置: {exchange}/{symbol}/{method}/{dir_str}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/close")
async def force_close(exchange: str, symbol: str, method: str, direction: str = None):
    """
    强制清仓（执行实际交易 + 清除仓位记录）
    
    Args:
        direction: "long"/"short"/不传=全部平仓
    """
    try:
        success, msg = trading_engine.force_close(exchange, symbol, method, direction=direction)
        return {"success": success, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/close_all")
async def force_close_all():
    """清掉所有仓位"""
    results = []
    for task_key in list(task_manager.active_tasks):
        parts = task_key.split(":")
        if len(parts) == 4:
            _, exchange, symbol, method = parts
            success, msg = trading_engine.force_close(exchange, symbol, method)
            results.append({"task": task_key, "success": success, "message": msg})
    return {"success": True, "data": results}


# ==================== 风险监控（新增）====================

@app.get("/liquidation_status")
async def get_liquidation_status(symbol: str = None):
    """
    查看爆仓风险状态
    
    返回各仓位的保证金率、预估强平价等信息
    """
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


# ==================== 策略管理 ====================

@app.get("/strategies")
async def list_strategies():
    """查看所有可用策略"""
    return {"success": True, "data": strategy_registry.list_strategies()}


# ==================== 配置查看（新增）====================

@app.get("/config")
async def get_config():
    """查看当前交易配置（脱敏）"""
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


# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    print("========= 启动 Autobot Trading Service =========")
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT)
