"""
BGM 生成客户端 - 调用 MiniMax music-01。
注意: v1 阶段先把接口封好,真实调通等 P7 验收时验证。
"""
from __future__ import annotations
import binascii
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from app.core.config import get_settings
from app.core.errors import PodcastAgentError
from app.core.http import get_http_client
from app.core.logging import get_logger
from app.utils.rate_limit import get_rate_limiter

log = get_logger(__name__)


class MusicError(PodcastAgentError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message, code="music_error", retryable=retryable)


@dataclass
class MusicResult:
    audio_bytes: bytes
    duration_ms: int
    prompt: str


class MusicClient:
    def __init__(self):
        self.settings = get_settings()
        self.limiter = get_rate_limiter()

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((MusicError, httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(log, "WARNING"),
        reraise=True,
    )
    async def generate_bgm(
        self,
        prompt: str,
        lyrics: str = "",
        duration_sec: int = 60,
        output_path: Path | None = None,
    ) -> MusicResult:
        """
        生成 BGM。
        prompt 例: "soft lo-fi background music, no vocals, 60bpm"
        """
        await self.limiter.acquire()

        client = get_http_client()
        body = {
            "model": self.settings.music_model,
            "prompt": prompt,
            "lyrics": lyrics,
            "audio_setting": {
                "sample_rate": self.settings.audio_sample_rate,
                "bitrate": self.settings.audio_bitrate,
                "format": self.settings.audio_format,
            },
        }

        log.info("music.generate", prompt=prompt[:60], duration_sec=duration_sec)

        try:
            resp = await client.post(
                "/music_generation",
                json=body,
                timeout=httpx.Timeout(self.settings.music_timeout_sec, connect=10.0),
            )
        except httpx.TimeoutException as e:
            raise MusicError(f"Music 超时: {e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise MusicError(f"Music 网络错误: {e}", retryable=True) from e

        if resp.status_code != 200:
            raise MusicError(f"Music HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        # 实际 API 返回格式待 P7 验收,这里先按 hex 编码做兜底
        if "data" in data and "audio" in data["data"]:
            audio_bytes = binascii.unhexlify(data["data"]["audio"])
        elif "audio" in data:
            audio_bytes = binascii.unhexlify(data["audio"])
        else:
            raise MusicError(f"Music 响应格式未知: {json.dumps(data)[:300]}")

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_bytes)

        log.info("music.done", size=len(audio_bytes))
        return MusicResult(
            audio_bytes=audio_bytes,
            duration_ms=duration_sec * 1000,
            prompt=prompt,
        )


_client: MusicClient | None = None


def get_music_client() -> MusicClient:
    global _client
    if _client is None:
        _client = MusicClient()
    return _client