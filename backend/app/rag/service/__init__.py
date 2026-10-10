"""RAG 服务装配层（原 app/rag/service.py 包化，2026-10-10）。

RAGService = RetrieveMixin + IngestMixin + DocsMixin + RAGServiceBase 的薄装配；
对外导入路径不变（from app.rag.service import RAGService / UploadOutcome）。
方法按职责拆分：入库与索引账本 → ingest、在线检索 → retrieve、文档与生成 → docs，
各自只允许依赖 base。
"""
from .base import RAGServiceBase, UploadOutcome
from .docs import DocsMixin
from .ingest import IngestMixin
from .retrieve import RetrieveMixin


class RAGService(RetrieveMixin, IngestMixin, DocsMixin, RAGServiceBase):
    """RAG 服务：加载、Parent-Child 切分、嵌入、存储、检索、重排、生成串成完整流程。"""


__all__ = ["RAGService", "UploadOutcome"]
