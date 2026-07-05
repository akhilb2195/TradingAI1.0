import redis
import json


class RedisManager:

    def __init__(self):
        self.client = redis.Redis(
            host="localhost",
            port=6379,
            db=0,
            decode_responses=True
        )

        print("✅ Redis Connected")

    def _safe(self, value):
        return "" if value is None else value

    def save_market_data(self, message):

        symbol = message.get("symbol")

        if not symbol:
            return

        tick = {
            "symbol": self._safe(message.get("symbol")),
            "ltp": self._safe(message.get("ltp")),
            "prev_close": self._safe(message.get("prev_close_price")),
            "open": self._safe(message.get("open_price")),
            "high": self._safe(message.get("high_price")),
            "low": self._safe(message.get("low_price")),
            "volume": self._safe(message.get("vol_traded_today")),
            "bid": self._safe(message.get("bid_price")),
            "ask": self._safe(message.get("ask_price")),
            "buy_qty": self._safe(message.get("tot_buy_qty")),
            "sell_qty": self._safe(message.get("tot_sell_qty")),
            "change": self._safe(message.get("ch")),
            "change_percent": self._safe(message.get("chp")),
            "timestamp": self._safe(
                message.get("last_traded_time")
                or message.get("exch_feed_time")
            ),
        }

        # ------------------------------
        # Latest Snapshot
        # ------------------------------
        self.client.hset(symbol, mapping=tick)

        # ------------------------------
        # Tick History
        # ------------------------------
        self.client.xadd(
            f"{symbol}:ticks",
            tick
        )

    def get_latest(self, symbol):

        return self.client.hgetall(symbol)

    def get_all_latest(self):

        data = {}

        for key in self.client.keys("*"):

            if not key.endswith(":ticks"):
                data[key] = self.client.hgetall(key)

        return data

    def get_ticks(self, symbol, count=100):

        return self.client.xrevrange(
            f"{symbol}:ticks",
            count=count
        )

    def clear_database(self):

        self.client.flushdb()

        print("Redis Cleared")