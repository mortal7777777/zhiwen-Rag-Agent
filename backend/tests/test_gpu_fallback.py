"""CUDA 异步错误自动降级 CPU + 重试逻辑（用 fake 模型，不真正跑 GPU）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from langchain_core.documents import Document

from app.rag.embeddings import LocalBGEEmbeddings
from app.rag.reranker import LocalReranker


class FakeEmbed:
    def __init__(self):
        self.fail = True

    def encode(self, *args, **kwargs):
        if self.fail:
            self.fail = False
            raise RuntimeError("CUDA error: unknown error")
        return np.array([[0.1] * 768])


def test_embedding_falls_back_to_cpu():
    emb = LocalBGEEmbeddings(Path("."), device="cuda")
    fake = FakeEmbed()
    emb._ensure_model = lambda: fake
    vec = emb.embed_query("test")
    assert len(vec) == 1 and len(vec[0]) == 768
    assert emb.device == "cpu"
    assert emb._cuda_failed_at is not None


class FakeRerank:
    def __init__(self):
        self.fail = True

    def predict(self, pairs, **kwargs):
        if self.fail:
            self.fail = False
            raise RuntimeError("CUDA error: unknown error")
        return [1.0] * len(pairs)


def test_reranker_falls_back_to_cpu():
    rr = LocalReranker(Path("."), device="cuda")
    fake = FakeRerank()
    rr._ensure_model = lambda: fake
    out = rr.rerank(
        "q",
        [Document(page_content="a"), Document(page_content="b")],
        top_k=1,
    )
    assert len(out) == 1
    assert rr.device == "cpu"
