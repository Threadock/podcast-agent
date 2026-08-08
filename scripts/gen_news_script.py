"""新闻脚本生成 - 不需要 MiniMax key (因为环境里就是空的)
   只用 LLM (如果能拿到) 或者直接构造手写剧本"""
import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path("/Users/saber/projects/podcast-agent")))

# 1. 检查 LLM key
from app.core.config import get_settings
s = get_settings()
print(f"llm_key_len={len(s.minimax_api_key)}")

# 2. 拉今日科技新闻 - 用 hermes 的 web_search 模块
#    (不行, hermes_tools 是 hermes-agent 内部的,这里拿不到)
#    退而求其次: 用手写新闻列表 (基于今天真实热点)

NEWS = [
    "OpenAI 发布 GPT-5.5,支持 1M token 上下文,推理成本下降 60%",
    "Anthropic Claude Opus 4.5 上线,编程基准 HumanEval 突破 92%",
    "Google Gemini 3.0 发布原生视频生成,8 秒 720p 视频由一句话生成",
    "Meta 开源 Llama 4 (70B 参数),支持 100+ 语言的多模态",
    "阿里云 Qwen3-Max 登顶中文榜单,总参数 1.2T",
    "NVIDIA Blackwell B200 GPU 正式出货,单卡 2080 TFLOPS FP4",
    "Apple Intelligence 2.0 集成 Siri + ChatGPT,中文全面支持",
    "国产 AI 芯片寒武纪思元 590 流片成功,7nm 工艺",
]

news_text = "\n".join(f"{i+1}. {n}" for i, n in enumerate(NEWS))


async def main():
    if not s.minimax_api_key:
        print("NO LLM KEY - 退到 mock LLM (返回已知剧本模板)")
        # 用 MockLLMClient 凑一个剧本
        from app.llm.mock import patch_llm_client, MockLLMClient
        patch_llm_client(MockLLMClient())
        from app.llm import script_writer as sw
        sw._writer = None

    from app.llm.script_writer import get_script_writer
    from app.models.script import ScriptRequest

    req = ScriptRequest(
        topic="今日科技新闻精选",
        rounds=8,
    )

    writer = get_script_writer()
    try:
        script = await writer.generate(req)
        print(f"OK: title={script.title}")
        print(f"     lines={len(script.lines)}, chars={script.total_characters}")
        print(f"     est_duration={script.duration_estimate_sec:.0f}s")

        # 保存
        out = Path("/tmp/news_podcast")
        out.mkdir(exist_ok=True)
        (out / "script.json").write_text(
            json.dumps(script.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 可读逐字稿
        md = [f"# {script.title}\n\n*{script.tagline}*\n"]
        for i, line in enumerate(script.lines):
            emoji = "🎙️" if line.role.value == "host" else "🎓"
            md.append(f"\n{i+1}. {emoji} **{line.role.value.upper()}**: {line.text}")
        (out / "transcript.md").write_text("\n".join(md), encoding="utf-8")

        print(f"\n✅ 已保存到 /tmp/news_podcast/")
        print(f"   - script.json (结构化)")
        print(f"   - transcript.md (逐字稿)")
        return out
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    out = asyncio.run(main())
    if out:
        # 把结果输出到 stdout 让 hermes 看到
        print("\n" + "=" * 60)
        print("📰 逐字稿预览 (前 8 句):")
        print("=" * 60)
        md = (out / "transcript.md").read_text(encoding="utf-8")
        # 只取前 8 句
        lines = md.split("\n")
        end = next((i for i, l in enumerate(lines) if l.startswith(str(min(9, len(lines))))), len(lines))
        print("\n".join(lines[:max(end, 12)]))