"""
共享 httpx 异步客户端 - 连接池复用。
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """单例 httpx 客户端 (连接池复用)"""
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(
            base_url=s.minimax_base_url,
            timeout=httpx.Timeout(s.llm_timeout_sec, connect=10.0),
            headers={
                "Authorization": f"Bearer {s.minimax_api_key}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
        log.info("http_client.initialized", base_url=s.minimax_base_url)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        log.info("http_client.closed")


@asynccontextmanager
async def http_lifespan() -> AsyncIterator[None]:
    """FastAPI lifespan 上下文管理器"""
    get_http_client()
    try:
        yield
    finally:
        await close_http_client()