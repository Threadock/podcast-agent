"""
LLM 客户端 - 异步 + 重试 + 流式(可选)。
封装 MiniMax /v1/text/chatcompletion_v2 (OpenAI 兼容端点)。
"""
from __future__ import annotations
import json
from typing import Any, AsyncIterator

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import structlog

from app.core.config import get_settings
from app.core.errors import LLMError, LLMResponseInvalid
from app.core.http import get_http_client
from app.core.logging import get_logger

log = get_logger(__name__)


class LLMClient:
    """异步 LLM 客户端 (单例)"""

    def __init__(self):
        self.settings = get_settings()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = get_http_client()
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type((LLMError, httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(log, "WARNING"),
        reraise=True,
    )
    async def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        同步式 chat。返回 content (不含 reasoning)。

        MiniMax-M3 启用 thinking 后 reasoning 可能吃光 token,
        所以 max_tokens 默认拉到 16384。
        """
        client = await self._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature or self.settings.llm_temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "reasoning_split": True,  # M3: 让 reasoning 走 reasoning_details
        }

        log.info("llm.request", model=body["model"], prompt_chars=len(prompt))

        try:
            resp = await client.post("/text/chatcompletion_v2", json=body)
        except httpx.TimeoutException as e:
            raise LLMError(f"LLM 调用超时: {e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 网络错误: {e}", retryable=True) from e

        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content", "")
        finish = choice.get("finish_reason", "?")

        log.info(
            "llm.response",
            finish_reason=finish,
            content_len=len(content),
            prompt_tokens=data.get("usage", {}).get("prompt_tokens"),
            completion_tokens=data.get("usage", {}).get("completion_tokens"),
        )

        if not content:
            # tracing
            reasoning = choice["message"].get("reasoning_content", "")
            log.error(
                "llm.empty_content",
                finish_reason=finish,
                reasoning_preview=reasoning[:200],
            )
            raise LLMResponseInvalid(f"LLM 返回空 content (finish={finish})")

        if finish == "length":
            raise LLMResponseInvalid(f"LLM 输出被 max_tokens 截断 (finish=length)")

        return content

    async def chat_stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """流式输出 token (用于长剧本生成时实时显示进度)"""
        client = await self._get_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": True,
            "reasoning_split": True,
        }

        async with client.stream("POST", "/text/chatcompletion_v2", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client