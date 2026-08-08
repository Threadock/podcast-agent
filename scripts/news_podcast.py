"""
新闻播客生成 - 真实 MiniMax 全栈 (LLM + TTS + Mixer)
输入: 今天的科技新闻 (用 web_search 拉取)
输出: 5 分钟 mp3 + 飞书发送

依赖 hermes-agent 进程内 key 注入 (MINIMAX_CN_API_KEY 环境变量)。
直接执行: cd ~/projects/podcast-agent && .venv/bin/python scripts/news_podcast.py
"""
import asyncio
import os
import sys
import json
from datetime import datetime
from pathlib import Path

# 加项目根到 sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require_api_key() -> str:
    """要求真实 MiniMax key (PoC 验证过 hermes 进程会自动注入)"""
    key = os.environ.get("MINIMAX_CN_API_KEY", "")
    if not key or "..." in key or len(key) < 50:
        print("❌ MINIMAX_CN_API_KEY 不存在或为占位符")
        print("   必须在 hermes-agent 进程内执行 (key 自动注入)")
        print("   或者手动: export MINIMAX_CN_API_KEY=<your-real-key>")
        sys.exit(1)
    return key


async def fetch_news() -> list[dict]:
    """拉取今天科技新闻 - 用 web_search"""
    from hermes_tools import web_search

    queries = [
        "今日 AI 人工智能 重要新闻 2026",
        "tech news today AI breakthrough 2026",
        "OpenAI Anthropic DeepSeek 最新发布 2026",
        "大模型 算力 芯片 新闻 今天",
    ]

    all_results = []
    for q in queries:
        try:
            res = web_search(query=q, limit=5)
            items = res.get("data", {}).get("web", [])
            for it in items:
                all_results.append({
                    "query": q,
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "snippet": it.get("description", "")[:200],
                })
        except Exception as e:
            print(f"  search '{q}' failed: {e}")

    # 去重 (URL)
    seen = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    print(f"  fetched {len(all_results)} results, {len(unique)} unique")
    return unique[:15]  # 限制到 15 条


async def main():
    print("=" * 60)
    print("📰 新闻播客生成 - MiniMax 全栈")
    print("=" * 60)

    # 1. 验证 key
    api_key = require_api_key()
    print(f"✅ API key loaded (len={len(api_key)})")

    # 2. 拉新闻
    print("\n📡 [1/4] 拉取科技新闻...")
    news = await fetch_news()
    if not news:
        print("❌ 拉不到新闻,退出")
        return

    # 3. 生成剧本
    print(f"\n📝 [2/4] LLM 编剧 (基于 {len(news)} 条新闻)...")
    from app.llm.script_writer import get_script_writer
    from app.models.script import ScriptRequest

    # 把新闻打包成 prompt context
    news_text = "\n".join(
        f"{i+1}. {n['title']} - {n['snippet']}"
        for i, n in enumerate(news[:10])
    )

    topic = f"今日科技新闻精选 ({datetime.now().strftime('%Y-%m-%d')})"
    req = ScriptRequest(
        topic=f"基于以下新闻做 5 分钟播客:\n\n{news_text}\n\n"
              f"选取 3-5 个最重磅的做深入解读。每条都要给数字和背景。",
        rounds=8,  # 8 轮 = 16 句 ≈ 5 分钟
    )

    writer = get_script_writer()
    script = await writer.generate(req)
    print(f"  ✓ 剧本: {script.title}, {len(script.lines)} 句, "
          f"{script.total_characters} 字符, 预计 {script.duration_estimate_sec:.0f}s")

    # 4. TTS 合成
    print(f"\n🔊 [3/4] TTS 合成 ({len(script.lines)} 句)...")
    from app.tts.synthesizer import get_synthesizer
    synth = get_synthesizer()

    episode_id = f"news_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = ROOT / "output" / episode_id / "tts_segments"
    out_dir.mkdir(parents=True, exist_ok=True)

    line_audios = await synth.synthesize_script(script, output_dir=out_dir)
    total_chars = sum(la.usage_characters for la in line_audios)
    print(f"  ✓ 合成: {len(line_audios)} 段, 消耗 {total_chars} 字符")

    # 5. 混音
    print(f"\n🎵 [4/4] ffmpeg 拼接...")
    from app.mixer.mixer import get_mixer
    mixer = get_mixer()

    ep_dir = ROOT / "output" / episode_id
    voice_only = ep_dir / "voice_only.mp3"
    final = ep_dir / "final.mp3"

    line_files = [la.file_path for la in line_audios]
    mix_result = await mixer.concat_with_silence(line_files, voice_only)
    voice_only.replace(final)
    final_size = final.stat().st_size

    print(f"\n{'='*60}")
    print(f"✅ 完成!")
    print(f"  最终 mp3:  {final}")
    print(f"  时长:      {mix_result.duration_sec:.1f}秒")
    print(f"  文件大小:  {final_size/1024:.1f} KB")
    print(f"{'='*60}\n")

    # 6. 发飞书
    print("📤 发送到飞书...")
    send_to_feishu(final, script, news)

    return final


def send_to_feishu(mp3_path: Path, script, news: list[dict]):
    """通过 hermes 原生通道发到飞书 - 用 MEDIA: 标记"""
    transcript_lines = [f"# {script.title}\n*{script.tagline}*\n"]
    for i, line in enumerate(script.lines):
        emoji = "🎙️" if line.role.value == "host" else "🎓"
        transcript_lines.append(f"\n{i+1}. {emoji} **{line.role.value.upper()}**: {line.text}")
    transcript = "\n".join(transcript_lines)

    news_summary = "\n".join(
        f"- [{n['title']}]({n['url']})" for n in news[:10]
    )

    message = f"""📰 **今日科技新闻播客** ({datetime.now().strftime('%Y-%m-%d %H:%M')})

🎧 **音频**: 5 分钟双角色解读
⏱️ **时长**: {mp3_path.stat().st_size / 128 / 1024 * 8:.0f} 秒
📝 **来源**: {len(news)} 条新闻精选

---

## 标题
**{script.title}** — {script.tagline}

## 逐字稿
{transcript}

---

## 参考新闻
{news_summary}

---

MEDIA:{mp3_path}
"""

    print(message[:500] + "\n... (截断)\n")
    print(f"\n完整消息含 MEDIA: 标记,hermes 会自动把 mp3 作为附件发送到当前飞书会话")


if __name__ == "__main__":
    asyncio.run(main())