"""`RAGService` 包化拆分契约（2026-10-10，原 app/rag/service.py 946 行 → service/ 包）。

镜像 agent 侧 test_service_structure.py 的三条契约：
1. 方法体里的相对导入必须真能解析（层级 +1 最容易漏；本包化时踩过
   `from ..config` 少补一层点的坑，以及 docs 引用 UploadOutcome 未从 base 导入的
   NameError——上线前靠 API 上传测试的 500 才暴露）；
2. mixin 之间不得互相 import，只允许依赖 base；
3. 对外契约方法名保持 + 主装配文件保持薄（行数棘轮）。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RAG_DIR = BACKEND / "app" / "rag"
SERVICE_DIR = RAG_DIR / "service"

PACKAGE = "app.rag.service"
MIXINS = {
    "base": "RAGServiceBase",
    "ingest": "IngestMixin",
    "retrieve": "RetrieveMixin",
    "docs": "DocsMixin",
}

# 拆分后必须仍可调用的方法（名字 = 对外契约，别在重构里悄悄丢）
EXPECTED_METHODS = [
    "embeddings",
    "reranker",
    "vision",
    "store",
    "chat",
    "dedup",
    "index_lock",
    "query_expander",
    "refresh",
    "acquire",
    "release",
    "acquire_gpu",
    "release_gpu",
    "acquire_llm",
    "release_llm",
    "retrieve_locked",
    "ensure_index",
    "retrieve",
    "ask",
    "list_documents",
    "add_documents",
    "delete_document",
    "rebuild_index",
    "index_status",
]


def _relative_imports(path: Path) -> list[tuple[int, ast.ImportFrom]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
    ]


def _resolve(package: str, node: ast.ImportFrom) -> str:
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def test_service_modules_exist_with_expected_mixins():
    for module, cls_name in MIXINS.items():
        path = SERVICE_DIR / f"{module}.py"
        assert path.exists(), f"缺少 service/{module}.py"
        module_obj = importlib.import_module(f"{PACKAGE}.{module}")
        assert hasattr(module_obj, cls_name), f"service/{module}.py 缺少 {cls_name}"


def test_old_service_module_is_gone():
    """防双份回归：包化后不允许 app/rag/service.py 与 service/ 并存。"""
    assert not (RAG_DIR / "service.py").exists(), (
        "app/rag/service.py 又出现了：包与模块并存会让导入来源随环境漂移"
    )


def test_relative_imports_inside_mixins_resolve():
    broken: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, node in _relative_imports(path):
            target = _resolve(PACKAGE, node)
            try:
                importlib.import_module(target)
            except Exception as exc:  # noqa: BLE001 - 报错即层级不对
                broken.append(
                    f"{path.name}:{lineno} {'.' * node.level}{node.module or ''} -> {target} ({exc})"
                )
    assert not broken, "相对导入无法解析：\n" + "\n".join(broken)


def test_mixins_do_not_import_each_other():
    problems: list[str] = []
    for path in sorted(SERVICE_DIR.glob("*.py")):
        name = path.stem
        if name in ("__init__", "base"):
            continue
        for lineno, node in _relative_imports(path):
            if node.level != 1:
                continue
            target = (node.module or "").split(".")[0]
            if target in MIXINS and target not in (name, "base"):
                problems.append(f"{path.name}:{lineno} 导入了兄弟 mixin {target}")
    assert not problems, "\n".join(problems)


def test_docs_mixin_sees_upload_outcome():
    """回归：UploadOutcome 定义在 base，docs.add_documents 引用它——
    包化首版漏了 `from .base import UploadOutcome`，上传接口直接 500。"""
    docs = importlib.import_module(f"{PACKAGE}.docs")
    assert hasattr(docs, "UploadOutcome"), "docs 混入缺少 UploadOutcome（应从 base 导入）"


def test_composed_service_keeps_public_methods():
    from app.rag.service import RAGService

    missing = [m for m in EXPECTED_METHODS if not hasattr(RAGService, m)]
    assert not missing, f"拆分后丢失了这些方法：{missing}"
    names = {c.__name__ for c in RAGService.__mro__}
    for cls_name in MIXINS.values():
        assert cls_name in names, f"{cls_name} 未进入 MRO"


def test_assembly_stays_thin():
    """行数棘轮：__init__.py 只做装配（拆分前单文件 946 行）。"""
    lines = len((SERVICE_DIR / "__init__.py").read_text(encoding="utf-8").splitlines())
    assert lines <= 60, (
        f"service/__init__.py 已 {lines} 行（上限 60）：新增逻辑请放进对应 mixin"
        "（ingest / retrieve / docs，或共享基座 base）"
    )
