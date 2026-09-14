"""任务调度管理器

【架构修复说明】
- B①: 每个 Task 实例现在有两个独立的协程循环：
       * signal_loop  → 严格对齐 timeframe 整点触发，跑完整信号检查
       * stop_loop    → 高频心跳（默认 30s），跑 check_stop
       两者通过 trading_engine.execute(mode=...) 区分。
       这让策略里的非信号周期止盈止损真正能被调用到。
- A③: TaskManager 增加 heartbeat + watchdog 协程：
       * 每 60s 输出一行 [HEARTBEAT] 日志，便于外部脚本检测进程冻结
       * watchdog 检查主调度循环的更新时间戳，超过阈值则输出 CRITICAL
       注意：进程整体卡死时同进程协程也会卡，最终可靠的看门狗依然要靠
       systemd / supervisor 等外部进程监控读取日志做判断。
"""
import asyncio
import json
import time
import traceback

from autobot.cache.redis import redis_client
from autobot.utils.logger import logger
from autobot.core.engine import trading_engine

# 止损检查心跳间隔（秒）—— 用环境变量也可以
STOP_CHECK_INTERVAL_SEC = 30
# 信号轮询间隔（秒）—— 每分钟轮询一次，引擎内部会过滤未收盘K线并去重，
# 只有检测到新的已收盘K线才真正计算/执行信号
SIGNAL_POLL_INTERVAL_SEC = 60
# 主调度循环健康阈值（秒）—— 超过这个时长没更新心跳就告警
WATCHDOG_STALL_SEC = 180
# 心跳日志输出间隔（秒）
HEARTBEAT_LOG_SEC = 60


