"""
Mock TTS 客户端 - 测试用,生成有效的静音 MP3。
不调用真实 API,但生成的 mp3 能被 ffmpeg 识别并拼接。
"""
from __future__ import annotations
import asyncio
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.tts.registry import VoiceProfile


@dataclass
class TTSResult:
    audio_bytes: bytes
    duration_ms: int
    char_count: int
    usage_characters: int
    audio_size_bytes: int


def _make_silent_mp3(duration_ms: int) -> bytes:
    """
    生成指定时长(毫秒)的静音 MP3。
    调 ffmpeg lavfi 生成临时文件,然后读 bytes (Mock 仍走 ffmpeg 但不需要 network)。
    """
    import subprocess
    import tempfile

    # 估算字节数: 32kbps = 4KB/s, 加上 ID3 头
    secs = duration_ms / 1000.0
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r=32000:cl=mono",
            "-t", str(secs),
            "-acodec", "libmp3lame",
            "-b:a", "32k",
            tmp_path,
        ], check=True, capture_output=True)
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class MockTTSClient:
    """不调 API,返回静音 MP3 (按文字字数估算时长)"""

    def __init__(self, fail_mode: Literal[None, "network", "api_error"] = None,
                 chars_per_sec: float = 3.5):
        self.fail_mode = fail_mode
        self.chars_per_sec = chars_per_sec

    async def synthesize(
        self,
        text: str,
        profile: VoiceProfile,
        output_path: Path | None = None,
    ) -> TTSResult:
        await asyncio.sleep(0.01)  # 模拟网络延迟

        if self.fail_mode == "network":
            raise ConnectionError("Mock: simulated network error")
        if self.fail_mode == "api_error":
            from app.core.errors import TTSError
            raise TTSError("Mock: simulated API error", retryable=False)

        # 中文约 3.5 字/秒
        duration_ms = int(len(text) / self.chars_per_sec * 1000)
        mp3 = _make_silent_mp3(duration_ms)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(mp3)

        return TTSResult(
            audio_bytes=mp3,
            duration_ms=duration_ms,
            char_count=len(text),
            usage_characters=len(text),
            audio_size_bytes=len(mp3),
        )


def patch_tts_client(mock_instance):
    """替换全局 TTS 客户端"""
    from app.tts import client as tc
    from app.tts import synthesizer as syn
    tc._client = mock_instance
    syn._synthesizer = None  # 强制下次重建