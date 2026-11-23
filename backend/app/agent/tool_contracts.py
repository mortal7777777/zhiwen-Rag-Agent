"""工具契约注册表：声明哪些工具会改状态、改完用哪个工具回读、答复里必须提哪些字段。

契约是纯元数据，不改变工具数组、不产生模型可见的 tool_call。用户自写工具
模块（如 ``user_tools/tau2_bridge.py``）可导出模块级 ``TOOL_CONTRACTS``，
由 ``user_tool_loader`` 汇总到这里；核心工具（写文件/编辑/删除）在此声明。

注册表是进程级只读快照：``load_user_tools()`` 每个请求都会调用，因此这里
**不做 per-request reset**（并发请求会互相清空）。first-wins + 锁；测试用
``reset_contracts()`` 显式重置。请求级使用 ``contracts_snapshot()`` 拷贝。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContract:
    """单个工具的副作用契约。

    target_arg / target_from_result 用于定位"这次改的是谁"；
    readback_tool + readback_arg 用于写后回读；
    answer_* / status_keywords / amount_mode 用于 finalize 答复核对。
    """

    mutating: bool = False
    target_arg: str | None = None
    target_from_result: str | None = None
    readback_tool: str | None = None
    readback_arg: str | None = None
    readback_from_result: bool = False
    compare_pairs: tuple[tuple[str, str], ...] = ()
    enum_args: Mapping[str, frozenset[str]] = field(default_factory=dict)
    verified_fields: tuple[str, ...] = ()
    answer_id_args: tuple[str, ...] = ()
    answer_result_fields: tuple[str, ...] = ()
    status_keywords: tuple[str, ...] = ()
    amount_mode: str = "last"
    requires_consent: bool = False


# 生产文件工具：写/编辑后回读一次确认落盘；删除后无法回读，只记账。
# 不设 answer_id_args：那只是"答复里有没有回显路径"的格式核对，并不代表
# 写入成功；真正的验证走 readback_tool。显式 answer_* 契约（评测）仍可声明。
CORE_CONTRACTS: dict[str, ToolContract] = {
    "write_file": ToolContract(
        mutating=True,
        target_arg="path",
        readback_tool="read_file",
        readback_arg="path",
    ),
    "edit_file": ToolContract(
        mutating=True,
        target_arg="path",
        readback_tool="read_file",
        readback_arg="path",
    ),
    "delete_file": ToolContract(
        mutating=True,
        target_arg="path",
    ),
}

_FIELDS = set(ToolContract.__dataclass_fields__)
_LOCK = threading.RLock()
_REGISTRY: dict[str, ToolContract] = {}
_SOURCES: dict[str, str] = {}


def _coerce(raw: ToolContract | Mapping[str, Any]) -> ToolContract:
    if isinstance(raw, ToolContract):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError(f"contract 必须是 ToolContract 或 dict，收到 {type(raw)!r}")
    unknown = set(raw) - _FIELDS
    if unknown:
        logger.warning("工具契约含未知字段 %s，已忽略", sorted(unknown))
    data: dict[str, Any] = {k: raw[k] for k in _FIELDS if k in raw}
    for key in (
        "verified_fields",
        "answer_id_args",
        "answer_result_fields",
        "status_keywords",
    ):
        if key in data:
            data[key] = tuple(data[key])
    if "compare_pairs" in data:
        data["compare_pairs"] = tuple(tuple(pair) for pair in data["compare_pairs"])
    if "enum_args" in data:
        data["enum_args"] = {
            str(k): frozenset(str(x) for x in v)
            for k, v in data["enum_args"].items()
        }
    return ToolContract(**data)


def register_contracts(
    mapping: Mapping[str, ToolContract | Mapping[str, Any]] | None,
    *,
    source: str = "",
) -> None:
    """合并契约；同名 first-wins，冲突只告警不覆盖。"""
    if not mapping:
        return
    with _LOCK:
        for name, raw in mapping.items():
            try:
                contract = _coerce(raw)
            except Exception as exc:
                logger.warning("跳过非法工具契约 %s（%s）：%s", name, source, exc)
                continue
            existing = _REGISTRY.get(name)
            if existing is not None:
                if existing != contract:
                    logger.warning(
                        "工具契约冲突 %s：保留 %s，忽略 %s",
                        name,
                        _SOURCES.get(name, "?"),
                        source or "?",
                    )
                continue
            _REGISTRY[str(name)] = contract
            _SOURCES[str(name)] = source or "?"


def get_contract(name: str) -> ToolContract | None:
    with _LOCK:
        return _REGISTRY.get(name)


def contracts_snapshot() -> dict[str, ToolContract]:
    """请求级快照；调用方不要修改返回的 contract 对象。"""
    with _LOCK:
        return dict(_REGISTRY)


def reset_contracts() -> None:
    """仅测试用：清空后重新注册核心契约。"""
    with _LOCK:
        _REGISTRY.clear()
        _SOURCES.clear()
    register_contracts(CORE_CONTRACTS, source="core")


register_contracts(CORE_CONTRACTS, source="core")
