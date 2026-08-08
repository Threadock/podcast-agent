"""
Orchestrator - 端到端编排: 编剧 → TTS → 混音。
负责状态机推进、错误处理、断点恢复。
"""
from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.script_writer import get_script_writer
from app.mixer.mixer import Mixer, get_mixer
from app.models.script import ScriptRequest
from app.storage.db import Episode, EpisodeState, get_storage
from app.tts.registry import VoiceRegistry
from app.tts.synthesizer import Synthesizer, get_synthesizer

log = get_logger(__name__)


@dataclass
class OrchestratorResult:
    episode_id: str
    final_path: Path
    duration_sec: float
    size_bytes: int
    total_usage_chars: int


class Orchestrator:
    def __init__(
        self,
        storage=None,
        writer=None,
        synthesizer=None,
        mixer=None,
    ):
        self.storage = storage or get_storage()
        self.writer = writer or get_script_writer()
        self.synthesizer = synthesizer or get_synthesizer()
        self.mixer = mixer or get_mixer()
        self.settings = get_settings()

    async def create_episode(self, topic: str, rounds: int,
                             voice_overrides: dict | None = None) -> str:
        """创建 episode,返回 episode_id"""
        ep_id = f"ep_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        voice_host = voice_overrides.get("host", "female-chengshu") if voice_overrides else "female-chengshu"
        voice_guest = voice_overrides.get("guest", "male-qn-jingying") if voice_overrides else "male-qn-jingying"

        await self.storage.create_episode(ep_id, topic, rounds,
                                          voice_id_host=voice_host,
                                          voice_id_guest=voice_guest)
        log.info("orchestrator.episode_created", id=ep_id)
        return ep_id

    async def generate_script(self, episode_id: str, topic: str, rounds: int) -> Script:
        """Step 1: 生成剧本"""
        req = ScriptRequest(topic=topic, rounds=rounds)
        script = await self.writer.generate(req)
        # 一次性保存脚本 + 切到 scripted 状态
        await self.storage.transition_episode(
            episode_id, EpisodeState.SCRIPTED, script=script
        )
        return script

    async def synthesize_all(self, episode_id: str, script,
                             voice_overrides: dict | None = None) -> list:
        """Step 2: 逐句合成 (支持断点恢复)"""
        await self.storage.transition_episode(episode_id, EpisodeState.SYNTHESIZING)

        # 断点恢复: 检查哪些句已经合成
        completed = await self.storage.get_completed_line_indices(episode_id)
        log.info("orchestrator.synthesize_resume",
                 episode_id=episode_id,
                 already_done=len(completed),
                 total=len(script.lines))

        # 自定义角色音色
        registry = None
        if voice_overrides:
            from app.models.script import Role
            typed_overrides = {}
            if "host" in voice_overrides:
                typed_overrides[Role.HOST] = voice_overrides["host"]
            if "guest" in voice_overrides:
                typed_overrides[Role.GUEST] = voice_overrides["guest"]
            registry = VoiceRegistry(custom=typed_overrides)
            self.synthesizer.registry = registry

        # 输出目录
        out_dir = self.settings.output_dir / episode_id / "tts_segments"
        out_dir.mkdir(parents=True, exist_ok=True)

        results = []
        total_usage = 0
        for i, line in enumerate(script.lines):
            if i in completed:
                # 已存在,从数据库恢复路径
                audios = await self.storage.get_line_audios(episode_id)
                existing = next((a for a in audios if a["line_index"] == i), None)
                if existing:
                    from app.tts.synthesizer import LineAudio
                    results.append(LineAudio(
                        index=i,
                        role=existing["role"],
                        text=existing["text"],
                        voice_id=existing["voice_id"],
                        emotion=existing["emotion"],
                        file_path=Path(existing["file_path"]),
                        duration_ms=existing["duration_ms"],
                        usage_characters=existing["usage_chars"],
                        audio_size_bytes=Path(existing["file_path"]).stat().st_size,
                    ))
                    continue

            # 合成新的一句
            profile = self.synthesizer.registry.get(line.role)
            result = await self.synthesizer.tts.synthesize(
                text=line.text,
                profile=profile,
                output_path=out_dir / f"line_{i:02d}_{line.role.value}.mp3",
            )
            await self.storage.save_line_audio(
                episode_id, i, line.role.value, line.text,
                profile.voice_id, profile.emotion,
                str(result.audio_bytes and (out_dir / f"line_{i:02d}_{line.role.value}.mp3")),
                result.duration_ms, result.usage_characters,
            )
            await self.storage.record_quota(episode_id, "tts", result.usage_characters)
            total_usage += result.usage_characters

            from app.tts.synthesizer import LineAudio
            results.append(LineAudio(
                index=i, role=line.role.value, text=line.text,
                voice_id=profile.voice_id, emotion=profile.emotion,
                file_path=out_dir / f"line_{i:02d}_{line.role.value}.mp3",
                duration_ms=result.duration_ms,
                usage_characters=result.usage_characters,
                audio_size_bytes=result.audio_size_bytes,
            ))

        log.info("orchestrator.synthesize_done",
                 episode_id=episode_id,
                 lines=len(results),
                 total_usage=total_usage)
        return results

    async def mix_episode(self, episode_id: str, line_audios: list,
                          bgm_prompt: str | None = None) -> OrchestratorResult:
        """Step 3: 拼接 + 可选 BGM 混音"""
        await self.storage.transition_episode(episode_id, EpisodeState.MIXING)

        out_dir = self.settings.output_dir / episode_id
        line_files = [la.file_path for la in line_audios]
        voice_only = out_dir / "voice_only.mp3"
        final = out_dir / "final.mp3"

        # 1) 拼接人声
        concat_result = await self.mixer.concat_with_silence(line_files, voice_only)
        log.info("orchestrator.concat_done",
                 duration=concat_result.duration_sec,
                 size=concat_result.size_bytes)

        # 2) 可选 BGM
        if bgm_prompt:
            from app.mixer.music_client import get_music_client
            bgm_path = out_dir / "bgm.mp3"
            music_client = get_music_client()
            try:
                await music_client.generate_bgm(
                    prompt=bgm_prompt,
                    duration_sec=int(concat_result.duration_sec) + 10,
                    output_path=bgm_path,
                )
                await self.storage.record_quota(episode_id, "music", 1)
                mix_result = await self.mixer.mix_with_bgm(voice_only, bgm_path, final)
            except Exception as e:
                log.warning("orchestrator.bgm_failed, falling back to voice only",
                            error=str(e))
                voice_only.replace(final)
                mix_result = concat_result
                mix_result = type(mix_result)(output_path=final,
                                              duration_sec=concat_result.duration_sec,
                                              size_bytes=concat_result.size_bytes)
        else:
            voice_only.replace(final)
            mix_result = type(concat_result)(output_path=final,
                                             duration_sec=concat_result.duration_sec,
                                             size_bytes=concat_result.size_bytes)

        # 3) 完成
        await self.storage.transition_episode(
            episode_id, EpisodeState.COMPLETED,
            final_path=str(final),
            duration_sec=mix_result.duration_sec,
            size_bytes=mix_result.size_bytes,
        )

        total_usage = sum(la.usage_characters for la in line_audios)
        return OrchestratorResult(
            episode_id=episode_id,
            final_path=final,
            duration_sec=mix_result.duration_sec,
            size_bytes=mix_result.size_bytes,
            total_usage_chars=total_usage,
        )

    async def generate_full(
        self,
        topic: str,
        rounds: int = 3,
        bgm_prompt: str | None = None,
        voice_overrides: dict | None = None,
    ) -> OrchestratorResult:
        """一键生成完整播客"""
        ep_id = await self.create_episode(topic, rounds, voice_overrides)
        try:
            script = await self.generate_script(ep_id, topic, rounds)
            audios = await self.synthesize_all(ep_id, script, voice_overrides)
            result = await self.mix_episode(ep_id, audios, bgm_prompt)
            return result
        except Exception as e:
            log.error("orchestrator.failed", episode_id=ep_id, error=str(e))
            await self.storage.transition_episode(
                ep_id, EpisodeState.FAILED, error_message=str(e)
            )
            raise


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator