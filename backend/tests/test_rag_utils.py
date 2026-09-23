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


def test_tessdata_probe_disabled():
    """PDF 解析前应短路 pymupdf 的 Tesseract 探测。

    否则每份 PDF 会起两次子进程（tesseract --list-langs / where tesseract），
    中文 Windows 下输出 GBK 而进程处于 UTF-8 模式 → 读取线程刷
    UnicodeDecodeError traceback（结果不受影响，但日志噪音且白耗两次进程启动）。
    """
    from app.rag.loader import _silence_tessdata_probe

    _silence_tessdata_probe()
    import pymupdf

    assert getattr(pymupdf, "_zhiwen_no_tessdata", False) is True
    assert pymupdf.get_tessdata() is None  # 不再触发子进程探查
