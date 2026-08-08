FROM python:3.11-slim

# 系统依赖: ffmpeg + libsndfile (用于音频处理)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖,利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY app/ ./app/
COPY pytest.ini ./

# 数据和输出挂载点
RUN mkdir -p /app/data /app/output
VOLUME ["/app/data", "/app/output"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=2).raise_for_status()" \
    || exit 1

EXPOSE 8000

# 生产模式: 单 worker, 多进程交给 gunicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]