# 后端镜像：lite（默认，纯 API 模式）/ full（本地 BGE 模型）两档
#   lite: docker build --build-arg PROFILE=lite .
#   full: docker build --build-arg PROFILE=full .
#   PIP_INDEX_URL: 基础依赖镜像加速（可选，如 https://pypi.tuna.tsinghua.edu.cn/simple）
#
# 分层策略（lite/full 切换不触发重装）：
#   1) 基础依赖层（两种模式共用，requirements 不变即命中缓存，与 PROFILE 无关）
#   2) full 深度学习层：torch 由 torch_wheels/ 离线安装（wheel 下载见
#      scripts/download_torch_wheels.sh），独立命令、独立层
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ---------- 基础依赖层 ----------
ARG PIP_INDEX_URL=
COPY backend/requirements.txt backend/requirements-lite.txt ./
RUN pip install -r requirements-lite.txt ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"}

# ---------- full 深度学习层（torch 离线 wheel + nvidia 拆包走 index） ----------
# torch 主 wheel（含 CUDA 主库）已由 scripts/download_torch_wheels.sh 预下载；
# 官方自 2.6 起把 nvidia-*（cudnn/cublas 等）拆成独立包，保存在 index 上，
# 构建容器直连 PIP_INDEX_URL 拉取（国内镜像直连，无需宿主代理）。
ARG PROFILE=lite
COPY torch_wheels/ /torch_wheels
RUN if [ "$PROFILE" = "full" ]; then \
      pip install --find-links=/torch_wheels torch ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
      && pip install sentence-transformers ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
      && rm -rf /torch_wheels; \
    fi

# ---------- 代码层 ----------
COPY backend/app ./app
COPY backend/run.py ./run.py

EXPOSE 8000

# 生产模式直接跑 uvicorn（热重载只用于本机开发）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
