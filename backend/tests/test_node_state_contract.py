"""图节点的状态契约（P1-4）：返回值必须已声明，且新增"直接写 state"要有意识。

两条隐性约定过去只写在注释里：
1. **节点内直接改 state 的字段不生效**，必须放进返回值 dict —— 本次实测
   有 47 处直接写（多数"改了同时返回同一个 list/值"所以行为正确，但极易
   踩坑：改了却忘了返回，就是静默丢失）。本文件用**棘轮**（ratchet）锁住
   数量：只允许减少，不允许增加。
2. **节点返回的键必须在 AgentState 里声明** —— 未声明字段虽然运行时能用
   （TypedDict total=False），但检查点/回放/契约测试都看不到它，是"隐形状态"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

NODES_DIR = Path(__file__).resolve().parents[1] / "app" / "agent" / "nodes"
STATE_FILE = Path(__file__).resolve().parents[1] / "app" / "agent" / "state.py"

# 直接写 state[...] 的棘轮基线（2026-10-08 实测；只允许下降）
STATE_WRITE_BASELINE = {
    "agent.py": 11,
    "finalize.py": 2,
    "prepare.py": 14,
    "subagent.py": 4,
    "tools.py": 16,
}


def _returns_in_own_scope(fn: ast.FunctionDef) -> list[ast.Return]:
    """收集函数**自身作用域**里的 return，不进入嵌套函数（嵌套返回的是工具结果 dict）。"""
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _declared_state_fields() -> set[str]:
    tree = ast.parse(STATE_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AgentState":
            return {
                n.target.id
                for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    raise AssertionError("未找到 AgentState 定义")


def _count_state_writes(path: Path) -> int:
    """统计直接写 state 的次数：state[k] = v 与 state[k].append/extend/..."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and getattr(target.value, "id", "") == "state":
                    count += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            holder = node.func.value
            if isinstance(holder, ast.Subscript) and getattr(holder.value, "id", "") == "state":
                count += 1
    return count


@pytest.mark.parametrize("filename", sorted(STATE_WRITE_BASELINE))
def test_direct_state_writes_do_not_grow(filename: str):
    """棘轮：直接写 state 的次数不得超过基线（新增写入请改为返回值，或解释后更新基线）。"""
    count = _count_state_writes(NODES_DIR / filename)
    baseline = STATE_WRITE_BASELINE[filename]
    assert count <= baseline, (
        f"{filename} 直接写 state 的次数从 {baseline} 增加到 {count}："
        "节点内直接改 state 不会生效，请把改动放进返回值 dict"
        "（确属必要请同步本文件的基线并在提交说明里写明原因）"
    )


def test_node_return_keys_are_declared_in_agent_state():
    """每个 *_node 函数返回的字面量键，必须在 AgentState 里声明。"""
    declared = _declared_state_fields()
    missing: dict[str, set[str]] = {}
    checked = 0
    for path in sorted(NODES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.endswith("_node")
        ]:
            checked += 1
            for node in _returns_in_own_scope(fn):
                if not isinstance(node.value, ast.Dict):
                    continue
                for key in node.value.keys:
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and key.value not in declared
                    ):
                        missing.setdefault(key.value, set()).add(f"{path.name}:{fn.name}")
    assert checked >= 7, f"应至少覆盖 7 个节点函数，实际只找到 {checked} 个"
    assert not missing, (
        "以下返回键未在 AgentState 声明（隐形状态）：\n"
        + "\n".join(f"  {k} <- {','.join(sorted(v))}" for k, v in sorted(missing.items()))
    )
