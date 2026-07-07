"""
redis_config.py
----------------
Shared Redis connection settings, imported by producer.py, consumer.py,
candle_builder.py, and launcher.py.
"""

import redis

REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

ACTIVE_SYMBOLS_KEY = "ticks:active_symbols"
CANDLE_PREFIX = "candles"


def get_redis_client():
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=True,
    )


def stream_key(symbol: str) -> str:
    return f"ticks:{symbol}"


def candle_key(symbol: str, timeframe: str) -> str:
    return f"{CANDLE_PREFIX}:{symbol}:{timeframe}"