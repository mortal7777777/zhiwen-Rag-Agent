"""RAG 通用工具单测：设备检测与 CUDA 错误识别。"""

from __future__ import annotations

import pytest

from app.rag.utils import detect_device, is_cuda_error


@pytest.mark.parametrize(
    "msg",
    [
        "CUDA error: unknown error",
        "CUDA error: out of memory",
        "CUDA error: an illegal memory access was encountered",
        "CUDA error: device-side assert triggered",
    ],
)
def test_is_cuda_error_positive(msg):
    assert is_cuda_error(RuntimeError(msg))


def test_is_cuda_error_negative():
    assert not is_cuda_error(RuntimeError("some other failure"))
    assert not is_cuda_error(ValueError("bad input"))


def test_detect_device():
    assert detect_device() in ("cuda", "cpu")
