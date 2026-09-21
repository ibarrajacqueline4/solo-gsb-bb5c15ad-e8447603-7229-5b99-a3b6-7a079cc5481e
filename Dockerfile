FROM python:3.11-slim

# 不生成 .pyc、不缓冲 stdout，便于容器内观察日志
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OR-Tools 运行时依赖（slim 镜像缺 libgomp 等共享库）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libprotobuf32 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据库文件所在目录（compose 会挂载命名卷到这里）
RUN mkdir -p /data
ENV PLANNER_DB=/data/planner.db

EXPOSE 8000

# 建表在应用启动（lifespan）时自动执行
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
