"""工具层公共辅助。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, tool
from pydantic import BaseModel, Field

from ...rag.retriever import KnowledgeBaseRetriever
from ...runtime_config import effective_or

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 550) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"
