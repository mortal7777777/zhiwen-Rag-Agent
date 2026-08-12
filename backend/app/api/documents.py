"""知识库文档管理接口：列表、上传、删除、索引重建与状态。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..rag.service import RAGService
from ..schemas import DocumentInfo, IndexStatus
from .deps import get_service

router = APIRouter(tags=["documents"])


@router.get("/documents", response_model=list[DocumentInfo])
def list_documents(service: RAGService = Depends(get_service)) -> list[dict]:
    """列出 data/ 目录下所有支持的知识库文件。"""
    return service.list_documents()


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
