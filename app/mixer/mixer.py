"""
Mixer - 用 ffmpeg 把多句 TTS 拼接 + 加 BGM + 响度归一化。
"""
from __future__ import annotations
import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class MixResult:
    output_path: Path
    duration_sec: float
    size_bytes: int


class Mixer:
    def __init__(self):
        self.settings = get_settings()

    async def concat_with_silence(
        self,
        line_files: list[Path],
        output_path: Path,
        silence_ms: int | None = None,
    ) -> MixResult:
        """
        拼接多句音频,中间塞静音间隔。

        Args:
            line_files: 按顺序的 mp3 文件列表
            output_path: 最终输出文件
            silence_ms: 每句之间的静音时长 (ms), 默认从 settings 取
        """
        if not line_files:
            raise ValueError("line_files 不能为空")

        silence_ms = silence_ms or self.settings.silence_between_lines_ms
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 生成临时静音文件
        silence_path = output_path.parent / "_silence.mp3"
        if not silence_path.exists():
            await self._generate_silence(silence_path, silence_ms)

        # 写 concat 描述文件
        concat_file = output_path.parent / "_concat.txt"
        with concat_file.open("w") as f:
            for i, line_file in enumerate(line_files):
                f.write(f"file '{line_file.resolve().as_posix()}'\n")
                if i < len(line_files) - 1:
                    f.write(f"file '{silence_path.resolve().as_posix()}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output_path),
        ]

        log.info("mixer.concat", files=len(line_files), output=output_path.name)
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat 失败:\n{result.stderr[-500:]}")

        # 清理临时文件
        concat_file.unlink(missing_ok=True)

        duration = await self._probe_duration(output_path)
        size = output_path.stat().st_size
        log.info("mixer.concat.done", duration_sec=duration, size=size)
        return MixResult(output_path=output_path, duration_sec=duration, size_bytes=size)

    async def mix_with_bgm(
        self,
        voice_path: Path,
        bgm_path: Path,
        output_path: Path,
        bgm_volume_db: float = -18.0,
        target_loudness_lufs: float | None = None,
    ) -> MixResult:
        """
        人声 + BGM 双轨混音,BGM 自动循环到人声长度,BGM 降低音量。
        然后做响度归一化到播客标准 (-16 LUFS)。
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        target_lufs = target_loudness_lufs or self.settings.target_loudness_lufs

        # 1) 双轨混音 + BGM 循环 + 降低音量
        mixed_path = output_path.parent / "_mixed_pre_normalize.mp3"
        cmd1 = [
            "ffmpeg", "-y",
            "-i", str(voice_path),
            "-stream_loop", "-1", "-i", str(bgm_path),
            "-filter_complex",
            f"[1:a]volume={bgm_volume_db}dB,aloop=loop=-1:size=0[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[mixed]",
            "-map", "[mixed]",
            "-ac", str(self.settings.audio_channels),
            "-ar", str(self.settings.audio_sample_rate),
            "-c:a", "libmp3lame",
            "-b:a", f"{self.settings.audio_bitrate//1000}k",
            str(mixed_path),
        ]
        log.info("mixer.bgm.amix", voice=voice_path.name, bgm=bgm_path.name)
        result = await asyncio.to_thread(subprocess.run, cmd1, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg amix 失败:\n{result.stderr[-500:]}")

        # 2) 响度归一化 (loudnorm filter,两遍法精度更高)
        # 第一遍:测量
        measure_cmd = [
            "ffmpeg", "-i", str(mixed_path),
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]
        measure = await asyncio.to_thread(subprocess.run, measure_cmd, capture_output=True, text=True)
        if measure.returncode != 0:
            log.warning("mixer.loudnorm.measure_failed, using simple copy",
                        stderr=measure.stderr[-200:])
            # fallback: 直接复制
            mixed_path.replace(output_path)
        else:
            # 解析测量结果
            import re
            json_start = measure.stderr.rfind("{")
            json_end = measure.stderr.rfind("}") + 1
            if json_start > 0 and json_end > json_start:
                try:
                    import json as json_mod
                    measured = json_mod.loads(measure.stderr[json_start:json_end])
                except json_mod.JSONDecodeError:
                    measured = {}
            else:
                measured = {}

            # 第二遍:应用归一化
            norm_filter = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured.get('input_i', '')}:"
                f"measured_TP={measured.get('input_tp', '')}:"
                f"measured_LRA={measured.get('input_lra', '')}:"
                f"measured_thresh={measured.get('input_thresh', '')}:"
                f"offset={measured.get('target_offset', '')}:"
                f"linear=true:print_format=summary"
            )
            cmd_norm = [
                "ffmpeg", "-y", "-i", str(mixed_path),
                "-af", norm_filter,
                "-ac", str(self.settings.audio_channels),
                "-ar", str(self.settings.audio_sample_rate),
                "-c:a", "libmp3lame",
                "-b:a", f"{self.settings.audio_bitrate//1000}k",
                str(output_path),
            ]
            log.info("mixer.bgm.loudnorm")
            norm = await asyncio.to_thread(subprocess.run, cmd_norm, capture_output=True, text=True)
            if norm.returncode != 0:
                log.warning("mixer.bgm.loudnorm.failed, using pre-normalize",
                            stderr=norm.stderr[-200:])
                mixed_path.replace(output_path)
            else:
                mixed_path.unlink(missing_ok=True)

        duration = await self._probe_duration(output_path)
        size = output_path.stat().st_size
        log.info("mixer.bgm.done", duration_sec=duration, size=size)
        return MixResult(output_path=output_path, duration_sec=duration, size_bytes=size)

    async def _generate_silence(self, path: Path, duration_ms: int) -> None:
        """生成指定时长的静音 MP3"""
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r={self.settings.audio_sample_rate}:cl=mono",
            "-t", str(duration_ms / 1000.0),
            "-q:a", "9",
            "-acodec", "libmp3lame",
            str(path),
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"生成静音失败:\n{result.stderr[-300:]}")

    async def _probe_duration(self, path: Path) -> float:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return 0.0
        try:
            return float(result.stdout.strip())
        except ValueError:
            return 0.0


_mixer: Mixer | None = None


def get_mixer() -> Mixer:
    global _mixer
    if _mixer is None:
        _mixer = Mixer()
    return _mixer