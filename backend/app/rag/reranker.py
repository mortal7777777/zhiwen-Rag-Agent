"""本地 BGE Reranker 封装（CrossEncoder），模型懒加载且只加载一次。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import torch
from langchain_core.documents import Document
from sentence_transformers import CrossEncoder

from .utils import detect_device, is_cuda_error

logger = logging.getLogger(__name__)

# CUDA 失败后冷却时间（秒）：冷却期内直接走 CPU，冷却后自动探测 GPU 是否恢复
CUDA_RETRY_AFTER = 300.0


def resolve_snapshot_dir(cache_dir: Path) -> Path:
    """从 HuggingFace 缓存目录结构解析真正的模型快照目录。

    models--BAAI--bge-reranker-v2-m3/
      ├── refs/main            -> commit 哈希
      └── snapshots/<commit>/config.json
    """
    cache_dir = Path(cache_dir)
    refs_main = cache_dir / "refs" / "main"
    if refs_main.exists():
        snapshot = refs_main.read_text(encoding="utf-8").strip()
        model_dir = cache_dir / "snapshots" / snapshot
        if model_dir.exists():
            return model_dir
    snapshots = sorted((cache_dir / "snapshots").glob("*"))
    if snapshots:
        return snapshots[0]
    raise FileNotFoundError(f"本地重排序模型不存在：{cache_dir}")


class LocalReranker:
    """对候选文本做逐对打分精排（cross-encoder）。"""

    def __init__(self, cache_dir: Path, device: str | None = None):
        self.cache_dir = Path(cache_dir)
        self.device = device or detect_device()
        self._model: CrossEncoder | None = None
        self._cuda_failed_at: float | None = None

    def _ensure_model(self) -> CrossEncoder:
        if self._model is None:
            self._maybe_retry_cuda()
            model_dir = resolve_snapshot_dir(self.cache_dir)
            self._model = CrossEncoder(str(model_dir), device=self.device)
        return self._model

    def _maybe_retry_cuda(self) -> None:
        """冷却期后探测 CUDA 是否恢复；恢复则切回 GPU 并重建模型。"""
        if self._cuda_failed_at is None or self.device.startswith("cuda"):
            return
        if time.time() - self._cuda_failed_at < CUDA_RETRY_AFTER:
            return
        try:
            if not torch.cuda.is_available():
                return
            probe = torch.zeros(1, device="cuda")
            _ = (probe @ probe).item()
            self.device = "cuda"
            self._model = None
            self._cuda_failed_at = None
            logger.info("Reranker：CUDA 恢复探测成功，已切回 GPU")
        except Exception:
            self._cuda_failed_at = time.time()

    def _fallback_to_cpu(self, exc: Exception) -> None:
        """CUDA 出错后降级：释放 GPU 模型，切到 CPU，后续请求直接走 CPU。"""
        old_device = self.device
        self.device = "cpu"
        if self._model is not None:
            try:
                del self._model
                torch.cuda.empty_cache()
            except Exception:
                pass
        self._model = None
        self._cuda_failed_at = time.time()
        logger.error(
            "Reranker CUDA 推理失败，已自动降级到 CPU（%s 秒后自动探测恢复）：%s",
            CUDA_RETRY_AFTER,
            exc,
        )

    def rerank(
        self,
        query: str,
        documents: list[Document],
        top_k: int = 4,
    ) -> list[Document]:
        """把 query 与每个候选逐对打分，按分数降序保留前 top_k 条。"""
        if not documents:
            return []
        try:
            model = self._ensure_model()
            pairs = [(query, doc.page_content) for doc in documents]
            scores = model.predict(pairs, batch_size=8, show_progress_bar=False)
            ranked = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
            result: list[Document] = []
            for doc, score in ranked[:top_k]:
                doc.metadata["rerank_score"] = round(float(score), 4)
                result.append(doc)
            return result
        except Exception as exc:
            if self.device.startswith("cuda") and is_cuda_error(exc):
                self._fallback_to_cpu(exc)
                return self.rerank(query, documents, top_k=top_k)
            raise
