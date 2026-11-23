"""变更前预校验：在工具真正执行前拦掉明显错误的 mutation。

只做确定性检查（目标缺失、枚举非法、新旧目标相同、转人工需用户明确要求），
不猜测业务 gold 答案。失败结果复用 tools 节点现有的 retry 协议，让模型看到
错误后自行修正。
"""

from __future__ import annotations

from .tool_contracts import ToolContract

_HUMAN_KEYWORDS = (
    "human",
    "real person",
    "speak to a person",
    "talk to a person",
    "speak to an agent",
    "talk to an agent",
    "transfer me",
    "connect me to a human",
    "connect me to an agent",
    "supervisor",
    "人工",
    "真人",
    "转人工",
    "主管",
)


def _last_user_text(text: str) -> str:
    return (text or "").casefold()


def _user_asked_human(text: str) -> bool:
    normalized = _last_user_text(text)
    return any(keyword in normalized for keyword in _HUMAN_KEYWORDS)


def validate_call(
    contract: ToolContract | None,
    name: str,
    args: dict,
    *,
    last_user_text: str = "",
    enabled: bool = True,
    check_consent: bool = True,
) -> str | None:
    """返回错误说明；None 表示放行。"""
    if not enabled or contract is None:
        return None

    if contract.target_arg:
        value = (args or {}).get(contract.target_arg)
        if value in (None, "", [], {}):
            return (
                f"{name} 缺少目标参数 {contract.target_arg}；"
                "请先确定要操作的对象再执行。"
            )

    for key, allowed in (contract.enum_args or {}).items():
        if key not in (args or {}):
            continue
        value = str(args[key])
        if value not in allowed:
            return (
                f"{name} 的 {key} 取值非法（{value!r}）；"
                f"可选值：{', '.join(sorted(allowed))}。"
            )

    for old_key, new_key in contract.compare_pairs:
        old = (args or {}).get(old_key)
        new = (args or {}).get(new_key)
        if isinstance(old, (list, tuple)) and isinstance(new, (list, tuple)):
            if len(old) != len(new):
                return (
                    f"{name} 的 {old_key} 与 {new_key} 数量不一致"
                    f"（{len(old)} vs {len(new)}）；请逐一对应后再执行。"
                )
            if old and all(str(a) == str(b) for a, b in zip(old, new)):
                return (
                    f"{name} 的新旧目标完全相同，这次修改不会产生任何变化；"
                    "请选择真正不同的目标，或直接告知用户无需修改。"
                )
        elif old is not None and new is not None and str(old) == str(new):
            return (
                f"{name} 的新旧目标完全相同，这次修改不会产生任何变化；"
                "请选择真正不同的目标，或直接告知用户无需修改。"
            )

    if (
        contract.requires_consent
        and check_consent
        and not _user_asked_human(last_user_text)
    ):
        return (
            "用户没有明确要求转人工。请先自行尝试用现有工具解决问题；"
            "只有在确实超出权限、且已向用户说明已完成/未完成事项后，才可转人工。"
        )

    return None
