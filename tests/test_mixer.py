"""测试 Mixer (ffmpeg 多轨混音)"""
import pytest
from pathlib import Path

from app.mixer.mixer import Mixer


@pytest.fixture
def mixer():
    return Mixer()


@pytest.fixture
def voice_files(tmp_path: Path):
    """生成 3 句不同长度的语音 mp3"""
    files = []
    for i, secs in enumerate([1.0, 1.5, 0.8]):
        f = tmp_path / f"line_{i}.mp3"
        # 用 ffmpeg lavfi 生成静音
        import subprocess
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r=32000:cl=mono",
            "-t", str(secs), "-q:a", "9",
            "-acodec", "libmp3lame",
            str(f),
        ], check=True, capture_output=True)
        files.append(f)
    return files


@pytest.fixture
def bgm_file(tmp_path: Path):
    """生成 5 秒的纯音调 (440Hz) 模拟 BGM"""
    import subprocess
    f = tmp_path / "bgm.mp3"
    # 用 sine 波
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=5",
        "-ac", "1", "-ar", "32000",
        "-acodec", "libmp3lame",
        str(f),
    ], check=True, capture_output=True)
    return f


class TestConcatWithSilence:
    @pytest.mark.asyncio
    async def test_concat_three_files(self, mixer, voice_files, tmp_path):
        output = tmp_path / "concatenated.mp3"
        result = await mixer.concat_with_silence(voice_files, output)

        assert output.exists()
        assert result.duration_sec > 0
        # 3 句总时长 1.0 + 1.5 + 0.8 = 3.3 + 2 段静音 (默认 250ms)
        # 实际允许 ±0.3 秒误差
        assert 3.5 < result.duration_sec < 4.0

    @pytest.mark.asyncio
    async def test_concat_empty_raises(self, mixer, tmp_path):
        with pytest.raises(ValueError, match="不能为空"):
            await mixer.concat_with_silence([], tmp_path / "out.mp3")

    @pytest.mark.asyncio
    async def test_concat_custom_silence(self, mixer, voice_files, tmp_path):
        output = tmp_path / "out.mp3"
        result = await mixer.concat_with_silence(voice_files, output, silence_ms=500)
        # 静音更长 → 总时长更长
        assert result.duration_sec > 4.0


class TestMixWithBGM:
    @pytest.mark.asyncio
    async def test_mix_voice_and_bgm(self, mixer, voice_files, bgm_file, tmp_path):
        # 先拼接人声
        voice_path = tmp_path / "voice.mp3"
        await mixer.concat_with_silence(voice_files, voice_path)

        # 混音
        output = tmp_path / "with_bgm.mp3"
        result = await mixer.mix_with_bgm(voice_path, bgm_file, output)

        assert output.exists()
        # 时长跟人声一致 (BGM 被 truncate / loop)
        assert result.duration_sec > 3.0
        assert result.size_bytes > 0

    @pytest.mark.asyncio
    async def test_mix_with_custom_loudness(self, mixer, voice_files, bgm_file, tmp_path):
        voice_path = tmp_path / "voice.mp3"
        await mixer.concat_with_silence(voice_files, voice_path)
        output = tmp_path / "loud.mp3"
        result = await mixer.mix_with_bgm(voice_path, bgm_file, output, target_loudness_lufs=-14.0)
        assert result.duration_sec > 0