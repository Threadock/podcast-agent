"""测试数据模型"""
import pytest
from pydantic import ValidationError

from app.models.script import Line, Role, Script, ScriptRequest


class TestLine:
    def test_basic(self):
        line = Line(role=Role.HOST, text="大家好，欢迎收听")
        assert line.role == Role.HOST
        assert line.text == "大家好，欢迎收听"

    def test_text_cleaned(self):
        line = Line(role=Role.GUEST, text="  hello   world  ")
        assert line.text == "hello world"

    def test_too_short_text_rejected(self):
        with pytest.raises(ValidationError):
            Line(role=Role.HOST, text="hi")

    def test_too_long_text_rejected(self):
        with pytest.raises(ValidationError):
            Line(role=Role.HOST, text="x" * 201)


class TestScript:
    def _make_lines(self, n=6):
        # 严格交替: host, guest, host, guest, host, guest
        return [
            Line(role=Role.HOST if i % 2 == 0 else Role.GUEST, text=f"第{i+1}句台词测试内容")
            for i in range(n)
        ]

    def test_valid_script(self):
        script = Script(title="测试标题", tagline="副标题", lines=self._make_lines(4))
        assert len(script.lines) == 4
        assert script.total_characters > 0
        assert script.duration_estimate_sec > 0

    def test_alternation_enforced(self):
        """LLM 返回全部 host 也能被自动修复:除最后一句外严格交替,最后强制 host"""
        bad_lines = [Line(role=Role.HOST, text="全部是 host 的台词测试内容")] * 6
        script = Script(title="测试标题", lines=bad_lines)
        roles = [l.role for l in script.lines]
        # 除末尾两连 host (最后一个强制),其余交替
        assert roles == [Role.HOST, Role.GUEST, Role.HOST, Role.GUEST, Role.HOST, Role.HOST]

    def test_last_line_must_be_host(self):
        """最后一句是 guest 会被改成 host (接受两连 host)"""
        lines = [
            Line(role=Role.HOST, text="开场第一句话够长"),
            Line(role=Role.GUEST, text="嘉宾回答够长一些"),
        ]
        script = Script(title="测试标题", lines=lines)
        assert script.lines[-1].role == Role.HOST

    def test_alternation_3_lines(self):
        """3句 = [H, G, H] 正常交替,无需修复"""
        lines = [
            Line(role=Role.HOST, text="开场第一句话够长"),
            Line(role=Role.GUEST, text="嘉宾回答够长一些"),
            Line(role=Role.HOST, text="结尾召唤订阅够长"),
        ]
        script = Script(title="测试", lines=lines)
        assert [l.role for l in script.lines] == [Role.HOST, Role.GUEST, Role.HOST]

    def test_too_few_lines_rejected(self):
        with pytest.raises(ValidationError):
            Script(title="测试", lines=[Line(role=Role.HOST, text="只有一句测试")])

    def test_too_long_title_rejected(self):
        with pytest.raises(ValidationError):
            Script(title="x" * 31, lines=self._make_lines(4))


class TestScriptRequest:
    def test_defaults(self):
        req = ScriptRequest(topic="AI 编程")
        assert req.rounds == 3
        assert req.voice_overrides == {}

    def test_rounds_bounds(self):
        with pytest.raises(ValidationError):
            ScriptRequest(topic="x", rounds=0)
        with pytest.raises(ValidationError):
            ScriptRequest(topic="x", rounds=21)

    def test_topic_min_length(self):
        with pytest.raises(ValidationError):
            ScriptRequest(topic="x")