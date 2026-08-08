"""
剧本生成服务 - 把 LLM 输出解析为 Script (Pydantic) 模型。
"""
from __future__ import annotations
import json
import re

from app.core.errors import LLMResponseInvalid
from app.core.logging import get_logger
from app.llm.client import get_llm_client
from app.models.script import Script, ScriptRequest

log = get_logger(__name__)


SCRIPT_SYSTEM_PROMPT = """你是专业播客编剧,严格输出 JSON,不加任何解释文字。"""

SCRIPT_PROMPT_TEMPLATE = """## 任务
围绕主题「{topic}」写一段播客对话。

## 角色
- host: 女主持人(开场、追问、收尾)
- guest: 男嘉宾(解释事实、举数字、深入分析)

## 强制规则
1. 严格交替: host, guest, host, guest, host, guest ...
2. 总共 {n} 句台词 ({rounds} 轮对话)
3. 每句 30-80 字,口语化
4. 至少 3 个具体年份/数字/产品名/事件
5. 最后一句必须是 host,包含订阅/点赞/下期预告

## 输出格式 (只输出 JSON,不要 markdown 包裹)
{{
  "title": "标题(15字以内)",
  "tagline": "副标题",
  "lines": [
    {{"role": "host",  "text": "..."}},
    {{"role": "guest", "text": "..."}}
  ]
}}
"""


def _strip_markdown_fence(content: str) -> str:
    """剥离 ```json ... ``` 包裹"""
    content = content.strip()
    if content.startswith("```"):
        parts = content.split("```", 2)
        if len(parts) >= 3:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            return inner.strip().rstrip("`").strip()
    return content


class ScriptWriter:
    """剧本生成高层封装"""

    def __init__(self):
        self.llm = get_llm_client()

    async def generate(self, request: ScriptRequest) -> Script:
        """
        生成剧本。LLM 输出 → JSON → Pydantic Script (含交替修复)。
        """
        n = request.rounds * 2
        prompt = SCRIPT_PROMPT_TEMPLATE.format(
            topic=request.topic,
            rounds=request.rounds,
            n=n,
        )

        log.info("script_writer.generate", topic=request.topic, rounds=request.rounds)

        raw = await self.llm.chat(
            prompt=prompt,
            system=SCRIPT_SYSTEM_PROMPT,
        )
        content = _strip_markdown_fence(raw)

        # 尝试解析 JSON (用正则做容错,有时 LLM 会在 JSON 外加废话)
        json_match = re.search(r"\{[\s\S]*\}", content)
        if not json_match:
            raise LLMResponseInvalid(f"LLM 输出找不到 JSON: {content[:200]}")
        json_str = json_match.group(0)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise LLMResponseInvalid(f"JSON 解析失败: {e}\n{json_str[:300]}")

        try:
            script = Script(**data)
        except Exception as e:
            raise LLMResponseInvalid(f"Pydantic 校验失败: {e}\n{data}")

        log.info(
            "script_writer.done",
            title=script.title,
            lines=len(script.lines),
            chars=script.total_characters,
        )
        return script


_writer: ScriptWriter | None = None


def get_script_writer() -> ScriptWriter:
    global _writer
    if _writer is None:
        _writer = ScriptWriter()
    return _writer