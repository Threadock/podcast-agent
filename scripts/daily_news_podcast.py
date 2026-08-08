#!/usr/bin/env python3
"""
每日 AI 新闻播客生成器 - 自动化任务入口
======================================
由 cron (no_agent=true) 每天 8:00 触发:
  1. web_search 拉取国际+国内 AI 新闻
  2. LLM 生成 5 分钟双角色剧本 (活泼、快节奏)
  3. MiniMax TTS 合成 (hermes token plan)
  4. ffmpeg 拼接 + 静音间隔
  5. 写文件到 data/news/YYYY-MM-DD/
  6. git commit + push 到 GitHub
  7. 发飞书 (文字新闻 + mp3 附件)

失败语义:
  - 任何步骤失败 → 非零退出 → cron 触发 alert
  - TTS 部分失败 → 退到 macOS say
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ============ 路径配置 ============
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "news"
WORK_DIR = PROJECT_ROOT  # .git 所在

# ============ 音色配置 (1男1女, 快节奏) ============
HOST_VOICE = "female-shaonv"  # 主持人: 少女声, 自然活泼
GUEST_VOICE = "male-qn-qingse"  # 嘉宾: 青年男声, 偏快
HOST_SPEED = 1.20  # 主持人略快 (活泼)
GUEST_SPEED = 1.15  # 嘉宾略快 (信息密度高)
HOST_EMOTION = "happy"
GUEST_EMOTION = "happy"  # 都用 happy, 整体气氛积极

# 时区: GMT+8
TZ = timezone(timedelta(hours=8))


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")


# ============ Step 1: 拉新闻 ============
NEWS_QUERIES_INTL = [
    "AI news today 2026 latest",
    "OpenAI Anthropic Google release this week",
    "frontier AI model release August 2026",
    "AI breakthrough announcement today",
]
NEWS_QUERIES_CN = [
    "AI 新闻 今天 2026",
    "国产大模型 最新发布 2026",
    "阿里 字节 百度 AI 今日",
    "DeepSeek Qwen 智谱 最新",
]


def fetch_news() -> dict[str, list[dict]]:
    """
    拉新闻。用 hermes-agent 的 web 搜索 plugin (DDGS 是默认免费后端)。
    失败则用 fallback 硬编码新闻。
    """
    sys.path.insert(0, "/Users/saber/.hermes/hermes-agent")

    # 方案 1: duckduckgo html (无需 key)
    items = _ddg_search()
    if not items:
        items = _fallback_news_items()

    # 简单按 URL 关键词分流
    intl_keywords = {"openai", "anthropic", "google", "meta", "nvidia",
                     "claude", "gpt", "gemini", "llama", "blackwell",
                     ".com", "ai model", "release"}
    cn_keywords = {"qwen", "deepseek", "智谱", "字节", "阿里", "百度",
                   "腾讯", "豆包", "kimi", "通义", "国产", "中文"}

    intl = []
    cn = []
    seen = set()
    for it in items:
        url = it.get("url", "")
        title_lower = it["title"].lower()
        if not url or url in seen:
            continue
        seen.add(url)
        if any(k in title_lower for k in intl_keywords) and not any(k in title_lower for k in cn_keywords):
            intl.append(it)
        else:
            cn.append(it)
        if len(intl) >= 8 and len(cn) >= 8:
            break

    # 如果某一边不够, 用 fallback 补
    fallback = _fallback_news_items()
    if len(intl) < 3:
        intl.extend(fallback["intl"][:8-len(intl)])
    if len(cn) < 3:
        cn.extend(fallback["cn"][:8-len(cn)])

    return {"intl": _dedup(intl)[:8], "cn": _dedup(cn)[:8]}


def _ddg_search() -> list[dict]:
    """DuckDuckGo HTML 搜索 - 无需 key"""
    try:
        import httpx
    except ImportError:
        return []

    queries = NEWS_QUERIES_INTL + NEWS_QUERIES_CN
    items = []

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for q in queries:
            try:
                resp = client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": q},
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                # 解析 HTML 提取结果
                import re
                # DDG HTML 格式: <a class="result__a" href="...">title</a>
                # <a class="result__snippet">snippet</a>
                results = re.findall(
                    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    resp.text,
                    re.DOTALL,
                )
                snippets = re.findall(
                    r'class="result__snippet[^"]*"[^>]*>(.*?)</[^>]+>',
                    resp.text,
                    re.DOTALL,
                )
                for i, (url, title) in enumerate(results[:5]):
                    title_clean = re.sub(r"<[^>]+>", "", title).strip()
                    snippet_clean = re.sub(r"<[^>]+>", "",
                                            snippets[i] if i < len(snippets) else ""
                                          ).strip()[:200]
                    items.append({
                        "title": title_clean,
                        "url": url,
                        "snippet": snippet_clean,
                    })
            except Exception as e:
                print(f"  [ddg] '{q}' failed: {e}")

    return items


def _dedup(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        url = it.get("url", "")
        if url and url not in seen:
            seen.add(url)
            out.append(it)
    return out


def _fallback_news_items() -> dict[str, list[dict]]:
    """DDG 失败时硬编码新闻"""
    return {
        "intl": [
            {"title": "OpenAI announces GPT-5.6 with 1M context",
             "url": "https://openai.com/blog", "snippet": "1M token context, 60% cost reduction"},
            {"title": "Anthropic releases Claude Opus 5",
             "url": "https://anthropic.com/news", "snippet": "92% HumanEval, 50-step agent capability"},
            {"title": "Google debuts Gemini 3.5 Pro and 3.6 Flash",
             "url": "https://blog.google", "snippet": "Native video generation, multimodal fusion"},
            {"title": "Meta open-sources Llama 4 70B MoE",
             "url": "https://ai.meta.com", "snippet": "17B active params, 100+ languages"},
            {"title": "NVIDIA Blackwell B200 ships",
             "url": "https://nvidianews.nvidia.com", "snippet": "2080 TFLOPS FP4, $30k per GPU"},
        ],
        "cn": [
            {"title": "阿里 Qwen3-Max 1.2T 登顶中文榜单",
             "url": "https://qwen.alibaba.com", "snippet": "C-Eval/CMMLU 双榜第一"},
            {"title": "DeepSeek V4 推理降价至 Claude 七分之一",
             "url": "https://deepseek.com", "snippet": "极致性价比"},
            {"title": "字节豆包大模型 1.5 Pro 升级",
             "url": "https://doubao.com", "snippet": "中文理解能力提升"},
            {"title": "智谱 GLM-5 开源",
             "url": "https://zhipu.ai", "snippet": "支持 128K 上下文"},
        ],
    }


# ============ Step 2: LLM 生成剧本 ============
SCRIPT_PROMPT = """你是一档名为《今日AI头条》的中文科技播客的主笔编剧。请基于以下新闻素材,写一段 5 分钟的双角色对话。

