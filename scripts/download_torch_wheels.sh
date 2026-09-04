#!/usr/bin/env bash
# 下载 full 模式所需的 torch（CUDA 版）wheel 到 torch_wheels/（进入 Docker 构建上下文）。
#
# 为什么要离线 wheel：Docker 构建容器无法使用宿主机代理，从公共 PyPI 镜像
# 下载 nvidia-* 分解包（cudnn 等）在国内网络下极慢；宿主机走代理下载好再离线安装，
# 又快又稳，且 torch 层与 lite 基础层完全解耦。
#
# 用法（宿主机，代理默认 http://127.0.0.1:7890，可通过 PROXY 覆盖）：
#   bash scripts/download_torch_wheels.sh

set -e
cd "$(dirname "$0")/.."
mkdir -p torch_wheels

PROXY="${PROXY:-http://127.0.0.1:7890}"
export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" NO_PROXY="localhost,127.0.0.1"

PY_VER="${PY_VER:-311}"   # 容器内 python 3.11 → 311
PLAT="${PLAT:-manylinux_2_28_x86_64}"
CUDA_INDEX="${CUDA_INDEX:-https://download.pytorch.org/whl/cu128}"

echo ">>> 下载 torch（CUDA wheel，约 2.5GB，走代理 $PROXY）"
pip download torch --index-url "$CUDA_INDEX" \
  --platform "$PLAT" --python-version "$PY_VER" \
  --only-binary=:all: --no-deps -d torch_wheels

echo ">>> 下载 sentence-transformers（本体，依赖安装时从 PIP_INDEX_URL 拉取）"
pip download sentence-transformers --no-deps --only-binary=:all: -d torch_wheels

echo ">>> 完成："
ls -la torch_wheels
