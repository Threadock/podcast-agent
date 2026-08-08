"""
FastAPI 主应用 - P0 阶段只验证骨架 + health 端点。
"""
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.http import http_lifespan
from app.core.logging import configure_logging, get_logger


configure_logging(level=get_settings().log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期:启动时初始化 http 客户端,关闭时释放。"""
    log.info("app.starting", name=app.title, version=app.version)
    async with http_lifespan():
        yield
    log.info("app.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="AI 播客生成 Agent - MiniMax 全栈",
        lifespan=lifespan,
    )

    from app.api.routes import router as api_router
    app.include_router(api_router)

    @app.get("/health")
    async def health() -> dict:
        """健康检查 + 配置摘要(脱敏)"""
        return {
            "status": "ok",
            "version": settings.app_version,
            "environment": settings.environment,
            "llm_model": settings.llm_model,
            "tts_model": settings.tts_model,
            "has_api_key": bool(settings.minimax_api_key),
        }

    @app.get("/")
    async def root() -> dict:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()