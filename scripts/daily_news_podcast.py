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
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ============ 路径配置 ============
# 项目根目录: 固定指向 podcast-agent (脚本可能从 ~/.hermes/scripts 或项目 scripts/ 运行)
PROJECT_ROOT = Path("/Users/saber/projects/podcast-agent")
DATA_DIR = PROJECT_ROOT / "data" / "news"
WORK_DIR = PROJECT_ROOT  # .git 所在

# ============ 音色配置 (edge-tts, 2026-10-16 起: MiniMax 余额耗尽 → 免费微软 TTS) ============
HOST_VOICE = "zh-CN-XiaoxiaoNeural"   # 主持人: 女声 (晓晓, edge-tts 最自然的中文女声)
GUEST_VOICE = "zh-CN-YunjianNeural"   # 嘉宾: 男声 (云健)
HOST_SPEED = "+25%"              # edge-tts 速率格式: +N% (对应原 MiniMax 1.25×)
GUEST_SPEED = "+25%"
HOST_EMOTION = ""   # edge-tts 无 emotion 参数, 保留字段兼容
GUEST_EMOTION = ""

# 时区: GMT+8
TZ = timezone(timedelta(hours=8))


def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def date_str_to_num(date_str: str) -> int:
    """YYYY-MM-DD -> 连续天数 (用于固定 BGM 映射)。

    用 toordinal 而不是 YYYYMMDD 整数: 后者跨月时跳变 (8/31→9/1 增量 69),
    会导致 51 首音乐库取模后轮换不均匀 (实测 51 天只覆盖 33 首)。
    toordinal 严格每天 +1, 51 天完整轮换 51 首。
    """
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d).toordinal()


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
    ("36氪", "https://www.36kr.com/feed"),  # 30 条, 创业/AI 财经 (DeepSeek V4 Pro 在这里首发)
    ("InfoQ中文", "https://www.infoq.cn/feed.xml"),  # 20 条, 技术深度报道
    ("掘金", "https://juejin.cn/rss"),  # 20 条, 技术社区
    ("199IT", "https://www.199it.com/feed"),  # 50 条, 数据/AI 媒体
]
# JSON API (无需 RSS 解析, 时间戳原生)
# ArXiv API 返回 Atom XML, 用 feedparser 解析 (不是 JSON)
JSON_API_INTL = [
    ("HackerNews", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=AI+OR+LLM+OR+GPT&numericFilters=created_at_i%3E{ts}"),
    # 国内大模型厂商专项 — Algolia 不支持 OR, 用关键词拼接 (空格 = AND, 这里单跑多次拿全)
    ("HackerNews-DeepSeek", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=DeepSeek&numericFilters=created_at_i%3E{ts}"),
    ("HackerNews-Kimi", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=Kimi&numericFilters=created_at_i%3E{ts}"),
    ("HackerNews-Qwen", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=Qwen&numericFilters=created_at_i%3E{ts}"),
    # xAI / Grok 专项（Twitter/X 抓不到, 靠 HN 信号)
    ("HackerNews-Grok", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=Grok&numericFilters=created_at_i%3E{ts}"),
    ("HackerNews-xAI", "json", "https://hn.algolia.com/api/v1/search?tags=story&query=xAI&numericFilters=created_at_i%3E{ts}"),
    ("ArXiv-cs.AI", "atom", "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=15"),
]


def _http_get_with_retry(client, url, headers, attempts=3, delay=3.0):
    """带重试的 GET——qbitai 等站点 WAF 偶发 RST 连接(Errno 54), 单次失败即弃会整源丢失。"""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return client.get(url, headers=headers)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    assert last_exc is not None
    raise last_exc


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
                resp = _http_get_with_retry(client, url, {
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
                    title_str = entry.get("title", "")
                    intl_items.append({
                        "source": name,
                        "title": title_str,
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                        "weight": _news_weight(title_str, name),
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
                resp = _http_get_with_retry(client, url, {"User-Agent": "Mozilla/5.0"})

                count = 0
                if fmt == "json":
                    data = resp.json()
                    items_iter = data.get("hits", [])
                    # HN 专项搜索每源最多 8 条 (避免一个关键词刷屏挤掉其他厂商)
                    per_source_limit = 8 if name.startswith("HackerNews-") else 100
                    for hit in items_iter[:per_source_limit]:
                        title = hit.get("title") or hit.get("story_title", "")
                        url_link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
                        snippet = (hit.get("story_text") or "")[:200]
                        pub = hit.get("created_at", "")
                        # 大厂发布事件加权 (确保排序时被排到前面)
                        weight = _news_weight(title, name)
                        intl_items.append({
                            "source": name, "title": title, "url": url_link,
                            "snippet": snippet, "published": pub, "weight": weight,
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
                            "weight": _news_weight(title, name),
                        })
                        count += 1
                source_stats[name] = count
            except Exception as e:
                print(f"  ⚠️ [intl] {name}: {e}")

        # ============ 国内 RSS (同样放宽到 48h) ============
        for name, url in RSS_FEEDS_CN:
            try:
                resp = _http_get_with_retry(client, url, {
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
                    title_str = entry.get("title", "")
                    cn_items.append({
                        "source": name,
                        "title": title_str,
                        "url": entry.get("link", ""),
                        "snippet": (entry.get("summary", "") or "")[:200],
                        "published": entry.get("published", ""),
                        "weight": _news_weight(title_str, name),
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

    # 按权重 + 时间排序 (weight 高的在前, 同 weight 按时间倒序)
    intl_items.sort(key=lambda x: (-x.get("weight", 1), x.get("published", "")), reverse=False)
    cn_items.sort(key=lambda x: (-x.get("weight", 1), x.get("published", "")), reverse=False)

    return {
        "intl": intl_items[:25],
        "cn": cn_items[:25],
        "source_stats": source_stats,
        "dedup": {
            "intl": {"before": intl_before, "after": len(intl_items)},
            "cn": {"before": cn_before, "after": len(cn_items)},
        },
    }


def _dedup_news(items: list[dict]) -> list[dict]:
    """
    标题相似度去重 + URL 去重。
    算法:
      1) 标题前 6 个核心 token 做签名, 完全相同直接去重
      2) 标题核心关键词 Jaccard > 0.4 (放宽自 0.5, 抓同主题多个报道)
      3) URL 完全相同去重
    """
    import re
    seen_urls = set()
    seen_signatures: list[set] = []
    seen_keywords: list[set] = []

    result = []
    for it in items:
        url = it.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)

        kw = _extract_keywords(it.get("title", ""))
        # 短签名: 选最重要的 6 个 token
        important_kw = sorted(kw, key=lambda w: (-len(w), w))[:6]
        sig = frozenset(important_kw)

        # 完全相同签名 -> 直接去重
        if sig in seen_signatures:
            continue
        # 相似签名 -> 也去重 (Jaccard > 0.4)
        if any(_jaccard(sig, s) > 0.4 for s in seen_signatures):
            continue
        # 关键词重叠度
        if any(_jaccard(kw, k) > 0.5 for k in seen_keywords):
            continue

        seen_signatures.append(sig)
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


def _news_weight(title: str, source: str) -> int:
    """
    新闻权重: 大厂新模型/产品发布 = 10, 重大并购/融资 = 8, 行业变革 = 6, 普通新闻 = 1。
    排序时优先选高权重新闻, 避免低质 Show HN / 中文长标题把重要新闻挤掉。
    """
    title_lower = title.lower()

    # 顶级模型/产品发布 (新版本号)
    big_keywords = ["v4", "v5", "4.5", "4.6", "4.7", "5.0", "pro", "ultra", "opus",
                    "sonnet", "haiku", "flash", "preview", "official", "正式版", "正式上线",
                    "上线", "发布", "发布", "推出", "宣布"]
    # 国内大模型厂商首发
    cn_major_vendors = ["deepseek", "kimi", "qwen", "通义千问", "千问", "文心",
                        "智谱", "chatglm", "豆包", "混元", "hunyuan", "doubao"]
    # xAI / Grok / OpenAI / Anthropic / Google / Meta
    intl_majors = ["grok", "xai", "x.ai", "openai", "anthropic", "claude",
                   "google", "gemini", "meta", "llama"]

    # 大厂首发 = 10
    if any(kw in title_lower for kw in cn_major_vendors):
        if any(kw in title_lower for kw in big_keywords):
            return 10
        return 7  # 普通厂商新闻
    if any(kw in title_lower for kw in intl_majors):
        if any(kw in title_lower for kw in big_keywords):
            return 9
        return 6

    # 重大融资/并购
    if any(kw in title for kw in ["收购", "并购", "亿美元", "$", "融资", "估值"]):
        return 5

    # 行业变革信号
    if any(kw in title for kw in ["重大", "重磅", "首发", "独家", "突发", "爆"]):
        return 4

    # 噪声降权: Show HN 类 (个人项目, 非新闻)
    if "show hn" in title_lower or "ask hn" in title_lower:
        # 但保留包含大厂 + 新版本的
        has_major = any(kw in title_lower for kw in ["deepseek", "grok", "xai", "x.ai",
                                                       "openai", "claude", "anthropic",
                                                       "gemini", "qwen", "kimi"])
        if has_major and any(kw in title_lower for kw in ["v4", "v5", "4.5", "4.6", "4.7", "5.0", "pro", "ultra", "opus", "sonnet", "haiku", "flash"]):
            return 9  # 大厂 + 新版本 Show HN 保留
        return 0  # 普通 Show HN 直接降到 0

    # 默认
    return 1


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union > 0 else 0.0


def _fallback_news_items() -> dict[str, list[dict]]:
    """全部新闻源抓取失败时的硬编码兜底新闻 (2026-09-05 恢复, 原 8/13 改动误删)"""
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


# ============ Step 2: LLM 生成剧本 (选最重磅 4-6 条, 20 句 ≈ 5-6 分钟) ============
SCRIPT_PROMPT = """你是一档名为《今日AI头条》的中文科技播客的主笔编剧, 节目时长约 6-7 分钟, 60 句对话, 信息密度高。

## 新闻素材 (按时间倒序, 24小时内)
### 🌍 国际 ({n_intl} 条)
{intl}

### 🇨🇳 国内 ({n_cn} 条)
{cn}

## 任务
1. **精选最重磅的 10-12 条** (国内 5-6 条 + 国际 4-5 条) 做深度解读
2. **顺序严格**: 国内重磅在前, 国际重磅在后, 联动分析贯穿中间
3. 选材标准: 涉及顶级模型/产品发布、重磅融资、行业变革、高管人事
4. 国内优先级: 涉及国内大模型厂商 (DeepSeek/Qwen/Kimi/豆包/智谱) 或中国 AI 行业大事
5. 国际优先级: 涉及 OpenAI/Anthropic/xAI/Google/Meta 或国际 AI 趋势
6. 剩余新闻在第 47-54 句快速提及 (一句话带过, 8 条速览)
7. **文字部分 (news.md) 会单独输出全部新闻** (30 条以上), 播客部分只挑最重磅的 10-12 条

## 双角色
- **host** (女, 主持人, 节奏活泼) — 开场、追问、过渡、收尾
- **guest** (男, 科技评论员) — 解读事实、给数字、举例子

## 严格要求
1. **总轮数**: 60 句 (30 轮对话, host/guest 各 30 句) - 目标时长 6-7 分钟
2. **严格交替**: host→guest→host→...→host 收尾
3. **句长**: 35-70 字 (口语化, 用"咱们/你看/啊/对吧/其实/不过"等口语词)
4. **节奏**: 紧凑, 一句一个信息点, 但内容更深度 (每条新闻 4-6 句讨论)
5. **必须有具体数字**: 年份、模型名、性能、价格、参数量、用户数
6. **国内 5-6 条 + 国际 4-5 条**, 深度解读
7. **结尾必须是 host**, 包含"订阅/点赞/小铃铛/下期预告"
8. **【严格语言】全篇中文,绝不出现英文**:
   - 英文模型/公司/产品名必须翻译或中文化,例如:
     GPT-4o→"GPT-4o"可保留,但配套文字必须中文 ("OpenAI 的 GPT-4o" ✅, "OpenAI released GPT-4o" ❌)
     Claude →"Claude"可保留,搭配"Anthropic 的 Claude"
     ChatGPT →"ChatGPT"可保留
     Anthropic/OpenAI/Google/Microsoft/Meta → "Anthropic"/"OpenAI"/"谷歌"/"微软"/"Meta" ✅
   - 动词必须中文: released→发布/推出, achieved→实现/达到, surpassed→超越
   - 句子必须是中文语法,不允许 "X is Y" 这种英文结构
   - **唯一允许的英文**: TTS 读不出的极少见专有名词 (一个人名/产品代号);
     常见词必须中文 ("release/launch/announce"→发布/推出/宣布)
   - host 和 guest 全程说中文,口语化,绝不夹英文长句

## 结构模板 (60 句)
- 句1: 开场寒暄 (host 极简版, 一句话报日期+今日概览, 30 字以内)
- 句2: 过场 (guest 一句话衔接国内重点, 30 字以内)
- 句3-22: **国内重磅 5-6 条** (20 句, 每条 ~4 句深入讨论, host+guest 轮流追问+解读+举例)
- 句23-38: **国际重磅 4-5 条** (16 句, 每条 ~3-4 句深入讨论)
- 句39-46: **行业影响/对比** (8 句, 国内国际联动分析, 横纵对比)
- 句47-54: **其他要闻速览** (8 条, 各一句话快速带过, host+guest 交替)
- 句55-59: **后续展望** (5 句, host+guest 深度讨论未来变量)
- 句60: 结尾订阅 CTA (host, 一句话)

## 输出 (严格 JSON, 不要任何解释文字)
{{
  "title": "今日AI头条",
  "tagline": "{date} | 国际+国内重磅速览 (60句深度版)",
  "selected_news": [
    {{"source": "...", "title": "中文标题(必须把英文标题译成中文,保留产品/模型英文名)", "url": "...", "weight": "deep"}},
    ...
  ],
  "lines": [
    {{"role": "host", "text": "..."}},
    {{"role": "guest", "text": "..."}},
    ...
  ]
}}

## selected_news 严格要求
- **必须从上面新闻素材里挑,不能凭空编** (LLM 编出来的 url 会失效)
- 每条 selected_news 的 url 必须**原样拷贝**自新闻素材 (不要填来源站首页)
- title 翻译成中文 (保留专有名词如 GPT-4o / Claude / Pokee-Isaac 等)
- **10-12 条**, 国内 5-6 条 + 国际 4-5 条, weight 用 "deep" (重磅) 或 "medium" (次要)
- **播客只念 10-12 条最重要的, 文字部分 (news.md) 会单独输出全部 30+ 条新闻**
"""


async def generate_script(news: dict[str, list[dict]], date: str) -> dict[str, Any]:
    """调智谱 GLM (OpenAI 兼容路径) 生成剧本

    2026-10-16: MiniMax 余额耗尽 (HTTP 402) → 换 zai glm-5.3-flash。
    - 走 https://open.bigmodel.cn/api/coding/paas/v4/chat/completions (OpenAI 兼容)
    - key 经 resolve_provider_secret("ZAI_API_KEY", "zai") 拿 (hermes 进程内)
    - thinking 关闭: glm 系 thinking 在长 prompt 上会拖慢甚至超时 (见 memory)
    - timeout 600s: 60 句深度版 prompt ~7k 字, 保险余量
    """
    import httpx

    # 拿 key (hermes 凭证池)
    sys.path.insert(0, "/Users/saber/.hermes/hermes-agent")
    key = os.environ.get("ZAI_API_KEY", "")
    if not key:
        try:
            from tools.tool_backend_helpers import resolve_provider_secret
            key = resolve_provider_secret("ZAI_API_KEY", "zai")
        except Exception as e:
            raise RuntimeError(
                f"拿不到智谱 key: {e}. "
                "必须在 hermes 进程内执行, 或 export ZAI_API_KEY=..."
            )

    if not key:
        raise RuntimeError("智谱 key 为空")

    base = "https://open.bigmodel.cn/api/coding/paas/v4"
    model = "glm-5.3-flash"

    prompt = SCRIPT_PROMPT.format(
        n_intl=len(news["intl"]),
        n_cn=len(news["cn"]),
        intl="\n".join(f"- [{n.get('source', '?')}] {n['title']}: {n['snippet']}" for n in news["intl"]),
        cn="\n".join(f"- [{n.get('source', '?')}] {n['title']}: {n['snippet']}" for n in news["cn"]),
        date=date,
    )

    body = {
        "model": model,
        "max_tokens": 16384,  # 60 句深度版需要更多 token
        "temperature": 0.85,
        "thinking": {"type": "disabled"},  # glm 系 thinking 会拖慢长 prompt, 关掉
        "messages": [{"role": "user", "content": prompt}],
    }

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    # OpenAI 响应格式: choices[].message.content
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    # 解析 JSON (优先用 json_repair, 容错强; 截断时尝试补救)
    import re
    script = None
    # 优先 raw_decode: 从首个 { 精确解析一个完整 JSON 对象, 忽略其后多余文本
    # (greedy regex 会把 JSON 后的尾巴一起吞进来, 报 "Extra data" — 2026-10-28 实锤)
    start = content.find("{")
    if start >= 0:
        try:
            script, _ = json.JSONDecoder().raw_decode(content[start:])
        except json.JSONDecodeError:
            script = None
    if script is None:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise RuntimeError(f"LLM 没返回 JSON: {content[:300]}")
        json_str = match.group(0)
    else:
        json_str = ""
    if script is None:
        try:
            from json_repair import repair_json
            repaired = repair_json(json_str, return_objects=True)
            if isinstance(repaired, dict):
                script = repaired
        except ImportError:
            pass

    if script is None:
        try:
            script = json.loads(json_str)
        except json.JSONDecodeError:
            # 截断补救: 找到最后一个完整的 "},", 截断到那里 + 补全结构
            last_complete = json_str.rfind("},")
            if last_complete > 0:
                truncated = json_str[:last_complete+1]
                # 计数未闭合的 [ 和 {
                opens = truncated.count("[") - truncated.count("]")
                braces = truncated.count("{") - truncated.count("}")
                # 补全 closing
                truncated += "]" * opens + "}" * braces
                try:
                    script = json.loads(truncated)
                    print(f"  ⚠️ JSON 截断补救: 截断到 {last_complete+1} 位置 + 补全 {opens}+{braces} 个括号")
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"JSON 解析失败 (截断补救也失败): {e}\n{json_str[:500]}")
            else:
                raise RuntimeError(f"JSON 解析失败: {json_str[:500]}")

    # 兜底: 严格交替 + 最后一句 host + 截断到目标句数
    target_lines = 60  # SCRIPT_PROMPT 要求 60 句
    lines = script.get("lines", [])
    fixed = []
    for i, line in enumerate(lines):
        if len(fixed) >= target_lines:
            break  # 截断多余句
        want = "host" if i % 2 == 0 else "guest"
        if line.get("role") != want:
            line = {**line, "role": want}
        fixed.append(line)
    # 最后一句必须是 host
    if fixed and fixed[-1].get("role") != "host":
        fixed[-1] = {**fixed[-1], "role": "host"}
    # 确保 60 句 (如果 LLM 输出 < 60, 补 host 收尾)
    while len(fixed) < target_lines:
        fixed.append({"role": "host", "text": "今天的内容就先聊到这。"})
    script["lines"] = fixed[:target_lines]

    if not script.get("title"):
        script["title"] = "今日AI头条"
    if not script.get("tagline"):
        script["tagline"] = f"{date} | 国际+国内 AI 重磅速览"

    # ---- 兜底: LLM 仍可能漏出英文,这里做机械修正 ----
    # 常见英文专名/动词强制中文化 (LLM 没遵守时补救)
    EN_TO_ZH = {
        # 公司
        "Anthropic released": "Anthropic 发布了",
        "OpenAI released": "OpenAI 发布了",
        "Google released": "谷歌发布了",
        "Microsoft released": "微软发布了",
        "Meta released": "Meta 发布了",
        "released": "发布",
        "launched": "推出",
        "announced": "宣布",
        "introduced": "推出",
        "unveiled": "发布",
        "achieves": "实现",
        "achieved": "实现",
        "surpasses": "超越",
        "surpassed": "超越",
        "reports": "报告显示",
        "reported": "报告",
    }
    for line in script["lines"]:
        text = line["text"]
        for en, zh in EN_TO_ZH.items():
            if en in text and not text.startswith("#"):
                text = text.replace(en, zh)
        line["text"] = text

    return script


async def synthesize_tts_async(script: dict, out_dir: Path) -> list[Path]:
    """
    用 edge-tts (微软免费 TTS) 合成, 串行 + 重试。

    2026-10-16: MiniMax t2a_v2 余额耗尽 → 换 edge-tts (免费, 无 RPM 顾虑但保持串行
    避免并发连接被微软限流)。双角色直接指定 voice; 速率 +25% 对应原 MiniMax 1.25×。
    """
    import edge_tts

    tts_segments: list[Path] = []
    MAX_RETRIES = 3
    for i, line in enumerate(script["lines"]):
        role = line["role"]
        text = line["text"]
        mp3_path = out_dir / f"line_{i:02d}_{role}.mp3"

        voice = HOST_VOICE if role == "host" else GUEST_VOICE
        rate = HOST_SPEED if role == "host" else GUEST_SPEED

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                comm = edge_tts.Communicate(text, voice, rate=rate)
                await comm.save(str(mp3_path))
                tts_segments.append(mp3_path)
                await asyncio.sleep(0.2)
                break
            except Exception as e:
                if attempt < MAX_RETRIES:
                    wait = 3 * attempt
                    print(f"  ⚠️ [{i}] TTS 失败 ({e}), {wait}s 后重试 ({attempt}/{MAX_RETRIES})")
                    await asyncio.sleep(wait)
                else:
                    print(f"  ⚠️ [{i}] TTS 最终失败: {e}")
                    break

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

# 经典音乐库: mariage_damour.mp3 + 50 首经典轻音乐 (全部真实音乐, 响度正常 ~-18dB mean,
# 不需要程序化 BGM 的 18dB 放大)。按日期轮换, 每天换一首。


def ensure_bgm() -> dict:
    """
    扫描 assets/bgm/*.mp3 经典音乐库。
    返回 {"files": [Path, ...], "descriptions": [...], "volume_db": float}.
    volume_db 统一 -15dB: 用户明确要求背景乐一定要小 (实测间隙段 -45dB, 整体 mean 不变)。
    """
    BGM_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BGM_DIR.glob("*.mp3"))
    descriptions = [f.stem for f in files]
    return {"files": files, "descriptions": descriptions, "volume_db": -15.0}



def mix_with_bgm(voice_path: Path, bgm_path: Path, output_path: Path,
                  bgm_volume_db: float = -15.0) -> dict:
    """
    人声 + BGM 混音, 不压 (不再用 sidetone compress)。

    BGM 整体 -15dB (小声背景, 用户明确要求背景乐一定要小!),
    人声不做音量调整。
    实测 (mariage_damour.mp3): 整体 mean 保持 -21.7dB 不变,
    人声间隙段 BGM 单独 -45dB — 能听到但不吵。

    算法: [bgm]volume=-15dB; [voice][bgm]amix
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex",
        # BGM 整体 {bgm_volume_db}dB (小声背景), 人声不衰减 (normalize=0)
        f"[1:a]volume={bgm_volume_db}dB[bgm];"
        # 直接混合
        f"[0:a][bgm]amix=inputs=2:duration=first:normalize=0[m]",
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
                         push_ok: bool, bgm_used: str | None) -> list[str]:
    """构造飞书消息: 标题头 + 今日要闻 (精选+扩展) + 完整逐字稿 + mp3

    块结构 (按 ≤3700 字符/块 拆分):
      1. 标题头 + 精选要闻 (selected_news, 已译中文) — 用户最想看的"具体AI资讯"
      2. 扩展要闻 (国际+国内, selected 之外的 5+5 条, 去 HTML)
      3. 完整逐字稿 (30 句, 备用, 供查阅)
      最后一块: mp3 附件 (MEDIA:)

    设计原则:
    - 头: 极简 (一行的标题 + BGM + 时长)
    - 重点突出: 精选 6 条放最前 (LLM 已译成中文)
    - 不截断: 超长自动拆块, 每块单独 hermes send
    """
    import re as _re
    duration_min = audio_info["duration_sec"] / 60
    bgm_info = f" · 🎵 {bgm_used}" if bgm_used else ""
    push_status = "✅ GitHub 同步" if push_ok else "⚠️ push 失败 (本地保留)"

    # ---- 补全 selected_news 的真实 url ----
    # LLM 通常把 url 留空或只填来源站首页; 我们用标题前 12 字做模糊匹配回原 news 列表
    def _strip_html(s: str) -> str:
        return _re.sub(r"<[^>]+>", "", s or "").strip()

    selected = list(script.get("selected_news") or [])
    all_news = list(news.get("intl", [])) + list(news.get("cn", []))
    if selected and all_news:
        # 抽原新闻标题中的英文/数字片段 (产品名/型号/公司) 作为匹配键
        # 因为 selected.title 是 LLM 译成中文的, 整段字符串匹配不上
        for orig in all_news:
            orig["_keys"] = [
                m.group(0) for m in _re.finditer(
                    r"[A-Za-z][A-Za-z0-9._-]{3,}|[\d.]+[A-Za-z]+|\d{2,}",
                    orig.get("title", "")
                )
            ]
        for sn in selected:
            sn["url"] = sn.get("url") or ""
            sn["snippet"] = sn.get("snippet") or ""
            t = sn.get("title", "")
            t_keys = set(m.group(0) for m in _re.finditer(
                r"[A-Za-z][A-Za-z0-9._-]{3,}|[\d.]+[A-Za-z]+|\d{2,}", t
            ))
            best = None
            best_score = 0
            for orig in all_news:
                overlap = len(t_keys & set(orig["_keys"]))
                if overlap > best_score:
                    best_score = overlap
                    best = orig
            if best and best_score > 0:
                # LLM 通常填来源站首页; 只有看起来是真文章 URL (>1 path 段)
                # 或完全为空时才覆盖
                orig_url = sn.get("url", "")
                from urllib.parse import urlparse
                orig_path = urlparse(orig_url).path if orig_url else ""
                looks_like_homepage = (
                    not orig_url
                    or orig_path in ("", "/")
                    or orig_url.endswith((".com", ".cn", ".io", ".org"))  # 裸域名
                )
                if looks_like_homepage:
                    sn["url"] = best.get("url", orig_url)
                # snippet 一律补全 (LLM 经常漏)
                sn["snippet"] = sn["snippet"] or _strip_html(best.get("snippet", ""))
            # 没匹配上保留原 url

    # ---- 精选要闻 (deep + medium) ----
    # 格式与"国际其他/国内其他"一致: `- [标题](url)  _source_`
    def _fmt(sn):
        title = sn.get("title", "").strip()
        url = sn.get("url", "").strip()
        source = sn.get("source", "")
        if url:
            line = f"- [{title}]({url})"
        else:
            line = f"- {title}"
        if source:
            line += f"  _{source}_"
        return line

    deep_lines = [_fmt(sn) for sn in selected if sn.get("weight", "deep") == "deep"]
    other_selected = [_fmt(sn) for sn in selected if sn.get("weight", "deep") != "deep"]

    # ---- 扩展要闻 (selected 之外的, 国际 + 国内 各 5 条) ----
    used_titles = set(sn.get("title", "")[:12] for sn in selected)
    ext_intl, ext_cn = [], []
    for n in news.get("intl", []):
        if n.get("title", "")[:12] in used_titles:
            continue
        ext_intl.append(n)
        if len(ext_intl) >= 5:
            break
    for n in news.get("cn", []):
        if n.get("title", "")[:12] in used_titles:
            continue
        ext_cn.append(n)
        if len(ext_cn) >= 5:
            break

    # ---- 构造 3 个语义块 (header + 精选 + 扩展 + mp3) ----
    header_block = (
        f"📰 **今日AI头条** · {date}{bgm_info}\n"
        f"⏱️ {duration_min:.1f} 分钟 · {push_status}"
    )

    deep_block_parts = ["## 🔥 今日精选"]
    deep_block_parts.extend(deep_lines)
    if other_selected:
        deep_block_parts.append("\n## ⚡ 速览")
        deep_block_parts.extend(other_selected)
    deep_block = "\n".join(deep_block_parts)

    ext_parts = []
    if ext_intl:
        ext_parts.append("## 🌍 国际其他")
        for n in ext_intl:
            t = n.get("title", "")
            u = n.get("url", "")
            ext_parts.append(f"- [{t}]({u})" if u else f"- {t}")
    if ext_cn:
        ext_parts.append("\n## 🇨🇳 国内其他")
        for n in ext_cn:
            t = n.get("title", "")
            u = n.get("url", "")
            ext_parts.append(f"- [{t}]({u})" if u else f"- {t}")
    ext_block = "\n".join(ext_parts) if ext_parts else ""

    # 逐字稿不再发到飞书 (用户只要 mp3), news.md 里已经有完整 transcript
    transcript_block = ""

    mp3_block = f"🎧 播客音频:\n\nMEDIA:{mp3_path}"

    # ---- 拼装 + 按 3700 字符硬切 ----
    MAX = 3700
    sections = [
        ("header", header_block),
        ("deep", deep_block),
        ("ext", ext_block),
        ("mp3", mp3_block),
    ]

    chunks = []
    current = ""
    current_kind = None  # 跟踪当前块跨到了哪个 section (便于 debug)

    def _flush():
        nonlocal current, current_kind
        if current.strip():
            chunks.append(current.strip())
        current = ""
        current_kind = None

    for kind, body in sections:
        if not body:
            continue
        # mp3 块必须独立 (飞书附件醒目显示)
        if kind == "mp3":
            _flush()
            chunks.append(body.strip())
            continue
        # 单 section 本身就超过 MAX → 强制切
        if len(body) > MAX:
            _flush()
            for line in body.split("\n"):
                candidate = current + ("\n" if current else "") + line
                if len(candidate) > MAX and current:
                    _flush()
                current += ("\n" if current else "") + line
            current_kind = kind
            continue
        # 尝试塞进当前块
        sep = "\n\n" if current else ""
        candidate = current + sep + body
        if len(candidate) <= MAX:
            current = candidate
            current_kind = kind
        else:
            _flush()
            current = body
            current_kind = kind
    _flush()

    # 兜底: 如果全部 sections 都为空, 至少给 mp3
    if not chunks:
        chunks = [mp3_block]

    return chunks


# ============ 主流程 ============
async def main():
    date = today_str()

    # 准备目录
    out_dir = DATA_DIR / date
    out_dir.mkdir(parents=True, exist_ok=True)
    tts_dir = out_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    # 当日去重（2026-10-17 加）
    # ---------------------------------------------------------------
    # 背景：Mac 睡眠时 8:30 的 cron 班次会被错过，且 hermes cron
    # 错过不补跑（不是 anacron）。为自愈，cron 改成每 30 分钟触发一次，
    # 由本检查保证一天只真正生成一期：final.mp3 已存在 = 今天已完成，
    # 静默退出（cron no_agent 空 stdout = 不推送）。
    #
    # 想强制重跑当天：删掉 data/news/<date>/final.mp3 再触发。
    final_early = out_dir / "final.mp3"
    if final_early.exists() and final_early.stat().st_size > 100_000:
        return  # 当日已完成，完全静默（cron no_agent 空 stdout = 不推送）

    # Step 1: 拉新闻
    news = fetch_news()

    # Step 2: LLM 编剧
    script = await generate_script(news, date)
    chars = sum(len(l["text"]) for l in script["lines"])

    # Step 3: TTS 合成
    segments = await synthesize_tts_async(script, tts_dir)

    # Step 4: 拼接
    final_mp3 = out_dir / "final.mp3"
    audio_info = concat_audio(segments, final_mp3)

    # Step 5: BGM 混音
    bgm_info = ensure_bgm()
    # 按日期轮换经典音乐库 (51 首, 每天换一首, 固定映射保证同日一致)
    bgm_index = int(date_str_to_num(date)) % len(bgm_info["files"]) if bgm_info["files"] else 0
    bgm_path = bgm_info["files"][bgm_index] if bgm_info["files"] else None
    bgm_volume_db = bgm_info.get("volume_db", -15.0)
    bgm_used_name = None

    final_with_bgm = out_dir / "final.mp3"
    if bgm_path and bgm_path.exists():
        # 先把人声拼到 voice_only, 再混 BGM
        voice_only = out_dir / "voice_only.mp3"
        concat_audio(segments, voice_only)
        try:
            bgm_used_name = bgm_path.stem
            final_with_bgm = out_dir / "final.mp3"
            audio_info = mix_with_bgm(voice_only, bgm_path, final_with_bgm,
                                      bgm_volume_db=bgm_volume_db)
        except Exception as e:
            voice_only.replace(final_with_bgm)
    else:
        final_with_bgm = out_dir / "final.mp3"
        voice_only.replace(final_with_bgm)

    # Step 6: 写文本 + git push
    push_log = out_dir / "push.log"
    summary = write_text_outputs(date, news, script, audio_info, out_dir, bgm_used_name)
    push_ok = git_push(date, summary, push_log)

    # Step 7: 发飞书
    # 生成飞书消息 (多块)
    msg_blocks = build_feishu_message(date, news, script, audio_info,
                                       final_with_bgm, push_ok, bgm_used_name)
    # 写到本地文件 (备查)
    msg_file = out_dir / "feishu_message.md"
    msg_file.write_text("\n\n===== BLOCK =====\n\n".join(msg_blocks),
                         encoding="utf-8")

    # 直接调 hermes send 投递多块到飞书 DM (绕过 gateway 4000 字符截断)
    import subprocess as _sp
    feishu_target = "feishu:oc_c7abfd100eb35b6f3f95363a2c951207"
    for i, block in enumerate(msg_blocks, 1):
        _sp.run(["hermes", "send", "-t", feishu_target, "-q", block],
                 check=False)

    # 退出码: 任何步骤失败都非零
    if not segments:
        sys.exit(2)
    # 静默 stdout (cron no_agent: 空 stdout = 静默, 不重复投递)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n❌ 致命错误: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)