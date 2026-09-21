"""记忆召回与去重单测。

- 召回：只在"零命中"时用最近记忆兜底（不再补位凑满 top_k，避免每轮
  注入无关记忆 + D 块每轮变化）；
- 去重：对全库比较（旧行为只比最近 10 条，更早的近似重复会漏判）。
"""

from __future__ import annotations

import pytest

from app.agent import context as context_mod
from app.agent.context import ContextService


@pytest.fixture(autouse=True)
def _clear_memory_vec_cache():
    """向量缓存是模块级的，用例之间必须隔离（否则上一条用例的向量被复用）。"""
    context_mod._memory_vec_cache.clear()
    yield
    context_mod._memory_vec_cache.clear()


class FakeSettings:
    memory_enabled = True
    memory_top_k = 3
    memory_min_score = 0.35
    memory_max_tokens = 600
    memory_candidate_limit = 100
    memory_recent_fallback = 50
    memory_empty_fallback = 2


class FakeEmbeddings:
    """按文本查表的假 embedding。

    查询默认 [0,1]、文档默认 [1,0]（互相正交 → 相似度 0）；
    登记过的文本按表取值（同一文本既作查询又作文档时取同向 → 相似度 1）。
    """

    def __init__(self, mapping: dict[str, list[float]] | None = None):
        self.mapping = mapping or {}

    def embed_query(self, text: str) -> list[float]:
        return self.mapping.get(text, [0.0, 1.0])

    def embed_documents(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return [self.mapping.get(t, [1.0, 0.0]) for t in texts]


def _memories(n: int = 5) -> list[dict]:
    return [
        {
            "id": i + 1,
            "content": f"记忆内容 {i + 1}",
            "category": "other",
            "updated_at": f"2026-09-21T10:00:0{i}",
        }
        for i in range(n)
    ]


def test_recall_zero_hit_uses_recent_fallback(monkeypatch):
    monkeypatch.setattr(context_mod.repo, "list_memories", lambda db, limit=500: _memories())
    service = ContextService(FakeSettings(), chat=None)
    emb = FakeEmbeddings()  # 全部正交 → 零命中

    hits = service.retrieve_memories(None, emb, "完全不相关的问题")
    assert hits == ["记忆内容 1", "记忆内容 2"]  # 最近 2 条兜底


def test_recall_with_hits_does_not_pad_to_top_k(monkeypatch):
    """有命中时不再用最近记忆补满 top_k（旧行为会补齐 3 条）。"""
    monkeypatch.setattr(context_mod.repo, "list_memories", lambda db, limit=500: _memories())
    service = ContextService(FakeSettings(), chat=None)
    emb = FakeEmbeddings({"记忆内容 3": [0.0, 1.0]})  # 只有第 3 条与问题同向

    hits = service.retrieve_memories(None, emb, "记忆内容 3")
    assert hits == ["记忆内容 3"]  # 1 条命中就是 1 条，不补位


def test_recall_respects_token_budget(monkeypatch):
    monkeypatch.setattr(
        context_mod.repo,
        "list_memories",
        lambda db, limit=500: [{"id": 1, "content": "很长" * 200}],
    )
    settings = FakeSettings()
    settings.memory_max_tokens = 50  # 预算很小 → 超预算的内容被裁掉
    service = ContextService(settings, chat=None)

    assert service.retrieve_memories(None, FakeEmbeddings(), "问题") == []


def test_recall_counts_hits_for_kept_memories(monkeypatch):
    """被注入的记忆要累加命中计数（衰减与整合归档的依据）。"""
    monkeypatch.setattr(context_mod.repo, "list_memories", lambda db, limit=500: _memories())
    calls: list[list[int]] = []
    monkeypatch.setattr(context_mod.repo, "bump_memory_hits", lambda db, ids: calls.append(list(ids)))
    service = ContextService(FakeSettings(), chat=None)
    emb = FakeEmbeddings({"记忆内容 3": [0.0, 1.0]})

    hits = service.retrieve_memories(object(), emb, "记忆内容 3")
    assert hits == ["记忆内容 3"]
    assert calls == [[3]]  # 只记真正注入的那条

    # 零命中兜底不算命中（不是"相关"，只是背景）
    calls.clear()
    context_mod._memory_vec_cache.clear()  # 换一套互不相同的向量空间，避免缓存干扰
    service.retrieve_memories(object(), FakeEmbeddings(), "完全无关的问题")
    assert calls == []


def test_recall_reuses_cached_vectors(monkeypatch):
    """第二轮召回不应重新向量化整库（缓存按 id+updated_at 命中）。"""
    monkeypatch.setattr(context_mod.repo, "list_memories", lambda db, limit=500: _memories())
    monkeypatch.setattr(context_mod.repo, "bump_memory_hits", lambda db, ids: None)
    service = ContextService(FakeSettings(), chat=None)

    class CountingEmbeddings(FakeEmbeddings):
        def __init__(self):
            super().__init__({"记忆内容 1": [0.0, 1.0]})
            self.doc_calls = 0

        def embed_documents(self, texts, batch_size=64):
            self.doc_calls += 1
            return super().embed_documents(texts, batch_size)

    emb = CountingEmbeddings()
    service.retrieve_memories(object(), emb, "记忆内容 1")
    assert emb.doc_calls == 1
    service.retrieve_memories(object(), emb, "记忆内容 1")
    assert emb.doc_calls == 1  # 第二次全部命中缓存


def test_recall_finds_semantic_match_without_keyword_overlap(monkeypatch):
    """换说法也能召回（旧 n-gram 粗筛的短板）：问题与记忆无共同字面词。"""
    memories = [
        {"id": 7, "content": "用户习惯用 PyCharm 写 Python", "category": "preference",
         "updated_at": "2026-09-21T10:00:00"},
        {"id": 8, "content": "用户住在成都", "category": "profile",
         "updated_at": "2026-09-21T10:00:01"},
    ]
    monkeypatch.setattr(context_mod.repo, "list_memories", lambda db, limit=500: memories)
    monkeypatch.setattr(context_mod.repo, "bump_memory_hits", lambda db, ids: None)
    # 假向量：把"IDE 用什么好"与 PyCharm 那条映射到同一方向
    emb = FakeEmbeddings({
        "IDE 用什么好": [0.0, 1.0],
        "用户习惯用 PyCharm 写 Python": [0.0, 1.0],
    })
    service = ContextService(FakeSettings(), chat=None)

    hits = service.retrieve_memories(object(), emb, "IDE 用什么好")
    assert hits == ["用户习惯用 PyCharm 写 Python"]


def test_is_duplicate_detects_match_beyond_recent_window():
    """全库比对：与第 1 条向量相同（位于"最近 10 条"之外）也能识别。"""
    texts = [f"记忆{i}" for i in range(15)]
    vecs = [[0.0, 1.0] for _ in texts]
    vecs[0] = [1.0, 0.0]
    emb = FakeEmbeddings({"新事实": [1.0, 0.0]})

    assert ContextService._is_duplicate("新事实", texts, vecs, emb) is True
    # 对照旧行为（只比最近 10 条）会漏判：
    assert ContextService._is_duplicate("新事实", texts, vecs[-10:], emb) is False


def test_is_duplicate_exact_text_and_empty():
    emb = FakeEmbeddings()
    assert ContextService._is_duplicate("一样的", ["一样的"], [], emb) is True
    assert ContextService._is_duplicate("新的", [], [], emb) is False
