"""轮内提示词（从 nodes/{agent,tools}.py 内联注入抽出，2026-10-09）。

这里只放**轮内注入的 SystemMessage 文本**：预算/重试/派发/验证/进度等
瞬时提示。静态系统提示（角色、工具规则、技能目录）在 agent/prompts.py。

⚠️ 字节稳定性契约：这些文本会进入消息链并参与 DeepSeek 等供应商的
**前缀缓存键**——改动任何一个字符都会使历史会话在该轮之后的缓存全部失效
（表现为 cache_read 骤降、成本上升）。修改提示词属于破坏性变更，需在
提交说明里注明；`tests/test_node_prompts.py` 用快照锁住当前字节。

纯函数约定：全部无副作用、不读全局状态，参数进来、字符串出去，
供节点与原单测直接调用。
"""

from __future__ import annotations

# ---------------- agent 节点 ----------------

#: 预算将尽（还剩最后一次工具调用，且有计划步骤）
LAST_CALL_WARNING = (
    "你只剩最后一次工具调用机会。若信息仍不完整，"
    "请利用这次机会做一次补充检索（优先 knowledge_base_search"
    " 或 web_search）；之后必须给出最终回答。"
    "若仍不足，明确告诉用户缺少什么，不要编造。"
)

#: XML 工具调用标记解析失败的有界重试提示
XML_RETRY_HINT = (
    "你上一条输出包含 XML 风格工具调用标记（如 <tool_calls>/<invoke>），"
    "未能解析执行。请改用标准工具调用格式继续（不要把工具调用写成 XML 文本），"
    "或直接给出最终回答。"
)

#: 空正文（输出预算被思考吃光）重试提示；重试轮会改用无思考模型
EMPTY_RETRY_HINT = (
    "你上一条输出没有产生任何正文（内容可能全部消耗在思考上、"
    "并被输出长度上限截断）。请直接输出给用户的回答正文，"
    "不要再重复思考；若信息确实不足，明确说明缺少什么。"
)

#: 计划模式提交硬约束：无工具调用收尾（把计划写成正文）时推回提示。
#: 弱模型/网关（2026-10-10 OpenCode Go 实测）会把计划当正文输出而不调
#: exit_plan_mode，导致确认卡永不出现；此处明确两条合法路径并要求提交。
PLAN_SUBMIT_HINT = (
    "你仍在计划模式：上一条回答没有调用任何工具就结束了。"
    "计划模式下只有两条路——需要继续了解现状就用只读工具探索；"
    "探索完成后必须调用 exit_plan_mode 提交结构化步骤（steps 数组），"
    "不要把计划写成正文。请立即调用工具完成提交。"
)

#: 预算已耗尽且计划未提交：只剩"提交"一条路。
#: 2026-10-10 实测：探索烧光预算后，既有"请汇报进度"提示与提交要求冲突，
#: 模型选择继续探索/正文汇报；此处不留探索出口（tools 预算分支与 agent 推回共用）。
PLAN_SUBMIT_HINT_FORCED = (
    "你的工具调用预算已耗尽，但计划尚未提交。不要再调用探索工具、"
    "不要再输出正文汇报——立即调用 exit_plan_mode，把已经调研到的内容"
    "整理为结构化步骤（steps 数组）提交，这是本轮的强制要求。"
)


def dispatch_cap_notice(dispatch_cap: int) -> str:
    return (
        f"本轮派发已达上限（{dispatch_cap} 轮），不再受理新的派发声明。"
        "请自己完成剩余工作或直接作答。"
    )


def dispatch_ignored_notice(others: list) -> str:
    return (
        "本轮已受理派发声明，同轮的其它工具调用未执行"
        f"（{', '.join(str((tc or {}).get('name') or '') for tc in others)}）。"
        "子任务结果返回后，如仍需这些调用请重新发起。"
    )


def downgraded_notice(downgraded: int) -> str:
    return (
        f"本轮声明了多个 execute 子任务，只放开第一个；"
        f"其余 {downgraded} 个已按 research 只读执行"
        "（并行写同一目录有冲突风险）。需要串行执行请下一轮再派。"
    )


def plan_remaining_notice(remaining: list) -> str:
    remaining_text = "；".join(
        str(item.get("text") or item.get("step") or item)
        for item in remaining[:6]
    )
    return (
        "【计划硬约束】以下步骤尚未完成："
        f"{remaining_text}\n"
        "请继续完成它们，不要提前输出最终回答；"
        "若某步确实无需执行或已完成，先用 todo_update "
        "complete 标记为已完成（无需执行的用 remove 删除），"
        "再结束。"
    )