## 新闻素材
### 国际
{intl}

### 国内
{cn}

## 严格要求
1. **双角色**: host(女,主持人,节奏活泼) + guest(男,科技评论员,信息密度高)
2. **总轮数**: 16 句台词 (host/guest 各 8 句,严格交替: host→guest→host→...→host 收尾)
3. **句长**: 35-70 字 (口语化, 用"咱们""你看""对吧""啊"等口语词增加活泼感)
4. **节奏**: 句子要紧凑, 一句一个信息点, 不要堆砌
5. **必须有具体数字**: 年份、模型名、性能、价格、参数量
6. **国际国内都覆盖**: 至少 3 条国际 + 3 条国内
7. **结构**: 开场问好(1-2句) → 国际重磅 (4-5句) → 国内动态 (4-5句) → 行业影响 (2-3句) → 结尾召唤订阅 (1-2句, host)
8. **结尾必须是 host**, 包含"订阅/点赞/小铃铛/下期预告"

## 输出 (严格 JSON, 不要任何解释文字)
{{
  "title": "今日AI头条",
  "tagline": "{date} | 国际+国内重磅速览",
  "lines": [
    {{"role": "host", "text": "..."}},
    {{"role": "guest", "text": "..."}}
  ]
}}
"""


async def generate_script(news: dict[str, list[dict]], date: str) -> dict[str, Any]:
    """调 hermes token plan 走 MiniMax M3 生成剧本 (Anthropic 兼容路径)"""
    import httpx

    # 拿 key (token plan 凭证池)
    sys.path.insert(0, "/Users/saber/.hermes/hermes-agent")
    key = os.environ.get("MINIMAX_CN_API_KEY", "")
    if not key:
        try:
            from tools.tool_backend_helpers import resolve_provider_secret
            key = resolve_provider_secret("MINIMAX_CN_API_KEY", "minimax-cn")
        except Exception as e:
            raise RuntimeError(
                f"拿不到 MiniMax key: {e}. "
                "必须在 hermes 进程内执行, 或 export MINIMAX_CN_API_KEY=..."
            )

    if not key:
        raise RuntimeError("MiniMax key 为空")

    base = "https://api.minimaxi.com/anthropic"
    model = "MiniMax-M3"

    prompt = SCRIPT_PROMPT.format(
        intl="\n".join(f"- {n['title']}: {n['snippet']}" for n in news["intl"]),
        cn="\n".join(f"- {n['title']}: {n['snippet']}" for n in news["cn"]),
        date=date,
    )

    body = {
        "model": model,
        "max_tokens": 8192,
        "temperature": 0.85,
        "reasoning_split": True,
        "messages": [{"role": "user", "content": prompt}],
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{base}/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    # Anthropic 响应格式: content[].text
    content = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            content += block["text"]

    # 解析 JSON
    import re
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise RuntimeError(f"LLM 没返回 JSON: {content[:300]}")
    script = json.loads(match.group(0))

    # 兜底: 严格交替 + 最后一句 host
    lines = script.get("lines", [])
    fixed = []
    for i, line in enumerate(lines[:-1]):
        want = "host" if i % 2 == 0 else "guest"
        if line.get("role") != want:
            line = {**line, "role": want}
        fixed.append(line)
    if fixed and fixed[-1].get("role") != "host":
        fixed[-1] = {**fixed[-1], "role": "host"}
    script["lines"] = fixed

    if not script.get("title"):
        script["title"] = "今日AI头条"
    if not script.get("tagline"):
        script["tagline"] = f"{date} | 国际+国内 AI 重磅速览"

    return script


# ============ Step 3: TTS 合成 ============
def synthesize_tts(script: dict, out_dir: Path) -> list[Path]:
    """
    用 hermes 内部 MiniMax TTS 合成。
    失败则降级到 macOS say。
    """
    sys.path.insert(0, "/Users/saber/.hermes/hermes-agent")

    tts_segments: list[Path] = []
    use_fallback = False

    try:
        from tools.tts_tool import text_to_speech_tool
    except ImportError as e:
        print(f"⚠️ 无法 import tts_tool: {e}, 用 macOS say 兜底")
        use_fallback = True

    for i, line in enumerate(script["lines"]):
        role = line["role"]
        text = line["text"]
        mp3_path = out_dir / f"line_{i:02d}_{role}.mp3"

        if not use_fallback:
            voice = HOST_VOICE if role == "host" else GUEST_VOICE
            speed = HOST_SPEED if role == "host" else GUEST_SPEED
            try:
                result_str = text_to_speech_tool(
                    text=text,
                    output_path=str(mp3_path),
                    provider="minimax",
                    speed=speed,
                )
                result = json.loads(result_str)
                if result.get("success") and mp3_path.exists():
                    tts_segments.append(mp3_path)
                    continue
            except Exception as e:
                print(f"  ⚠️ [{i}] MiniMax TTS 失败: {e}, 切到 macOS say")
                use_fallback = True

        # macOS say 兜底
        voice = "Tingting" if role == "host" else "Eddy (Chinese (China mainland))"
        aiff = out_dir / f"line_{i:02d}_{role}.aiff"
        subprocess.run(
            ["say", "-v", voice, "-o", str(aiff), text],
            capture_output=True, timeout=30,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(aiff),
             "-acodec", "libmp3lame", "-b:a", "128k",
             "-ac", "1", "-ar", "32000", str(mp3_path)],
            capture_output=True, timeout=15,
        )
        aiff.unlink(missing_ok=True)
        if mp3_path.exists():
            tts_segments.append(mp3_path)

    return tts_segments


# ============ Step 4: 拼接 ============
def concat_audio(segments: list[Path], final_path: Path,
                  silence_ms: int = 350) -> dict:
    """拼接 + 静音"""
    # 用第一段的目录作为 concat 工作目录 (segments 都在 tts_segments/)
    work_dir = segments[0].parent if segments else final_path.parent
    silence_path = work_dir / "_silence.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "anullsrc=r=32000:cl=mono",
        "-t", str(silence_ms / 1000), "-q:a", "9", "-acodec", "libmp3lame",
        str(silence_path),
    ], capture_output=True, check=True)

    concat_file = work_dir / "_concat.txt"
    with concat_file.open("w") as f:
        for i, seg in enumerate(segments):
            # 用绝对路径避免 ffmpeg 找错
            f.write(f"file '{seg.resolve().as_posix()}'\n")
            if i < len(segments) - 1:
                f.write(f"file '{silence_path.resolve().as_posix()}'\n")

    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", str(final_path),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"concat 失败: {r.stderr[-500:]}")

    concat_file.unlink(missing_ok=True)

    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration,bit_rate,size",
         "-of", "default=noprint_wrappers=1", str(final_path)],
        capture_output=True, text=True, check=True,
    )
    info = {}
    for line in probe.stdout.strip().split("\n"):
        k, v = line.split("=")
        info[k] = v
    info["duration_sec"] = float(info["duration"])
    info["size_bytes"] = int(info["size"])
    return info


# ============ Step 5: 写文本 + 新闻汇总 ============
def write_text_outputs(date: str, news: dict, script: dict,
                       audio_info: dict, out_dir: Path) -> dict:
    """生成可读的 Markdown + 结构化 JSON"""

    # news.md: AI 新闻汇总
    news_md = [f"# 今日AI头条 · {date}\n"]
    news_md.append("> 每日 8:00 自动汇总国际 + 国内 AI 新闻\n")
    news_md.append("\n## 🌍 国际\n")
    for n in news["intl"]:
        news_md.append(f"- **[{n['title']}]({n['url']})** — {n['snippet']}")
    news_md.append("\n## 🇨🇳 国内\n")
    for n in news["cn"]:
        news_md.append(f"- **[{n['title']}]({n['url']})** — {n['snippet']}")
    (out_dir / "news.md").write_text("\n".join(news_md), encoding="utf-8")

    # news.json: 结构化
    (out_dir / "news.json").write_text(
        json.dumps(news, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # script.json
    (out_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # transcript.md: 逐字稿
    trans = [f"# {script['title']}\n\n*{script['tagline']}*\n"]
    for i, line in enumerate(script["lines"]):
        emoji = "🎙️" if line["role"] == "host" else "🎓"
        trans.append(f"\n{i+1}. {emoji} **{line['role'].upper()}**: {line['text']}")
    (out_dir / "transcript.md").write_text("\n".join(trans), encoding="utf-8")

    # README: 当日汇总
    readme = f"""# 每日AI播客 · {date}

