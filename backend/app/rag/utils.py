"""通用小工具：设备检测、中文分词。"""

from __future__ import annotations

try:
    import torch
except ImportError:  # lite 模式（纯 API 嵌入/重排）不安装 torch
    torch = None  # type: ignore[assignment]


def detect_device() -> str:
    """自动选择运行设备：有 CUDA 用 GPU，否则用 CPU。"""
    return "cuda" if torch is not None and torch.cuda.is_available() else "cpu"


def is_cuda_error(exc: Exception) -> bool:
    """判断异常是否为 CUDA 相关错误（含异步 kernel 报错）。

    CUDA 的"unknown error / illegal memory access"是异步内核错误，
    一旦出现会污染整个 CUDA 上下文，后续调用持续失败直到进程重启；
    因此上层需要识别这类错误并降级到 CPU。
    """
    if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    msg = str(exc).lower()
    return (
        "cuda" in msg
        and (
            "error" in msg
            or "out of memory" in msg
            or "illegal memory" in msg
            or "device-side assert" in msg
        )
    )


def jieba_tokenize(text: str) -> list[str]:
    """jieba 搜索引擎模式分词，供 BM25 检索和索引使用。

    OpenSearch 默认没有中文分词插件，所以把词用空格切开，
    配合 whitespace 分词器实现"词级 BM25"。
    """
    import jieba  # 延迟导入，避免不使用时拖慢启动

    if not text:
        return []
    return [token for token in jieba.cut_for_search(text) if token.strip()]
