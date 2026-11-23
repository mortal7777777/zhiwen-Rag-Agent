"""PP-StructureV3 混合解析：路由画像 / 页级缓存 / 回退 / manifest 解析版本。"""

from __future__ import annotations

import io
import json
import sys
import time
import types

import pymupdf
import pytest
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, ToolMessage

from app.agent.nodes.agent import _kb_image_paths_from_state, _vision_message
from app.agent.nodes.prepare import _build_question_message

from app.config import Settings
from app.rag.paddle_ocr import (
    CACHE_VERSION,
    PageOCR,
    PaddleOCRClient,
    choose_pdf_backend,
    profile_pdf,
)
from app.rag.loader import (
    PARSE_SCHEMA_VERSION,
    _load_pdf_native,
    _ocr_sparse_pdf_pages,
    _strip_repeated_lines,
    load_pdf,
)
from app.rag.service import IndexRebuildInProgress, RAGService


# ---------------- 路由 ----------------

def test_choose_backend_rules():
    assert choose_pdf_backend({"pages": 10, "sparse_ratio": 0.9}, "auto") == "paddle"
    assert choose_pdf_backend({"pages": 10, "slide_ratio": 0.8}, "auto") == "paddle"
    assert choose_pdf_backend(
        {"pages": 10, "avg_chars": 450, "image_area_ratio": 0.10}, "auto"
    ) == "paddle"
    assert choose_pdf_backend(
        {"pages": 10, "avg_chars": 1000, "image_area_ratio": 0.01, "sparse_ratio": 0.01},
        "auto",
    ) == "pymupdf"
    assert choose_pdf_backend({"pages": 10}, "pymupdf") == "pymupdf"
    assert choose_pdf_backend({"pages": 10}, "paddle") == "paddle"


def test_profile_routes_text_pdf_to_native(tmp_path):
    pdf = tmp_path / "text.pdf"
    with pymupdf.open() as doc:
        for i in range(4):
            page = doc.new_page()
            page.insert_text((72, 72), f"Hello page {i} " * 30)
        doc.save(pdf)
    profile = profile_pdf(pdf)
    assert profile["pages"] == 4
    assert profile["avg_chars"] > 100
    assert choose_pdf_backend(profile, "auto") == "pymupdf"


def test_profile_routes_image_pdf_to_paddle(tmp_path):
    from PIL import Image, ImageDraw

    buf = io.BytesIO()
    img = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(img).text((20, 20), "scanned page", fill="black")
    img.save(buf, format="PNG")

    pdf = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(pdf)
    profile = profile_pdf(pdf)
    assert profile["sparse_ratio"] == 1.0
    assert choose_pdf_backend(profile, "auto") == "paddle"


# ---------------- 重复页眉页脚剔除 ----------------

def test_strip_repeated_lines_removes_banner_keeps_content():
    docs = [
        Document(
            page_content=f"广告横幅\n唯一的正文 {i}",
            metadata={"page": i + 1},
        )
        for i in range(4)
    ]
    out = _strip_repeated_lines(docs)
    assert len(out) == 4
    assert all("广告横幅" not in d.page_content for d in out)
    assert "唯一的正文 0" in out[0].page_content


# ---------------- 页级缓存 ----------------

