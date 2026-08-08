# Podcast Agent

AI 播客生成 Agent — MiniMax 全栈 (LLM + TTS + Music + Mixing)。

## 特性

- 🎙️ **单 API 端到端**: 一行 JSON 生成完整播客(剧本 + 语音 + 混音)
- 🎭 **双角色对话**: host + guest,严格交替,自动修复 LLM 输出
- 🔊 **MiniMax TTS**: 支持声音克隆、情绪控制、多音色切换
- 🎵 **BGM 混音**: 自动循环 + 响度归一 (-16 LUFS 播客标准)
- 💾 **状态持久化**: SQLite + 断点恢复 (LLM 写一半挂了能从 TTS 继续)
- 📊 **配额追踪**: TTS 字符/Music 调用计数,防止超额
- 🔁 **异步 + 重试**: tenacity 指数退避,LLM/TTS 最多 3 次重试
- 🌐 **REST API + OpenAPI**: FastAPI 自动生成 docs
- 🧪 **58 个测试**: 100% 覆盖核心业务流

## 快速开始

### 1. 安装依赖
```bash
uv venv .venv --python 3.11
uv pip install -r requirements.txt
```

### 2. 配置 API key
```bash
cp .env.example .env
# 编辑 .env 填入 MINIMAX_CN_API_KEY
```

### 3. 启动服务
```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
```

### 4. 生成播客
```bash
curl -X POST http://127.0.0.1:8765/api/v1/podcasts \
  -H "Content-Type: application/json" \
  -d '{"topic": "AI编程简史", "rounds": 3}'
```

返回示例:
```json
{
  "episode_id": "ep_20260808_153000_a1b2c3",
  "topic": "AI编程简史",
  "duration_sec": 77.2,
  "size_bytes": 1193000,
  "usage_chars": 672,
  "download_url": "/api/v1/podcasts/ep_.../audio"
}
```

## Docker

```bash
docker build -t podcast-agent .
docker run -p 8765:8000 -v $(pwd)/data:/app/data podcast-agent
# 或
docker compose up
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/podcasts` | 同步生成播客 |
| `GET` | `/api/v1/podcasts` | 列出所有 episodes |
| `GET` | `/api/v1/podcasts/{id}` | episode 详情 |
| `GET` | `/api/v1/podcasts/{id}/audio` | 下载 mp3 |
| `GET` | `/api/v1/podcasts/{id}/transcript` | 逐字稿 JSON |
| `GET` | `/api/v1/voices` | 可选音色列表 |
| `GET` | `/api/v1/quota` | 配额使用情况 |

Swagger 文档: <http://localhost:8765/docs>

## 测试

```bash
.venv/bin/pytest                    # 全部
.venv/bin/pytest tests/test_api.py  # 仅 API 集成测试
.venv/bin/pytest -k mixer           # 仅混音测试
```

## 架构

```
HTTP Request
     ↓
[API Gateway] (FastAPI + 校验)
     ↓
[Orchestrator]   ←── 状态机 (SQLite)
     ↓                ↓
[LLM]→剧本    [TTS]→音频  [Music]→BGM
     ↓                ↓
[Mixer] ←─────────────┘
     ↓
最终 MP3 (16 LUFS)
```

详见 `app/` 目录结构。

## 已知限制 (v0.1)

- 同步端点适合短剧集 (<3 分钟),长剧集应改异步任务队列 (P7 之后)
- Music API 接口定义完成但未在 v0.1 端到端验证 (P7 验收时跑)
- 无鉴权 — 生产部署需要加 API Key / OAuth
- 单 SQLite 文件 — 高并发需要 Postgres (v2)