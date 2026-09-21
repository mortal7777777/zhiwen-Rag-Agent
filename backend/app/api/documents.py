"""知识库文档管理接口：列表、上传、删除、预览、自定义分类、索引重建与状态。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..db import get_db, get_db_optional
from ..db import repository as repo
from ..rag import versioning
from ..rag.loader import TEXT_SUFFIXES
from ..rag.service import RAGService
from ..schemas import (
    DocumentInfo,
    DocumentMetaIn,
    DocumentRenameIn,
    DocumentVersionArchiveIn,
    DocumentVersionRestoreIn,
    DocumentVersionsArchiveOut,
    DocumentVersionsDeleteOut,
    DocumentVersionsOut,
    DocumentVersionsRestoreOut,
    IndexStatus,
    UploadCheckOut,
)
from .deps import get_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

# 预览内容上限：超长截断（完整内容走下载/本地打开）。
# 2026-08-16 20万→100万：引用「查看原文」要在预览文本里定位，大书截断会让
# 引用定位失效（配合前端 content-visibility 按需渲染，长文本不卡）
PREVIEW_MAX_CHARS = 1_000_000

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


@router.put("/documents/data-dir")
def set_data_dir(
    payload: dict,
    db=Depends(get_db),
    service: RAGService = Depends(get_service),
) -> dict:
    """更改知识库数据目录：即时生效并持久化，返回新目录与提示。

    新目录不存在会自动创建；切换后旧索引仍指向旧目录，需要用户
    在界面点击「重建索引」把新目录的文档嵌入 OpenSearch。
    """
    raw = (payload or {}).get("path")
    if not raw or not str(raw).strip():
        raise HTTPException(status_code=400, detail="目录路径不能为空")
    from ..config import get_settings
    from ..runtime_config import save_overrides

    settings = get_settings()
    try:
        target = Path(str(raw)).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"目录不可用：{exc}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"不是有效目录：{target}")
    # 保存覆盖（内部会把路径同步到 settings 单例，RAGService 立即读到新值）
    save_overrides(db, {"data_dir": str(target)}, settings=settings)
    service.refresh()
    return {
        "ok": True,
        "data_dir": str(target),
        "hint": "目录已切换。旧索引仍指向原目录，请点击「重建索引」让新目录的文档生效。",
    }


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
            # GBK/GB18030 中文书按 utf-8 读会乱码（errors=ignore 丢字节）
            from ..rag.loader import read_text_robust

            content = read_text_robust(target)
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


def _build_upload_check(service: RAGService, db, checks: list[dict]) -> dict:
    """把 service 的原始冲突报告补上 would_replace / suggested_version_no。"""
    from ..rag.versioning import suggest_next_version

    files_out = []
    for item in checks:
        same = [x for x in item["conflicts"] if x["kind"] == "same_name"]
        existing: list[str] = []
        if same and db is not None:
            try:
                existing = [
                    v["version_no"]
                    for v in repo.list_document_versions(
                        db, same[0]["existing_relative_path"]
                    )
                ]
            except Exception:
                existing = []
        files_out.append(
            {
                "file_name": item["file_name"],
                "would_replace": bool(same),
                "suggested_version_no": suggest_next_version(existing),
                "conflicts": item["conflicts"],
            }
        )
    return {
        "files": files_out,
        "has_conflict": any(f["conflicts"] for f in files_out),
    }


@router.post("/documents/upload", response_model=list[DocumentInfo] | UploadCheckOut)
def upload_documents(
    files: list[UploadFile] = File(..., description="支持 txt/md/csv/docx/xlsx/pdf"),
    dry_run: bool = Form(False, description="只做查重预检，不落盘"),
    conflict_policy: str = Form("ask", description="ask=有冲突时 409 / proceed=确认后放行"),
    archive_version_no: str | None = Form(None, description="同名重传时旧文件的归档版本号"),
    archive_note: str = Form("", description="归档备注（可选）"),
    service: RAGService = Depends(get_service),
    db=Depends(get_db_optional),
):
    """上传一个或多个文档，并增量更新索引（带查重与同名归档）。

    - 无冲突：200 + 文档列表（老契约不变）；
    - 有冲突且 policy=ask：409，detail={"code":"upload_conflict","files":[...]}，零写入；
    - 同名重传（policy=proceed）：旧文件归档到 data_versions/，需 archive_version_no；
    - dry_run=true：只返回查重报告，不落盘。
    """
    version_no = (archive_version_no or "").strip()
    # 同名 + 版本号已被占用 → 提前 409（避免文件已归档才失败）
    if conflict_policy == "proceed" and version_no:
        if db is None:
            raise HTTPException(
                status_code=409, detail="数据库未连接，无法归档历史版本，请检查 MySQL 后重试"
            )
        for f in files:
            name = Path(f.filename or "").name
            if (service.settings.data_dir / name).exists() and repo.version_no_exists(
                db, name, version_no
            ):
                raise HTTPException(status_code=409, detail=f"版本号 {version_no} 已存在")

    try:
        outcome = service.add_documents(
            files,
            dry_run=dry_run,
            conflict_policy=conflict_policy,
            archive_version_no=version_no or None,
            archive_note=archive_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"上传失败：{exc}")

    if outcome.mode == "dry_run":
        return _build_upload_check(service, db, outcome.conflicts)
    if outcome.mode == "conflict":
        report = _build_upload_check(service, db, outcome.conflicts)
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_conflict", "message": "检测到上传冲突，请确认后继续", **report},
        )

    # committed：落归档版本行（同名重传的旧文件）
    if outcome.archived:
        if db is None:
            raise HTTPException(
                status_code=409, detail="数据库未连接，无法归档历史版本，请检查 MySQL 后重试"
            )
        for item in outcome.archived:
            try:
                repo.add_document_version(
                    db,
                    doc_relative_path=item["doc_relative_path"],
                    version_no=item["version_no"],
                    file_name=item["file_name"],
                    original_name=item["original_name"],
                    size=item["size"],
                    note=item["note"],
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=f"归档版本写入失败：{exc}")
    return outcome.documents


@router.post("/documents/rename", response_model=list[DocumentInfo])
def rename_document_endpoint(
    payload: DocumentRenameIn,
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> list[dict]:
    """重命名知识库文档（重名 / 非法名拒绝；改名后该书重建索引）。"""
    try:
        return versioning.rename_document(
            service,
            db,
            relative_path=payload.relative_path,
            new_name=payload.new_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重命名失败：{exc}")


@router.get("/documents/versions", response_model=DocumentVersionsOut)
def list_document_versions_endpoint(
    path: str = Query(..., description="文档相对路径"),
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> dict:
    """某文档的版本列表（当前版本 + 历史版本）。"""
    try:
        return versioning.list_versions(service, db, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取版本列表失败：{exc}")


@router.post("/documents/versions/archive", response_model=DocumentVersionsArchiveOut)
def archive_document_version_endpoint(
    payload: DocumentVersionArchiveIn,
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> dict:
    """把 source 文档归档为 target 文档的历史版本（移出检索、保留文件）。"""
    try:
        return versioning.archive_as_version(
            service,
            db,
            source_path=payload.source_path,
            target_path=payload.target_path,
            version_no=payload.version_no,
            note=payload.note,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"归档失败：{exc}")


@router.post(
    "/documents/versions/{version_id}/restore", response_model=DocumentVersionsRestoreOut
)
def restore_document_version_endpoint(
    version_id: int,
    payload: DocumentVersionRestoreIn,
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> dict:
    """恢复某历史版本为当前（当前版本归档为 new_version_no，两者交换）。"""
    try:
        return versioning.restore_version(
            service,
            db,
            version_id=version_id,
            new_version_no=payload.new_version_no,
            note=payload.note,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"恢复失败：{exc}")


@router.delete("/documents/versions/{version_id}", response_model=DocumentVersionsDeleteOut)
def delete_document_version_endpoint(
    version_id: int,
    service: RAGService = Depends(get_service),
    db=Depends(get_db),
) -> dict:
    """彻底删除某历史版本（归档文件 + 版本行）。"""
    try:
        return versioning.delete_version(service, db, version_id=version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除版本失败：{exc}")


@router.delete("/documents/{relative_path:path}", response_model=list[DocumentInfo])
def delete_document(
    relative_path: str,
    purge_versions: bool = Query(False, description="同时删除该文档的全部历史版本"),
    service: RAGService = Depends(get_service),
    db=Depends(get_db_optional),
) -> list[dict]:
    """删除知识库文件并移除其向量块（per-doc 增量维护，无需全量重建）。"""
    try:
        service.delete_document(relative_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}")
    if purge_versions and db is not None:
        try:
            repo.delete_document_versions_all(db, relative_path)
            root = versioning.versions_dir(service.settings).resolve()
            vdir = (root / relative_path).resolve()
            if root in vdir.parents:  # 防路径穿越
                shutil.rmtree(vdir, ignore_errors=True)
        except Exception as exc:
            logger.warning("清理历史版本失败：%s", exc)
    return service.list_documents()


@router.post("/index/rebuild", response_model=IndexStatus)
def rebuild_index(service: RAGService = Depends(get_service)) -> dict:
    """强制重建整个索引（蓝绿：写新物理索引 + 别名原子切换）。"""
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
