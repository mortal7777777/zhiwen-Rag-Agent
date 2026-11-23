"""嵌入输出必须与入参一一对应：空白文本不能悄悄缩短向量列表。"""

from __future__ import annotations

from pathlib import Path

from app.rag.embeddings import APIBGEEmbeddings, LocalBGEEmbeddings


class _Vectors(list):
    def tolist(self):
        return list(self)


class _FakeLocalModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, **kwargs):
        self.calls.append(list(texts))
        return _Vectors([0.1, 0.2, 0.3] for _ in texts)


def test_local_embed_documents_keeps_blank_slots():
    emb = LocalBGEEmbeddings(Path("unused"), device="cpu")
    fake = _FakeLocalModel()
    emb._model = fake  # 跳过真实模型加载
    vectors = emb.embed_documents(["a", "   ", ""])
    assert len(vectors) == 3
    assert fake.calls == [["a", " ", " "]]


def test_api_embed_documents_keeps_blank_slots():
    emb = APIBGEEmbeddings("http://example.invalid", "k")
    seen: list[list[str]] = []

    def fake_post(payload: dict) -> dict:
        seen.append(list(payload["input"]))
        return {
            "data": [
                {"index": i, "embedding": [0.1, 0.2]}
                for i in range(len(payload["input"]))
            ]
        }

    emb._post = fake_post  # type: ignore[method-assign]
    vectors = emb.embed_documents(["a", "   ", "b"])
    assert len(vectors) == 3
    assert seen == [["a", " ", "b"]]
