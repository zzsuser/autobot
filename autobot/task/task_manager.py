"""任务调度管理器 - 基于旧版补全，使用 trading_engine 替代 worker"""
import asyncio
import json
import time
from autobot.cache.redis import redis_client
from autobot.utils.logger import logger
from autobot.core.engine import trading_engine


class TaskManager:
    SCHEDULE_TASK_KEY = "schedule_task"

    def __init__(self):
        self.tasks = {}                    # 任务配置（内存）: {timeframe: {exchange: {symbol: {method: 1}}}}
        self.is_shutdown = False
        self.stop_signal = asyncio.Event() # 停止信号
        self.active_tasks = set()          # 活跃任务key集合
        self.task_instances = {}           # task_key -> Task 实例映射

    # ==================== Redis 持久化 ====================

    async def load_tasks_from_redis(self):
        """从Redis加载任务配置"""
        try:
            data = redis_client.get(self.SCHEDULE_TASK_KEY)
            if data:
                loaded = json.loads(data)
                self.tasks = self._migrate_tasks_structure(loaded)
            logger.info(f"加载任务成功: {self.tasks}")
        except Exception as e:
            logger.error(f"加载任务失败: {e}")

    async def save_tasks_to_redis(self):
        """保存任务到Redis"""
        try:
            redis_client.set(self.SCHEDULE_TASK_KEY, json.dumps(self.tasks))
            logger.info("任务已保存到Redis")
        except Exception as e:
            logger.error(f"保存任务失败: {e}")

    def _migrate_tasks_structure(self, old_tasks):
        """
        兼容旧的三层结构，迁移为四层结构
        旧: {timeframe: {exchange: {symbol: count}}}
        新: {timeframe: {exchange: {symbol: {method: count}}}}
        """
        migrated = {}
        for timeframe, exchanges in old_tasks.items():
            migrated[timeframe] = {}
            for exchange, symbols in exchanges.items():
                migrated[timeframe][exchange] = {}
                for symbol, value in symbols.items():
                    if isinstance(value, (int, float)):
                        # 旧结构: symbol -> count，用默认method
                        migrated[timeframe][exchange][symbol] = {"supertrend_tema": value}
                    elif isinstance(value, list):
                        # 列表结构: symbol -> [method1, method2, ...]
                        migrated[timeframe][exchange][symbol] = {m: 1 for m in value}
                    elif isinstance(value, dict):
                        # 已经是新结构
                        migrated[timeframe][exchange][symbol] = value
                    else:
                        migrated[timeframe][exchange][symbol] = {"supertrend_tema": 1}
        return migrated

    # ==================== 调度器 ====================

    async def start_scheduled_tasks(self):
        """启动定时调度器 - 每秒检查是否到达任务触发时间"""
        logger.info("任务调度器启动")
        try:
            while not self.is_shutdown:
                current_time = time.time()
                for timeframe in list(self.tasks.keys()):
                    interval_sec = float(timeframe) * 60
                    if interval_sec > 0 and current_time % interval_sec < 1:
                        logger.debug(f"触发 {timeframe} 分钟周期任务")
                        await self._execute_timeframe(timeframe)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("调度器接收到取消信号")
        except Exception as e:
            logger.error(f"调度器异常: {e}")
            raise
        finally:
            logger.info("调度器已停止")

    async def stop_scheduled_tasks(self):
        """停止所有定时任务"""
        self.is_shutdown = True
        self.stop_signal.set()
        logger.info("停止任务调度中...")

        # 停止所有活跃的 Task 实例
        for key in list(self.active_tasks):
            task_instance = self.task_instances.get(key)
            if task_instance:
                try:
                    await task_instance.stop()
                except Exception as e:
                    logger.error(f"停止任务 {key} 异常: {e}")

        self.active_tasks.clear()
        self.task_instances.clear()
        logger.info("所有任务已停止")

    # ==================== 任务执行 ====================

    async def _execute_timeframe(self, timeframe):
        """执行指定周期的所有任务"""

        # 1. 构建当前配置中该 timeframe 的所有 task_key
        current_keys = set()
        for exchange, symbols in self.tasks.get(timeframe, {}).items():
            for symbol, methods in symbols.items():
                for method in methods:
                    key = f"{timeframe}:{exchange}:{symbol}:{method}"
                    current_keys.add(key)

        # 2. 清理已完成、已删除、��停止的任务实例
        for key in list(self.active_tasks):
            if key.startswith(f"{timeframe}:"):
                task_instance = self.task_instances.get(key)

                should_remove = (
                    key not in current_keys or          # 配置已删除
                    not task_instance or                 # 实例不存在
                    not task_instance.is_running or      # 已停止运行
                    (task_instance._task and task_instance._task.done())  # 异步任务已完成
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

        # 3. 启动新任务或重启已停止的任务
        tasks_to_execute = []
        for key in current_keys:
            if key not in self.active_tasks:
                _, exchange, symbol, method = key.split(":")
                task = Task(exchange, symbol, timeframe, method)
                self.active_tasks.add(key)
                self.task_instances[key] = task
                tasks_to_execute.append(task.start())
                logger.debug(f"准备启动任务: {key}")

        # 4. 并发执行新任务
        if tasks_to_execute:
            logger.info(f"启动 {len(tasks_to_execute)} 个新任务")
            await asyncio.gather(*tasks_to_execute)

    # ==================== 任务增删 ====================

    async def add_task(self, timeframe, exchange, symbol, method):
        """添加任务"""
        logger.info(f"添加任务: {timeframe}/{exchange}/{symbol}/{method}")

        if timeframe not in self.tasks:
            self.tasks[timeframe] = {}
        if exchange not in self.tasks[timeframe]:
            self.tasks[timeframe][exchange] = {}
        if symbol not in self.tasks[timeframe][exchange]:
            self.tasks[timeframe][exchange][symbol] = {}

        if method not in self.tasks[timeframe][exchange][symbol]:
            self.tasks[timeframe][exchange][symbol][method] = 1
            await self.save_tasks_to_redis()
            # 立即触发该 timeframe 的任务调度，让新任务尽快启动
            await self._execute_timeframe(timeframe)
            logger.info(f"任务已添加并启动: {timeframe}:{exchange}:{symbol}:{method}")
        else:
            logger.info(f"任务已存在，无需重复添加: {timeframe}:{exchange}:{symbol}:{method}")

    async def remove_task(self, timeframe, exchange, symbol, method):
        """移除任务"""
        logger.info(f"移除任务: {timeframe}/{exchange}/{symbol}/{method}")

        if (timeframe in self.tasks and
            exchange in self.tasks[timeframe] and
            symbol in self.tasks[timeframe][exchange] and
            method in self.tasks[timeframe][exchange][symbol]):

            # 先停止对应的 Task 实例
            key = f"{timeframe}:{exchange}:{symbol}:{method}"
            task_instance = self.task_instances.get(key)
            if task_instance:
                try:
                    await task_instance.stop()
                except Exception as e:
                    logger.error(f"停止任务 {key} 异常: {e}")
            self.active_tasks.discard(key)
            self.task_instances.pop(key, None)

            # 从配置中逐层删除
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
            logger.warning(f"任务不存在，无法移除: {timeframe}/{exchange}/{symbol}/{method}")


class Task:
    """
    独立任务实例 - 每个 exchange/symbol/method 组合对应一个 Task
    负责按 timeframe 周期循环调用 trading_engine.execute()
    """

    def __init__(self, exchange, symbol, timeframe, method):
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.method = method
        self.is_running = False
        self._task = None  # asyncio.Task 对象

    async def fetch_data(self):
        """执行一次交易任务（通过 trading_engine）"""
        try:
            # trading_engine.execute 是同步方法，放到线程池执行避免阻塞事件循环
            loop = asyncio.get_event_loop()
            success, msg = await loop.run_in_executor(
                None,
                trading_engine.execute,
                self.exchange,
                self.symbol,
                self.timeframe,
                self.method,
            )
            if success:
                logger.info(f"[{self.exchange}:{self.symbol}:{self.method}] 执行成功: {msg}")
            else:
                logger.debug(f"[{self.exchange}:{self.symbol}:{self.method}] 执行结果: {msg}")
        except Exception as e:
            logger.error(
                f"fetch_data异常: {self.exchange}-{self.symbol}-{self.method}, 错误: {e}"
            )

    async def schedule_task(self):
        """按 timeframe 周期循环调度（对齐时钟整点）"""
        task_name = f"{self.exchange}-{self.symbol}-{self.method}"
        logger.info(f"任务开始: {task_name} ({self.timeframe}min)")

        tf_seconds = float(self.timeframe) * 60

        # 首次启动：先睡到下一个 timeframe 整点（多睡 1 秒确保 K 线已收盘）
        now_ts = time.time()
        next_aligned = (int(now_ts // tf_seconds) + 1) * tf_seconds
        first_sleep = next_aligned - now_ts + 1
        logger.info(f"任务 {task_name} 首次对齐等待 {first_sleep:.1f}s 至下个 {self.timeframe}min 整点")
        try:
            await asyncio.sleep(first_sleep)
        except asyncio.CancelledError:
            logger.info(f"任务在对齐等待时被取消: {task_name}")
            self.is_running = False
            return

        try:
            while self.is_running:
                start_time = time.perf_counter()
                await self.fetch_data()
                elapsed = time.perf_counter() - start_time

                # 每次结束后重新对齐，避免漂移
                now_ts = time.time()
                next_aligned = (int(now_ts // tf_seconds) + 1) * tf_seconds
                sleep_time = max(0.5, next_aligned - now_ts + 1)
                logger.debug(
                    f"下次调度 {task_name}: elapsed={elapsed:.2f}s, sleep={sleep_time:.2f}s"
                )
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info(f"任务被取消: {task_name}")
        except Exception as e:
            logger.error(f"任务执行异常: {task_name}, 错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            self.is_running = False
            logger.info(f"任务停止: {task_name}")

    async def start(self):
        """启动任务"""
        self.is_running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.schedule_task())

    async def stop(self):
        """停止任务"""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        logger.debug(f"Task已停止: {self.exchange}-{self.symbol}-{self.method}")


# 全局实例
task_manager = TaskManager()