def test_vlm_page_cache_roundtrip(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = PaddleOCRClient(settings)
    assert client.load_vlm_text(pdf, 7) == ""
    client.save_vlm_text(pdf, 7, "VLM 页面文本")
    assert client.load_vlm_text(pdf, 7) == "VLM 页面文本"


def test_page_cache_roundtrip(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = PaddleOCRClient(settings)
    cache_dir = client._cache_dir(pdf)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "p0002_img00.png").write_bytes(b"png")
    payload = {
        "cache_version": CACHE_VERSION,
        "markdown": "页内容 [[IMG:p0002_img00]]",
        "text_chars": 4,
        "native_chars": 0,
        "images": [{"id": "p0002_img00", "file": "p0002_img00.png", "bbox": [0, 0, 10, 10]}],
        "ocr_conf": 0.9,
        "image_ocr_chars": 3,
        "elapsed_s": 1.2,
    }
    (cache_dir / "p0002_d200.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    cached = client._load_cache(pdf, 2)
    assert cached is not None and cached.from_cache
    assert "页内容" in cached.markdown
    assert client.image_rel_path(pdf, payload["images"][0]).startswith("ocr_cache/")


# ---------------- Paddle 路径与逐页回退 ----------------

class _FakeClient:
    def __init__(self, pages: list[PageOCR | None]):
        self.pages = pages
        self.last_error = ""
        self.calls: list[int] = []

    def available(self) -> bool:
        return True

    def ocr_page(self, pdf_path, page, **kw):
        self.calls.append(page)
        return self.pages[page - 1]

    def cached_pages(self, pdf_path):
        return 0

    def image_rel_path(self, pdf_path, image):
        return f"ocr_cache/fake/{image['file']}"


def _make_text_pdf(path, pages=3):
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"native page {i + 1} text " * 10)
        doc.save(path)


def test_load_pdf_paddle_path_and_page_fallback(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_text_pdf(pdf, 3)
    fake = _FakeClient(
        [
            PageOCR(
                page=1,
                markdown="# 标题\n[[IMG:p0001_img0]]\n脑图文字",
                text_chars=10,
                native_chars=0,
                images=[{"id": "p0001_img0", "file": "p0001_img0.png", "bbox": [0, 0, 5, 5]}],
                ocr_conf=0.95,
            ),
            None,  # 第 2 页 Worker 失败 -> 回退原生文本
            PageOCR(page=3, markdown="第三页内容", text_chars=5, native_chars=0),
        ]
    )
    entry: dict = {}
    docs = load_pdf(
        pdf,
        layout=False,
        paddle_client=fake,
        parse_backend="paddle",
        report_entry=entry,
    )
    assert [d.metadata["page"] for d in docs] == [1, 2, 3]
    assert docs[0].metadata["parse_backend"] == "paddle"
    assert docs[0].metadata["image_refs"][0]["path"].startswith("ocr_cache/")
    assert docs[1].metadata["parse_backend"] == "native_fallback"
    assert entry["paddle_pages"] == 2
    assert entry["fallback_pages"] == 1


class _FakeVision:
    def __init__(self):
        self.calls = 0
        self.configured = True

    def describe_image_bytes(self, raw, filename="", question=""):
        self.calls += 1
        return "VLM 识别文本"

    def describe_images(self, image_data_urls, question=""):
        self.calls += 1
        assert question
        assert image_data_urls
        return "视觉模型识图结果"


def test_load_pdf_vlm_fallback_on_low_confidence(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_text_pdf(pdf, 2)
    fake = _FakeClient(
        [
            PageOCR(page=1, markdown="低置信内容", text_chars=5, native_chars=0, ocr_conf=0.3),
            PageOCR(
                page=2,
                markdown="正常内容" * 20,
                text_chars=80,
                native_chars=0,
                ocr_conf=0.95,
            ),
        ]
    )
    vision = _FakeVision()
    entry: dict = {}
    docs = load_pdf(
        pdf,
        layout=False,
        paddle_client=fake,
        parse_backend="paddle",
        report_entry=entry,
        vision=vision,
        vlm_fallback=True,
        vlm_confidence_min=0.8,
    )
    assert vision.calls == 1
    assert "VLM 识别文本" in docs[0].page_content
    assert "[[VLM_OCR]]" not in docs[0].page_content
    assert docs[0].metadata["vlm_ocr"] is True
    assert "[[VLM_OCR]]" not in docs[1].page_content
    assert entry["vlm_pages"] == 1


def test_load_pdf_failed_page_uses_vlm_fallback(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_text_pdf(pdf, 2)
    fake = _FakeClient(
        [
            None,
            PageOCR(page=2, markdown="正常页" * 20, text_chars=80, native_chars=0),
        ]
    )
    vision = _FakeVision()
    entry: dict = {}
    docs = load_pdf(
        pdf,
        layout=False,
        paddle_client=fake,
        parse_backend="paddle",
        report_entry=entry,
        vision=vision,
        vlm_fallback=True,
    )
    assert vision.calls == 1
    assert docs[0].metadata["parse_backend"] == "vlm_fallback"
    assert docs[0].metadata["vlm_ocr"] is True
    assert entry["vlm_pages"] == 1
    assert entry["fallback_pages"] == 0


def test_load_pdf_worker_down_uses_existing_cache(tmp_path):
    """Worker 崩溃不应丢掉已缓存页：缓存优先，缺失页逐页回退。"""
    pdf = tmp_path / "doc.pdf"
    _make_text_pdf(pdf, 3)
    fake = _FakeClient(
        [
            PageOCR(page=1, markdown="缓存页一", text_chars=4, native_chars=0),
            None,
            PageOCR(page=3, markdown="缓存页三", text_chars=4, native_chars=0),
        ]
    )
    fake.available = lambda: False  # type: ignore[assignment]
    fake.cached_pages = lambda path: 2  # type: ignore[assignment]
    entry: dict = {}
    docs = load_pdf(
        pdf,
        layout=False,
        paddle_client=fake,
        parse_backend="paddle",
        report_entry=entry,
    )
    assert docs[0].metadata["parse_backend"] == "paddle"
    assert docs[1].metadata["parse_backend"] == "native_fallback"
    assert "缓存" in " ".join(entry["warnings"])


def test_load_pdf_worker_down_falls_back(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_text_pdf(pdf, 2)
    fake = _FakeClient([None, None])
    fake.available = lambda: False  # type: ignore[assignment]
    entry: dict = {}
    docs = load_pdf(
        pdf,
        layout=False,
        paddle_client=fake,
        parse_backend="paddle",
        report_entry=entry,
    )
    assert docs and all(d.metadata["parse_backend"] == "pymupdf" for d in docs)
    assert entry["warnings"]


# ---------------- manifest 解析版本 ----------------

class _StubStore:
    def __init__(self):
        self.pinged = 0

    def ping(self):
        self.pinged += 1

    def resolve(self):
        return "kb_alias_bge_768_v4", True

    def doc_count(self, index=None):
        return 10

    def bulk_index(self, chunks, embeddings, batch_size=0, target=None, batch_guard=None):
        return len(chunks)

    def delete_by_document(self, relative_path, target=None):
        return 0


def _service(tmp_path) -> RAGService:
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    service = RAGService(settings)
    service._store = _StubStore()
    service._physical_base = lambda: "kb_alias_bge_768_v4"  # type: ignore[assignment]
    return service


def _write_manifest(service, parse_version: str | None):
    fps = service._compute_file_fingerprints()
    payload = {"files": fps, "parent_count": 1, "child_count": 10}
    if parse_version is not None:
        payload["parse_version"] = parse_version
    service._index_meta_dir().mkdir(parents=True, exist_ok=True)
    service._manifest_file().write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_parse_version_change_forces_full_rebuild(tmp_path, monkeypatch):
    service = _service(tmp_path)
    (service.settings.data_dir / "a.md").write_text("x" * 100, encoding="utf-8")
    _write_manifest(service, parse_version="legacy-v1")
    calls: list[dict] = []
    monkeypatch.setattr(
        service, "_full_rebuild", lambda store, fps: calls.append({"fps": fps})
    )
    service._run_ensure_index()
    assert len(calls) == 1


def test_matching_parse_version_reuses_index(tmp_path):
    service = _service(tmp_path)
    (service.settings.data_dir / "a.md").write_text("x" * 100, encoding="utf-8")
    _write_manifest(service, parse_version=PARSE_SCHEMA_VERSION)
    service._run_ensure_index()  # 命中缓存直接返回；若走重建会因桩 store 缺方法而失败


def _fake_torch(monkeypatch):
    fake = types.SimpleNamespace(
        cuda=types.SimpleNamespace(empty_cache=lambda: None)
    )
    monkeypatch.setitem(sys.modules, "torch", fake)


def test_gpu_idle_unload_releases_models(tmp_path, monkeypatch):
    _fake_torch(monkeypatch)
    service = _service(tmp_path)
    service._embeddings = object()
    service._reranker = object()
    service._last_gpu_use = time.monotonic() - 120
    monkeypatch.setattr(service, "_gpu_idle_minutes", lambda: 1)
    service._gpu_idle_unload()
    assert service._embeddings is None
    assert service._reranker is None
    assert service.gpu_models_status()["embeddings_loaded"] is False


def test_gpu_idle_unload_skips_active_users(tmp_path, monkeypatch):
    _fake_torch(monkeypatch)
    service = _service(tmp_path)
    service._embeddings = object()
    service._reranker = object()
    service._gpu_users = 1
    service._last_gpu_use = time.monotonic() - 120
    monkeypatch.setattr(service, "_gpu_idle_minutes", lambda: 1)
    service._gpu_idle_unload()
    assert service._embeddings is not None
    assert service._reranker is not None


def test_gpu_idle_unload_zero_disables_timer(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._embeddings = object()
    service._reranker = object()
    monkeypatch.setattr(service, "_gpu_idle_minutes", lambda: 0)
    service._schedule_gpu_unload_locked()
    assert service._gpu_timer is None


def test_loader_kwargs_does_not_raise_with_lifecycle_settings(tmp_path):
    """防止 _loader_kwargs 新增读点漏导入（effective_or 曾导致解析 NameError）。"""
    service = _service(tmp_path)
    kwargs = service._loader_kwargs()
    assert kwargs["parse_backend"] in ("auto", "pymupdf", "paddle")
    assert "paddle_client" in kwargs


def test_kb_vision_enrichment_injects_answer(tmp_path):
    service = _service(tmp_path)
    img_dir = service.settings.meta_dir / "ocr_cache" / "doc"
    img_dir.mkdir(parents=True)
    (img_dir / "p1.png").write_bytes(b"png")
    fake = _FakeVision()
    service._vision = fake
    result = {
        "results": [
            {
                "content": "题干文字",
                "images": [{"id": "p1", "path": "ocr_cache/doc/p1.png"}],
            }
        ]
    }
    service.enrich_result_with_vision(result, "例2这道图形推理选什么？")
    assert fake.calls == 1
    assert result["vision_answer"] == "视觉模型识图结果"
    assert result["vision_images"] == 1
    assert "[视觉识图]" in result["results"][0]["content"]
    assert result["results"][0]["vision_used"] is True


def test_kb_vision_enrichment_skips_when_main_model_has_vision(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        meta_dir=tmp_path / "meta",
        main_model_vision=True,
    )
    settings.ensure_dirs()
    service = RAGService(settings)
    img_dir = settings.meta_dir / "ocr_cache" / "doc"
    img_dir.mkdir(parents=True)
    (img_dir / "p1.png").write_bytes(b"png")
    fake = _FakeVision()
    service._vision = fake
    result = {"results": [{"content": "x", "images": [{"id": "p1", "path": "ocr_cache/doc/p1.png"}]}]}
    service.enrich_result_with_vision(result, "例2图形推理")
    assert fake.calls == 0
    assert "vision_answer" not in result


def test_load_image_data_urls_validates_ocr_cache(tmp_path):
    service = _service(tmp_path)
    img_dir = service.settings.meta_dir / "ocr_cache" / "doc"
    img_dir.mkdir(parents=True)
    (img_dir / "p1.png").write_bytes(b"png")
    urls = service.load_image_data_urls(["ocr_cache/doc/p1.png"], 3)
    assert len(urls) == 1 and urls[0].startswith("data:image/png;base64,")
    assert service.load_image_data_urls(["../secret.png"], 3) == []


def test_build_question_message_with_main_vision():
    data_url = "data:image/png;base64,AA"
    multimodal = _build_question_message("看图", [data_url], True)
    assert isinstance(multimodal.content, list)
    assert multimodal.content[1]["image_url"]["url"] == data_url
    text_only = _build_question_message("看图", [data_url], False)
    assert text_only.content == "看图"


def test_kb_image_paths_from_state_and_vision_message():
    tool_message = ToolMessage(
        content=json.dumps(
            {
                "results": [
                    {"images": [{"id": "p1", "path": "ocr_cache/doc/p1.png"}]}
                ]
            }
        ),
        tool_call_id="tc1",
    )
    paths = _kb_image_paths_from_state(
        [HumanMessage(content="q"), tool_message], 3
    )
    assert paths == ["ocr_cache/doc/p1.png"]
    message = _vision_message(["data:image/png;base64,AA"])
    assert message.content[1]["type"] == "image_url"


def test_kb_vision_enrichment_skips_non_visual_question(tmp_path):
    service = _service(tmp_path)
    img_dir = service.settings.meta_dir / "ocr_cache" / "doc"
    img_dir.mkdir(parents=True)
    (img_dir / "p1.png").write_bytes(b"png")
    fake = _FakeVision()
    service._vision = fake
    result = {"results": [{"content": "x", "images": [{"id": "p1", "path": "ocr_cache/doc/p1.png"}]}]}
    service.enrich_result_with_vision(result, "这本书讲了什么")
    assert fake.calls == 0
    assert "vision_answer" not in result


def test_paddle_lazy_start_disabled(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        meta_dir=tmp_path / "meta",
        paddle_ocr_lazy_start=False,
    )
    settings.ensure_dirs()
    client = PaddleOCRClient(settings)
    client.available = lambda force=False: False  # type: ignore[assignment]
    assert client.ensure_started() is False


def test_ensure_index_defers_parse_upgrade_to_background(tmp_path, monkeypatch):
    """解析器升级不能在下一次检索/上传请求里同步触发数小时重建。"""
    service = _service(tmp_path)
    (service.settings.data_dir / "a.md").write_text("x" * 100, encoding="utf-8")
    _write_manifest(service, parse_version="legacy-v1")
    calls: list[bool] = []
    monkeypatch.setattr(
        service,
        "_run_ensure_index",
        lambda force_rebuild=False: calls.append(force_rebuild) or service._store,
    )
    service.ensure_index()
    assert calls == []


# ---------------- 2026-11-18 回归：重建护栏 / 版本账本 / 页序 / 启动退避 ----------------

def test_index_lock_rejects_during_background_rebuild(tmp_path):
    """重建线程持锁数小时：写操作必须快速失败而不是排队挂死。"""
    service = _service(tmp_path)
    service._rebuild_state["running"] = True
    with pytest.raises(IndexRebuildInProgress):
        with service.index_lock():
            pass


def test_incremental_preserves_parse_version(tmp_path):
    """单文件重解析/增量不得把"解析器升级待重建"标记清掉。"""
    service = _service(tmp_path)
    (service.settings.data_dir / "a.md").write_text("正文" * 50, encoding="utf-8")
    _write_manifest(service, parse_version="legacy-v0")
    service._chunks_file().write_text(
        json.dumps({"chunks": []}, ensure_ascii=False), encoding="utf-8"
    )
    fps = service._compute_file_fingerprints()
    old_fps = dict(service._read_manifest()["files"])
    service._apply_incremental(
        service._store, fps, old_fps, added=["a.md"], changed=[], removed=[]
    )
    manifest = json.loads(service._manifest_file().read_text(encoding="utf-8"))
    assert manifest["parse_version"] == "legacy-v0"
    assert "a.md" in manifest["files"]


def test_full_rebuild_skips_failed_fingerprints():
    """解析失败的文件不写指纹，下一次增量才会自动重试。"""
    fps = {"a.md": "1", "b.pdf": "2"}
    reports = {
        "a.md": {"ok": True},
        "b.pdf": {"ok": False, "warnings": ["boom"]},
    }
    assert RAGService._parsed_fingerprints(fps, reports) == {"a.md": "1"}


def test_sparse_ocr_replaces_page_in_place(tmp_path):
    """兜底 OCR 必须写回原页：追加会破坏 IR 的页序并留下近空存根。"""
    pdf = tmp_path / "scan.pdf"
    _make_text_pdf(pdf, 3)
    docs = [
        Document(
            page_content="短",
            metadata={"source": "scan.pdf", "page": page},
        )
        for page in (1, 2, 3)
    ]
    out = _ocr_sparse_pdf_pages(pdf, docs, _FakeVision(), max_pages=3)
    assert [d.metadata["page"] for d in out] == [1, 2, 3]
    assert len(out) == 3
    assert out[0].page_content.startswith("[扫描页 OCR]")
    assert out[0].metadata.get("ocr") is True


def test_ensure_started_cooldown_after_child_exit(tmp_path):
    """子进程秒退时快速返回并进入冷却，避免每个文件重复等满超时。"""
    settings = Settings(
        data_dir=tmp_path / "data",
        meta_dir=tmp_path / "meta",
        paddle_ocr_timeout=1,
    )
    settings.ensure_dirs()
    client = PaddleOCRClient(settings)
    client.available = lambda force=False: False  # type: ignore[assignment]
    spawns: list[int] = []

    class _DeadProc:
        returncode = 1
        pid = 12345

        def poll(self):
            return 1

    def _spawn():
        spawns.append(1)
        client._start_proc = _DeadProc()

    client._spawn_worker = _spawn  # type: ignore[assignment]
    started = time.monotonic()
    assert client.ensure_started() is False
    assert time.monotonic() - started < 5  # 不按 start_timeout 等待
    assert len(spawns) == 1
    assert "退出" in client.last_error
    # 冷却期内不再重复拉起（否则重建 N 个文件 = N 次冷启动等待）
    assert client.ensure_started() is False
    assert len(spawns) == 1


# ---------------- 2026-11-18 中低优先级回归 ----------------

def test_strip_repeated_lines_keeps_options_in_middle():
    """题库里重复的选项行不在页首/页尾窗口，不能被当页眉删掉。"""
    docs = [
        Document(
            page_content=(
                "页眉公司\n正文一\n正文二\n正文三\n"
                "A. 正确 B. 错误\n更多一\n更多二\n更多三"
            ),
            metadata={"page": i + 1},
        )
        for i in range(4)
    ]
    out = _strip_repeated_lines(docs)
    assert len(out) == 4
    assert all("页眉公司" not in d.page_content for d in out)
    assert all("A. 正确 B. 错误" in d.page_content for d in out)


def test_strip_repeated_lines_keeps_table_rows():
    """重复表头/表格行不参与页眉剔除，否则会破坏 Markdown 表格结构。"""
    docs = [
        Document(
            page_content=f"| 项目 | 数值 |\n|---|---|\n| A | {i} |",
            metadata={"page": i + 1},
        )
        for i in range(4)
    ]
    out = _strip_repeated_lines(docs)
    assert all("| 项目 | 数值 |" in d.page_content for d in out)


def test_native_pdf_fallback_pages_are_one_based(tmp_path):
    """PyPDFLoader 的 0 基页码要归一成 1 基，来源卡片才不会显示第 0 页。"""
    pdf = tmp_path / "a.pdf"
    _make_text_pdf(pdf, 2)
    docs = _load_pdf_native(pdf, layout=False, workers=0, entry={})
    assert [d.metadata["page"] for d in docs] == [1, 2]


def test_ocr_page_skips_cache_on_pipeline_mismatch(tmp_path):
    """Worker 管线版本与客户端不一致时结果可用但不落缓存。"""
    settings = Settings(data_dir=tmp_path / "data", meta_dir=tmp_path / "meta")
    settings.ensure_dirs()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = PaddleOCRClient(settings)
    client.available = lambda force=False: True  # type: ignore[assignment]
    client._sync_worker_settings = lambda: None  # type: ignore[assignment]
    client._post_page = lambda payload: {  # type: ignore[assignment]
        "markdown": "旧管线",
        "text_chars": 3,
        "pipeline_version": "old-pipeline",
    }
    result = client.ocr_page(pdf, 1)
    assert result is not None and result.markdown == "旧管线"
    assert client._load_cache(pdf, 1) is None
    assert "不匹配" in client.last_error

    client._post_page = lambda payload: {  # type: ignore[assignment]
        "markdown": "新管线",
        "text_chars": 3,
        "pipeline_version": CACHE_VERSION,
    }
    assert client.ocr_page(pdf, 2) is not None
    assert client._load_cache(pdf, 2) is not None


def test_worker_params_mismatch_detects_device_and_limits(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        meta_dir=tmp_path / "meta",
        paddle_ocr_device="cpu",
        paddle_ocr_idle_timeout_s=600,
        paddle_ocr_pages_per_process=300,
    )
    settings.ensure_dirs()
    client = PaddleOCRClient(settings)
    client._health_data = {
        "idle_timeout_s": 600,
        "max_pages_per_process": 300,
        "device": "cpu",
    }
    assert client._params_mismatch() is False
    client._health_data["idle_timeout_s"] = 60
    assert client._params_mismatch() is True
    client._health_data = {
        "idle_timeout_s": 600,
        "max_pages_per_process": 300,
        "device": "gpu",
    }
    assert client._params_mismatch() is True


def test_parse_upgrade_pending_flags_stale_manifest(tmp_path):
    service = _service(tmp_path)
    _write_manifest(service, parse_version="legacy-v0")
    assert service.parse_upgrade_pending() is True
    _write_manifest(service, parse_version=PARSE_SCHEMA_VERSION)
    assert service.parse_upgrade_pending() is False
