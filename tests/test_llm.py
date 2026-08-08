"""测试剧本生成服务 (Mock LLM)"""
import json
import pytest

from app.core.errors import LLMResponseInvalid
from app.llm.mock import MOCK_SCRIPTS, MockLLMClient, patch_llm_client
from app.llm.script_writer import _strip_markdown_fence, get_script_writer
from app.models.script import Role, ScriptRequest


@pytest.fixture(autouse=True)
def reset_singletons():
    """每个测试前清空 script_writer 单例"""
    from app.llm import script_writer as sw
    sw._writer = None
    yield
    sw._writer = None


class TestMarkdownStrip:
    def test_strip_json_fence(self):
        text = "```json\n{\"a\": 1}\n```"
        assert _strip_markdown_fence(text) == '{"a": 1}'

    def test_strip_plain_fence(self):
        text = "```\n{\"a\": 1}\n```"
        assert _strip_markdown_fence(text) == '{"a": 1}'

    def test_no_fence(self):
        text = '{"a": 1}'
        assert _strip_markdown_fence(text) == '{"a": 1}'

    def test_strip_with_surrounding_text_returns_unchanged(self):
        """_strip_markdown_fence 不知道处理 surrounding text,留给 script_writer 用正则兜底"""
        text = "好的这是剧本：\n```json\n{\"a\": 1}\n```\n结束"
        # 原样返回 - 实际提取靠 script_writer.generate 的 re.search
        assert _strip_markdown_fence(text) == text


class TestScriptWriter:
    @pytest.mark.asyncio
    async def test_generate_ai_history(self):
        """Mock LLM 返回 AI 编程简史剧本,验证解析 + 校验 + 交替修复"""
        mock = MockLLMClient()
        patch_llm_client(mock)
        writer = get_script_writer()
        req = ScriptRequest(topic="AI编程简史", rounds=3)
        script = await writer.generate(req)

        assert script.title == "AI编程简史"
        assert len(script.lines) == 6
        # 最后一句必须是 host
        assert script.lines[-1].role == Role.HOST
        # 严格交替 (除最后一句)
        roles = [l.role for l in script.lines]
        assert roles == [Role.HOST, Role.GUEST, Role.HOST, Role.GUEST, Role.HOST, Role.HOST]
        # 信息密度 (Mock 剧本里有 1950/1956/1997/2012/2020/2022/2023 数字)
        all_text = " ".join(l.text for l in script.lines)
        assert any(year in all_text for year in ["1950", "1956", "2020", "2023"])

    @pytest.mark.asyncio
    async def test_generate_handles_bad_alternation(self):
        """Mock 返回全部 host,客户端兜底修复"""
        mock = MockLLMClient()
        patch_llm_client(mock)
        writer = get_script_writer()
        # 强制 rounds=2 (4句),LLM 的 mock 会给 6 句 - 用主题来覆盖
        req = ScriptRequest(topic="AI编程简史", rounds=2)
        script = await writer.generate(req)
        # 6 句固定,跟 rounds 无关 (Mock 总是返回完整脚本)
        assert script.lines[-1].role == Role.HOST

    @pytest.mark.asyncio
    async def test_generate_with_invalid_json(self):
        """LLM 返回非 JSON,验证错误转换"""
        mock = MockLLMClient(fail_mode="invalid_json")
        patch_llm_client(mock)
        writer = get_script_writer()
        req = ScriptRequest(topic="AI编程简史")
        with pytest.raises(LLMResponseInvalid):
            await writer.generate(req)

    @pytest.mark.asyncio
    async def test_generate_with_empty_response(self):
        """LLM 返回空 content"""
        mock = MockLLMClient(fail_mode="empty")
        patch_llm_client(mock)
        writer = get_script_writer()
        req = ScriptRequest(topic="AI编程简史")
        with pytest.raises(LLMResponseInvalid):
            await writer.generate(req)