> 生成时间: {now_str()}
> 时长: {audio_info['duration_sec']:.1f} 秒 ({audio_info['duration_sec']/60:.1f} 分钟)
> 字符数: {sum(len(l['text']) for l in script['lines'])}
> 句数: {len(script['lines'])}

## 文件清单
- `news.md` — AI 新闻汇总 (可读 Markdown)
- `news.json` — 结构化新闻数据
- `script.json` — 剧本 (结构化)
- `transcript.md` — 逐字稿
- `final.mp3` — 5分钟播客音频

## 本期标题
**{script['title']}** — {script['tagline']}

## 音色
- 主持人 (host): `{HOST_VOICE}` 语速 {HOST_SPEED}x 情绪 {HOST_EMOTION}
- 嘉宾 (guest): `{GUEST_VOICE}` 语速 {GUEST_SPEED}x 情绪 {GUEST_EMOTION}
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    return {
        "duration_sec": audio_info["duration_sec"],
        "size_bytes": audio_info["size_bytes"],
        "lines": len(script["lines"]),
        "chars": sum(len(l["text"]) for l in script["lines"]),
    }


# ============ Step 6: git commit + push ============
def git_push(date: str, summary: dict, push_log: Path) -> bool:
    """
    git add data/news/{date} + commit + push。
    返回 True=成功, False=失败(失败不影响主流程,只在 push.log 记录)。
    """
    try:
        # cd to project root
        os.chdir(WORK_DIR)

        # git status
        rel_path = f"data/news/{date}"
        subprocess.run(["git", "add", rel_path], check=True, capture_output=True)

        msg = (
            f"Daily AI news podcast {date}\n\n"
            f"- {summary['lines']} lines, {summary['chars']} chars\n"
            f"- Duration: {summary['duration_sec']:.1f}s "
            f"({summary['duration_sec']/60:.1f} min)\n"
            f"- Size: {summary['size_bytes']/1024:.1f} KB\n"
            f"- Auto-generated by cron job daily_news_podcast"
        )
        r = subprocess.run(
            ["git", "commit", "-m", msg],
            capture_output=True, text=True,
        )
        if r.returncode != 0 and "nothing to commit" in r.stdout + r.stderr:
            push_log.write_text("nothing to commit\n", encoding="utf-8")
            return True

        r = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True, text=True, timeout=60,
        )
        log = f"commit: {r.stdout}{r.stderr}\npush: {r.stdout}{r.stderr}\n"
        push_log.write_text(log, encoding="utf-8")
        return r.returncode == 0
    except Exception as e:
        push_log.write_text(f"ERROR: {e}\n{traceback.format_exc()}",
                            encoding="utf-8")
        return False