# ---------------- tools 节点 ----------------


def plan_confirmed_notice(steps: list) -> str:
    return (
        f"用户已确认执行计划（共 {len(steps)} 步），已退出计划模式并播种任务清单，"
        "请立即按顺序开始执行（不要重新规划）：\n"
        + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        + "\n每完成一步立即用 todo_update complete 标记；"
        "有未完成步骤时不要提前给出最终回答。"
    )


def hook_context_notice(contexts: list) -> str:
    return "PostToolUse hook 附加信息：\n" + "\n".join(contexts)


def tool_failure_notice(name: str, error: str) -> str:
    return (
        f"工具 {name} 执行失败：{error}\n"
        "这是一次可重试的失败，不消耗你的工具调用预算。"
        "请换一种方式继续完成任务，不要就此停止：\n"
        "1. 先 list_dir / read_file / grep_search 确认路径与现状；\n"
        "2. 按错误信息修正参数（例如改用工作目录内的相对路径，或先创建父目录）；\n"
        "3. 若目标确实不可达（如在工作目录之外），向用户说明限制，"
        "并给出可落地的替代方案（写入工作目录内、修改工作目录等），"
        "询问用户后再继续。\n"
        "任务未完成前，请持续尝试合理的替代方法。"
    )


def verify_pass_notice(lines: list) -> str:
    return (
        "写后自动验证结果（全部通过）：\n"
        + "\n\n".join(lines)
        + "\n\n可以继续后续步骤。"
    )


def verify_fail_notice(
    lines: list, failed_files: int, fail_count: int, retries_left: int
) -> str:
    """lines 是全部验证结果行；failed_files 是其中失败的文件数（两者可不等）。"""
    msg = (
        f"写后自动验证失败（{failed_files} 个文件，第 {fail_count} 次失败）：\n"
        + "\n\n".join(lines)
    )
    if retries_left > 0:
        msg += (
            f"\n\n请根据上面的错误修复文件（edit_file/write_file）并等待复验，"
            f"还有 {retries_left} 次复验机会。"
        )
    else:
        msg += (
            "\n\n已到验证重试上限。请停止继续修复，"
            "向用户如实说明验证未通过的原因、影响范围与建议。"
        )
    return msg


def plan_progress_notice(done: int, total: int, remaining: list) -> str:
    progress = [f"计划进度：已完成 {done}/{total} 步。"]
    if remaining:
        current = remaining[0]
        progress.append(f"当前步骤：{current.get('text', '')}")
        progress.append(
            "剩余步骤："
            + "；".join(str(t.get("text", "")) for t in remaining)
        )
        # 与 agent 节点的硬约束一致：有未完成步骤就提示（不再按关键词 hint 过滤）
        progress.append(
            "硬约束：剩余步骤尚未完成，请继续按顺序执行；不要提前输出最终回答。"
        )
    else:
        progress.append("剩余：无，可以基于已获取的信息作答。")
    return "\n".join(progress)


def budget_exhausted_notice(task_mode: bool) -> str:
    return (
        (
            "本轮项目任务的工具调用预算已用尽，但任务尚未完成。"
            "请给出【进度汇报】：已完成了哪些步骤、还有哪些未完成、"
            "下一步计划做什么；并明确告诉用户：回复“继续”即可接着做。"
            "不要假装任务已完成。"
        )
        if task_mode
        else (
            "你已达到本轮允许的工具调用次数上限。"
            "请基于已经获取的信息直接给出最终回答；"
            "如果信息仍然不足，请明确告诉用户缺少什么，不要继续调用工具。"
        )
    )


def failure_limit_notice(task_mode: bool) -> str:
    return (
        (
            "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
            "请给出【进度汇报】：任务卡在哪一步、失败原因、用户需要做什么"
            "（例如修改工作目录、提供权限、确认替代路径）；"
            "并告诉用户回复“继续”即可接着做。不要再尝试调用工具。"
        )
        if task_mode
        else (
            "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
            "请基于已经获得的信息给出最终回答，向用户如实说明："
            "任务卡在哪一步、失败原因、以及用户需要做什么（例如修改工作目录、"
            "提供权限、确认替代路径）。不要再尝试调用工具。"
        )
    )
