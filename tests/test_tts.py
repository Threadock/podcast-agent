"""测试 TTS 角色注册表 + 合成器 (Mock TTS)"""
import pytest

from app.core.errors import TTSError
from app.models.script import Line, Role, Script
from app.tts.mock import MockTTSClient, patch_tts_client
from app.tts.registry import VoiceProfile, VoiceRegistry
from app.tts.synthesizer import Synthesizer


class TestVoiceRegistry:
    def test_default_profiles(self):
        reg = VoiceRegistry()
        host = reg.get(Role.HOST)
        guest = reg.get(Role.GUEST)
        assert host.voice_id == "female-chengshu"
        assert guest.voice_id == "male-qn-jingying"
        # 主持人比嘉宾快 (节奏感)
        assert host.speed > guest.speed

    def test_custom_override(self):
        reg = VoiceRegistry(custom={Role.HOST: "female-shaonv"})
        assert reg.get(Role.HOST).voice_id == "female-shaonv"
        # 嘉宾不受影响
        assert reg.get(Role.GUEST).voice_id == "male-qn-jingying"

    def test_invalid_voice_name_keeps_default(self):
        reg = VoiceRegistry(custom={Role.HOST: "not-a-real-voice"})
        # 找不到,fallback 到默认
        assert reg.get(Role.HOST).voice_id == "female-chengshu"

    def test_list_available(self):
        available = VoiceRegistry.list_available()
        assert "host_alternatives" in available
        assert "guest_alternatives" in available
        assert "female-chengshu" in available["host_alternatives"]


class TestSynthesizer:
    @pytest.fixture(autouse=True)
    def reset(self, tmp_path):
        from app.tts import synthesizer as syn
        syn._synthesizer = None
        self.tmp_path = tmp_path
        yield
        syn._synthesizer = None

    def _make_script(self) -> Script:
        lines = [
            Line(role=Role.HOST, text="大家好，欢迎收听本期节目！"),
            Line(role=Role.GUEST, text="今天我们来聊AI编程的简史。"),
            Line(role=Role.HOST, text="记得订阅和点赞哦，下次见！"),
        ]
        return Script(title="测试", lines=lines)

    @pytest.mark.asyncio
    async def test_synthesize_all_lines(self):
        """3 句台词全部合成,生成对应文件"""
        mock = MockTTSClient()
        patch_tts_client(mock)
        syn = Synthesizer()
        script = self._make_script()

        results = await syn.synthesize_script(script, output_dir=self.tmp_path)

        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.index == i
            assert r.role == script.lines[i].role.value
            assert r.file_path.exists()
            assert r.audio_size_bytes > 0
            assert r.duration_ms > 0

    @pytest.mark.asyncio
    async def test_synthesize_uses_correct_voices(self):
        """host 用女声,guest 用男声"""
        mock = MockTTSClient()
        patch_tts_client(mock)
        syn = Synthesizer()
        script = self._make_script()
        results = await syn.synthesize_script(script, output_dir=self.tmp_path)

        host_voices = [r.voice_id for r in results if r.role == "host"]
        guest_voices = [r.voice_id for r in results if r.role == "guest"]
        assert all(v == "female-chengshu" for v in host_voices)
        assert all(v == "male-qn-jingying" for v in guest_voices)

    @pytest.mark.asyncio
    async def test_synthesize_propagates_error(self):
        """TTS 失败应该往上抛"""
        mock = MockTTSClient(fail_mode="api_error")
        patch_tts_client(mock)
        syn = Synthesizer()
        script = self._make_script()
        with pytest.raises(TTSError):
            await syn.synthesize_script(script, output_dir=self.tmp_path)

    @pytest.mark.asyncio
    async def test_synthesize_respects_override(self):
        """用户指定 voice 覆盖默认"""
        mock = MockTTSClient()
        patch_tts_client(mock)
        syn = Synthesizer(registry=VoiceRegistry(custom={Role.HOST: "female-shaonv"}))
        script = self._make_script()
        results = await syn.synthesize_script(script, output_dir=self.tmp_path)
        assert all(r.voice_id == "female-shaonv" for r in results if r.role == "host")