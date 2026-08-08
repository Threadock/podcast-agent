"""
应用配置 - Pydantic Settings,自动从 .env / 环境变量加载,
API key 缺失时回退读 ~/.hermes/auth.json (Hermes 凭证池)。
"""
from __future__ import annotations
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_credential_from_hermes() -> str | None:
    """
    从 Hermes 的凭证池 ~/.hermes/auth.json 读取 MiniMax key。
    这是开发环境的 fallback,生产环境应该用专用 vault。
    """
    auth_path = Path.home() / ".hermes" / "auth.json"
    if not auth_path.exists():
        return None
    try:
        auth = json.loads(auth_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    pool = auth.get("credential_pool", {})
    for provider in ("minimax-cn", "minimax"):
        items = pool.get(provider, [])
        if not (isinstance(items, list) and items):
            continue
        entry = items[0]
        for field_name, value in entry.items():
            if isinstance(value, str) and value.startswith("sk-") and "..." not in value:
                return value
    return None


class Settings(BaseSettings):
    """全局配置。所有字段可被同名环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ============ 应用元信息 ============
    app_name: str = "podcast-agent"
    app_version: str = "0.1.0"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # ============ MiniMax API ============
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_api_key: str = Field(default="", alias="MINIMAX_CN_API_KEY")

    # ============ 模型配置 ============
    llm_model: str = "MiniMax-M3"
    llm_max_tokens: int = 16384
    llm_temperature: float = 0.7
    llm_timeout_sec: float = 180.0
    llm_max_retries: int = 3

    tts_model: str = "speech-2.6-hd"
    tts_timeout_sec: float = 60.0
    tts_max_retries: int = 3
    tts_rate_limit_rps: float = 3.0

    music_model: str = "music-01"
    music_timeout_sec: float = 120.0
    music_max_retries: int = 2

    # ============ 存储 ============
    data_dir: Path = PROJECT_ROOT / "data"
    output_dir: Path = PROJECT_ROOT / "output"
    sqlite_path: Path = PROJECT_ROOT / "data" / "podcast.db"

    # ============ 音频参数 ============
    audio_sample_rate: int = 32000
    audio_bitrate: int = 128000
    audio_format: str = "mp3"
    audio_channels: int = 1
    silence_between_lines_ms: int = 250
    target_loudness_lufs: float = -16.0

    # ============ 角色配置 ============
    role_default_voice: dict[str, tuple[str, str]] = {
        "host":  ("female-chengshu", "happy"),
        "guest": ("male-qn-jingying", "neutral"),
    }

    def model_post_init(self, __context) -> None:
        """Pydantic 初始化后,如果 key 仍为空,从 Hermes 凭证池加载"""
        if not self.minimax_api_key:
            fallback = _load_credential_from_hermes()
            if fallback:
                self.minimax_api_key = fallback
                os.environ["MINIMAX_CN_API_KEY"] = fallback  # 让 httpx client 也能拿到


@lru_cache
def get_settings() -> Settings:
    """单例 - 整个进程共享一份配置"""
    s = Settings()
    # 确保目录存在
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.output_dir.mkdir(parents=True, exist_ok=True)
    return s