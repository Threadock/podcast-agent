"""
统一异常体系。PoC 用 sys.exit() 抛错,企业级必须用异常 + 中间件。
"""
from __future__ import annotations


class PodcastAgentError(Exception):
    """所有业务异常的基类"""

    def __init__(self, message: str, *, code: str = "internal_error", retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# ============ LLM 异常 ============
class LLMError(PodcastAgentError):
    """LLM 调用相关错误"""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message, code="llm_error", retryable=retryable)


class LLMResponseInvalid(LLMError):
    """LLM 返回内容无法解析为预期格式"""

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


# ============ TTS 异常 ============
class TTSError(PodcastAgentError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message, code="tts_error", retryable=retryable)


# ============ 配额异常 ============
class QuotaExceededError(PodcastAgentError):
    def __init__(self, used: int, limit: int):
        super().__init__(
            f"配额耗尽: 已用 {used}/{limit}",
            code="quota_exceeded",
            retryable=False,
        )
        self.used = used
        self.limit = limit


# ============ 状态异常 ============
class EpisodeNotFoundError(PodcastAgentError):
    def __init__(self, episode_id: str):
        super().__init__(f"找不到 episode: {episode_id}", code="not_found", retryable=False)


class EpisodeStateError(PodcastAgentError):
    """episode 状态非法转换 (例如对已完成的 episode 调用合成)"""

    def __init__(self, episode_id: str, current: str, target: str):
        super().__init__(
            f"episode {episode_id} 状态非法: 当前={current}, 目标={target}",
            code="invalid_state",
            retryable=False,
        )