class TaskManager:
    SCHEDULE_TASK_KEY = "schedule_task"

    def __init__(self):
        self.tasks = {}                    # 任务配置（内存）
        self.is_shutdown = False
        self.stop_signal = asyncio.Event()
        self.active_tasks = set()
        self.task_instances = {}           # task_key -> Task 实例

        # 健康监控
        self._last_main_loop_ts = time.time()
        self._heartbeat_task = None
        self._watchdog_task = None

    # ==================== Redis 持久化 ====================

    async def load_tasks_from_redis(self):
        try:
            data = redis_client.get(self.SCHEDULE_TASK_KEY)
            if data:
                loaded = json.loads(data)
                self.tasks = self._migrate_tasks_structure(loaded)
            logger.info(f"加载任务成功: {self.tasks}")
        except Exception as e:
            logger.error(f"加载任务失败: {e}")

    async def save_tasks_to_redis(self):
        try:
            redis_client.set(self.SCHEDULE_TASK_KEY, json.dumps(self.tasks))
            logger.info("任务已保存到Redis")
        except Exception as e:
            logger.error(f"保存任务失败: {e}")

    def _migrate_tasks_structure(self, old_tasks):
        """兼容旧的三层结构 → 四层"""
        migrated = {}
        for timeframe, exchanges in old_tasks.items():
            migrated[timeframe] = {}
            for exchange, symbols in exchanges.items():
                migrated[timeframe][exchange] = {}
                for symbol, value in symbols.items():
                    if isinstance(value, (int, float)):
                        migrated[timeframe][exchange][symbol] = {"supertrend_tema": value}
                    elif isinstance(value, list):
                        migrated[timeframe][exchange][symbol] = {m: 1 for m in value}
                    elif isinstance(value, dict):
                        migrated[timeframe][exchange][symbol] = value
                    else:
                        migrated[timeframe][exchange][symbol] = {"supertrend_tema": 1}
        return migrated

    # ==================== 主调度器 ====================

    async def start_scheduled_tasks(self):
        """主调度循环 - 每秒检查 timeframe 整点"""
        logger.info("任务调度器启动")

        # 同时启动心跳和看门狗
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        try:
            while not self.is_shutdown:
                current_time = time.time()
                self._last_main_loop_ts = current_time  # 更新健康戳

                # 每分钟对账一次任务实例（创建/清理），任务创建后由各自循环负责轮询
                if current_time % 60 < 1:
                    for timeframe in list(self.tasks.keys()):
                        logger.debug(f"触发 {timeframe} 分钟周期任务")
                        await self._execute_timeframe(timeframe)

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("调度器接收到取消信号")
        except Exception as e:
            logger.error(f"调度器异常: {e}\n{traceback.format_exc()}")
            raise
        finally:
            logger.info("调度器已停止")

    async def stop_scheduled_tasks(self):
        """停止所有定时任务"""
        self.is_shutdown = True
        self.stop_signal.set()
        logger.info("停止任务调度中...")

        for key in list(self.active_tasks):
            task_instance = self.task_instances.get(key)
            if task_instance:
                try:
                    await task_instance.stop()
                except Exception as e:
                    logger.error(f"停止任务 {key} 异常: {e}")

        # 停止心跳 / 看门狗
        for t in (self._heartbeat_task, self._watchdog_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self.active_tasks.clear()
        self.task_instances.clear()
        logger.info("所有任务已停止")

    # ==================== A③: 心跳 + 看门狗 ====================

    async def _heartbeat_loop(self):
        """每分钟输出一行心跳日志，方便外部监控判断进程是否冻结"""
        while not self.is_shutdown:
            try:
                await asyncio.sleep(HEARTBEAT_LOG_SEC)
                if self.is_shutdown:
                    break
                stalled = time.time() - self._last_main_loop_ts
                logger.info(
                    f"[HEARTBEAT] 调度器存活, 活跃任务={len(self.active_tasks)}, "
                    f"主循环 {stalled:.1f}s 前更新"
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"心跳异常: {e}")

    async def _watchdog_loop(self):
        """看门狗：检查主调度循环是否长时间没有更新心跳"""
        while not self.is_shutdown:
            try:
                await asyncio.sleep(60)
                if self.is_shutdown:
                    break
                stalled = time.time() - self._last_main_loop_ts
                if stalled > WATCHDOG_STALL_SEC:
                    logger.critical(
                        f"⚠️ [WATCHDOG] 调度器主循环已 {stalled:.0f}s 未更新心跳！"
                        f" 进程可能已卡死，建议外部脚本（systemd / supervisor）重启服务。"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"看门狗异常: {e}")

    # ==================== 任务实例管理 ====================

    async def _execute_timeframe(self, timeframe):
        """每秒触发时检查：清理失效任务，启动新任务"""
        # 1. 当前配置中该 timeframe 应有的所有 task_key
        current_keys = set()
        for exchange, symbols in self.tasks.get(timeframe, {}).items():
            for symbol, methods in symbols.items():
                for method in methods:
                    key = f"{timeframe}:{exchange}:{symbol}:{method}"
                    current_keys.add(key)

        # 2. 清理已停止 / 已删除的任务
        for key in list(self.active_tasks):
            if not key.startswith(f"{timeframe}:"):
                continue
            task_instance = self.task_instances.get(key)

            should_remove = (
                key not in current_keys
                or not task_instance
                or not task_instance.is_running
                or task_instance.is_finished()
            )

            if should_remove:
                logger.debug(f"移除已停止的任务实例: {key}")
                if task_instance:
                    try:
                        await task_instance.stop()
                    except Exception as e:
                        logger.debug(f"停止任务时出错: {e}")
                self.active_tasks.discard(key)
                self.task_instances.pop(key, None)

        # 3. 启动新任务
        tasks_to_execute = []
        for key in current_keys:
            if key not in self.active_tasks:
                _, exchange, symbol, method = key.split(":")
                task = Task(exchange, symbol, timeframe, method)
                self.active_tasks.add(key)
                self.task_instances[key] = task
                tasks_to_execute.append(task.start())
                logger.debug(f"准备启动任务: {key}")

        if tasks_to_execute:
            logger.info(f"启动 {len(tasks_to_execute)} 个新任务")
            await asyncio.gather(*tasks_to_execute)

    # ==================== 任务增删 ====================

    async def add_task(self, timeframe, exchange, symbol, method):
        logger.info(f"添加任务: {timeframe}/{exchange}/{symbol}/{method}")

        self.tasks.setdefault(timeframe, {}).setdefault(exchange, {}).setdefault(symbol, {})

        if method not in self.tasks[timeframe][exchange][symbol]:
            self.tasks[timeframe][exchange][symbol][method] = 1
            await self.save_tasks_to_redis()
            await self._execute_timeframe(timeframe)  # 立即启动
            logger.info(f"任务已添加并启动: {timeframe}:{exchange}:{symbol}:{method}")
        else:
            logger.info(f"任务已存在: {timeframe}:{exchange}:{symbol}:{method}")

    async def remove_task(self, timeframe, exchange, symbol, method):
        logger.info(f"移除任务: {timeframe}/{exchange}/{symbol}/{method}")

        if (timeframe in self.tasks
                and exchange in self.tasks[timeframe]
                and symbol in self.tasks[timeframe][exchange]
                and method in self.tasks[timeframe][exchange][symbol]):

            key = f"{timeframe}:{exchange}:{symbol}:{method}"
            task_instance = self.task_instances.get(key)
            if task_instance:
                try:
                    await task_instance.stop()
                except Exception as e:
                    logger.error(f"停止任务 {key} 异常: {e}")
            self.active_tasks.discard(key)
            self.task_instances.pop(key, None)

            # 逐层清理
            del self.tasks[timeframe][exchange][symbol][method]
            if not self.tasks[timeframe][exchange][symbol]:
                del self.tasks[timeframe][exchange][symbol]
            if not self.tasks[timeframe][exchange]:
                del self.tasks[timeframe][exchange]
            if not self.tasks[timeframe]:
                del self.tasks[timeframe]

            await self.save_tasks_to_redis()
            logger.info(f"任务已移除: {key}")
        else:
            logger.warning(f"任务不存在: {timeframe}/{exchange}/{symbol}/{method}")


class Task:
    """
    独立任务实例 - 每个 exchange/symbol/method 组合一个

    内部跑两个协程循环：
      - signal_loop:  按 timeframe 整点对齐触发，调 execute(mode="signal")
      - stop_loop:    每 STOP_CHECK_INTERVAL_SEC 触发一次，调 execute(mode="stop_check")
    """

    def __init__(self, exchange, symbol, timeframe, method):
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.method = method
        self.is_running = False
        self._signal_task = None
        self._stop_task = None

    @property
    def task_name(self) -> str:
        return f"{self.exchange}-{self.symbol}-{self.method}"

    def is_finished(self) -> bool:
        """两个内部协程都结束才算 finished"""
        sig_done = (self._signal_task is None) or self._signal_task.done()
        stop_done = (self._stop_task is None) or self._stop_task.done()
        return sig_done and stop_done

    # ---------- 执行入口 ----------

    async def _run_engine(self, mode: str):
        """统一调度 engine.execute（mode = signal / stop_check）"""
        try:
            loop = asyncio.get_event_loop()
            success, msg = await loop.run_in_executor(
                None,
                trading_engine.execute,
                self.exchange, self.symbol, self.timeframe, self.method, mode,
            )
            if success:
                tag = "信号" if mode == "signal" else "止损"
                logger.info(f"[{self.task_name}] {tag}执行: {msg}")
            else:
                logger.debug(f"[{self.task_name}] {mode}结果: {msg}")
        except Exception as e:
            logger.error(f"engine.execute 异常 ({mode}): {self.task_name}: {e}", exc_info=True)

    # ---------- signal_loop：5min 整点 ----------

    async def signal_loop(self):
        logger.info(
            f"信号循环开始: {self.task_name} (每{SIGNAL_POLL_INTERVAL_SEC}s轮询)"
        )

        try:
            while self.is_running:
                start_time = time.perf_counter()
                await self._run_engine("signal")
                elapsed = time.perf_counter() - start_time

                # 每分钟轮询：引擎内部只对「已收盘且未处理过」的K线产生信号
                sleep_time = max(1.0, SIGNAL_POLL_INTERVAL_SEC - elapsed)
                logger.debug(
                    f"下次信号 {self.task_name}: elapsed={elapsed:.2f}s, sleep={sleep_time:.2f}s"
                )
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info(f"信号循环被取消: {self.task_name}")
        except Exception as e:
            logger.error(f"信号循环异常: {self.task_name}: {e}\n{traceback.format_exc()}")
        finally:
            logger.info(f"信号循环停止: {self.task_name}")

    # ---------- stop_loop：高频止损 ----------

    async def stop_loop(self):
        """高频止盈止损循环"""
        logger.info(
            f"止损循环开始: {self.task_name} (每{STOP_CHECK_INTERVAL_SEC}s)"
        )

        # 错开 5s，避免和信号检查同时撞数据库 / OKX
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

        try:
            while self.is_running:
                await self._run_engine("stop_check")
                try:
                    await asyncio.sleep(STOP_CHECK_INTERVAL_SEC)
                except asyncio.CancelledError:
                    break
        except asyncio.CancelledError:
            logger.info(f"止损循环被取消: {self.task_name}")
        except Exception as e:
            logger.error(f"止损循环异常: {self.task_name}: {e}\n{traceback.format_exc()}")
        finally:
            logger.info(f"止损循环停止: {self.task_name}")

    # ---------- 启停 ----------

    async def start(self):
        self.is_running = True
        if self._signal_task is None or self._signal_task.done():
            self._signal_task = asyncio.create_task(self.signal_loop())
        if self._stop_task is None or self._stop_task.done():
            self._stop_task = asyncio.create_task(self.stop_loop())

    async def stop(self):
        self.is_running = False
        for t in (self._signal_task, self._stop_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._signal_task = None
        self._stop_task = None
        logger.debug(f"Task已停止: {self.task_name}")


# 全局实例
task_manager = TaskManager()
