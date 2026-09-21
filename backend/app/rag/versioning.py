"""版本管理：路径/版本号纯工具 + 文件与索引编排。

模型：当前版本 = data/ 里的文件本身；`document_versions` 表只存归档行。
版本号是标识、当前/归档是状态（当前版本不一定是最大号）。
归档文件放 data 的**兄弟目录** data_versions/——放 data/ 下会被
list_data_files 的 rglob 扫进索引。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from .loader import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)

VERSIONS_DIR_NAME = "data_versions"
RENAME_MAX = 200
VERSION_NO_MAX = 50
_ARCHIVE_PATH_MAX = 240  # Windows 路径上限保护（含 data_versions 前缀的整串长度）

_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_NUM_VER_RE = re.compile(r"^v?(\d+)$", re.IGNORECASE)


def versions_dir(settings) -> Path:
    """推导归档目录：data_dir 的兄弟目录 data_versions/（跟随运行时 data_dir 覆盖）。"""
    data = Path(settings.data_dir).resolve()
    if data.parent == data:
        raise ValueError("data_dir 位于盘根，无法推导版本目录，请更换数据目录")
    return data.parent / VERSIONS_DIR_NAME


def validate_version_no(value: str) -> str:
    """版本号校验：非空、≤50 字、无路径非法字符、首尾非点；返回清洗后的值。"""
    s = str(value or "").strip()
    if not s:
        raise ValueError("版本号不能为空")
    if len(s) > VERSION_NO_MAX:
        raise ValueError(f"版本号过长（不超过 {VERSION_NO_MAX} 字）")
    if _INVALID_CHARS_RE.search(s):
        raise ValueError('版本号不能包含 \\ / : * ? " < > | 或控制字符')
    if s.startswith(".") or s.endswith("."):
        raise ValueError("版本号不能以点开头或结尾")
    return s


def suggest_next_version(existing: list[str]) -> str:
    """按现存版本号里的最大数字 +1 给建议（v1/v2…）；无数字版本则 v1。"""
    mx = 0
    for value in existing or []:
        match = _NUM_VER_RE.match(str(value).strip())
        if match:
            mx = max(mx, int(match.group(1)))
    return f"v{mx + 1}"


def version_file_name(version_no: str, original_name: str) -> str:
    return f"{version_no}__{original_name}"


def archive_path(settings, doc_relative_path: str, file_name: str) -> Path:
    """归档文件的落盘路径（带总长护栏）。"""
    path = versions_dir(settings) / doc_relative_path / file_name
    if len(str(path)) > _ARCHIVE_PATH_MAX:
        raise ValueError("归档路径过长（接近系统路径上限），请缩短书名或版本号")
    return path


def archive_file(settings, doc_relative_path: str, src: Path, version_no: str) -> Path:
    """把 src 文件移入归档目录（data 与 data_versions 同盘，用 replace 原子移动）。"""
    target = archive_path(
        settings, doc_relative_path, version_file_name(version_no, src.name)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    src.replace(target)
    return target


# ============================================================
# 编排（需要 RAGService 与 DB Session；repo 延迟导入避免环）
# ============================================================

def _versions_rows(service, db, doc_relative_path: str) -> list[dict]:
    from ..db import repository as repo

    rows = repo.list_document_versions(db, doc_relative_path)
    out = []
    for row in rows:
        item = dict(row)
        created = item.get("created_at")
        if hasattr(created, "isoformat"):
            item["created_at"] = created.isoformat(timespec="seconds")
        item["file_exists"] = archive_path(
            service.settings, doc_relative_path, item["file_name"]
        ).is_file()
        out.append(item)
    return out


def list_versions(service, db, doc_relative_path: str) -> dict:
    """当前版本（data/ 文件）+ 历史版本列表；两者都不存在时 404。"""
    data_dir = Path(service.settings.data_dir)
    current_path = data_dir / doc_relative_path
    current = None
    if current_path.is_file():
        st = current_path.stat()
        current = {
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        }
    versions = _versions_rows(service, db, doc_relative_path)
    if current is None and not versions:
        raise FileNotFoundError(f"文档不存在：{doc_relative_path}")
    return {
        "doc_relative_path": doc_relative_path,
        "name": Path(doc_relative_path).name,
        "current": current,
        "suggested_version_no": suggest_next_version([v["version_no"] for v in versions]),
        "versions": versions,
    }


def archive_as_version(
    service, db, *, source_path: str, target_path: str, version_no: str, note: str = ""
) -> dict:
    """把文档 A 归档为文档 B 的历史版本：移文件 → 记行 → 删 A 元数据 → 删 A 的向量块。"""
    from ..db import repository as repo

    source_path = str(source_path or "").strip()
    target_path = str(target_path or "").strip()
    if not source_path or not target_path:
        raise ValueError("source_path 与 target_path 不能为空")
    if source_path == target_path:
        raise ValueError("不能把文档归档为它自己的历史版本")
    version_no = validate_version_no(version_no)
    data_dir = Path(service.settings.data_dir)
    src = data_dir / source_path
    dst = data_dir / target_path
    if not src.is_file():
        raise FileNotFoundError(f"源文档不存在：{source_path}")
    if not dst.is_file():
        raise FileNotFoundError(f"目标文档不存在：{target_path}")
    if repo.version_no_exists(db, target_path, version_no):
        raise FileExistsError(f"版本号 {version_no} 已存在")

    with service.index_lock():
        size = src.stat().st_size
        moved = archive_file(service.settings, target_path, src, version_no)
        row = None
        try:
            row = repo.add_document_version(
                db,
                doc_relative_path=target_path,
                version_no=version_no,
                file_name=moved.name,
                original_name=src.name,
                size=size,
                note=(note or "").strip(),
            )
            repo.delete_document_meta(db, source_path)
        except Exception:
            if row is not None:
                try:
                    repo.delete_document_version(db, row.id)
                except Exception:
                    pass
            try:
                moved.replace(src)
            except Exception as rollback_exc:
                logger.warning("归档回滚失败：%s", rollback_exc)
            raise
        service.ensure_index()  # removed=[A] → 零嵌入删块

    logger.info("已归档《%s》为《%s》的 %s 版本", source_path, target_path, version_no)
    payload = list_versions(service, db, target_path)
    payload["documents"] = service.list_documents()
    return payload


def restore_version(
    service, db, *, version_id: int, new_version_no: str, note: str = ""
) -> dict:
    """恢复某历史版本为当前，原当前版本归档（两者交换；当前不一定是最大号）。

    跨扩展名恢复：版本文件以「主名 + 版本扩展名」落回 data/，文档族整体改名为新路径。
    """
    from ..db import repository as repo

    row = repo.get_document_version(db, version_id)
    if row is None:
        raise FileNotFoundError("版本不存在")
    doc_rel = row.doc_relative_path
    doc_dir = Path(doc_rel).parent
    doc_name = Path(doc_rel).name
    version_file = archive_path(service.settings, doc_rel, row.file_name)
    if not version_file.is_file():
        raise FileNotFoundError("归档文件缺失（可能被手工删除），无法恢复")
    data_dir = Path(service.settings.data_dir)
    current_path = data_dir / doc_rel

    replaced_row = None
    with service.index_lock():
        if current_path.is_file():
            no = validate_version_no(new_version_no or "")
            if repo.version_no_exists(db, doc_rel, no):
                raise FileExistsError(f"版本号 {no} 已存在")
            size = current_path.stat().st_size
            moved = archive_file(service.settings, doc_rel, current_path, no)
            try:
                replaced_row = repo.add_document_version(
                    db,
                    doc_relative_path=doc_rel,
                    version_no=no,
                    file_name=moved.name,
                    original_name=doc_name,
                    size=size,
                    note=(note or "").strip(),
                )
            except Exception:
                moved.replace(current_path)
                raise

        # 版本文件落回 data/
        target_name = doc_name
        if version_file.suffix.lower() != current_path.suffix.lower():
            target_name = Path(doc_name).stem + version_file.suffix
        if target_name == doc_name:
            new_rel = doc_rel
        elif str(doc_dir) == ".":
            new_rel = target_name
        else:
            new_rel = (doc_dir / target_name).as_posix()
        target_path = data_dir / new_rel
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            version_file.replace(target_path)
        except Exception:
            # 尽力回滚：刚归档的当前版本移回 + 删除其版本行
            if replaced_row is not None:
                try:
                    repo.delete_document_version(db, replaced_row.id)
                    archive_path(
                        service.settings, doc_rel, replaced_row.file_name
                    ).replace(current_path)
                except Exception as rollback_exc:
                    logger.warning("恢复回滚失败：%s", rollback_exc)
            raise
        repo.delete_document_version(db, version_id)

        new_relative_path = None
        if new_rel != doc_rel:
            old_vdir = versions_dir(service.settings) / doc_rel
            new_vdir = versions_dir(service.settings) / new_rel
            if old_vdir.is_dir() and not new_vdir.exists():
                new_vdir.parent.mkdir(parents=True, exist_ok=True)
                old_vdir.rename(new_vdir)
            repo.rename_document_meta(db, doc_rel, new_rel)
            repo.rename_document_versions(db, doc_rel, new_rel)
            new_relative_path = new_rel

        service.ensure_index()  # changed（或跨扩展名时 removed+added）→ 重嵌

    logger.info("已恢复《%s》的 %s 版本为当前", doc_rel, row.version_no)
    payload = list_versions(service, db, new_rel)
    payload["documents"] = service.list_documents()
    if new_relative_path:
        payload["new_relative_path"] = new_relative_path
    return payload


def delete_version(service, db, *, version_id: int) -> dict:
    """彻底删除某历史版本（归档文件 + 版本行）。"""
    from ..db import repository as repo

    row = repo.get_document_version(db, version_id)
    if row is None:
        raise FileNotFoundError("版本不存在")
    doc_rel = row.doc_relative_path
    path = archive_path(service.settings, doc_rel, row.file_name)
    if path.is_file():
        path.unlink()
        try:
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError:
            pass
    repo.delete_document_version(db, version_id)
    logger.info("已彻底删除《%s》的 %s 版本", doc_rel, row.version_no)
    return {"versions": _versions_rows(service, db, doc_rel)}


def rename_document(service, db, *, relative_path: str, new_name: str) -> list[dict]:
    """重命名（仅同目录改基名）：重名（精确/归一化）→ FileExistsError。"""
    from ..db import repository as repo

    data_dir = Path(service.settings.data_dir)
    old_path = data_dir / relative_path
    if not old_path.is_file():
        raise FileNotFoundError("文件不存在")
    name = Path(str(new_name or "").strip()).name.strip()
    if not name or name in (".", ".."):
        raise ValueError("新文件名不能为空")
    if len(name) > RENAME_MAX:
        raise ValueError(f"文件名过长（不超过 {RENAME_MAX} 字）")
    if _INVALID_CHARS_RE.search(name):
        raise ValueError('文件名不能包含 \\ / : * ? " < > | 或控制字符')
    if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"不支持的文件格式：{name}")
    if name.lower() == old_path.name.lower():
        raise ValueError("新文件名与原名相同")
    parent = Path(relative_path).parent
    new_rel = name if str(parent) == "." else (parent / name).as_posix()
    conflict = service.dedup.check_name_taken(name, exclude_rel=relative_path)
    if conflict:
        raise FileExistsError(f"重名：已存在《{conflict}》")
    new_path = data_dir / new_rel
    if new_path.exists():
        raise FileExistsError(f"重名：已存在《{new_rel}》")

    old_vdir = versions_dir(service.settings) / relative_path
    new_vdir = versions_dir(service.settings) / new_rel
    with service.index_lock():
        moved_vdir = False
        if old_vdir.is_dir():
            if new_vdir.exists():
                raise ValueError(
                    "data_versions 下已存在同名目录，无法随改名迁移，请先手工处理"
                )
            new_vdir.parent.mkdir(parents=True, exist_ok=True)
            old_vdir.rename(new_vdir)
            moved_vdir = True
        try:
            old_path.rename(new_path)
            repo.rename_document_meta(db, relative_path, new_rel)
            repo.rename_document_versions(db, relative_path, new_rel)
        except Exception:
            try:
                if new_path.exists() and not old_path.exists():
                    new_path.rename(old_path)
                if moved_vdir and new_vdir.exists() and not old_vdir.exists():
                    old_vdir.parent.mkdir(parents=True, exist_ok=True)
                    new_vdir.rename(old_vdir)
            except Exception as rollback_exc:
                logger.warning("改名回滚失败：%s", rollback_exc)
            raise
        service.ensure_index()  # removed+added → 该书全删重嵌

    logger.info("文档已改名：%s → %s", relative_path, new_rel)
    return service.list_documents()
