"""
REST API 路由
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

from app.core.errors import EpisodeNotFoundError
from app.core.logging import get_logger
from app.core.orchestrator import get_orchestrator
from app.models.script import ScriptRequest, Script
from app.storage.db import EpisodeState, get_storage
from app.tts.registry import VoiceRegistry

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["podcast"])


@router.post("/podcasts", status_code=201)
async def create_podcast(request: ScriptRequest) -> dict[str, Any]:
    """
    同步生成完整播客 (适合短剧集,30-60秒)。

    长剧集 (>3 分钟) 推荐用 /podcasts/{id} 异步端点。
    """
    orch = get_orchestrator()
    try:
        result = await orch.generate_full(
            topic=request.topic,
            rounds=request.rounds,
            voice_overrides=request.voice_overrides,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "episode_id": result.episode_id,
        "topic": request.topic,
        "duration_sec": result.duration_sec,
        "size_bytes": result.size_bytes,
        "usage_chars": result.total_usage_chars,
        "download_url": f"/api/v1/podcasts/{result.episode_id}/audio",
        "transcript_url": f"/api/v1/podcasts/{result.episode_id}/transcript",
    }


@router.get("/podcasts")
async def list_podcasts(
    limit: int = Query(50, ge=1, le=200),
    state: EpisodeState | None = None,
) -> list[dict[str, Any]]:
    """列出所有 podcast episodes"""
    storage = get_storage()
    eps = await storage.list_episodes(limit=limit, state=state)
    return [ep.to_dict() for ep in eps]


@router.get("/podcasts/{episode_id}")
async def get_podcast(episode_id: str) -> dict[str, Any]:
    """获取单个 episode 详情"""
    storage = get_storage()
    try:
        ep = await storage.get_episode(episode_id)
    except EpisodeNotFoundError:
        raise HTTPException(status_code=404, detail=f"episode {episode_id} not found")
    return ep.to_dict()


@router.get("/podcasts/{episode_id}/audio")
async def download_audio(episode_id: str) -> FileResponse:
    """下载最终 mp3"""
    storage = get_storage()
    try:
        ep = await storage.get_episode(episode_id)
    except EpisodeNotFoundError:
        raise HTTPException(status_code=404, detail=f"episode {episode_id} not found")
    if ep.state != EpisodeState.COMPLETED or not ep.final_path:
        raise HTTPException(status_code=409,
                            detail=f"episode is in state {ep.state.value}, not ready")
    p = Path(ep.final_path)
    if not p.exists():
        raise HTTPException(status_code=410, detail="audio file deleted")
    return FileResponse(p, media_type="audio/mpeg", filename=f"{episode_id}.mp3")


@router.get("/podcasts/{episode_id}/transcript", response_model=None)
async def get_transcript(episode_id: str) -> dict[str, Any]:
    """获取逐字稿 (结构化 JSON)"""
    storage = get_storage()
    try:
        ep = await storage.get_episode(episode_id)
    except EpisodeNotFoundError:
        raise HTTPException(status_code=404, detail=f"episode {episode_id} not found")
    if not ep.script:
        raise HTTPException(status_code=409, detail="script not yet generated")
    return ep.script.model_dump()


@router.get("/voices")
async def list_voices() -> dict[str, list[str]]:
    """列出所有可选音色"""
    return VoiceRegistry.list_available()


@router.get("/quota")
async def get_quota(kind: str | None = None) -> dict[str, Any]:
    """查看配额消耗"""
    storage = get_storage()
    total = await storage.get_quota_total(kind)
    return {"total": total, "kind": kind, "unit": "characters"}