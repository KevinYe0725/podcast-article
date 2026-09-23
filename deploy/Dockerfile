# 服务器部署镜像：只装 Web 需要的东西，**不装转写后端**
#
# 为什么：服务器（2 核 / 1.8G / 无 GPU）只负责「看」——文章、检索、时间戳回听、导出。
# 生成与转写在你自己的 Mac 上跑（mlx-whisper，Apple Silicon，约 38x 实时）。
# 服务器使用云端 ASR，不在 2 核主机上安装本地 Whisper。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖再拷代码：以后改代码不会让依赖层失效，重建只要几秒
COPY pyproject.toml README.md ./
COPY podcast_article ./podcast_article
RUN pip install --no-cache-dir .

# 前端与入口
COPY web ./web
COPY webapp.py ./

# 数据目录（compose 里挂卷）：文章、库、队列、订阅都在这里，容器删了也不丢
ENV PA_OUTPUT_DIR=/data/output \
    PA_LIBRARY_FILE=/data/library.json \
    PA_QUEUE_FILE=/data/queue.json \
    PA_FEEDS_FILE=/data/feeds.json \
    PA_READONLY=0 \
    PA_ASR_BACKEND=cloud \
    PA_SCHEDULER=0
RUN mkdir -p /data/output

EXPOSE 8787

# 容器内监听 8787，由 Caddy 反代；对外不发布端口（compose 里只绑 127.0.0.1）
CMD ["python", "webapp.py", "--host", "0.0.0.0", "--port", "8787"]
