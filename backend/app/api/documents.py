"""知识库文档管理接口：列表、上传、删除、预览、自定义分类、索引重建与状态。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..db import get_db
from ..db import repository as repo
from ..rag.loader import TEXT_SUFFIXES
from ..rag.service import RAGService
from ..schemas import DocumentInfo, DocumentMetaIn, IndexStatus
from .deps import get_service

router = APIRouter(tags=["documents"])

# 预览内容上限：超长截断（完整内容走下载/本地打开）
PREVIEW_MAX_CHARS = 200_000

# 专业查看器格式的 MIME 类型（Windows 注册表猜测不可靠，显式指定）
FILE_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _resolve_document(service: RAGService, path: str) -> Path:
    """校验相对路径并把文档解析为 data_dir 内的绝对路径（拒绝穿越）。"""
    data_dir = service.settings.data_dir.resolve()
    target = (service.settings.data_dir / path).resolve()
    if target != data_dir and data_dir not in target.parents:
        raise HTTPException(status_code=400, detail="非法路径：超出知识库目录")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


@router.get("/documents", response_model=list[DocumentInfo])
def list_documents(
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> list[dict]:
    """列出 data/ 目录下所有支持的知识库文件（附带用户自定义分类）。"""
    docs = service.list_documents()
    try:
        metas = repo.list_document_meta(db)
    except Exception:
        metas = {}
    for doc in docs:
        meta = metas.get(doc.get("relative_path") or "") or {}
        doc["category"] = meta.get("category", "")
        doc["tags"] = meta.get("tags", "")
    return docs


@router.get("/documents/preview")
def preview_document(
    path: str = Query(..., description="文档相对路径"),
    service: RAGService = Depends(get_service),
) -> dict:
    """预览文档内容：md/txt 返回原文，pdf/docx/epub/xlsx/csv 提取文本。

    kind=markdown 时前端按 Markdown 渲染，否则按纯文本展示；
    超过 20 万字符截断并标记 truncated。
    """
    data_dir = service.settings.data_dir.resolve()
    target = _resolve_document(service, path)
    suffix = target.suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            content = target.read_text(encoding="utf-8", errors="ignore")
            kind = "markdown" if suffix in (".md", ".markdown") else "text"
        else:
            # 复用索引加载器提取文本（pdf 不走布局模式，预览要快）
            from ..rag.loader import load_documents

            rel = target.relative_to(data_dir).as_posix()
            docs = load_documents(
                service.settings.data_dir, {rel}, layout_pdf=False
            )
            content = "\n\n".join(d.page_content for d in docs)
            kind = "text" if suffix in (".csv", ".xlsx") else "markdown"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"文档解析失败：{exc}")
    truncated = len(content) > PREVIEW_MAX_CHARS
    return {
        "name": target.name,
        "relative_path": path,
        "suffix": suffix,
        "kind": kind,
        "content": content[:PREVIEW_MAX_CHARS],
        "char_count": len(content),
        "truncated": truncated,
    }


@router.get("/documents/file/{relative_path:path}")
def get_document_file(
    relative_path: str,
    service: RAGService = Depends(get_service),
) -> FileResponse:
    """返回文档原始字节（pdf.js / epub.js / docx-preview 等专业查看器用）。

    只读，复用预览端点的路径校验（拒绝穿越、限 data_dir 内）。
    """
    target = _resolve_document(service, relative_path)
    suffix = target.suffix.lower()
    media_type = FILE_MEDIA_TYPES.get(suffix)
    if media_type is None and suffix not in TEXT_SUFFIXES | {".csv", ".doc"}:
        # 非查看器目标格式也允许下载（列表里能看到的都算），但显式二进制流
        media_type = "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


@router.get("/documents/meta")
def get_documents_meta(db=Depends(get_db)) -> dict:
    """全部文档的自定义分类元数据：{relative_path: {category, tags, notes}}。"""
    try:
        return repo.list_document_meta(db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取文档元数据失败：{exc}")


@router.put("/documents/meta")
def save_document_meta(payload: DocumentMetaIn, db=Depends(get_db)) -> dict:
    """保存某个文档的分类/标签/备注，返回全量元数据。"""
    try:
        repo.upsert_document_meta(
            db,
            payload.relative_path,
            category=payload.category,
            tags=payload.tags,
            notes=payload.notes,
        )
        return repo.list_document_meta(db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存文档元数据失败：{exc}")


@router.post("/documents/upload", response_model=list[DocumentInfo])
def upload_documents(
    files: list[UploadFile] = File(..., description="支持 txt/md/csv/docx/xlsx/pdf"),
    service: RAGService = Depends(get_service),
) -> list[dict]:
    """上传一个或多个文档，并增量更新 OpenSearch 索引。"""
    try:
        return service.add_documents(files)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"上传失败：{exc}")


@router.delete("/documents/{relative_path:path}", response_model=list[DocumentInfo])
def delete_document(
    relative_path: str,
    service: RAGService = Depends(get_service),
) -> list[dict]:
    """删除知识库文件并重建索引（删除会影响旧向量，重建最稳妥）。"""
    try:
        service.delete_document(relative_path)
        return service.list_documents()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}")


@router.post("/index/rebuild", response_model=IndexStatus)
def rebuild_index(service: RAGService = Depends(get_service)) -> dict:
    """强制重建整个索引（删除旧索引后全量嵌入）。"""
    try:
        return service.rebuild_index()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重建失败：{exc}")


@router.get("/index/status", response_model=IndexStatus)
def index_status(service: RAGService = Depends(get_service)) -> dict:
    """查看索引状态：向量条数、文本块数、数据目录。"""
    try:
        return service.index_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取状态失败：{exc}")
