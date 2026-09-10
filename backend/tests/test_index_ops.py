"""索引运维单测：文档内寻址 ID、per-doc 删除请求、别名切换、增量应用流程。

不依赖 GPU / OpenSearch / 网络：store 用桩 HTTP 客户端，service 用桩 store。
"""

from __future__ import annotations

import json

from langchain_core.documents import Document

from app.config import Settings
from app.rag.service import RAGService
from app.rag.store import OpenSearchStore, chunk_doc_id


# ---------------- chunk_doc_id（文档内内容寻址） ----------------


def test_chunk_doc_id_scoped_by_document():
    content = "同一段内容"
    assert chunk_doc_id("a.pdf", content) == chunk_doc_id("a.pdf", content)  # 稳定
    assert chunk_doc_id("a.pdf", content) != chunk_doc_id("b.pdf", content)  # 跨文件独立
    assert chunk_doc_id("", content) != chunk_doc_id("a.pdf", content)  # 无路径退化


# ---------------- store：桩 HTTP 客户端 ----------------


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RecorderClient:
    """记录请求的桩客户端；alias_map 模拟 GET /_alias/{name} 的响应。"""

    def __init__(self, alias_map: dict | None = None):
        self.calls: list[tuple] = []
        self.alias_map = alias_map or {}

    def request(self, method: str, path: str, **kw):
        self.calls.append((method, path, kw))
        return _Resp()

    def get(self, path: str):
        self.calls.append(("GET", path, {}))
        data = self.alias_map.get(path)
        if data is None:
            return _Resp(status=404)
        return _Resp(payload=data)

    def head(self, path: str):
        self.calls.append(("HEAD", path, {}))
        return _Resp(status=404)

    def post(self, path: str, **kw):
        self.calls.append(("POST", path, kw))
        return _Resp()


def _store_with(client: _RecorderClient) -> OpenSearchStore:
    store = OpenSearchStore(url="http://stub:9200", index_name="kb_alias", dimension=8)
    store.client = client
    return store


def test_delete_by_document_targets_relative_path_field():
    client = _RecorderClient()
    store = _store_with(client)
    store.delete_by_document("子目录/书.pdf")
    method, path, kw = client.calls[-1]
    assert method == "POST"
    assert "/_delete_by_query" in path and "refresh=true" in path
    assert kw["json"] == {"query": {"term": {"metadata_relative_path": "子目录/书.pdf"}}}


def test_swap_alias_removes_old_and_adds_new():
    client = _RecorderClient(alias_map={"/_alias/kb_alias": {"kb_alias_old": {}}})
    store = _store_with(client)
    store.swap_alias("kb_alias_new")
    method, path, kw = client.calls[-1]
    assert (method, path) == ("POST", "/_aliases")
    actions = kw["json"]["actions"]
    assert {"remove": {"index": "kb_alias_old", "alias": "kb_alias"}} in actions
    assert {"add": {"index": "kb_alias_new", "alias": "kb_alias"}} in actions


def test_swap_alias_first_time_only_adds():
    client = _RecorderClient()  # 别名不存在
    store = _store_with(client)
    store.swap_alias("kb_alias_new")
    actions = client.calls[-1][2]["json"]["actions"]
    assert actions == [{"add": {"index": "kb_alias_new", "alias": "kb_alias"}}]


# ---------------- service：增量应用流程（删/改/增 + 账本同步） ----------------


class _StubStore:
    """只记录调用的桩 store（不触 OpenSearch）。"""

    def __init__(self):
        self.deleted: list[str] = []
        self.bulk_sizes: list[int] = []
        self.count = 0

    def delete_by_document(self, relative_path: str, **kw) -> int:
        self.deleted.append(relative_path)
        return 2

    def bulk_index(self, chunks, embeddings, batch_size=64, target=None) -> int:
        self.bulk_sizes.append(len(chunks))
        self.count += len(chunks)
        return len(chunks)

    def doc_count(self, index=None) -> int:
        return self.count


class _FakeEmbeddings:
    dimension = 8

    def embed_documents(self, texts, batch_size=64):
        return [[0.0] * self.dimension for _ in texts]


def _service(tmp_path) -> RAGService:
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    service = RAGService(settings)
    service._embeddings = _FakeEmbeddings()
    service._loader_kwargs = lambda: {"layout_pdf": False, "pdf_workers": 1}
    return service


def test_apply_incremental_delete_modify_add(tmp_path):
    service = _service(tmp_path)
    data_dir = service.settings.data_dir
    (data_dir / "keep.md").write_text("保留的内容。" * 30, encoding="utf-8")
    (data_dir / "mod.md").write_text("修改后的内容。" * 30, encoding="utf-8")
    (data_dir / "new.md").write_text("新增的内容。" * 30, encoding="utf-8")

    file_fps = service._compute_file_fingerprints()
    old_fps = {
        "keep.md": file_fps["keep.md"],
        "mod.md": "old-fingerprint",
        "gone.md": "gone-fingerprint",
    }
    service._save_meta(
        old_fps,
        [
            Document(page_content="gone parent", metadata={"relative_path": "gone.md"}),
            Document(page_content="mod parent", metadata={"relative_path": "mod.md"}),
        ],
        child_count=2,
    )

    store = _StubStore()
    service._apply_incremental(
        store, file_fps, added=["new.md"], changed=["mod.md"], removed=["gone.md"]
    )

    # 删除与修改都先删旧块；只有新增/修改才重嵌
    assert set(store.deleted) == {"mod.md", "gone.md"}
    assert len(store.bulk_sizes) == 1 and store.bulk_sizes[0] > 0

    # 账本文件集 = 当前 data/ 内容；已删文件的 parent 副本被移除
    manifest = json.loads(service._manifest_file().read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"keep.md", "mod.md", "new.md"}
    chunks = json.loads(service._chunks_file().read_text(encoding="utf-8"))
    rels = [c["metadata"].get("relative_path") for c in chunks["chunks"]]
    assert "gone.md" not in rels
    assert "new.md" in rels and "mod.md" in rels


def test_apply_incremental_removal_only_never_embeds(tmp_path):
    """纯删除：零嵌入，只删块 + 同步账本。"""
    service = _service(tmp_path)
    data_dir = service.settings.data_dir
    (data_dir / "keep.md").write_text("保留的内容。" * 30, encoding="utf-8")

    file_fps = service._compute_file_fingerprints()
    old_fps = {**file_fps, "gone.md": "gone-fingerprint"}
    service._save_meta(
        old_fps,
        [Document(page_content="gone parent", metadata={"relative_path": "gone.md"})],
        child_count=1,
    )

    store = _StubStore()
    service._apply_incremental(store, file_fps, added=[], changed=[], removed=["gone.md"])

    assert store.deleted == ["gone.md"]
    assert store.bulk_sizes == []  # 零嵌入
    manifest = json.loads(service._manifest_file().read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"keep.md"}