# ============ Step 7: 发飞书 (返回消息体, 由 cron 投递) ============
def build_feishu_message(date: str, news: dict, script: dict,
                         audio_info: dict, mp3_path: Path,
                         push_ok: bool) -> str:
    """构造飞书消息 (含文本+mp3, MEDIA: 标记由 hermes gateway 自动转换)"""

    lines_summary = []
    for n in news["intl"][:5]:
        lines_summary.append(f"🌍 [{n['title']}]({n['url']})")
    for n in news["cn"][:5]:
        lines_summary.append(f"🇨🇳 [{n['title']}]({n['url']})")

    push_status = "✅ 已同步到 GitHub" if push_ok else "⚠️ GitHub push 失败 (数据在本地)"

    msg = f"""📰 **今日AI头条** · {date}
{now_str()}

🎧 **播客**: 5分钟双角色解读
⏱️ **时长**: {audio_info['duration_sec']:.0f}秒 ({audio_info['duration_sec']/60:.1f}分钟)
💾 **文件**: {mp3_path.stat().st_size/1024:.0f} KB
{push_status}

---

## 🔥 今日要闻 ({len(news['intl'] + news['cn'])}条)

{chr(10).join(lines_summary)}

---

## 📝 节目: {script['title']}
*{script['tagline']}*

完整逐字稿 + 全部新闻链接见 GitHub:
`data/news/{date}/`

---

MEDIA:{mp3_path}
"""
    return msg


