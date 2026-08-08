"""
合成器 - 把整个 Script 逐句合成,返回所有句段的元数据。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.models.script import Script
from app.tts.client import TTSClient, TTSResult, get_tts_client
from app.tts.registry import VoiceRegistry

log = get_logger(__name__)


@dataclass
class LineAudio:
    """单句合成结果 (含元数据)"""

    index: int
    role: str
    text: str
    voice_id: str
    emotion: str
    file_path: Path
    duration_ms: int
    usage_characters: int
    audio_size_bytes: int


class Synthesizer:
    def __init__(self, tts: TTSClient | None = None, registry: VoiceRegistry | None = None):
        self.tts = tts or get_tts_client()
        self.registry = registry or VoiceRegistry()

    async def synthesize_script(
        self,
        script: Script,
        output_dir: Path,
    ) -> list[LineAudio]:
        """
        合成整个剧本。

        Args:
            script: 已校验的 Script 对象
            output_dir: 输出目录,会生成 line_00_host.mp3 等文件

        Returns:
            每句的 LineAudio 列表,顺序与 script.lines 对应
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        results: list[LineAudio] = []
        log.info("synthesizer.start", total_lines=len(script.lines))

        for i, line in enumerate(script.lines):
            profile = self.registry.get(line.role)
            out_path = output_dir / f"line_{i:02d}_{line.role.value}.mp3"

            tts_result = await self.tts.synthesize(
                text=line.text,
                profile=profile,
                output_path=out_path,
            )

            results.append(LineAudio(
                index=i,
                role=line.role.value,
                text=line.text,
                voice_id=profile.voice_id,
                emotion=profile.emotion,
                file_path=out_path,
                duration_ms=tts_result.duration_ms,
                usage_characters=tts_result.usage_characters,
                audio_size_bytes=tts_result.audio_size_bytes,
            ))

            log.info(
                "synthesizer.line_done",
                index=i,
                role=line.role.value,
                voice=profile.voice_id,
                duration_ms=tts_result.duration_ms,
            )

        total_usage = sum(r.usage_characters for r in results)
        log.info(
            "synthesizer.done",
            total_lines=len(results),
            total_usage_chars=total_usage,
        )
        return results


_synthesizer: Synthesizer | None = None


def get_synthesizer() -> Synthesizer:
    global _synthesizer
    if _synthesizer is None:
        _synthesizer = Synthesizer()
    return _synthesizer