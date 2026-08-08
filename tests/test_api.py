"""
API 集成测试 - 用 httpx.AsyncClient + FastAPI TestClient。
所有 LLM/TTS 都 mock,storage 用临时 db。
"""
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.llm.mock import MockLLMClient, patch_llm_client
from app.tts.mock import MockTTSClient, patch_tts_client


@pytest.fixture(scope="session")
def app_instance(tmp_path_factory):
    """独立的 FastAPI app 实例 + 临时 db"""
    tmp = tmp_path_factory.mktemp("api_test")
    # 配置临时数据库
    import os
    os.environ["SQLITE_PATH_OVERRIDE"] = str(tmp / "test.db")

    # 用 monkey patch 替换 storage
    from app.storage import db as db_module
    db_module._storage = None  # 强制重建
    original_get_storage = db_module.get_storage

    def patched_get_storage():
        if db_module._storage is None:
            db_module._storage = db_module.Storage(db_path=tmp / "test.db")
        return db_module._storage

    db_module.get_storage = patched_get_storage

    # Patch LLM + TTS
    patch_llm_client(MockLLMClient())
    patch_tts_client(MockTTSClient())

    from app.main import create_app
    app = create_app()
    yield app

    # cleanup
    db_module._storage = None
    shutil.rmtree(tmp, ignore_errors=True)


@pytest_asyncio.fixture
async def client(app_instance):
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealthAndRoot:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "llm_model" in data

    @pytest.mark.asyncio
    async def test_root(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "podcast-agent"


class TestVoices:
    @pytest.mark.asyncio
    async def test_list_voices(self, client):
        resp = await client.get("/api/v1/voices")
        assert resp.status_code == 200
        data = resp.json()
        assert "host_alternatives" in data
        assert "guest_alternatives" in data
        assert "female-chengshu" in data["host_alternatives"]


class TestPodcastCRUD:
    @pytest.mark.asyncio
    async def test_create_podcast(self, client):
        resp = await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 3,
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "episode_id" in data
        assert data["duration_sec"] > 0
        assert data["size_bytes"] > 0
        assert "download_url" in data

    @pytest.mark.asyncio
    async def test_list_podcasts(self, client):
        # 先创建一个
        create_resp = await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 2,
        })
        assert create_resp.status_code == 201
        ep_id = create_resp.json()["episode_id"]

        # 列出
        resp = await client.get("/api/v1/podcasts")
        assert resp.status_code == 200
        episodes = resp.json()
        assert any(e["id"] == ep_id for e in episodes)

    @pytest.mark.asyncio
    async def test_get_podcast_detail(self, client):
        create_resp = await client.post("/api/v1/podcasts", json={
            "topic": "量子计算",
            "rounds": 2,
        })
        ep_id = create_resp.json()["episode_id"]

        resp = await client.get(f"/api/v1/podcasts/{ep_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic"] == "量子计算"
        assert data["state"] == "completed"
        assert data["script"] is not None

    @pytest.mark.asyncio
    async def test_get_not_found(self, client):
        resp = await client.get("/api/v1/podcasts/ep_nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_audio(self, client):
        create_resp = await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 2,
        })
        ep_id = create_resp.json()["episode_id"]

        resp = await client.get(f"/api/v1/podcasts/{ep_id}/audio")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/mpeg")
        assert len(resp.content) > 0

    @pytest.mark.asyncio
    async def test_get_transcript(self, client):
        create_resp = await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 2,
        })
        ep_id = create_resp.json()["episode_id"]

        resp = await client.get(f"/api/v1/podcasts/{ep_id}/transcript")
        assert resp.status_code == 200
        data = resp.json()
        # transcript 可能是 string 或 dict
        if isinstance(data, dict):
            assert "title" in data
            assert "lines" in data
        else:
            assert isinstance(data, str)
            assert "AI编程简史" in data or "line" in data.lower()


class TestQuota:
    @pytest.mark.asyncio
    async def test_get_quota_total(self, client):
        # 跑一次生成消耗配额
        await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 3,
        })

        resp = await client.get("/api/v1/quota")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert data["total"] > 0  # 至少消耗了一些 TTS 字符

    @pytest.mark.asyncio
    async def test_get_quota_filtered(self, client):
        resp = await client.get("/api/v1/quota?kind=tts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "tts"


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_topic_rejected(self, client):
        resp = await client.post("/api/v1/podcasts", json={
            "topic": "x",  # 太短
            "rounds": 3,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_rounds_rejected(self, client):
        resp = await client.post("/api/v1/podcasts", json={
            "topic": "AI编程简史",
            "rounds": 0,
        })
        assert resp.status_code == 422


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_schema(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "paths" in schema
        # 关键端点都在
        paths = schema["paths"]
        assert "/health" in paths
        assert "/api/v1/podcasts" in paths
        assert "/api/v1/voices" in paths
        assert "/api/v1/quota" in paths