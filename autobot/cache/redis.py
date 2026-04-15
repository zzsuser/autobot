"""Redis客户端 - 修复close问题"""
import redis
from redis import ConnectionPool
from autobot.config import RedisConfig

class RedisClient:

    def __init__(self, url, max_connections=10):
        self.pool = ConnectionPool.from_url(url, max_connections=max_connections)
        self.client = redis.StrictRedis(connection_pool=self.pool)

    def close(self):
        """关闭连接池"""
        if self.pool:
            self.pool.disconnect()

    def ping(self) -> bool:
        """健康检查"""
        try:
            return self.client.ping()
        except Exception:
            return False

    def set(self, key, value, expiration=None):
        return self.client.set(key, value, ex=expiration)

    def get(self, key):
        result = self.client.get(key)
        if result is not None:
            return result.decode("utf-8") if isinstance(result, bytes) else result
        return None

    def delete(self, key):
        return self.client.delete(key)

    def exists(self, key):
        return self.client.exists(key)

    def hset(self, name, mapping: dict, expiration=None):
        r = self.client.hset(name, mapping=mapping)
        if expiration:
            self.client.expire(name, expiration)
        return r

    def hget(self, name, key):
        return self.client.hget(name, key)

    def hgetall(self, key):
        return self.client.hgetall(key)

    def keys(self, pattern):
        return self.client.keys(pattern)

    def setex(self, key, expiration, value):
        return self.client.setex(key, expiration, value)

    def lpush(self, key, data):
        return self.client.lpush(key, data)

    def rpop(self, key):
        return self.client.rpop(key)

    def llen(self, key):
        return self.client.llen(key)


redis_client = RedisClient(RedisConfig.URL, RedisConfig.MAX_CONNECTIONS)
