"""
异步令牌桶限流器 - 控制 TTS/Music API 调用速率。
"""
from __future__ import annotations
import asyncio
import time


class AsyncRateLimiter:
    """简单令牌桶:每 1/rate 秒发一个 token"""

    def __init__(self, rate_per_sec: float):
        self.rate = rate_per_sec
        self.min_interval = 1.0 / rate_per_sec
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


_limiter: AsyncRateLimiter | None = None


def get_rate_limiter() -> AsyncRateLimiter:
    global _limiter
    if _limiter is None:
        from app.core.config import get_settings
        _limiter = AsyncRateLimiter(get_settings().tts_rate_limit_rps)
    return _limiter