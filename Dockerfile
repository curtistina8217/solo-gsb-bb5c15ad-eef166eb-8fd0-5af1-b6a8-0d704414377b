FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/subtitles.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时由 lifespan 自动初始化 SQLite 数据库
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
