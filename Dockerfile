# 后端镜像：lite（默认，纯 API 模式）/ full（本地 BGE 模型）两档
#   lite: docker build --build-arg PROFILE=lite .
#   full: docker build --build-arg PROFILE=full .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖层（独立缓存：代码变更不触发重装）
ARG PROFILE=lite
COPY backend/requirements.txt backend/requirements-lite.txt ./
RUN pip install -r requirements-lite.txt \
    && if [ "$PROFILE" = "full" ]; then pip install torch sentence-transformers; fi

# 代码层
COPY backend/app ./app
COPY backend/run.py ./run.py

EXPOSE 8000

# 生产模式直接跑 uvicorn（热重载只用于本机开发）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
