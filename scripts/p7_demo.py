"""
验收脚本: 不依赖真实 MiniMax key,用 Mock 全套跑通完整 pipeline。
产出真实 mp3 + 逐字稿 + 配额记录,验证架构没问题。

跑通后,P7 真实验收只需把 mock 替换成真实 client。
"""
import asyncio
import json
import shutil
from pathlib import Path

from app.llm.mock import MockLLMClient, patch_llm_client
from app.tts.mock import MockTTSClient, patch_tts_client
from app.core.orchestrator import get_orchestrator
from app.storage.db import get_storage_for_path


async def main():
    # 1. 准备环境: 临时 DB + 清空 output
    test_dir = Path("/tmp/podcast_p7_demo")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    db_path = test_dir / "test.db"
    output_dir = test_dir / "output"

    # 2. Patch 全部 mock + 替换 storage
    patch_llm_client(MockLLMClient())
    patch_tts_client(MockTTSClient())

    from app.storage import db as db_module
    db_module._storage = None

    def factory():
        if db_module._storage is None:
            db_module._storage = db_module.Storage(db_path=db_path)
        return db_module._storage

    db_module.get_storage = factory

    from app.core.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    s.output_dir = output_dir

    # 3. 跑 orchestrator
    orch = get_orchestrator()
    print("=" * 60)
    print("🎙️  P7 验收: 端到端跑一集")
    print("=" * 60)

    result = await orch.generate_full(
        topic="AI编程简史",
        rounds=3,
    )

    print()
    print("=" * 60)
    print("✅ 完成!")
    print("=" * 60)
    print(f"  Episode:    {result.episode_id}")
    print(f"  最终 mp3:   {result.final_path}")
    print(f"  时长:       {result.duration_sec:.1f}秒")
    print(f"  文件大小:   {result.size_bytes/1024:.1f} KB")
    print(f"  TTS 消耗:   {result.total_usage_chars} 字符")

    # 4. 验证 mp3 真存在 + ffprobe 能读
    import subprocess
    assert result.final_path.exists()
    probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,bit_rate,size",
        "-of", "json", str(result.final_path),
    ], capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)["format"]
    print()
    print("📊 ffprobe 实测:")
    print(f"  duration:   {info['duration']}秒")
    print(f"  bit_rate:   {int(info['bit_rate'])//1000}kbps")
    print(f"  size:       {int(info['size'])//1024} KB")

    # 5. 验证 episode 状态
    from app.storage.db import EpisodeState
    ep = await db_module.get_storage().get_episode(result.episode_id)
    assert ep.state == EpisodeState.COMPLETED, f"expected completed, got {ep.state}"
    print(f"\n✅ episode state: {ep.state.value}")

    # 6. 配额
    total = await db_module.get_storage().get_quota_total("tts")
    print(f"✅ quota usage: {total} chars (TTS)")

    print(f"\n🎧 试听: open {result.final_path}")
    print(f"📁 所有产物: {test_dir}/")


if __name__ == "__main__":
    asyncio.run(main())