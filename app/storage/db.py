"""
存储层 - SQLite + aiosqlite。
- episodes 表: 每集播客的状态机
- line_audios 表: 每句合成结果(支持断点恢复)
- quota_usage 表: TTS/Music 配额消耗记录
"""
from __future__ import annotations
import json
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from app.core.config import get_settings
from app.core.errors import EpisodeNotFoundError, EpisodeStateError
from app.core.logging import get_logger
from app.models.script import Script

log = get_logger(__name__)


class EpisodeState(str, Enum):
    PENDING = "pending"          # 已创建,未开始
    SCRIPTED = "scripted"        # 剧本已生成
    SYNTHESIZING = "synthesizing"
    MIXING = "mixing"
    COMPLETED = "completed"
    FAILED = "failed"


# 状态转换图
VALID_TRANSITIONS: dict[EpisodeState, set[EpisodeState]] = {
    EpisodeState.PENDING: {EpisodeState.SCRIPTED, EpisodeState.FAILED},
    EpisodeState.SCRIPTED: {EpisodeState.SYNTHESIZING, EpisodeState.FAILED},
    EpisodeState.SYNTHESIZING: {EpisodeState.MIXING, EpisodeState.FAILED},
    EpisodeState.MIXING: {EpisodeState.COMPLETED, EpisodeState.FAILED},
    EpisodeState.COMPLETED: set(),
    EpisodeState.FAILED: set(),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    rounds          INTEGER NOT NULL,
    state           TEXT NOT NULL,
    script_json     TEXT,           -- Script 序列化
    voice_id_host   TEXT,
    voice_id_guest  TEXT,
    final_path      TEXT,           -- 最终 mp3 路径
    duration_sec    REAL,
    size_bytes      INTEGER,
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_audios (
    episode_id      TEXT NOT NULL,
    line_index      INTEGER NOT NULL,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    voice_id        TEXT NOT NULL,
    emotion         TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    usage_chars     INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (episode_id, line_index),
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quota_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id      TEXT,
    kind            TEXT NOT NULL,  -- 'tts' | 'music' | 'llm'
    units           INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes(state);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at);
CREATE INDEX IF NOT EXISTS idx_quota_created ON quota_usage(created_at);
"""


class Episode:
    """episode 内存模型"""

    def __init__(
        self,
        id: str,
        topic: str,
        rounds: int,
        state: EpisodeState,
        script: Script | None = None,
        voice_id_host: str = "female-chengshu",
        voice_id_guest: str = "male-qn-jingying",
        final_path: str | None = None,
        duration_sec: float | None = None,
        size_bytes: int | None = None,
        error_message: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ):
        self.id = id
        self.topic = topic
        self.rounds = rounds
        self.state = state
        self.script = script
        self.voice_id_host = voice_id_host
        self.voice_id_guest = voice_id_guest
        self.final_path = final_path
        self.duration_sec = duration_sec
        self.size_bytes = size_bytes
        self.error_message = error_message
        self.created_at = created_at or datetime.utcnow().isoformat()
        self.updated_at = updated_at or datetime.utcnow().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "rounds": self.rounds,
            "state": self.state.value,
            "script": self.script.model_dump() if self.script else None,
            "voice_id_host": self.voice_id_host,
            "voice_id_guest": self.voice_id_guest,
            "final_path": self.final_path,
            "duration_sec": self.duration_sec,
            "size_bytes": self.size_bytes,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Storage:
    """SQLite 存储单例"""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_settings().sqlite_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True
        log.info("storage.schema_ready", db_path=str(self.db_path))

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    # ============ Episode CRUD ============
    async def create_episode(self, episode_id: str, topic: str, rounds: int,
                             voice_id_host: str = "female-chengshu",
                             voice_id_guest: str = "male-qn-jingying") -> Episode:
        ep = Episode(
            id=episode_id,
            topic=topic,
            rounds=rounds,
            state=EpisodeState.PENDING,
            voice_id_host=voice_id_host,
            voice_id_guest=voice_id_guest,
        )
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO episodes
                   (id, topic, rounds, state, voice_id_host, voice_id_guest, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ep.id, ep.topic, ep.rounds, ep.state.value,
                 ep.voice_id_host, ep.voice_id_guest,
                 ep.created_at, ep.updated_at),
            )
            await db.commit()
        log.info("storage.episode_created", id=episode_id, topic=topic)
        return ep

    async def get_episode(self, episode_id: str) -> Episode:
        async with self._connect() as db:
            async with db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            raise EpisodeNotFoundError(episode_id)
        script = None
        if row["script_json"]:
            script = Script(**json.loads(row["script_json"]))
        return Episode(
            id=row["id"],
            topic=row["topic"],
            rounds=row["rounds"],
            state=EpisodeState(row["state"]),
            script=script,
            voice_id_host=row["voice_id_host"],
            voice_id_guest=row["voice_id_guest"],
            final_path=row["final_path"],
            duration_sec=row["duration_sec"],
            size_bytes=row["size_bytes"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def transition_episode(
        self,
        episode_id: str,
        new_state: EpisodeState,
        **updates: Any,
    ) -> Episode:
        """状态转换,验证合法性"""
        ep = await self.get_episode(episode_id)
        if new_state not in VALID_TRANSITIONS[ep.state]:
            raise EpisodeStateError(episode_id, ep.state.value, new_state.value)

        updates["state"] = new_state.value
        updates["updated_at"] = datetime.utcnow().isoformat()

        # Script 是 dict 时序列化
        if "script" in updates and isinstance(updates["script"], Script):
            updates["script_json"] = json.dumps(updates.pop("script").model_dump(),
                                                ensure_ascii=False)

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [episode_id]

        async with self._connect() as db:
            await db.execute(
                f"UPDATE episodes SET {set_clause} WHERE id = ?",
                values,
            )
            await db.commit()
        log.info("storage.episode_transition", id=episode_id, new_state=new_state.value)
        return await self.get_episode(episode_id)

    async def list_episodes(self, limit: int = 50, state: EpisodeState | None = None) -> list[Episode]:
        sql = "SELECT * FROM episodes"
        params: list = []
        if state:
            sql += " WHERE state = ?"
            params.append(state.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._connect() as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [await self._row_to_episode(r) for r in rows]

    async def _row_to_episode(self, row: aiosqlite.Row) -> Episode:
        script = None
        if row["script_json"]:
            script = Script(**json.loads(row["script_json"]))
        return Episode(
            id=row["id"],
            topic=row["topic"],
            rounds=row["rounds"],
            state=EpisodeState(row["state"]),
            script=script,
            voice_id_host=row["voice_id_host"],
            voice_id_guest=row["voice_id_guest"],
            final_path=row["final_path"],
            duration_sec=row["duration_sec"],
            size_bytes=row["size_bytes"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ============ Line Audio ============
    async def save_line_audio(
        self,
        episode_id: str,
        line_index: int,
        role: str,
        text: str,
        voice_id: str,
        emotion: str,
        file_path: str,
        duration_ms: int,
        usage_chars: int,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO line_audios
                   (episode_id, line_index, role, text, voice_id, emotion,
                    file_path, duration_ms, usage_chars, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (episode_id, line_index, role, text, voice_id, emotion,
                 file_path, duration_ms, usage_chars,
                 datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def get_completed_line_indices(self, episode_id: str) -> set[int]:
        """断点恢复: 返回已合成的 line_index 集合"""
        async with self._connect() as db:
            async with db.execute(
                "SELECT line_index FROM line_audios WHERE episode_id = ?",
                (episode_id,),
            ) as cur:
                rows = await cur.fetchall()
        return {r["line_index"] for r in rows}

    async def get_line_audios(self, episode_id: str) -> list[dict]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT * FROM line_audios WHERE episode_id = ? ORDER BY line_index",
                (episode_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ============ Quota ============
    async def record_quota(self, episode_id: str | None, kind: str, units: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO quota_usage (episode_id, kind, units, created_at) VALUES (?, ?, ?, ?)",
                (episode_id, kind, units, datetime.utcnow().isoformat()),
            )
            await db.commit()

    async def get_quota_total(self, kind: str | None = None) -> int:
        sql = "SELECT COALESCE(SUM(units), 0) as total FROM quota_usage"
        params: list = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        async with self._connect() as db:
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return row["total"]


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


def get_storage_for_path(db_path: Path) -> Storage:
    """为测试提供临时数据库的工厂"""
    return Storage(db_path=db_path)