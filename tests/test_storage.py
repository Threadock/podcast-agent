"""测试 SQLite 存储 + 状态机 + 断点恢复"""
import pytest
from pathlib import Path

from app.core.errors import EpisodeNotFoundError, EpisodeStateError
from app.models.script import Line, Role, Script
from app.storage.db import EpisodeState, Storage, get_storage_for_path


@pytest.fixture
async def storage(tmp_path: Path):
    s = get_storage_for_path(tmp_path / "test.db")
    yield s
    # cleanup happens automatically


class TestEpisodeCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get(self, storage: Storage):
        ep = await storage.create_episode("ep_001", "AI编程简史", rounds=3)
        assert ep.state == EpisodeState.PENDING
        assert ep.id == "ep_001"

        fetched = await storage.get_episode("ep_001")
        assert fetched.topic == "AI编程简史"
        assert fetched.rounds == 3

    @pytest.mark.asyncio
    async def test_get_not_found(self, storage: Storage):
        with pytest.raises(EpisodeNotFoundError):
            await storage.get_episode("ep_nonexistent")


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_valid_transition(self, storage: Storage):
        await storage.create_episode("ep_002", "量子计算", rounds=2)
        ep = await storage.transition_episode("ep_002", EpisodeState.SCRIPTED)
        assert ep.state == EpisodeState.SCRIPTED

        ep = await storage.transition_episode("ep_002", EpisodeState.SYNTHESIZING)
        assert ep.state == EpisodeState.SYNTHESIZING

    @pytest.mark.asyncio
    async def test_skip_states_rejected(self, storage: Storage):
        """PENDING 不能直接跳到 COMPLETED"""
        await storage.create_episode("ep_003", "x", rounds=2)
        with pytest.raises(EpisodeStateError):
            await storage.transition_episode("ep_003", EpisodeState.COMPLETED)

    @pytest.mark.asyncio
    async def test_completed_is_terminal(self, storage: Storage):
        """COMPLETED 不能再转换"""
        await storage.create_episode("ep_004", "x", rounds=2)
        for next_state in [EpisodeState.SCRIPTED, EpisodeState.SYNTHESIZING,
                           EpisodeState.MIXING, EpisodeState.COMPLETED]:
            await storage.transition_episode("ep_004", next_state)
        with pytest.raises(EpisodeStateError):
            await storage.transition_episode("ep_004", EpisodeState.SCRIPTED)

    @pytest.mark.asyncio
    async def test_save_script_with_transition(self, storage: Storage):
        script = Script(
            title="测试",
            lines=[
                Line(role=Role.HOST, text="开场第一句话够长"),
                Line(role=Role.GUEST, text="嘉宾回答够长一些"),
            ],
        )
        await storage.create_episode("ep_005", "x", rounds=1)
        ep = await storage.transition_episode("ep_005", EpisodeState.SCRIPTED, script=script)
        assert ep.script is not None
        assert ep.script.title == "测试"
        # 从数据库再读一遍
        ep2 = await storage.get_episode("ep_005")
        assert ep2.script is not None
        assert ep2.script.title == "测试"


class TestLineAudios:
    @pytest.mark.asyncio
    async def test_save_and_get_completed(self, storage: Storage):
        await storage.create_episode("ep_006", "x", rounds=3)
        for i in range(3):
            await storage.save_line_audio(
                "ep_006", i, "host", f"台词{i}够长测试", "voice1", "happy",
                f"/tmp/line_{i}.mp3", 2000, 10,
            )
        completed = await storage.get_completed_line_indices("ep_006")
        assert completed == {0, 1, 2}

    @pytest.mark.asyncio
    async def test_resume_partial(self, storage: Storage):
        """断点恢复:只合成了一半,记录里有 0,1 没有 2"""
        await storage.create_episode("ep_007", "x", rounds=3)
        for i in [0, 1]:
            await storage.save_line_audio(
                "ep_007", i, "host", f"台词{i}够长测试", "voice1", "happy",
                f"/tmp/line_{i}.mp3", 2000, 10,
            )
        completed = await storage.get_completed_line_indices("ep_007")
        assert 2 not in completed
        # 下次从 index 2 开始继续

    @pytest.mark.asyncio
    async def test_get_line_audios_ordered(self, storage: Storage):
        await storage.create_episode("ep_008", "x", rounds=3)
        # 故意乱序插入
        for i in [2, 0, 1]:
            await storage.save_line_audio(
                "ep_008", i, "host", f"台词{i}", "voice", "happy",
                f"/tmp/line_{i}.mp3", 2000, 10,
            )
        audios = await storage.get_line_audios("ep_008")
        assert [a["line_index"] for a in audios] == [0, 1, 2]


class TestQuota:
    @pytest.mark.asyncio
    async def test_record_and_total(self, storage: Storage):
        await storage.record_quota("ep_009", "tts", 100)
        await storage.record_quota("ep_009", "tts", 200)
        await storage.record_quota("ep_009", "music", 1)
        assert await storage.get_quota_total("tts") == 300
        assert await storage.get_quota_total("music") == 1
        assert await storage.get_quota_total() == 301