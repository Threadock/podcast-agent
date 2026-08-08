"""
角色注册表 - 角色 → (voice_id, emotion, speed, pitch) 映射。
"""
from __future__ import annotations
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.script import Role

log = get_logger(__name__)


@dataclass(frozen=True)
class VoiceProfile:
    """角色语音配置"""

    voice_id: str
    emotion: str = "neutral"
    speed: float = 1.0
    pitch: int = 0


class VoiceRegistry:
    """所有角色语音配置在这里集中管理。LLM 编剧产出 Script 后,
    合成阶段用这个 registry 给每个 Line 分配 VoiceProfile。
    """

    DEFAULT_PROFILES: dict[str, VoiceProfile] = {
        "host":  VoiceProfile(
            voice_id="female-chengshu",
            emotion="happy",
            speed=1.05,  # 主持人稍快,体现节奏感
        ),
        "guest": VoiceProfile(
            voice_id="male-qn-jingying",
            emotion="neutral",
            speed=0.95,  # 嘉宾稍慢,体现深度
        ),
    }

    # 可选的其他音色预设 (供 API 用户动态切换)
    ALTERNATIVE_HOST_VOICES: dict[str, VoiceProfile] = {
        "female-shaonv":  VoiceProfile(voice_id="female-shaonv", emotion="happy"),
        "female-yujie":   VoiceProfile(voice_id="female-yujie", emotion="neutral"),
        "female-chengshu": DEFAULT_PROFILES["host"],
    }
    ALTERNATIVE_GUEST_VOICES: dict[str, VoiceProfile] = {
        "male-qn-jingying": DEFAULT_PROFILES["guest"],
        "male-qn-qingse":  VoiceProfile(voice_id="male-qn-qingse", emotion="neutral"),
        "male-qn-yuanbu":  VoiceProfile(voice_id="male-qn-yuanbu", emotion="neutral", speed=0.9),
    }

    def __init__(self, custom: dict[Role, str] | None = None):
        """
        custom: 用户在请求中指定的角色覆盖
        例: {Role.HOST: "female-shaonv"} 把 host 换成少女音
        """
        self._profiles: dict[str, VoiceProfile] = dict(self.DEFAULT_PROFILES)
        if custom:
            for role, voice_name in custom.items():
                profile = self._lookup_alternative(role, voice_name)
                if profile:
                    self._profiles[role.value] = profile
                    log.info("voice_registry.override", role=role.value, voice=voice_name)

    def _lookup_alternative(self, role: Role, voice_name: str) -> VoiceProfile | None:
        if role == Role.HOST:
            return self.ALTERNATIVE_HOST_VOICES.get(voice_name)
        return self.ALTERNATIVE_GUEST_VOICES.get(voice_name)

    def get(self, role: Role) -> VoiceProfile:
        """获取角色的语音配置,找不到用 host 默认"""
        return self._profiles.get(role.value, self.DEFAULT_PROFILES["host"])

    @classmethod
    def list_available(cls) -> dict[str, list[str]]:
        """列出所有可选音色 (供 API 给前端用)"""
        return {
            "host_alternatives": list(cls.ALTERNATIVE_HOST_VOICES.keys()),
            "guest_alternatives": list(cls.ALTERNATIVE_GUEST_VOICES.keys()),
        }