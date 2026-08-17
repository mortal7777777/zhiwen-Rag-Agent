"""用户自写工具加载器：扫描 backend/app/user_tools/*.py，动态注册 LangChain 工具。

背景：Agent 通过本机制实现"自我扩展工具"能力（配合 tool-authoring 技能）——
用户要求新能力时，Agent 按技能规范写好工具文件放入 user_tools/ 目录，
下一次请求的 _prepare_node 重建工具列表时自动加载，无需重启后端。

每个 .py 文件约定：
- 定义一或多个 LangChain 工具：@tool 装饰器、StructuredTool.from_function、
  或模块级 ``tools`` 列表（list[BaseTool]）；
- 可导出 ``SENSITIVE`` 集合声明哪些工具是敏感操作（写/改/删/命令，需人工确认）；
  未声明该变量的模块，其全部工具默认按敏感处理（安全默认）；
- 模块顶层只做工具定义，重量逻辑放函数体内（每次请求都会重新 import）；
- 工具必须线程安全：不得触碰请求级 db Session，需要数据库时自行开独立
  短会话（SessionLocal）；使用 GPU 模型时用 rag_service.acquire_gpu()/release_gpu()；
- 返回 dict 带 summary 键；失败给 error（或非零 exit_code）供失败判定复用。

单个文件 import 失败只记日志跳过，不影响其他工具加载。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# 用户工具目录：backend/app/user_tools/
USER_TOOLS_DIR = Path(__file__).resolve().parent / "user_tools"

# 工具名 -> 是否敏感（由各模块的 SENSITIVE 声明汇总，供 permissions 查询）
# 每次 load_user_tools() 时重建
_SENSITIVE_BY_TOOL: dict[str, bool] = {}


def is_user_tool_sensitive(name: str) -> bool | None:
    """查询用户自写工具的敏感标记；非用户工具返回 None（交由默认规则）。"""
    return _SENSITIVE_BY_TOOL.get(name)


def _import_module(py: Path):
    """导入单个工具模块（独立模块对象，避免 sys.modules 缓存陈旧代码）。"""
    spec = importlib.util.spec_from_file_location(
        f"app.user_tools.{py.stem}", py
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {py.name} 创建模块 spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _collect_tools(mod) -> list:
    """收集模块内的 BaseTool：模块级属性 + 可选 tools 列表，去重保序。"""
    found: list = []
    seen: set[int] = set()
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        # 只认 langchain BaseTool 实例；@tool 装饰器产物即 StructuredTool 子类
        if isinstance(obj, BaseTool) and id(obj) not in seen:
            seen.add(id(obj))
            found.append(obj)
    listed = getattr(mod, "tools", None)
    if isinstance(listed, (list, tuple)):
        for t in listed:
            if isinstance(t, BaseTool) and id(t) not in seen:
                seen.add(id(t))
                found.append(t)
    return found


def load_user_tools() -> list:
    """扫描 user_tools/ 目录并返回全部用户工具（失败模块跳过）。"""
    global _SENSITIVE_BY_TOOL
    _SENSITIVE_BY_TOOL = {}
    tools: list = []
    if not USER_TOOLS_DIR.is_dir():
        return tools
    for py in sorted(USER_TOOLS_DIR.glob("*.py")):
        if py.name.startswith("_"):
            continue  # 下划线前缀 = 内部辅助模块，不作为工具
        try:
            mod = _import_module(py)
        except Exception as exc:
            logger.warning("用户工具 %s 导入失败，已跳过：%s", py.name, exc)
            continue
        found = _collect_tools(mod)
        if not found:
            logger.info("用户工具模块 %s 未定义任何工具", py.name)
            continue
        declared = getattr(mod, "SENSITIVE", None)
        if declared is None:
            # 未声明：安全默认，全部按敏感处理（读类工具请显式声明 SENSITIVE = set()）
            for t in found:
                _SENSITIVE_BY_TOOL[t.name] = True
        else:
            declared_set = set(declared)
            for t in found:
                _SENSITIVE_BY_TOOL[t.name] = t.name in declared_set
        tools.extend(found)
        logger.info("已加载用户工具 %d 个（来自 %s）", len(found), py.name)
    return tools
