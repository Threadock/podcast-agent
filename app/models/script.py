"""
剧本数据结构 - Pydantic v2。
LLM 输出反序列化为这些模型,自动校验、自动修复。
"""
from __future__ import annotations
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


class Role(str, Enum):
    """支持的播客角色"""

    HOST = "host"
    GUEST = "guest"


class Line(BaseModel):
    """单句台词"""

    role: Role
    text: Annotated[str, Field(min_length=4, max_length=200, description="台词文本")]

    @field_validator("text")
    @classmethod
    def _clean_text(cls, v: str) -> str:
        """去除首尾空白、合并多余空白"""
        return " ".join(v.split())


class Script(BaseModel):
    """完整播客剧本"""

    title: Annotated[str, Field(min_length=2, max_length=30)]
    tagline: Annotated[str, Field(default="", max_length=80)]
    lines: Annotated[list[Line], Field(min_length=2, max_length=200)]

    @model_validator(mode="after")
    def _enforce_structure(self) -> "Script":
        """
        业务规则:
        1. host/guest 交替 (允许结尾两连 host 以满足 CTA 规则)
        2. 最后一句必须是 host (call-to-action)
        """
        if len(self.lines) < 2:
            raise ValueError("至少需要 2 句台词")

        # 交替修复: 除最后一句外严格交替
        expected_seq = [Role.HOST, Role.GUEST]
        fixed = []
        for i, line in enumerate(self.lines[:-1]):
            want = expected_seq[i % 2]
            if line.role != want:
                line = line.model_copy(update={"role": want})
            fixed.append(line)
        # 最后一句强制 host
        last = self.lines[-1]
        if last.role != Role.HOST:
            last = last.model_copy(update={"role": Role.HOST})
        fixed.append(last)

        self.lines = fixed
        return self

    @property
    def total_characters(self) -> int:
        return sum(len(line.text) for line in self.lines)

    @property
    def duration_estimate_sec(self) -> float:
        """估算时长:中文 ~3.5 字/秒"""
        return self.total_characters / 3.5

    def to_prompt_input(self) -> str:
        """用于 LLM prompt 的简化格式"""
        lines_str = "\n".join(f"{i+1}. {l.role.value}: {l.text}" for i, l in enumerate(self.lines))
        return f"# {self.title}\n*{self.tagline}*\n\n{lines_str}"


class ScriptRequest(BaseModel):
    """用户请求:主题 + 轮数"""

    topic: Annotated[str, Field(min_length=2, max_length=100)]
    rounds: Annotated[int, Field(ge=1, le=20, default=3, description="对话轮数(1轮=2句)")]
    voice_overrides: dict[Role, str] = Field(default_factory=dict, description="角色自定义音色")