"""本地 BGE Embedding 封装（复用 day5_2 的思路，改为懒加载）。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

from .utils import detect_device, is_cuda_error

logger = logging.getLogger(__name__)

# BGE 官方推荐的查询前缀：让查询向量与文档向量的分布更接近
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文档："

# CUDA 失败后冷却时间（秒）：冷却期内直接走 CPU，冷却后自动探测 GPU 是否恢复
CUDA_RETRY_AFTER = 300.0


class LocalBGEEmbeddings:
    """适配 LangChain 接口的本地 BGE 嵌入模型。"""

    def __init__(
        self,
        model_dir: Path,
        device: str | None = None,
        use_fp16: bool | None = None,
    ):
        self.model_dir = Path(model_dir)
        self.device = device or detect_device()
        # CUDA 下默认 fp16：实测 4060 上 bge-base 嵌入提速约 3 倍，向量质量基本无损失
        if use_fp16 is None:
            use_fp16 = self.device.startswith("cuda")
        self.use_fp16 = use_fp16
        self._fp16_requested = bool(use_fp16)
        self._model: SentenceTransformer | None = None
        self._dimension: int | None = None
        self._cuda_failed_at: float | None = None

    def _ensure_model(self) -> SentenceTransformer:
        """首次使用时才加载模型，避免无谓启动开销。"""
        if self._model is None:
            self._maybe_retry_cuda()
            self._model = SentenceTransformer(str(self.model_dir), device=self.device)
            if self.use_fp16 and self.device.startswith("cuda"):
                self._model = self._model.half()
                logger.info("Embedding 模型已启用 fp16（%s）", self.device)
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
            # 小探针：真正触发一次 CUDA kernel，验证上下文未被污染
            probe = torch.zeros(1, device="cuda")
            _ = (probe @ probe).item()
            self.device = "cuda"
            self.use_fp16 = self._fp16_requested
            self._model = None
            self._cuda_failed_at = None
            logger.info("Embedding：CUDA 恢复探测成功，已切回 GPU")
        except Exception:
            # 上下文仍被污染：重新计时，继续用 CPU
            self._cuda_failed_at = time.time()

    def _fallback_to_cpu(self, exc: Exception) -> None:
        """CUDA 出错后降级：释放 GPU 模型，切到 CPU，后续请求直接走 CPU。"""
        old_device = self.device
        self.device = "cpu"
        self.use_fp16 = False
        if self._model is not None:
            try:
                del self._model
                torch.cuda.empty_cache()
            except Exception:
                pass
        self._model = None
        logger.error(
            "Embedding CUDA 推理失败，已自动降级到 CPU（%s 秒后自动探测恢复）：%s",
            CUDA_RETRY_AFTER,
            exc,
        )
        self._cuda_failed_at = time.time()

    @property
    def dimension(self) -> int:
        """向量维度（bge-base-zh-v1.5 为 768），用于创建索引映射。"""
        if self._dimension is None:
            self._dimension = len(self.embed_query("维度探测"))
        return self._dimension

    def embed_query(self, text: str) -> list[float]:
        """生成查询向量（带 BGE 前缀 + 归一化）。"""
        try:
            model = self._ensure_model()
            vector = model.encode(
                QUERY_PREFIX + text.strip(),
                normalize_embeddings=True,
                batch_size=1,
                show_progress_bar=False,
            )
            return vector.tolist()
        except Exception as exc:
            if self.device.startswith("cuda") and is_cuda_error(exc):
                self._fallback_to_cpu(exc)
                return self.embed_query(text)
            raise

    def embed_documents(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """批量生成文档向量。"""
        if not texts:
            return []
        cleaned = [text.strip() for text in texts if text.strip()]
        if not cleaned:
            return []
        try:
            model = self._ensure_model()
            vectors = model.encode(
                cleaned,
                normalize_embeddings=True,
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            return vectors.tolist()
        except Exception as exc:
            if self.device.startswith("cuda") and is_cuda_error(exc):
                self._fallback_to_cpu(exc)
                return self.embed_documents(texts, batch_size=batch_size)
            raise


class APIBGEEmbeddings:
    """OpenAI 兼容 API 嵌入（/v1/embeddings），接口与 LocalBGEEmbeddings 一致。

    适用于硅基流动（BAAI/bge-m3 等）、OpenAI（text-embedding-3-small）等
    提供 OpenAI 兼容嵌入端点的供应商。本地无 GPU / 不想占显存时替代本地 BGE。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "",
        timeout: float = 60.0,
        max_batch: int = 64,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""  # 空 = 使用供应商默认模型
        self.timeout = timeout
        self.max_batch = max_batch
        self._dimension: int | None = None

    @property
    def device(self) -> str:
        return "api"

    @property
    def dimension(self) -> int:
        """向量维度：首次调用探测（用一次真实嵌入）。"""
        if self._dimension is None:
            self._dimension = len(self.embed_query("维度探测"))
        return self._dimension

    def _post(self, payload: dict) -> dict:
        # 代理回退直连：远端 embedding API 不因系统代理未启动而 10061
        from ..network import make_httpx_client

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = dict(payload)
        if self.model:
            body["model"] = self.model
        resp = make_httpx_client(timeout=self.timeout).post(
            f"{self.base_url}/embeddings",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    def embed_query(self, text: str) -> list[float]:
        data = self._post({"input": [text]})
        try:
            return data["data"][0]["embedding"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"嵌入 API 响应格式异常：{data}") from exc

    def embed_documents(
        self, texts: list[str], batch_size: int | None = None
    ) -> list[list[float]]:
        """批量嵌入（按 max_batch 分批请求，保持输入顺序）。"""
        cleaned = [t.strip() for t in (texts or []) if t and t.strip()]
        if not cleaned:
            return []
        batch = batch_size or self.max_batch
        out: list[list[float]] = []
        for start in range(0, len(cleaned), batch):
            chunk = cleaned[start : start + batch]
            data = self._post({"input": chunk})
            items = data.get("data") or []
            # 供应商可能乱序返回：按 index 排序，保证顺序一致
            items.sort(key=lambda x: int(x.get("index", 0)))
            for item in items:
                vec = item.get("embedding")
                if vec is None:
                    raise RuntimeError(f"嵌入 API 响应缺少 embedding：{item}")
                out.append(vec)
        return out
