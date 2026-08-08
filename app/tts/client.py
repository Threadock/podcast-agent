"""
TTS 客户端 - 异步 + 重试 + 限流。
封装 MiniMax /v1/t2a_v2。
"""
from __future__ import annotations
import asyncio
import binascii
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
from app.core.errors import TTSError
from app.core.http import get_http_client
from app.core.logging import get_logger
from app.tts.registry import VoiceProfile
from app.utils.rate_limit import get_rate_limiter

log = get_logger(__name__)


@dataclass
class TTSResult:
    """单句 TTS 合成结果"""

    audio_bytes: bytes
    duration_ms: int
    char_count: int
    usage_characters: int
    audio_size_bytes: int


class TTSClient:
    def __init__(self):
        self.settings = get_settings()
        self.limiter = get_rate_limiter()

    async def _get_client(self) -> httpx.AsyncClient:
        return get_http_client()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((TTSError, httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(log, "WARNING"),
        reraise=True,
    )
    async def synthesize(
        self,
        text: str,
        profile: VoiceProfile,
        output_path: Path | None = None,
    ) -> TTSResult:
        """
        合成单句台词。

        Args:
            text: 台词文本
            profile: 角色的语音配置
            output_path: 可选,保存到本地;不传则只返回 bytes
        """
        await self.limiter.acquire()

        client = await self._get_client()
        body = {
            "model": self.settings.tts_model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": profile.voice_id,
                "speed": profile.speed,
                "vol": 1.0,
                "pitch": profile.pitch,
                "emotion": profile.emotion,
            },
            "audio_setting": {
                "sample_rate": self.settings.audio_sample_rate,
                "bitrate": self.settings.audio_bitrate,
                "format": self.settings.audio_format,
                "channel": self.settings.audio_channels,
            },
            "pronunciation_dict": {"tone": []},
            "language_boost": "auto",
        }

        log.info(
            "tts.synthesize",
            voice=profile.voice_id,
            emotion=profile.emotion,
            chars=len(text),
        )

        try:
            resp = await client.post(
                "/t2a_v2",
                json=body,
                timeout=httpx.Timeout(self.settings.tts_timeout_sec, connect=10.0),
            )
        except httpx.TimeoutException as e:
            raise TTSError(f"TTS 超时: {e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise TTSError(f"TTS 网络错误: {e}", retryable=True) from e

        if resp.status_code != 200:
            raise TTSError(f"TTS HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        base = data.get("base_resp", {})
        if base.get("status_code", 0) != 0:
            raise TTSError(f"TTS API 错误 [{base.get('status_code')}]: {base.get('status_msg')}")

        audio_bytes = binascii.unhexlify(data["data"]["audio"])
        extra = data.get("extra_info", {})

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_bytes)

        result = TTSResult(
            audio_bytes=audio_bytes,
            duration_ms=extra.get("audio_length", -1),
            char_count=len(text),
            usage_characters=extra.get("usage_characters", len(text)),
            audio_size_bytes=len(audio_bytes),
        )

        log.info(
            "tts.done",
            duration_ms=result.duration_ms,
            size_bytes=result.audio_size_bytes,
            usage=result.usage_characters,
        )
        return result


_client: TTSClient | None = None


def get_tts_client() -> TTSClient:
    global _client
    if _client is None:
        _client = TTSClient()
    return _client