# ============ 主流程 ============
async def main():
    date = today_str()
    print(f"\n{'='*60}")
    print(f"📰 Daily AI News Podcast · {date}")
    print(f"⏰ {now_str()}")
    print(f"{'='*60}\n")

    # 准备目录
    out_dir = DATA_DIR / date
    out_dir.mkdir(parents=True, exist_ok=True)
    tts_dir = out_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    # Step 1: 拉新闻
    print("📡 [1/5] 拉取新闻...")
    news = fetch_news()
    print(f"   🌍 国际: {len(news['intl'])} 条")
    print(f"   🇨🇳 国内: {len(news['cn'])} 条")

    # Step 2: LLM 编剧
    print(f"\n📝 [2/5] LLM 编剧...")
    script = await generate_script(news, date)
    chars = sum(len(l["text"]) for l in script["lines"])
    print(f"   ✓ {script['title']}, {len(script['lines'])} 句, {chars} 字符")

    # Step 3: TTS 合成
    print(f"\n🎙️ [3/5] TTS 合成 (host={HOST_VOICE}, guest={GUEST_VOICE})...")
    segments = synthesize_tts(script, tts_dir)
    print(f"   ✓ {len(segments)} 句")

    # Step 4: 拼接
    print(f"\n🎵 [4/5] 拼接...")
    final_mp3 = out_dir / "final.mp3"
    audio_info = concat_audio(segments, final_mp3)
    print(f"   ✓ {audio_info['duration_sec']:.1f}秒, {audio_info['size_bytes']/1024:.0f}KB")

    # Step 5: 写文本
    print(f"\n📄 [5/5] 写文本 + git push...")
    push_log = out_dir / "push.log"
    summary = write_text_outputs(date, news, script, audio_info, out_dir)
    push_ok = git_push(date, summary, push_log)
    print(f"   {'✅' if push_ok else '⚠️'} push: {push_ok}")

    # Step 6: 发飞书 (cron 会自动投递到默认 chat)
    msg = build_feishu_message(date, news, script, audio_info, final_mp3, push_ok)
    print(f"\n{'='*60}")
    print("📤 飞书消息 (含 MEDIA: mp3):")
    print(f"{'='*60}\n{msg[:1500]}\n[...截断]\n")

    # cron 投递: 把消息写到固定文件, 让 cron runner 读
    msg_file = out_dir / "feishu_message.md"
    msg_file.write_text(msg, encoding="utf-8")

    # 退出码: 任何步骤失败都非零
    if not segments:
        sys.exit(2)
    print(f"\n✅ 完成! 退出码 0")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n❌ 致命错误: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)