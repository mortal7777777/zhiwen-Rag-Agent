"""对话附件：上传 → 文本提取 → 临时存储。

前端把文件 POST 到 /api/agent/attachments，拿到 attachment id；发送消息时
只带 id 列表，由服务层在 prepare 阶段把提取出的文本注入上下文。
文件本身落在 META_DIR/attachments/<uuid>/（gitignored 的 opensearch_meta 下），
过期自动清理，不进入知识库索引。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from .rag.loader import (
    TEXT_SUFFIXES,
    load_doc,
    load_epub,
    load_pdf,
    load_xlsx,
    read_text_robust,
)

logger = logging.getLogger(__name__)

# 文档 + 常见文本/代码文件。图片走既有 data URL 视觉链路，不在此处。
DOCUMENT_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xlsx", ".csv", ".epub",
}
CODE_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".log", ".xml", ".html", ".htm",
    ".css", ".scss", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".vue", ".py", ".ipynb", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".ps1", ".bat",
    ".cmd", ".sql", ".r", ".m", ".swift", ".dart", ".lua", ".pl",
}
ALLOWED_SUFFIXES = DOCUMENT_SUFFIXES | CODE_SUFFIXES | TEXT_SUFFIXES

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class AttachmentError(ValueError):
    """可向用户展示的附件校验/解析错误。"""


def _settings_int(settings, name: str, fallback: int) -> int:
    try:
        value = int(getattr(settings, name, fallback) or fallback)
    except Exception:
        value = fallback
    return max(1, value)


def _root(settings) -> Path:
    root = Path(settings.meta_dir) / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(filename: str | None) -> str:
    name = Path(str(filename or "file")).name.strip()
    # 去掉控制字符与路径分隔符，保留可读文件名（只用于展示与扩展名判断）
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return name or "file"


def _read_bytes(settings, upload) -> bytes:
    max_mb = _settings_int(settings, "attachment_max_mb", 20)
    max_bytes = max_mb * 1024 * 1024
    try:
        data = upload.file.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001 - 统一转成可展示错误
        raise AttachmentError(f"读取文件失败：{exc}") from exc
    if not data:
        raise AttachmentError("文件为空")
    if len(data) > max_bytes:
        raise AttachmentError(f"文件超过 {max_mb}MB 上限")
    return data


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES or suffix in CODE_SUFFIXES:
        return read_text_robust(path)
    if suffix == ".csv":
        return read_text_robust(path)
    if suffix == ".docx":
        from langchain_community.document_loaders import Docx2txtLoader

        docs = Docx2txtLoader(str(path)).load()
    elif suffix == ".doc":
        docs = load_doc(path)
    elif suffix == ".pdf":
        # 对话附件只需要可读文本，关闭布局解析（更快、依赖更少）
        docs = load_pdf(path, layout=False)
    elif suffix == ".epub":
        docs = load_epub(path)
    elif suffix == ".xlsx":
        docs = load_xlsx(path)
    else:
        raise AttachmentError("不支持的文件类型")
    return "\n\n".join(str(doc.page_content or "") for doc in docs)


def cleanup_expired(settings, *, now: float | None = None) -> None:
    """删除超过保留时间的附件目录（尽力而为，失败不影响上传）。"""
    hours = _settings_int(settings, "attachment_retention_hours", 24)
    cutoff = (now if now is not None else time.time()) - hours * 3600
    root = _root(settings)
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError as exc:
        logger.warning("清理过期附件失败：%s", exc)


def save_bytes(settings, filename: str | None, data: bytes) -> dict:
    """保存附件字节并提取文本，返回可公开的元数据（不含正文）。"""
    name = _safe_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        supported = "、".join(sorted(ALLOWED_SUFFIXES))
        raise AttachmentError(f"不支持的文件类型：{suffix or '无扩展名'}（支持 {supported}）")
    max_mb = _settings_int(settings, "attachment_max_mb", 20)
    if len(data) > max_mb * 1024 * 1024:
        raise AttachmentError(f"文件超过 {max_mb}MB 上限")

    cleanup_expired(settings)
    attachment_id = uuid.uuid4().hex
    target_dir = _root(settings) / attachment_id
    target_dir.mkdir(parents=True, exist_ok=True)
    source = target_dir / f"source{suffix}"
    try:
        source.write_bytes(data)
        text = _extract_text(source)
        max_chars = _settings_int(settings, "attachment_max_chars", 30000)
        text = str(text or "").strip()
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        (target_dir / "text.txt").write_text(text, encoding="utf-8")
        meta = {
            "id": attachment_id,
            "name": name,
            "suffix": suffix,
            "size": len(data),
            "chars": len(text),
            "truncated": truncated,
            "created_at": time.time(),
        }
        (target_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except AttachmentError:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - 解析失败给出可展示原因
        shutil.rmtree(target_dir, ignore_errors=True)
        raise AttachmentError(f"解析文件失败：{exc}") from exc

    public = {
        "id": meta["id"],
        "name": meta["name"],
        "size": meta["size"],
        "chars": meta["chars"],
        "truncated": meta["truncated"],
    }
    if not text:
        public["warning"] = "未能从该文件提取文本（可能是扫描件或空文档）"
    return public


def save_upload(settings, upload) -> dict:
    """从 FastAPI UploadFile 保存附件。"""
    filename = getattr(upload, "filename", None)
    return save_bytes(settings, filename, _read_bytes(settings, upload))


def load_attachments(settings, ids: list[str] | None) -> list[dict]:
    """按 id 读取附件正文；无效/过期 id 静默跳过。

    总字符数在此处就截断（而不是等 prepare）：附件正文会随 state 进检查点，
    先截断可以避免快照被大文件撑爆。
    """
    out: list[dict] = []
    seen: set[str] = set()
    max_files = _settings_int(settings, "attachment_max_files", 5)
    remaining = _settings_int(settings, "attachment_total_chars", 60000)
    root = _root(settings)
    for raw in ids or []:
        attachment_id = str(raw or "").strip()
        if not _ID_RE.match(attachment_id) or attachment_id in seen:
            continue
        seen.add(attachment_id)
        folder = root / attachment_id
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            text = (folder / "text.txt").read_text(encoding="utf-8")
        except Exception:
            logger.info("附件不存在或已过期：%s", attachment_id)
            continue
        if remaining <= 0:
            text = ""
        else:
            text = text[:remaining]
            remaining -= len(text)
        meta["text"] = text
        out.append(meta)
        if len(out) >= max_files:
            break
    return out


def delete_attachment(settings, attachment_id: str) -> bool:
    """删除一个附件目录；id 非法或不存在返回 False。"""
    attachment_id = str(attachment_id or "").strip()
    if not _ID_RE.match(attachment_id):
        return False
    folder = _root(settings) / attachment_id
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True
