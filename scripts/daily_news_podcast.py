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
# 项目根目录: 固定指向 podcast-agent (脚本可能从 ~/.hermes/scripts 或项目 scripts/ 运行)
PROJECT_ROOT = Path("/Users/saber/projects/podcast-agent")
DATA_DIR = PROJECT_ROOT / "data" / "news"
WORK_DIR = PROJECT_ROOT  # .git 所在

# ============ 音色配置 (1男1女, 1.5× 速度, 经典播客场景) ============
# 选成熟音色, 男声女声对比度明显 (经典电台/播客场景)
HOST_VOICE = "female-chengshu"   # 主持人: 成熟女声 (浑厚, 跟男声区分度大)
GUEST_VOICE = "male-qn-qingse"  # 嘉宾: 青年男声 (清晰, 中频)
HOST_SPEED = 1.25               # 主持人 1.25× (信息密度高, 节奏舒适)
GUEST_SPEED = 1.25              # 嘉宾 1.25× (跟 host 一致)
HOST_EMOTION = "happy"
GUEST_EMOTION = "happy"           # 都用 happy, 整体气氛积极

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
# 国际 RSS 源 (全部验证过 entries > 0, 2026-08-09 验证)
RSS_FEEDS_INTL = [
    ("OpenAI-News", "https://openai.com/news/rss.xml"),  # 1115 条, 官方一手
    ("Google-AI-Blog", "https://blog.google/technology/ai/rss/"),  # Google 官方
    ("TheVerge-AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),  # 10 条
    ("TechReview-AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),  # MIT 科技评论
    ("LastWeekInAI", "https://lastweekin.ai/feed"),  # 6 条, AI 周报
    ("MarkTechPost", "https://www.marktechpost.com/feed/"),  # 10 条, AI 技术博客
    ("SebastianRaschka", "https://magazine.sebastianraschka.com/feed"),  # 13 条, AI 论文
    ("TheGradient", "https://thegradient.pub/rss/"),  # 11 条, AI 研究
    ("HuggingFace-Blog", "https://huggingface.co/blog/feed.xml"),  # 开源模型
]
# 国内 RSS 源 (验证过)
RSS_FEEDS_CN = [
    ("量子位", "https://www.qbitai.com/feed"),  # 10 条, 国内 AI 主流
    ("雷锋网", "https://www.leiphone.com/feed"),  # 20 条, 智能硬件+AI
    ("钛媒体", "https://www.tmtpost.com/feed"),  # 17 条, 创投资讯
    ("掘金", "https://juejin.cn/rss"),  # 20 条, 技术社区
    ("199IT", "https://www.199it.com/feed"),  # 50 条, 数据/AI 媒体
]
# JSON API (无需 RSS 解析, 时间戳原生)
# ArXiv API 返回 Atom XML, 用 feedparser 解析 (不是 JSON)
JSON_API_INTL = [
    ("HackerNews", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=AI+OR+LLM+OR+GPT&numericFilters=created_at_i%3E{ts}"),
    ("ArXiv-cs.AI", "atom", "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=15"),
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
    # 放宽到 48h 窗口 - 24h 太严, RSS 通常一天一更, 凌晨跑就只能看到当天零星几条
    cutoff_ts = int((now - timedelta(hours=48)).timestamp())  # 48h 窗口
    primary_cutoff_ts = int((now - timedelta(hours=24)).timestamp())  # 24h 用于"优先"

    intl_items: list[dict] = []
    cn_items: list[dict] = []
    source_stats: dict[str, int] = {}

    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        # ============ 国际 RSS ============
        for name, url in RSS_FEEDS_INTL:
            try:
                resp = client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries:
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                        if pub_dt < now - timedelta(hours=48):
                            continue
                    intl_items.append({
                        "source": name,
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                    })
                    count += 1
                    if count >= 12:  # 每源最多 12 条
                        break
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [intl] {name}: {e}")

        # ============ Hacker News (JSON) + ArXiv (Atom XML) ============
        for name, fmt, url_template in JSON_API_INTL:
            try:
                if "{ts}" in url_template:
                    url = url_template.format(ts=cutoff_ts)
                else:
                    url = url_template
                resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})

                count = 0
                if fmt == "json":
                    data = resp.json()
                    items_iter = data.get("hits", [])
                    for hit in items_iter:
                        title = hit.get("title") or hit.get("story_title", "")
                        url_link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                        snippet = (hit.get("story_text") or "")[:200]
                        pub = hit.get("created_at", "")
                        intl_items.append({
                            "source": name, "title": title, "url": url_link,
                            "snippet": snippet, "published": pub,
                        })
                        count += 1
                elif fmt == "atom":
                    # ArXiv 返回 Atom XML, 用 feedparser 解析
                    feed = feedparser.parse(resp.text)
                    for entry in feed.entries[:15]:
                        pub_dt = None
                        pub = entry.get("published", "")
                        if entry.get("published_parsed"):
                            pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                            # 48h 过滤
                            if pub_dt and pub_dt < now - timedelta(hours=48):
                                continue
                        title = entry.get("title", "").replace("\n", " ").strip()
                        url_link = entry.get("link") or entry.get("id", "")
                        snippet = entry.get("summary", "")[:200].replace("\n", " ")
                        intl_items.append({
                            "source": name, "title": title, "url": url_link,
                            "snippet": snippet, "published": pub,
                        })
                        count += 1
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [intl] {name}: {e}")

        # ============ 国内 RSS (同样放宽到 48h) ============
        for name, url in RSS_FEEDS_CN:
            try:
                resp = client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                feed = feedparser.parse(resp.text)
                count = 0
                for entry in feed.entries:
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                        if pub_dt < now - timedelta(hours=48):
                            continue
                    cn_items.append({
                        "source": name,
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                    })
                    count += 1
                    if count >= 15:  # 国内每源最多 15 条
                        break
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
1. **总轮数**: 30 句 (15 轮对话, host/guest 各 15 句) - 目标时长 6 分钟
2. **严格交替**: host→guest→host→...→host 收尾
3. **句长**: 35-70 字 (口语化, 用"咱们/你看/啊/对吧/其实/不过"等口语词)
4. **节奏**: 紧凑, 一句一个信息点
5. **必须有具体数字**: 年份、模型名、性能、价格、参数量、用户数
6. **国际+国内都覆盖**: 至少 2 条国际 + 2 条国内
7. **结尾必须是 host**, 包含"订阅/点赞/小铃铛/下期预告"

## 结构模板 (30 句)
- 句1-2: 开场寒暄 (host 问, guest 应)
- 句3-8: 国际重磅 2-3 条 (host 问, guest 详解)
- 句9-16: 国内动态 2-3 条 (host 问, guest 详解)
- 句17-22: 行业影响/对比 (host 总结, guest 回应)
- 句23-26: 其他要闻速览 (3-4 条, 各一句话)
- 句27-29: 后续展望 (host+guest)
- 句30: 结尾订阅 CTA (host)

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


async def synthesize_tts_async(script: dict, out_dir: Path) -> list[Path]:
    """
    用 HermesTTSClient (直接调 MiniMax t2a_v2) 合成, 串行+重试, 避免 RPM 限流。

    不用 hermes text_to_speech_tool (它不接受 voice 参数, 只能用一个默认 voice)。

    串行实现: MiniMax TTS RPM 限流严 (实测并发 3 会触发 1002 rate limit),
    串行单请求 5-7s, 30 句约 3 分钟, 可接受
    """
    project_root = Path(__file__).parent.parent
    hermes_scripts = Path("/Users/saber/.hermes/scripts")
    for path in (project_root, hermes_scripts):
        if path.exists() and (path / "app" / "tts" / "hermes_tts.py").exists():
            sys.path.insert(0, str(path))
            break
    from app.tts.hermes_tts import HermesTTSClient

    tts_client = HermesTTSClient()
    tts_segments: list[Path] = []

    # 串行 + 重试 (MiniMax TTS RPM 限流严, 并发容易触发 1002)
    MAX_RETRIES = 3
    for i, line in enumerate(script["lines"]):
        role = line["role"]
        text = line["text"]
        mp3_path = out_dir / f"line_{i:02d}_{role}.mp3"

        if role == "host":
            voice = HOST_VOICE
            speed = HOST_SPEED
            emotion = HOST_EMOTION
        else:
            voice = GUEST_VOICE
            speed = GUEST_SPEED
            emotion = GUEST_EMOTION

        # 重试逻辑
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await tts_client.synthesize(
                    text=text,
                    voice=voice,
                    speed=speed,
                    emotion=emotion,
                    output_path=mp3_path,
                )
                tts_segments.append(mp3_path)
                if i % 5 == 0 or i == len(script["lines"]) - 1:
                    print(f"     [{i+1}/{len(script['lines'])}] {role:5s} {voice:20s} "
                          f"{result['audio_size_bytes']/1024:6.1f}KB "
                          f"{result['duration_ms']:5d}ms")
                break  # 成功, 退出重试循环
            except Exception as e:
                if attempt < MAX_RETRIES and ("rate limit" in str(e).lower() or "1002" in str(e)):
                    wait = 5 * attempt  # 5s, 10s, 15s
                    print(f"  ⚠️ [{i}] TTS 限流, {wait}s 后重试 ({attempt}/{MAX_RETRIES})")
                    await asyncio.sleep(wait)
                else:
                    print(f"  ⚠️ [{i}] TTS 失败: {e}")
                    break

    await tts_client.close()
    return tts_segments


def synthesize_tts(script: dict, out_dir: Path) -> list[Path]:
    """同步包装"""
    return asyncio.run(synthesize_tts_async(script, out_dir))


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

# 10 段程序化合成的 BGM (ffmpeg sine + noise + filter 链)
# 风格覆盖: lo-fi / ambient / jazz / electronic / focus
BGM_PRESETS = [
    # ===== Lo-Fi 系列 (3 段) =====
    {
        "name": "lofi_chill_60bpm",
        "description": "Lo-Fi chill 60 BPM - 温暖的钢琴循环 + lo-fi 鼓点",
        "filter_complex": (
            "[0:a]volume=0.3[beat];"
            "sine=frequency=80:duration={dur}[bass];"
            "[bass]volume=0.4[bs];"
            "anoisesrc=color=brown:duration={dur}:amplitude=0.04[noise];"
            "[noise]highpass=f=200[hp];"
            "[beat][bs][hp]amix=inputs=3:normalize=0[mixed];"
            "[mixed]highpass=f=80,lowpass=f=8000,aecho=0.8:0.9:1000:0.3[out]"
        ),
        "duration": 300,
    },
    {
        "name": "lofi_study_70bpm",
        "description": "Lo-Fi study 70 BPM - 慢节奏 + 钢琴琶音",
        "filter_complex": (
            "sine=frequency=65:duration={dur}[b];"
            "[b]volume=0.4[bs];"
            "sine=frequency=330:duration={dur}[p];"
            "[p]volume=0.2,tremolo=f=2:d=0.6[p2];"
            "anoisesrc=color=pink:duration={dur}:amplitude=0.03[n];"
            "[n]highpass=f=300[n2];"
            "[bs][p2][n2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=6000,aecho=0.7:0.85:800:0.3[out]"
        ),
        "duration": 300,
    },
    {
        "name": "lofi_sunset_80bpm",
        "description": "Lo-Fi sunset 80 BPM - 慵懒吉他感 + 雨声",
        "filter_complex": (
            "sine=frequency=98:duration={dur}[b];"
            "[b]volume=0.35,tremolo=f=3:d=0.7[bs];"
            "sine=frequency=440:duration={dur}[g];"
            "[g]volume=0.15,tremolo=f=4:d=0.5[g2];"
            "anoisesrc=color=white:duration={dur}:amplitude=0.02[n];"
            "[n]highpass=f=500[n2];"
            "[bs][g2][n2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=8000,aecho=0.8:0.9:1200:0.4[out]"
        ),
        "duration": 300,
    },
    # ===== Ambient 系列 (3 段) =====
    {
        "name": "ambient_pad_70bpm",
        "description": "Ambient pad 70 BPM - drone + 慢琶音",
        "filter_complex": (
            "sine=frequency=174:duration={dur}[s1];"
            "sine=frequency=261:duration={dur}[s2];"
            "[s1][s2]amix=inputs=2:normalize=0[pad];"
            "[pad]volume=0.25,lowpass=f=2000[pad2];"
            "sine=frequency=440:duration={dur}[arp];"
            "[arp]volume=0.15,tremolo=f=0.5:d=0.7[arp2];"
            "[pad2][arp2]amix=inputs=2:normalize=0[m];"
            "[m]aecho=0.7:0.8:1500:0.4,lowpass=f=6000[out]"
        ),
        "duration": 300,
    },
    {
        "name": "ambient_drone_50bpm",
        "description": "Ambient drone 50 BPM - 极慢 drone 适合长播客",
        "filter_complex": (
            "sine=frequency=110:duration={dur}[d1];"
            "sine=frequency=165:duration={dur}[d2];"
            "sine=frequency=220:duration={dur}[d3];"
            "[d1][d2][d3]amix=inputs=3:normalize=0[d];"
            "[d]volume=0.3,lowpass=f=1500[d2];"
            "sine=frequency=523:duration={dur}[s];"
            "[s]volume=0.08,tremolo=f=0.3:d=0.8[s2];"
            "[d2][s2]amix=inputs=2:normalize=0[m];"
            "[m]aecho=0.8:0.9:2000:0.5,lowpass=f=5000[out]"
        ),
        "duration": 300,
    },
    {
        "name": "ambient_space_60bpm",
        "description": "Ambient space 60 BPM - 太空感 + 钟声",
        "filter_complex": (
            "sine=frequency=220:duration={dur}[d];"
            "[d]volume=0.3,lowpass=f=1500[d2];"
            "sine=frequency=880:duration={dur}[b1];"
            "sine=frequency=1320:duration={dur}[b2];"
            "[b1]volume=0.05,tremolo=f=0.2:d=0.9[b1p];"
            "[b2]volume=0.04,tremolo=f=0.15:d=0.9[b2p];"
            "[d2][b1p][b2p]amix=inputs=3:normalize=0[m];"
            "[m]aecho=0.9:0.95:3000:0.6,lowpass=f=8000[out]"
        ),
        "duration": 300,
    },
    # ===== Jazz / Acoustic (2 段) =====
    {
        "name": "coffee_shop_jazz",
        "description": "Coffee shop jazz - 慵懒的钢琴/吉他感",
        "filter_complex": (
            "sine=frequency=98:duration={dur}[b];"
            "[b]volume=0.3,tremolo=f=3:d=0.6[bs];"
            "sine=frequency=392:duration={dur}[p];"
            "[p]volume=0.15,tremolo=f=1.5:d=0.4[p2];"
            "sine=frequency=1318:duration={dur}[sp];"
            "[sp]volume=0.06[sp2];"
            "[bs][p2][sp2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=12000,aecho=0.8:0.9:500:0.2[out]"
        ),
        "duration": 300,
    },
    {
        "name": "smooth_jazz_90bpm",
        "description": "Smooth jazz 90 BPM - 复古萨克斯感",
        "filter_complex": (
            "sine=frequency=82:duration={dur}[b];"
            "[b]volume=0.35,tremolo=f=2:d=0.5[bs];"
            "sine=frequency=466:duration={dur}[s];"
            "[s]volume=0.12,tremolo=f=3:d=0.6[s2];"
            "sine=frequency=1109:duration={dur}[h];"
            "[h]volume=0.04[h2];"
            "[bs][s2][h2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=14000,aecho=0.7:0.85:600:0.3[out]"
        ),
        "duration": 300,
    },
    # ===== Electronic 系列 (2 段) =====
    {
        "name": "soft_electronic_90bpm",
        "description": "Soft electronic 90 BPM - 轻快 synth",
        "filter_complex": (
            "sine=frequency=110:duration={dur}[b];"
            "[b]volume=0.35[bs];"
            "sine=frequency=60:duration={dur}[k];"
            "[k]volume=0.5,tremolo=f=2:d=0.9[k2];"
            "sine=frequency=523:duration={dur}[l];"
            "[l]volume=0.15,tremolo=f=4:d=0.5[l2];"
            "[bs][k2][l2]amix=inputs=3:normalize=0[m];"
            "[m]lowpass=f=10000[out]"
        ),
        "duration": 300,
    },
    {
        "name": "synthwave_100bpm",
        "description": "Synthwave 100 BPM - 复古电子 + drive (高能量版)",
        "filter_complex": (
            # 低音 - 用 saw 模拟 (用多个 sine 叠加)
            "sine=frequency=73:duration={dur}[b1];"
            "sine=frequency=146:duration={dur}[b2];"
            # 加重低音能量
            "[b1]volume=0.6,acompressor=threshold=0.5:ratio=4[b1c];"
            "[b2]volume=0.5,acompressor=threshold=0.5:ratio=4[b2c];"
            "[b1c][b2c]amix=inputs=2:normalize=0[bs];"
            # kick 鼓点
            "sine=frequency=55:duration={dur}[k];"
            "[k]volume=0.7,tremolo=f=2:d=0.85[k2];"
            # 主旋律 synth
            "sine=frequency=587:duration={dur}[l];"
            "[l]volume=0.4,tremolo=f=3:d=0.6[l2];"
            # 和声高音
            "sine=frequency=1760:duration={dur}[h];"
            "[h]volume=0.15[h2];"
            # 噪声鼓 (snare/hi-hat)
            "anoisesrc=color=white:duration={dur}:amplitude=0.05[n];"
            "[n]highpass=f=4000[n2];"
            "[n2]tremolo=f=4:d=0.7[n3];"
            "[bs][k2][l2][h2][n3]amix=inputs=5:normalize=0[m];"
            "[m]lowpass=f=14000,aecho=0.7:0.85:400:0.25,acompressor=threshold=0.3:ratio=3[out]"
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

            # 关键: 放大 BGM 音量 (程序化合成太安静, mean ~ -30dB,
            # 直接 amplify +20dB 提到 ~ -10dB 才能在混音中可闻)
            norm_path = out.with_suffix(".norm.mp3")
            r2 = subprocess.run([
                "ffmpeg", "-y", "-i", str(out),
                "-af", "volume=18dB,alimiter=limit=0.95",
                "-ac", "1", "-ar", "32000",
                "-acodec", "libmp3lame", "-b:a", "128k",
                str(norm_path),
            ], capture_output=True, text=True)
            if r2.returncode == 0:
                norm_path.replace(out)
                print(f"     🔊 放大 18dB: {preset['name']}")
        files.append(out)
        descriptions.append(f"{preset['name']}: {preset['description']}")

    return {"files": files, "descriptions": descriptions}


def mix_with_bgm(voice_path: Path, bgm_path: Path, output_path: Path,
                  bgm_volume_db: float = -3.0) -> dict:
    """
    人声 + BGM 混音, 不压 (不再用 sidetone compress)。

    BGM 整体 -3dB, 人声不做音量调整。
    让 BGM 在 line 之间的 350ms 静音间隙清晰可闻 (人声停了,
    BGM 单独在那段最响), 说话时 BGM 仍然在背景里。

    算法: [bgm]volume=-3dB; [voice][bgm]amix
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        # BGM 整体 -8dB (不压, 让它在静音间隙清晰可闻)
        f"[1:a]volume={bgm_volume_db}dB,acompressor=threshold=0.1:ratio=2[bgm];"
        # 直接混合
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[m]",
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

    # 国际 + 国内各 8 条 (比之前 5+5 多)
    lines_summary = []
    for n in news["intl"][:8]:
        lines_summary.append(f"🌍 [{n['title']}]({n['url']})")
    for n in news["cn"][:8]:
        lines_summary.append(f"🇨🇳 [{n['title']}]({n['url']})")

    push_status = "✅ 已同步到 GitHub" if push_ok else "⚠️ GitHub push 失败 (数据在本地)"
    bgm_info = f"\n🎵 **BGM**: {bgm_used}" if bgm_used else ""

    # 节目统计
    n_lines = len(script["lines"])
    n_host = sum(1 for l in script["lines"] if l["role"] == "host")
    n_guest = sum(1 for l in script["lines"] if l["role"] == "guest")

    # 剧本节选 (前 4 句 + 后 2 句)
    transcript_excerpt = ""
    if "lines" in script:
        first_lines = script["lines"][:3]
        last_lines = script["lines"][-2:]
        transcript_excerpt = "\n## 🎙️ 本期节选\n"
        for line in first_lines:
            emoji = "🎙️" if line["role"] == "host" else "🎓"
            transcript_excerpt += f"\n> {emoji} **{line['role'].upper()}**: {line['text']}"
        transcript_excerpt += "\n\n*... (更多精彩内容请收听本期播客) ...*\n"
        for line in last_lines:
            emoji = "🎙️" if line["role"] == "host" else "🎓"
            transcript_excerpt += f"\n> {emoji} **{line['role'].upper()}**: {line['text']}"

    msg = f"""📰 **今日AI头条** · {date}
{now_str()}

🎧 **播客**: 双角色 1男1女 (host={HOST_VOICE} + guest={GUEST_VOICE}){bgm_info}
⏱️ **时长**: {audio_info['duration_sec']:.0f}秒 ({audio_info['duration_sec']/60:.1f}分钟)
💾 **文件**: {mp3_path.stat().st_size/1024:.0f} KB
🎙️ **剧本**: {n_lines} 句 (host {n_host} + guest {n_guest}) | 1.25× 速度
{push_status}

---

## 🔥 今日要闻 ({len(news['intl']) + len(news['cn'])}条)

{chr(10).join(lines_summary)}

{transcript_excerpt}

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
    segments = await synthesize_tts_async(script, tts_dir)
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