"""多格式文档加载：把 data/ 下的文件统一转成 LangChain Document。

解析策略（2026-11 混合路由）：
- PDF 先快速画像（稀疏页/字符密度/课件版式/图片面积）：
  * 扫描件、图片型课件 → PP-StructureV3 Worker（版面/阅读顺序/OCR/表格/公式）；
  * 健康文本层书籍 → PyMuPDF4LLM 布局解析（原生文本，避免 OCR 误差）；
- PP-StructureV3 不会 OCR 图片块，Worker 对图片裁剪后再跑 PaddleOCR 补文字；
- 解析失败 / Worker 不可用时逐页回退原生文本提取（绝不让整库构建失败）；
- 页眉页脚：布局模式用 header/footer=False，另加跨页重复行剔除兜底。
- EPUB 按章节分组；.doc 用 doc2txt/legacy-doc；Excel 按 sheet 逐行管道连接。
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_community.document_loaders import (
    CSVLoader,
    Docx2txtLoader,
    PyPDFLoader,
)
from langchain_core.documents import Document

# 解析器版本：写入 manifest；变化即触发全量重建（文件指纹不变时也不会复用旧索引）
# v2：跨页 parent 目标长度/图片标记原位插回/页眉剔除防误删 —— 需要重新解析
PARSE_SCHEMA_VERSION = "ir-v2-2026-11-18"

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".csv", ".doc", ".docx", ".xlsx", ".pdf", ".epub"}

# 文本类文件鲁棒解码：中文书籍常见 GBK/GB18030（langchain TextLoader 的
# autodetect 只试 utf-8/16/32，GBK 会被按 utf-16 解出全乱码）
_TEXT_ENCODINGS = ("utf-8", "gb18030", "utf-16", "latin-1")


def decode_text_bytes(raw: bytes) -> str:
    """按 utf-8 → gb18030 → utf-16 → latin-1 顺序尝试解码字节（供上传查重的近重复检测复用）。"""
    for enc in _TEXT_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def read_text_robust(path: Path) -> str:
    """按 utf-8 → gb18030 → utf-16 → latin-1 顺序尝试解码文本文件。"""
    return decode_text_bytes(path.read_bytes())


def list_data_files(data_dir: Path) -> list[Path]:
    """递归列出 data/ 下所有受支持格式的文件。"""
    return sorted(
        p
        for p in Path(data_dir).rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def load_xlsx(file_path: Path) -> list[Document]:
    """Excel 专用加载器：每个 sheet 生成一个 Document，每行用 | 连接单元格。"""
    from openpyxl import load_workbook  # 延迟导入

    workbook = load_workbook(file_path, data_only=True, read_only=True)
    docs: list[Document] = []
    try:
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = [
                    str(cell).strip()
                    for cell in row
                    if cell is not None and str(cell).strip()
                ]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                docs.append(
                    Document(
                        page_content="\n".join(rows),
                        metadata={"source": file_path.name, "sheet": sheet.title},
                    )
                )
    finally:
        workbook.close()
    return docs


def load_documents(
    data_dir: Path,
    rel_paths: set[str] | None = None,
    *,
    layout_pdf: bool = True,
    pdf_workers: int = 0,
    vision=None,
    vision_ocr_pages: int = 0,
    paddle_client=None,
    parse_backend: str = "auto",
    report: dict | None = None,
    progress_cb=None,
    vlm_fallback: bool = False,
    vlm_confidence_min: float = 0.8,
) -> list[Document]:
    """加载文档，按扩展名分发解析器。

    rel_paths：只加载指定相对路径的文件（增量索引时只读新文件）；
    None 时加载全部文件（全量重建时用）。
    layout_pdf：是否使用布局感知 PDF 解析（PyMuPDF4LLM）。
    pdf_workers：大 PDF 布局解析并行进程数（0=串行）。
    vision：SenseNova 视觉客户端（可选，用于扫描页 OCR 兜底）。
    vision_ocr_pages：单个 PDF 最多 OCR 页数，0 表示关闭。
    paddle_client：PP-StructureV3 Worker 客户端（可选；None 时全走原生解析）。
    parse_backend：auto | pymupdf | paddle（auto 按 PDF 画像路由）。
    report：可选统计容器；按相对路径写入每文件解析报告。
    """
    files = list_data_files(data_dir)
    if rel_paths is not None:
        files = [p for p in files if p.relative_to(data_dir).as_posix() in rel_paths]
    if not files:
        return []

    docs: list[Document] = []
    for file_path in files:
        suffix = file_path.suffix.lower()
        rel_path = file_path.relative_to(data_dir).as_posix()
        entry: dict = {
            "parse_backend": "native",
            "warnings": [],
            # ok=False 的文件不写入指纹账本，下次增量会自动重试
            "ok": True,
        }
        loaded: list[Document] = []
        try:
            if suffix in TEXT_SUFFIXES:
                loaded = [
                    Document(
                        page_content=read_text_robust(file_path),
                        metadata={"source": file_path.name},
                    )
                ]
            elif suffix == ".csv":
                loaded = CSVLoader(str(file_path), encoding="utf-8").load()
            elif suffix == ".docx":
                loaded = Docx2txtLoader(str(file_path)).load()
            elif suffix == ".pdf":
                loaded = load_pdf(
                    file_path,
                    layout=layout_pdf,
                    workers=pdf_workers,
                    vision=vision,
                    vision_ocr_pages=vision_ocr_pages,
                    paddle_client=paddle_client,
                    parse_backend=parse_backend,
                    report_entry=entry,
                    vlm_fallback=vlm_fallback,
                    vlm_confidence_min=vlm_confidence_min,
                )
            elif suffix == ".epub":
                loaded = load_epub(file_path)
            elif suffix == ".doc":
                loaded = load_doc(file_path)
            elif suffix == ".xlsx":
                loaded = load_xlsx(file_path)
            else:
                continue
            # 统一补相对路径元数据：检索结果据此定位原文（子目录重名文档不混淆）
            for doc in loaded:
                doc.metadata.setdefault("relative_path", rel_path)
            docs.extend(loaded)
            entry.setdefault("chars", sum(len(d.page_content) for d in loaded))
        except ImportError as exc:
            entry["ok"] = False
            entry["warnings"].append(f"缺少依赖 {exc.name}")
            print(f"  跳过 {file_path.name}：缺少依赖 {exc.name}")
        except Exception as exc:
            entry["ok"] = False
            entry["warnings"].append(f"{type(exc).__name__}: {exc}")
            print(f"  跳过 {file_path.name}：{exc}")
        finally:
            if report is not None:
                report[rel_path] = entry
            if progress_cb is not None:
                try:
                    progress_cb(rel_path, len(loaded))
                except Exception:
                    pass
    return docs


def load_pdf(
    file_path: Path | str,
    *,
    layout: bool = True,
    workers: int = 0,
    vision=None,
    vision_ocr_pages: int = 0,
    paddle_client=None,
    parse_backend: str = "auto",
    report_entry: dict | None = None,
    vlm_fallback: bool = False,
    vlm_confidence_min: float = 0.8,
) -> list[Document]:
    """PDF 加载：画像路由（PP-StructureV3 / 原生布局）+ 逐页回退。

    路由只看抽样画像，不读全文：扫描件/课件走 Worker，健康文本层书籍走
    PyMuPDF4LLM；Worker 不可用或整份失败时回退原生解析，保证索引可构建。
    """
    file_path = Path(file_path)
    entry = report_entry if report_entry is not None else {}
    entry.setdefault("warnings", [])

    from .paddle_ocr import choose_pdf_backend, profile_pdf

    profile = profile_pdf(file_path)
    entry["profile"] = profile
    selected = "pymupdf"
    if paddle_client is not None:
        selected = choose_pdf_backend(profile, parse_backend)
        entry["routed_backend"] = selected

    docs: list[Document] | None = None
    if selected == "paddle" and paddle_client is not None:
        docs = _load_pdf_paddle(
            file_path,
            paddle_client,
            entry,
            vision=vision,
            vlm_fallback=vlm_fallback,
            vlm_confidence_min=vlm_confidence_min,
        )
        if docs is None:
            entry["warnings"].append("PP-StructureV3 Worker 不可用或解析失败，回退原生解析")
        else:
            entry["parse_backend"] = "paddle"

    if docs is None:
        docs = _load_pdf_native(file_path, layout=layout, workers=workers, entry=entry)
        entry["parse_backend"] = "pymupdf"
        for doc in docs:
            doc.metadata.setdefault("parse_backend", "pymupdf")
        # 原生解析才需要 SenseNova 稀疏页兜底；Paddle 路径已有页级 OCR
        if vision is not None and vision_ocr_pages > 0:
            docs = _ocr_sparse_pdf_pages(file_path, docs, vision, vision_ocr_pages)

    if docs:
        before = len(docs)
        docs = _strip_repeated_lines(docs)
        if len(docs) != before:
            entry.setdefault("empty_pages", 0)
            entry["empty_pages"] += before - len(docs)
    layout = (
        "slide"
        if float(profile.get("slide_ratio", 0) or 0) >= 0.5
        else "book"
    )
    for doc in docs:
        doc.metadata.setdefault("doc_layout", layout)
    entry.setdefault("pages", len(docs))
    entry["chars"] = sum(len(d.page_content) for d in docs)
    return docs


def _load_pdf_native(
    file_path: Path,
    *,
    layout: bool,
    workers: int,
    entry: dict,
) -> list[Document]:
    """原生布局解析（PyMuPDF4LLM，header/footer 剔除），失败回退 PyPDFLoader。"""
    docs: list[Document] = []
    if layout:
        try:
            if workers > 0 and _pdf_page_count(str(file_path)) >= 300:
                docs = _layout_markdown_parallel(str(file_path), workers)
            else:
                docs = _layout_markdown_serial(str(file_path))
        except ImportError:
            docs = []
        except Exception as exc:
            entry["warnings"].append(f"PyMuPDF4LLM 失败：{exc}")
            print(f"  PyMuPDF4LLM 解析 {file_path.name} 失败，回退 PyPDFLoader：{exc}")
            docs = []

    if not docs:
        loaded = PyPDFLoader(str(file_path)).load()
        for doc in loaded:
            doc.metadata.setdefault("source", file_path.name)
            # PyPDFLoader 的 page 是 0 基；统一成与 pymupdf4llm 一致的 1 基
            page = doc.metadata.get("page")
            if isinstance(page, int):
                doc.metadata["page"] = page + 1
        docs = loaded
    return docs


def _load_pdf_paddle(
    file_path: Path,
    client,
    entry: dict,
    *,
    vision=None,
    vlm_fallback: bool = False,
    vlm_confidence_min: float = 0.8,
) -> list[Document] | None:
    """逐页调用 PP-StructureV3 Worker；Worker 不可用时优先复用已有页缓存。"""
    worker_up = client.available()
    if not worker_up and getattr(client, "lazy_start", False):
        worker_up = client.ensure_started()
        entry["worker_lazy_started"] = worker_up
    cached = client.cached_pages(file_path)
    if not worker_up and cached == 0:
        entry["warnings"].append(f"Worker 不可用且无缓存：{client.last_error}")
        return None
    if not worker_up:
        # 解析到一半 Worker 崩溃也不丢已解析页：缺失页在循环里逐页回退原生
        entry["warnings"].append(
            f"Worker 不可用，使用 {cached} 页缓存解析，缺失页回退原生：{client.last_error}"
        )

    import pymupdf

    docs: list[Document] = []
    stats = {
        "paddle_pages": 0,
        "fallback_pages": 0,
        "near_empty_pages": 0,
        "images": 0,
        "image_ocr_chars": 0,
        "vlm_pages": 0,
        "vlm_chars": 0,
        "_conf_sum": 0.0,
        "_conf_n": 0,
    }
    name = file_path.name
    try:
        def _vlm_page_text(page_obj, pno: int) -> str:
            cached = getattr(client, "load_vlm_text", None)
            if cached:
                text = cached(file_path, pno)
                if text:
                    return text
            pix = page_obj.get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72))
            text = (
                vision.describe_image_bytes(
                    pix.tobytes("png"),
                    filename=f"{name}_p{pno}.png",
                    question=(
                        "请完整识别这一页的正文、表格与公式，输出纯文本；"
                        "不确定的字符用□标记。"
                    ),
                )
                or ""
            ).strip()
            if text:
                saver = getattr(client, "save_vlm_text", None)
                if saver:
                    saver(file_path, pno, text)
            return text

        with pymupdf.open(str(file_path)) as pdf:
            total = pdf.page_count
            entry["pages"] = total
            for pno in range(1, total + 1):
                result = client.ocr_page(file_path, pno)
                if result is None:
                    # 单页失败：优先 VLM 整页 OCR，再回退原生文本
                    page = pdf.load_page(pno - 1)
                    if vlm_fallback and vision is not None:
                        try:
                            vtext = _vlm_page_text(page, pno)
                            if vtext:
                                stats["vlm_pages"] += 1
                                stats["vlm_chars"] += len(vtext)
                                docs.append(
                                    Document(
                                        page_content=vtext,
                                        metadata={
                                            "source": name,
                                            "page": pno,
                                            "parse_backend": "vlm_fallback",
                                            "vlm_ocr": True,
                                        },
                                    )
                                )
                                continue
                        except Exception as exc:
                            entry["warnings"].append(
                                f"VLM 页兜底失败 p{pno}：{type(exc).__name__}: {exc}"
                            )
                    text = page.get_text().strip()
                    stats["fallback_pages"] += 1
                    if text:
                        docs.append(
                            Document(
                                page_content=text,
                                metadata={
                                    "source": name,
                                    "page": pno,
                                    "parse_backend": "native_fallback",
                                },
                            )
                        )
                    continue
                stats["paddle_pages"] += 1
                if result.ocr_conf is not None:
                    stats["_conf_sum"] += float(result.ocr_conf)
                    stats["_conf_n"] += 1
                stats["images"] += len(result.images)
                stats["image_ocr_chars"] += result.image_ocr_chars

                text = (result.markdown or "").strip()
                meta: dict = {
                    "source": name,
                    "page": pno,
                    "parse_backend": "paddle",
                }
                if result.images:
                    meta["image_refs"] = [
                        {
                            "id": img.get("id"),
                            "path": client.image_rel_path(file_path, img),
                            "bbox": img.get("bbox"),
                        }
                        for img in result.images
                    ]
                if not text and result.images:
                    text = "\n".join(f"[[IMG:{img.get('id')}]]" for img in result.images)
                plain_chars = int(result.text_chars or 0)
                low_conf = (
                    result.ocr_conf is not None
                    and float(result.ocr_conf) < vlm_confidence_min
                )
                if vlm_fallback and vision is not None and (
                    plain_chars < 20 or low_conf
                ):
                    try:
                        page = pdf.load_page(pno - 1)
                        vtext = _vlm_page_text(page, pno)
                        if vtext:
                            text = (
                                (text + "\n\n" + vtext).strip()
                                if text
                                else vtext
                            )
                            meta["vlm_ocr"] = True
                            stats["vlm_pages"] += 1
                            stats["vlm_chars"] += len(vtext)
                    except Exception as exc:
                        entry["warnings"].append(
                            f"VLM 兜底第 {pno} 页失败：{type(exc).__name__}: {exc}"
                        )
                if not text:
                    stats["near_empty_pages"] += 1
                    continue
                if result.ocr_conf is not None:
                    meta["ocr_conf"] = round(float(result.ocr_conf), 4)
                docs.append(Document(page_content=text, metadata=meta))
    except Exception as exc:
        entry["warnings"].append(f"PP-StructureV3 解析失败：{type(exc).__name__}: {exc}")
        return None

    entry.update({k: v for k, v in stats.items() if not k.startswith("_")})
    if stats["_conf_n"]:
        entry["avg_ocr_conf"] = round(stats["_conf_sum"] / stats["_conf_n"], 4)
    if not docs:
        entry["warnings"].append("PP-StructureV3 未产出任何页面，回退原生解析")
        return None
    return docs


def _strip_repeated_lines(
    docs: list[Document],
    *,
    min_pages: int = 3,
    ratio: float = 0.8,
    max_len: int = 80,
    edge_lines: int = 3,
) -> list[Document]:
    """剔除跨页重复的页眉/页脚/广告横幅，避免污染每个 chunk。

    只在同一份 PDF 的页文档集合上生效；短文档（<3 页）不动。判定要求该行在
    ≥ratio 的页面里都出现在页首/页尾窗口，且不是表格行与选项行——否则题库
    里重复的选项（"A. 正确 B. 错误"）和重复表头也会被误删。
    """
    if len(docs) < min_pages:
        return docs
    from collections import Counter

    counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    for doc in docs:
        lines = [line for line in doc.page_content.splitlines() if line.strip()]
        total = len(lines)
        seen: set[str] = set()
        for index, line in enumerate(lines):
            key = re.sub(r"\s+", "", line)
            if not key or len(key) > max_len or key in seen:
                continue
            # 表格行/选项行不参与：删除会破坏表格结构或丢掉题目选项
            if key.startswith("|") or re.match(r"^[A-Da-d][.、)）]", key):
                continue
            seen.add(key)
            counts[key] += 1
            if index < edge_lines or index >= total - edge_lines:
                edge_counts[key] += 1
    need = max(min_pages, int(len(docs) * ratio))
    repeated = {
        key
        for key, count in counts.items()
        if count >= need and edge_counts[key] >= need
    }
    if not repeated:
        return docs

    out: list[Document] = []
    for doc in docs:
        kept = [
            line
            for line in doc.page_content.splitlines()
            if re.sub(r"\s+", "", line) not in repeated
        ]
        text = "\n".join(kept).strip()
        if not text:
            continue
        out.append(Document(page_content=text, metadata=dict(doc.metadata)))
    return out


def _silence_tessdata_probe() -> None:
    """让 pymupdf 不再探测 Tesseract（每份 PDF 少起两个子进程 + 免解码噪音）。

    pymupdf.get_tessdata() 会执行 `tesseract --list-langs` 与 `where tesseract`
    两个子进程；中文 Windows 下它们往 stderr 写 GBK（如"信息: 用提供的模式
    无法找到文件。"），而进程跑在 UTF-8 模式时，subprocess 的读取线程会抛
    UnicodeDecodeError —— 解析结果本身不受影响（pymupdf4llm 用 try/except
    接住了异常，判定"无 Tesseract"继续），但每份 PDF 会刷一屏 traceback。

    本项目扫描件 OCR 走 SenseNova（`pdf_vision_ocr_enabled`），不依赖
    Tesseract，因此把探测结果直接固定为"未安装"（返回 None，与现状等价）。
    """
    try:
        import pymupdf
    except Exception:
        return
    if getattr(pymupdf, "_zhiwen_no_tessdata", False):
        return

    def _get_tessdata(tessdata=None):
        return tessdata

    try:
        pymupdf.get_tessdata = _get_tessdata
        pymupdf._zhiwen_no_tessdata = True
    except Exception:  # pragma: no cover - 库结构变化时静默降级
        pass


def _pdf_page_count(file_path: str) -> int:
    """快速获取 PDF 页数（不加载版面模型）。"""
    import fitz

    with fitz.open(file_path) as doc:
        return doc.page_count


def _layout_markdown_serial(file_path: str) -> list[Document]:
    """串行布局解析：整本一次转 Markdown（带页码）。"""
    _silence_tessdata_probe()
    import pymupdf4llm

    pages = pymupdf4llm.to_markdown(
        file_path,
        page_chunks=True,
        show_progress=False,
        write_images=False,
        header=False,
        footer=False,
    )
    docs: list[Document] = []
    name = Path(file_path).name
    for page in pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        meta = dict(page.get("metadata") or {})
        docs.append(
            Document(
                page_content=text,
                metadata={"source": name, "page": meta.get("page_number") or 1},
            )
        )
    return docs


def _layout_markdown_parallel(file_path: str, workers: int) -> list[Document]:
    """按页范围多进程布局解析（布局推理是进程内全局状态，线程并行无效）。"""
    import math
    from concurrent.futures import ProcessPoolExecutor, as_completed

    total = _pdf_page_count(file_path)
    chunk = max(50, math.ceil(total / max(1, workers)))
    ranges = [
        (file_path, start, min(start + chunk, total))
        for start in range(0, total, chunk)
    ]
    docs: list[Document] = []
    failed = 0
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_layout_range, *r): r for r in ranges}
        for future in as_completed(futures):
            try:
                docs.extend(future.result())
            except Exception as exc:
                failed += 1
                print(f"  PDF 并行解析段失败：{exc}")
    if failed:
        print(f"  并行解析有 {failed} 段失败，回退串行解析整本")
        return _layout_markdown_serial(file_path)
    docs.sort(key=lambda d: d.metadata.get("page") or 0)
    return docs


def _layout_range(file_path: str, start: int, end: int) -> list[Document]:
    """子进程任务：解析 [start, end) 页范围（0 基页码）。"""
    _silence_tessdata_probe()  # 子进程是 spawn 出来的，父进程的补丁不继承
    import pymupdf4llm

    pages = pymupdf4llm.to_markdown(
        file_path,
        page_chunks=True,
        show_progress=False,
        write_images=False,
        pages=list(range(start, end)),
        header=False,
        footer=False,
    )
    docs: list[Document] = []
    name = Path(file_path).name
    for page in pages:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        meta = dict(page.get("metadata") or {})
        docs.append(
            Document(
                page_content=text,
                metadata={"source": name, "page": meta.get("page_number") or start + 1},
            )
        )
    return docs


def _ocr_sparse_pdf_pages(
    file_path: Path,
    docs: list[Document],
    vision,
    max_pages: int,
) -> list[Document]:
    """找出几乎无文本的页面（扫描件），用视觉模型 OCR 并写回对应页。

    写回而不是追加新 Document：追加会让页序错乱（IR 跨页父块会把末页与
    更早的 OCR 页拼在一起），还会留下近空的 native 存根造成重复内容。
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        return docs

    empty_pages: list[int] = []
    by_page: dict[int, Document] = {}
    for doc in docs:
        page = doc.metadata.get("page")
        if page is not None:
            by_page.setdefault(int(page), doc)
        text = (doc.page_content or "").strip()
        if page and len(text) < 30:
            empty_pages.append(int(page))
    if not empty_pages:
        return docs

    done = 0
    pdf = fitz.open(str(file_path))
    try:
        for page_no in sorted(set(empty_pages)):
            if done >= max_pages:
                break
            try:
                page = pdf.load_page(page_no - 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                raw = pix.tobytes("png")
                text = vision.describe_image_bytes(
                    raw,
                    filename=f"{file_path.name}_p{page_no}.png",
                    question=(
                        "这是 PDF 的第 %d 页扫描件。请完整识别页面内容，"
                        "包括所有标题、正文与表格，输出为易于阅读的纯文本。"
                        % page_no
                    ),
                )
                if text:
                    content = f"[扫描页 OCR] {text}"
                    existing = by_page.get(page_no)
                    if existing is not None:
                        existing.page_content = content
                        existing.metadata["ocr"] = True
                    else:
                        doc = Document(
                            page_content=content,
                            metadata={
                                "source": file_path.name,
                                "page": page_no,
                                "ocr": True,
                            },
                        )
                        docs.append(doc)
                        by_page[page_no] = doc
                    done += 1
            except Exception as exc:
                print(f"  OCR 第 {page_no} 页失败：{exc}")
    finally:
        pdf.close()
    if done:
        print(f"  已用视觉模型 OCR {file_path.name} 的 {done} 个扫描页")
    # 兜底排序：任何后补的页都回到正确位置，保证 IR 跨页合并的顺序
    docs.sort(key=lambda doc: int(doc.metadata.get("page") or 0))
    return docs


def load_doc(file_path: Path | str) -> list[Document]:
    """旧版二进制 .doc（Word 97-2003）加载器。

    优先使用 doc2txt（内置 antiword 二进制，跨平台、鲁棒），
    失败时退回 legacy-doc（纯 Python 实现，适合较小的文件）。
    """
    file_path = Path(file_path)
    text = ""
    errors: list[str] = []
    try:
        from doc2txt import extract_text as doc2txt_extract

        text = doc2txt_extract(str(file_path))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"doc2txt: {exc}")
        try:
            from legacy_doc import extract_text as legacy_extract

            result = legacy_extract(file_path.read_bytes())
            text = result.text if hasattr(result, "text") else str(result)
        except Exception as exc2:  # noqa: BLE001
            errors.append(f"legacy-doc: {exc2}")
    if not text:
        raise ValueError(f"无法解析 .doc 文件（{'；'.join(errors) or '空文本'}）")

    # 轻量归一化：统一换行、去掉零宽字符、压缩多余空行
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return [Document(page_content=text, metadata={"source": file_path.name})]


def load_epub(file_path: Path | str) -> list[Document]:
    """EPUB 加载器：提取全部文本内容，按章节分组。

    EPUB 本质是一个 ZIP 包，内含 XHTML 格式的章节文件。
    用 ebooklib 解包 + BeautifulSoup 提取纯文本。
    """
    file_path = Path(file_path)
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    book = epub.read_epub(str(file_path))
    docs: list[Document] = []
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        # item.get_content() 返回 bytes，用 BeautifulSoup 提取纯文本
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        if not text:
            continue
        # 用文件名作为章节标识
        chapter_name = item.get_name() or "未知章节"
        docs.append(
            Document(
                page_content=text,
                metadata={"source": file_path.name, "chapter": chapter_name},
            )
        )
    if not docs:
        print(f"  警告：{file_path.name} 未提取到任何文本内容")
    else:
        print(f"  已加载 ePub：{file_path.name}，共 {len(docs)} 个章节")
    return docs
