"""文档元数据与版本数据访问。"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import DocumentMeta, DocumentVersion
from ..sanitize import sanitize_sources, sanitize_token_usage, truncate_tool_trace

logger = logging.getLogger(__name__)


def list_document_meta(db: Session) -> dict[str, dict]:
    """全部文档元数据：{relative_path: {category, tags, notes}}。"""
    rows = db.execute(select(DocumentMeta)).scalars().all()
    return {
        row.relative_path: {
            "category": row.category or "",
            "tags": row.tags or "",
            "notes": row.notes or "",
        }
        for row in rows
    }


def upsert_document_meta(
    db: Session,
    relative_path: str,
    category: str = "",
    tags: str = "",
    notes: str | None = None,
) -> None:
    """保存某个文档的分类/标签/备注（存在则更新）。"""
    row = (
        db.execute(
            select(DocumentMeta).where(DocumentMeta.relative_path == relative_path)
        )
        .scalar_one_or_none()
    )
    if row is None:
        row = DocumentMeta(
            relative_path=relative_path,
            category=category,
            tags=tags,
            notes=notes,
        )
        db.add(row)
    else:
        row.category = category
        row.tags = tags
        if notes is not None:
            row.notes = notes
    db.commit()


def rename_document_meta(db: Session, old_path: str, new_path: str) -> bool:
    """改名同步文档元数据：目标路径若有残留行先删，再 UPDATE 归属。"""
    if old_path == new_path:
        return False
    db.execute(delete(DocumentMeta).where(DocumentMeta.relative_path == new_path))
    result = db.execute(
        update(DocumentMeta)
        .where(DocumentMeta.relative_path == old_path)
        .values(relative_path=new_path)
    )
    db.commit()
    return bool(result.rowcount)


def delete_document_meta(db: Session, relative_path: str) -> bool:
    """删除某文档的元数据行（归档 / 删除文档时清理，避免残留分类"幽灵筛选"）。"""
    result = db.execute(
        delete(DocumentMeta).where(DocumentMeta.relative_path == relative_path)
    )
    db.commit()
    return bool(result.rowcount)


def _version_to_dict(row: DocumentVersion) -> dict:
    return {
        "id": row.id,
        "doc_relative_path": row.doc_relative_path,
        "version_no": row.version_no,
        "file_name": row.file_name,
        "original_name": row.original_name,
        "note": row.note or "",
        "size": int(row.size or 0),
        "created_at": row.created_at,
    }


def list_document_versions(db: Session, doc_relative_path: str) -> list[dict]:
    rows = (
        db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.doc_relative_path == doc_relative_path)
            .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
        )
        .scalars()
        .all()
    )
    return [_version_to_dict(r) for r in rows]


def get_document_version(db: Session, version_id: int) -> DocumentVersion | None:
    return db.get(DocumentVersion, version_id)


def add_document_version(
    db: Session,
    *,
    doc_relative_path: str,
    version_no: str,
    file_name: str,
    original_name: str,
    size: int = 0,
    note: str = "",
) -> DocumentVersion:
    row = DocumentVersion(
        doc_relative_path=doc_relative_path,
        version_no=version_no,
        file_name=file_name,
        original_name=original_name,
        size=int(size or 0),
        note=note or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_document_version(db: Session, version_id: int) -> bool:
    row = db.get(DocumentVersion, version_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def version_no_exists(db: Session, doc_relative_path: str, version_no: str) -> bool:
    return (
        db.execute(
            select(DocumentVersion.id).where(
                DocumentVersion.doc_relative_path == doc_relative_path,
                DocumentVersion.version_no == version_no,
            )
        ).first()
        is not None
    )


def rename_document_versions(db: Session, old_path: str, new_path: str) -> int:
    """改名同步版本行归属（返回受影响行数）。"""
    if old_path == new_path:
        return 0
    result = db.execute(
        update(DocumentVersion)
        .where(DocumentVersion.doc_relative_path == old_path)
        .values(doc_relative_path=new_path)
    )
    db.commit()
    return int(result.rowcount or 0)


def delete_document_versions_all(db: Session, doc_relative_path: str) -> int:
    """删除某主文档的全部版本行（配合 purge 删除归档目录）。"""
    result = db.execute(
        delete(DocumentVersion).where(
            DocumentVersion.doc_relative_path == doc_relative_path
        )
    )
    db.commit()
    return int(result.rowcount or 0)
