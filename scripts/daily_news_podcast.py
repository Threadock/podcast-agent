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


def date_str_to_num(date_str: str) -> int:
    """YYYY-MM-DD -> 整数 hash (用于固定 BGM 映射)"""
    parts = date_str.split("-")
    return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")


# ============ 步骤 1: 拉新闻 (多源 + 24h 过滤 + 去重) ============
# 国内 RSS 源 (4个权威)
RSS_FEEDS_CN = [
    ("量子位", "https://www.qbitai.com/feed"),
    ("智东西", "https://zhidx.com/feed"),
    ("36氪", "https://36kr.com/feed"),
    ("机器之心", "https://www.jiqizhixin.com/rss"),
]
# 国外源: RSS + JSON API
RSS_FEEDS_INTL = [
    ("TechCrunch-AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("TheVerge-AI", "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
    ("ArsTechnica-AI", "https://feeds.arstechnica.com/arstechnica/ai"),
    ("OpenAI-Blog", "https://openai.com/blog/rss.xml"),
    ("Anthropic-News", "https://www.anthropic.com/news/rss.xml"),
    ("DeepMind-Blog", "https://deepmind.google/blog/rss/basic.xml"),
]
# JSON API (无需 RSS 解析, 时间戳原生)
JSON_API_INTL = [
    ("HackerNews", "https://hn.algolia.com/api/v1/search?tags=story&query=AI+OR+LLM+OR+GPT&numericFilters=created_at_i%3E{ts}"),
]


def fetch_news() -> dict[str, list[dict]]:
    """
    多源拉新闻, 24小时窗口过滤, 标题相似度去重。
    返回 {"intl": [...], "cn": [...], "all_sources": [...]}.
    """
    import httpx
    import feedparser  # type: ignore

    import re
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    cutoff_ts = int((now - timedelta(hours=24)).timestamp())  # 24h 窗口

    intl_items: list[dict] = []
    cn_items: list[dict] = []
    source_stats: dict[str, int] = {}

    # ============ 国际 RSS ============
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for name, url in RSS_FEEDS_INTL:
            try:
                resp = client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries[:8]:
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                        if pub_dt < now - timedelta(hours=24):
                            continue
                    intl_items.append({
                        "source": name,
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                    })
                    count += 1
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [intl] {name}: {e}")

        # ============ Hacker News Algolia API ============
        for name, url_template in JSON_API_INTL:
            url = url_template.format(ts=cutoff_ts)
            try:
                resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                data = resp.json()
                count = 0
                for hit in data.get("hits", [])[:8]:
                    intl_items.append({
                        "source": name,
                        "title": hit.get("title") or hit.get("story_title", ""),
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                        "snippet": (hit.get("story_text") or "")[:200],
                        "published": hit.get("created_at", ""),
                    })
                    count += 1
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [intl] {name}: {e}")

        # ============ Reddit JSON (只取 r/MachineLearning top 24h) ============
        try:
            resp = client.get(
                "https://www.reddit.com/r/MachineLearning/top.json?t=day&limit=8",
                headers={"User-Agent": "Mozilla/5.0 podcast-bot/1.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                count = 0
                for child in data.get("data", {}).get("children", []):
                    d = child.get("data", {})
                    intl_items.append({
                        "source": "Reddit-ML",
                        "title": d.get("title", ""),
                        "url": f"https://reddit.com{d.get('permalink', '')}",
                        "snippet": (d.get("selftext", "") or "")[:200],
                        "published": datetime.fromtimestamp(
                            d.get("created_utc", 0), tz=timezone.utc
                        ).isoformat(),
                    })
                    count += 1
                source_stats["Reddit-ML"] = count
        except Exception as e:
            print(f"  ⚠️ [intl] Reddit-ML: {e}")

        # ============ 国内 RSS ============
        for name, url in RSS_FEEDS_CN:
            try:
                resp = client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries[:8]:
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                        if pub_dt < now - timedelta(hours=24):
                            continue
                    cn_items.append({
                        "source": name,
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                    })
                    count += 1
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [cn] {name}: {e}")

    # ============ 去重 ============
    intl_before = len(intl_items)
    cn_before = len(cn_items)
    intl_items = _dedup_news(intl_items)
    cn_items = _dedup_news(cn_items)

    # ============ Fallback: 如果某一边空, 用硬编码 ============
    if len(intl_items) < 3 or len(cn_items) < 3:
        fallback = _fallback_news_items()
        if len(intl_items) < 3:
            intl_items.extend(fallback["intl"][:5-len(intl_items)])
        if len(cn_items) < 3:
            cn_items.extend(fallback["cn"][:5-len(cn_items)])

    # 按时间排序 (最新的在前)
    intl_items.sort(key=lambda x: x.get("published", ""), reverse=True)
    cn_items.sort(key=lambda x: x.get("published", ""), reverse=True)

    return {
        "intl": intl_items[:15],
        "cn": cn_items[:15],
        "source_stats": source_stats,
        "dedup": {
            "intl": {"before": intl_before, "after": len(intl_items)},
            "cn": {"before": cn_before, "after": len(cn_items)},
        },
    }


def _dedup_news(items: list[dict]) -> list[dict]:
    """
    标题相似度去重 + URL 去重。
    算法: 提取标题核心关键词 (中文 2-gram + 英文 word), Jaccard > 0.5 视为重复。
    """
    import re
    seen_urls = set()
    seen_keywords: list[set] = []

    result = []
    for it in items:
        url = it.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)

        kw = _extract_keywords(it.get("title", ""))
        if any(_jaccard(kw, k) > 0.5 for k in seen_keywords):
            continue
        seen_keywords.append(kw)

        result.append(it)
    return result


def _extract_keywords(title: str) -> set[str]:
    """中文2-gram + 英文word 提取核心词"""
    import re
    # 去标点
    title = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", title)
    # 中文 2-gram
    cn_grams = set()
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", title):
        word = m.group()
        if len(word) >= 4:
            for i in range(len(word) - 1):
                cn_grams.add(word[i:i+2])
    # 英文 word
    en_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{3,}", title))
    # 过滤停用词
    stop = {"the", "and", "for", "with", "from", "this", "that", "you", "your",
            "are", "will", "have", "has", "but", "not", "all", "can", "its"}
    en_words -= stop
    return cn_grams | en_words


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


# ============ Step 2: LLM 生成剧本 (选最重磅 4-6 条, 20 句 ≈ 5-6 分钟) ============
SCRIPT_PROMPT = """你是一档名为《今日AI头条》的中文科技播客的主笔编剧。

## 新闻素材 (按时间倒序, 24小时内)
### 🌍 国际 ({n_intl} 条)
{intl}

### 🇨🇳 国内 ({n_cn} 条)
{cn}

## 任务
1. **精选最重磅的 4-6 条** (国际+国内各 2-3 条) 做深度解读
2. 选材标准: 涉及顶级模型/产品发布、重磅融资、行业变革
3. 剩余新闻在第 17-19 句快速提及 (一句话带过)

## 双角色
- **host** (女, 主持人, 节奏活泼) — 开场、追问、过渡、收尾
- **guest** (男, 科技评论员) — 解读事实、给数字、举例子

## 严格要求
1. **总轮数**: 20 句 (10 轮对话, host/guest 各 10 句)
2. **严格交替**: host→guest→host→...→host 收尾
3. **句长**: 35-70 字 (口语化, 用"咱们/你看/啊/对吧/其实/不过"等口语词)
4. **节奏**: 紧凑, 一句一个信息点
5. **必须有具体数字**: 年份、模型名、性能、价格、参数量、用户数
6. **国际+国内都覆盖**: 至少 2 条国际 + 2 条国内
7. **结尾必须是 host**, 包含"订阅/点赞/小铃铛/下期预告"

## 结构模板
- 句1-2: 开场寒暄 (host 问, guest 应)
- 句3-6: 国际重磅 1-2 条 (host 问, guest 详解)
- 句7-10: 国内动态 1-2 条 (host 问, guest 详解)
- 句11-13: 行业影响/对比 (host 总结, guest 回应)
- 句14-16: 其他要闻速览 (2-3 条, 各一句话)
- 句17-19: 后续展望 (host+guest)
- 句20: 结尾订阅 CTA (host)

## 输出 (严格 JSON, 不要任何解释文字)
{{
  "title": "今日AI头条",
  "tagline": "{date} | 国际+国内重磅速览",
  "selected_news": [
    {{"source": "...", "title": "...", "url": "...", "weight": "deep"}},
    ...
  ],
  "lines": [
    {{"role": "host", "text": "..."}},
    {{"role": "guest", "text": "..."}},
    ...
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
        n_intl=len(news["intl"]),
        n_cn=len(news["cn"]),
        intl="\n".join(f"- [{n.get('source', '?')}] {n['title']}: {n['snippet']}" for n in news["intl"]),
        cn="\n".join(f"- [{n.get('source', '?')}] {n['title']}: {n['snippet']}" for n in news["cn"]),
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

    # 解析 JSON (优先用 json_repair, 容错强)
    import re
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise RuntimeError(f"LLM 没返回 JSON: {content[:300]}")
    json_str = match.group(0)

    try:
        from json_repair import repair_json
        repaired = repair_json(json_str, return_objects=True)
        script = repaired if isinstance(repaired, dict) else json.loads(json_str)
    except ImportError:
        try:
            script = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON 解析失败: {e}\n{json_str[:500]}")

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
                       audio_info: dict, out_dir: Path,
                       bgm_used: str | None = None) -> dict:
    """生成可读的 Markdown + 结构化 JSON"""

    # news.md: AI 新闻汇总
    news_md = [f"# 今日AI头条 · {date}\n"]
    news_md.append("> 每日 8:00 自动汇总国际 + 国内 AI 新闻 (24h 内)\n")
    if news.get("source_stats"):
        news_md.append("\n**抓取统计**: " + ", ".join(
            f"{k}={v}" for k, v in news["source_stats"].items() if v > 0
        ))
    if news.get("dedup"):
        d = news["dedup"]
        news_md.append(f"\n**去重**: 国际 {d['intl']['before']}→{d['intl']['after']}, "
                       f"国内 {d['cn']['before']}→{d['cn']['after']}")
    news_md.append("\n## 🌍 国际\n")
    for n in news["intl"]:
        src = n.get("source", "")
        news_md.append(f"- **[{n['title']}]({n['url']})** _{src}_ — {n['snippet']}")
    news_md.append("\n## 🇨🇳 国内\n")
    for n in news["cn"]:
        src = n.get("source", "")
        news_md.append(f"- **[{n['title']}]({n['url']})** _{src}_ — {n['snippet']}")
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
    bgm_line = f"\n> 🎵 BGM: {bgm_used}" if bgm_used else ""
    readme = f"""# 每日AI播客 · {date}

> 生成时间: {now_str()}
> 时长: {audio_info['duration_sec']:.1f} 秒 ({audio_info['duration_sec']/60:.1f} 分钟)
> 字符数: {sum(len(l['text']) for l in script['lines'])}
> 句数: {len(script['lines'])}
> 抓取: {len(news.get('intl', []))} 国际 + {len(news.get('cn', []))} 国内{bgm_line}

## 文件清单
- `news.md` — AI 新闻汇总 (可读 Markdown)
- `news.json` — 结构化新闻数据
- `script.json` — 剧本 (结构化)
- `transcript.md` — 逐字稿
- `voice_only.mp3` — 纯人声版
- `final.mp3` — 含 BGM 的最终版本

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


# ============ Step 4.5: BGM 模块 ============
BGM_DIR = PROJECT_ROOT / "assets" / "bgm"

# 4 段程序化合成的 BGM 配置 (用 ffmpeg 生成, 永久缓存)
BGM_PRESETS = [
    {
        "name": "lofi_chill_60bpm",
        "description": "Lo-Fi chill 60 BPM - 温暖的钢琴循环 + lo-fi 鼓点",
        "filter_complex": (
            # 暖色钢琴: sine 220Hz 弹旋律, 慢节奏
            "[0:a]volume=0.3[beat];"
            # 慢节奏 bass
            "sine=frequency=80:duration={dur}[bass];"
            "[bass]volume=0.4[bs];"
            # 白噪声当 vinyl hiss
            "anoisesrc=color=brown:duration={dur}:amplitude=0.04[noise];"
            "[noise]highpass=f=200[hp];"
            # 合成
            "[beat][bs][hp]amix=inputs=3:normalize=0[mixed];"
            # 高通 + 一点点混响
            "[mixed]highpass=f=80,lowpass=f=8000,aecho=0.8:0.9:1000:0.3[out]"
        ),
        "duration": 300,
    },
    {
        "name": "ambient_pad_70bpm",
        "description": "Ambient pad 70 BPM - drone + 慢琶音",
        "filter_complex": (
            # Pad: 两个 sine 叠加形成 drone
            "sine=frequency=174:duration={dur}[s1];"
            "sine=frequency=261:duration={dur}[s2];"
            "[s1][s2]amix=inputs=2:normalize=0[pad];"
            "[pad]volume=0.25,lowpass=f=2000[pad2];"
            # 慢琶音 (用 tremolo 模拟)
            "sine=frequency=440:duration={dur}[arp];"
            "[arp]volume=0.15,tremolo=f=0.5:d=0.7[arp2];"
            "[pad2][arp2]amix=inputs=2:normalize=0[m];"
            "[m]aecho=0.7:0.8:1500:0.4,lowpass=f=6000[out]"
        ),
        "duration": 300,
    },
    {
        "name": "soft_electronic_90bpm",
        "description": "Soft electronic 90 BPM - 轻快 synth",
        "filter_complex": (
            # synth bass
            "sine=frequency=110:duration={dur}[b];"
            "[b]volume=0.35[bs];"
            # 轻快 kick 节奏
            "sine=frequency=60:duration={dur}:sample_rate=32000[k];"
            "[k]volume=0.5,tremolo=f=2:d=0.9[k2];"
            # synth lead
            "sine=frequency=523:duration={dur}[l];"
            "[l]volume=0.15,tremolo=f=4:d=0.5[l2];"
            "[bs][k2][l2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=10000[out]"
        ),
        "duration": 300,
    },
    {
        "name": "coffee_shop_jazz",
        "description": "Coffee shop jazz - 慵懒的吉他/钢琴感",
        "filter_complex": (
            # 慢琶音 bass
            "sine=frequency=98:duration={dur}[b];"
            "[b]volume=0.3,tremolo=f=3:d=0.6[bs];"
            # 中频 sine 模拟钢琴
            "sine=frequency=392:duration={dur}[p];"
            "[p]volume=0.15,tremolo=f=1.5:d=0.4[p2];"
            # 高频 sparkle
            "sine=frequency=1318:duration={dur}[sp];"
            "[sp]volume=0.06[sp2];"
            "[bs][p2][sp2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=12000,aecho=0.8:0.9:500:0.2[out]"
        ),
        "duration": 300,
    },
]


def ensure_bgm() -> dict:
    """
    确保 4 段 BGM 存在, 缺失则程序化生成。
    返回 {"files": [Path, ...], "descriptions": [...]}.
    """
    BGM_DIR.mkdir(parents=True, exist_ok=True)

    files = []
    descriptions = []
    for preset in BGM_PRESETS:
        out = BGM_DIR / f"{preset['name']}.mp3"
        if not out.exists():
            print(f"  🎵 生成 BGM: {preset['name']} ({preset['description']})")
            dur = preset["duration"]
            fc = preset["filter_complex"].format(dur=dur)
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r=32000:cl=mono",
                "-filter_complex", fc,
                "-map", "[out]",
                "-t", str(dur),
                "-acodec", "libmp3lame", "-b:a", "96k",
                "-ar", "32000", "-ac", "1",
                str(out),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ⚠️ 生成 {preset['name']} 失败: {r.stderr[-200:]}")
                continue
        files.append(out)
        descriptions.append(f"{preset['name']}: {preset['description']}")

    return {"files": files, "descriptions": descriptions}


def mix_with_bgm(voice_path: Path, bgm_path: Path, output_path: Path,
                  bgm_volume_db: float = -22.0) -> dict:
    """
    人声 + BGM 混音。 BGM 自动循环到人声长度, 降低音量 (-22dB = 背景)。
    返回 {duration_sec, size_bytes}.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        f"[1:a]volume={bgm_volume_db}dB[bgm];"
        f"[0:a]volume=2dB[voice];"
        f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[m]",
        "-map", "[m]",
        "-ac", "1", "-ar", "32000",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(output_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"BGM 混音失败: {r.stderr[-500:]}")

    probe = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration,bit_rate,size",
         "-of", "default=noprint_wrappers=1", str(output_path)],
        capture_output=True, text=True, check=True,
    )
    info = {}
    for line in probe.stdout.strip().split("\n"):
        k, v = line.split("=")
        info[k] = v
    info["duration_sec"] = float(info["duration"])
    info["size_bytes"] = int(info["size"])
    return info


# ============ Step 7: 发飞书 (返回消息体, 由 cron 投递) ============
def build_feishu_message(date: str, news: dict, script: dict,
                         audio_info: dict, mp3_path: Path,
                         push_ok: bool, bgm_used: str | None) -> str:
    """构造飞书消息 (含文本+mp3, MEDIA: 标记由 hermes gateway 自动转换)"""

    lines_summary = []
    for n in news["intl"][:5]:
        lines_summary.append(f"🌍 [{n['title']}]({n['url']})")
    for n in news["cn"][:5]:
        lines_summary.append(f"🇨🇳 [{n['title']}]({n['url']})")

    push_status = "✅ 已同步到 GitHub" if push_ok else "⚠️ GitHub push 失败 (数据在本地)"
    bgm_info = f"\n🎵 **BGM**: {bgm_used}" if bgm_used else ""

    msg = f"""📰 **今日AI头条** · {date}
{now_str()}

🎧 **播客**: 5-6分钟双角色解读{bgm_info}
⏱️ **时长**: {audio_info['duration_sec']:.0f}秒 ({audio_info['duration_sec']/60:.1f}分钟)
💾 **文件**: {mp3_path.stat().st_size/1024:.0f} KB
{push_status}

---

## 🔥 今日要闻 ({len(news['intl']) + len(news['cn'])}条)

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
    print(f"\n🎵 [4/6] 拼接...")
    final_mp3 = out_dir / "final.mp3"
    audio_info = concat_audio(segments, final_mp3)
    print(f"   ✓ {audio_info['duration_sec']:.1f}秒, {audio_info['size_bytes']/1024:.0f}KB")

    # Step 5: BGM 混音
    print(f"\n🎵 [5/6] BGM 混音...")
    bgm_info = ensure_bgm()
    # 根据日期选 BGM (固定映射, 让每天的 BGM 一致)
    bgm_index = int(date_str_to_num(date)) % len(bgm_info["files"]) if bgm_info["files"] else 0
    bgm_path = bgm_info["files"][bgm_index] if bgm_info["files"] else None
    bgm_used_name = None

    final_with_bgm = out_dir / "final.mp3"
    if bgm_path and bgm_path.exists():
        # 先把人声拼到 voice_only, 再混 BGM
        voice_only = out_dir / "voice_only.mp3"
        concat_audio(segments, voice_only)
        try:
            bgm_used_name = bgm_path.stem
            final_with_bgm = out_dir / "final.mp3"
            audio_info = mix_with_bgm(voice_only, bgm_path, final_with_bgm)
            print(f"   ✓ BGM: {bgm_used_name}, 最终 {audio_info['duration_sec']:.1f}秒")
        except Exception as e:
            print(f"   ⚠️ BGM 混音失败: {e}, 用纯人声版")
            voice_only.replace(final_with_bgm)
    else:
        print("   ⚠️ 没有可用 BGM, 跳过")

    # Step 6: 写文本 + git push
    print(f"\n📄 [6/6] 写文本 + git push...")
    push_log = out_dir / "push.log"
    summary = write_text_outputs(date, news, script, audio_info, out_dir, bgm_used_name)
    push_ok = git_push(date, summary, push_log)
    print(f"   {'✅' if push_ok else '⚠️'} push: {push_ok}")

    # Step 7: 发飞书
    msg = build_feishu_message(date, news, script, audio_info, final_with_bgm, push_ok, bgm_used_name)
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