# 后端镜像：lite（默认 target，纯 API 模式）/ full（本地 BGE 模型 + GPU）
#   lite: docker compose up -d --build（compose 默认 target: lite）
#   full: docker compose -f docker-compose.yml -f docker-compose.full.yml up -d --build
#   PIP_INDEX_URL: 基础依赖镜像加速（可选，如 https://pypi.tuna.tsinghua.edu.cn/simple）
#
# 分层策略（lite/full 切换不触发重装，且 lite 构建完全不接触 torch）：
#   1) base 层：基础依赖（两种模式共用，requirements 不变即命中缓存）
#   2) full 层：torch 优先用 torch_wheels/ 离线 wheel（scripts/download_torch_wheels.sh
#      预下载；目录仅需存在，可为空——为空时自动回退 PIP_INDEX_URL 在线安装）
#
# 为什么拆 target：torch_wheels/ 未入库（约 867MB，gitignore）。旧版无条件
# COPY torch_wheels 让全新 clone 的 lite 构建也直接失败、且有 wheel 时会把
# 867MB 打进 lite 镜像（2026-10-14 审计修复，CI 有对应守门 job）。
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ---------- base：基础依赖层 ----------
# pip 缓存挂载：依赖层失效/构建失败重试时，已下载的包跨构建复用（缓存在构建器，
# 不进镜像层）。注意 ENV 里的 PIP_NO_CACHE_DIR=1 会让 pip 无视缓存目录，须临时解除。
ARG PIP_INDEX_URL=
COPY backend/requirements.txt backend/requirements-lite.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR pip install -r requirements-lite.txt ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"}

# ---------- lite：默认运行镜像（不含 torch / 本地模型）----------
FROM base AS lite
COPY backend/app ./app
COPY backend/run.py ./run.py
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------- full：在 base 上加 torch 深度学习层 ----------
FROM base AS full
ARG PIP_INDEX_URL=
COPY torch_wheels/ /torch_wheels
RUN --mount=type=cache,target=/root/.cache/pip \
    env -u PIP_NO_CACHE_DIR sh -c "pip install --find-links=/torch_wheels torch ${PIP_INDEX_URL:+--index-url \"$PIP_INDEX_URL\"} \
    && pip install --find-links=/torch_wheels sentence-transformers==6.0.1 ${PIP_INDEX_URL:+--index-url \"$PIP_INDEX_URL\"} \
    && rm -rf /torch_wheels"
COPY backend/app ./app
COPY backend/run.py ./run.py
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
