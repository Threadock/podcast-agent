"""
Hermes TTS client - 直接调 MiniMax /v1/t2a_v2, 完全控制 voice_id。

绕开 hermes 内部的 text_to_speech_tool (它不接受 voice 参数, 只能用 config.yaml 默认值)。

用法:
    client = HermesTTSClient()
    audio = await client.synthesize("你好", voice="female-chengshu", speed=1.5)
"""
from __future__ import annotations
import asyncio
import binascii
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

KEY_RESOLVER_HELPER = "/Users/saber/.hermes/hermes-agent"


def _resolve_minimax_key() -> str:
    """
    从 hermes 凭证池取 MiniMax token plan key。
    顺序: env > hermes resolve_provider_secret > ~/.hermes/auth.json
    """
    # 1) env
    key = os.environ.get("MINIMAX_CN_API_KEY", "")
    if key and "..." not in key and len(key) > 50:
        return key

    # 2) hermes resolve_provider_secret (用凭证池)
    try:
        sys.path.insert(0, KEY_RESOLVER_HELPER)
        from tools.tool_backend_helpers import resolve_provider_secret
        key = resolve_provider_secret("MINIMAX_CN_API_KEY", "minimax-cn")
        if key and len(key) > 50:
            return key
    except Exception as e:
        pass

    raise RuntimeError(
        "MiniMax key not found. Run from hermes 进程 (token plan), "
        "or set MINIMAX_CN_API_KEY env var."
    )


class HermesTTSClient:
    """直接调 MiniMax t2a_v2, 完全控制 voice_id / speed / emotion"""

    BASE_URL = "https://api.minimaxi.com/v1"  # base 路径, 不用 /t2a_v2
    T2A_PATH = "/t2a_v2"
    DEFAULT_MODEL = "speech-2.6-hd"  # 用最新 HD 版本

    def __init__(self, model: str | None = None):
        self.model = model or self.DEFAULT_MODEL
        self.api_key = _resolve_minimax_key()
        # HTTP 客户端连接池
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                timeout=httpx.Timeout(60.0, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
                follow_redirects=True,  # 关键: 跟随 307 redirect
            )
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize(
        self,
        text: str,
        voice: str = "female-chengshu",
        speed: float = 1.0,
        emotion: str = "neutral",
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        """
        合成单条语音。

        Returns:
            {"audio_bytes": bytes, "duration_ms": int, "usage_chars": int,
             "audio_size_bytes": int, "voice": str, "emotion": str}
        """
        client = await self._get_client()
        body = {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice,
                "speed": speed,
                "vol": 1.0,
                "pitch": 0,
                "emotion": emotion,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
            "pronunciation_dict": {"tone": []},
            "language_boost": "auto",
        }

        resp = await client.post(self.T2A_PATH, json=body)
        if resp.status_code != 200:
            raise RuntimeError(
                f"MiniMax TTS HTTP {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        base = data.get("base_resp", {})
        if base.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax TTS API error [{base.get('status_code')}]: {base.get('status_msg')}")

        audio_bytes = binascii.unhexlify(data["data"]["audio"])
        extra = data.get("extra_info", {})

        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_bytes)

        return {
            "audio_bytes": audio_bytes,
            "duration_ms": extra.get("audio_length", -1),
            "usage_chars": extra.get("usage_characters", len(text)),
            "audio_size_bytes": len(audio_bytes),
            "voice": voice,
            "emotion": emotion,
        }


# 同步 wrapper
def synthesize_sync(
    text: str,
    voice: str = "female-chengshu",
    speed: float = 1.0,
    emotion: str = "neutral",
    output_path: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        HermesTTSClient().synthesize(text, voice, speed, emotion, output_path)
    )


if __name__ == "__main__":
    # 测试: 同一个文本用 3 个 voice 合成
    print("=" * 60)
    print("🎙️ HermesTTSClient 测试")
    print("=" * 60)

    test_voices = [
        ("female-chengshu", "女声成熟"),
        ("female-shaonv", "女声少女"),
        ("male-qn-qingse", "男声青年"),
        ("male-qn-jingying", "男声磁性"),
    ]

    for voice, desc in test_voices:
        try:
            r = synthesize_sync("今天我们聊聊人工智能。", voice=voice, speed=1.5,
                                 output_path=f"/tmp/test_{voice}.mp3")
            print(f"  ✓ [{voice:20s}] {desc:10s} {r['audio_size_bytes']/1024:6.1f}KB "
                  f"{r['duration_ms']:5d}ms")
        except Exception as e:
            print(f"  ✗ [{voice}] {e}")