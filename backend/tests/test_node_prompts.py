"""轮内提示词字节快照（app/agent/nodes/prompts.py，2026-10-09 C-1 抽取时建立）。

为什么锁字节：这些文本进入消息链并参与 DeepSeek 等供应商的**前缀缓存键**，
改动任何一个字符都会使历史会话在该轮之后的缓存全部失效（cache_read 骤降、
成本上升）。本测试把当前文本固定下来——**修改提示词必然引发失败，这是有意
设计的**：改的时候同步更新快照，并在提交说明里注明缓存影响
（参见 CLAUDE.md「前缀缓存不变量」）。

同时覆盖本轮回迁的终止条件纯函数（真值表）。
"""

from __future__ import annotations

from app.agent.nodes import prompts as P


# ---------------- 静态常量 ----------------

def test_末次调用提示():
    assert P.LAST_CALL_WARNING == (
        "你只剩最后一次工具调用机会。若信息仍不完整，"
        "请利用这次机会做一次补充检索（优先 knowledge_base_search"
        " 或 web_search）；之后必须给出最终回答。"
        "若仍不足，明确告诉用户缺少什么，不要编造。"
    )


def test_xml重试提示():
    assert P.XML_RETRY_HINT == (
        "你上一条输出包含 XML 风格工具调用标记（如 <tool_calls>/<invoke>），"
        "未能解析执行。请改用标准工具调用格式继续（不要把工具调用写成 XML 文本），"
        "或直接给出最终回答。"
    )


def test_空正文重试提示():
    assert P.EMPTY_RETRY_HINT == (
        "你上一条输出没有产生任何正文（内容可能全部消耗在思考上、"
        "并被输出长度上限截断）。请直接输出给用户的回答正文，"
        "不要再重复思考；若信息确实不足，明确说明缺少什么。"
    )


def test_计划提交硬约束提示():
    assert P.PLAN_SUBMIT_HINT == (
        "你仍在计划模式：上一条回答没有调用任何工具就结束了。"
        "计划模式下只有两条路——需要继续了解现状就用只读工具探索；"
        "探索完成后必须调用 exit_plan_mode 提交结构化步骤（steps 数组），"
        "不要把计划写成正文。请立即调用工具完成提交。"
    )


def test_计划提交硬约束提示_预算耗尽版():
    assert P.PLAN_SUBMIT_HINT_FORCED == (
        "你的工具调用预算已耗尽，但计划尚未提交。不要再调用探索工具、"
        "不要再输出正文汇报——立即调用 exit_plan_mode，把已经调研到的内容"
        "整理为结构化步骤（steps 数组）提交，这是本轮的强制要求。"
    )


# ---------------- 构建函数（固定入参渲染快照） ----------------

def test_派发上限与忽略提示():
    assert P.dispatch_cap_notice(3) == (
        "本轮派发已达上限（3 轮），不再受理新的派发声明。请自己完成剩余工作或直接作答。"
    )
    assert P.dispatch_ignored_notice([{"name": "read_file"}, {"name": "bash"}]) == (
        "本轮已受理派发声明，同轮的其它工具调用未执行（read_file, bash）。"
        "子任务结果返回后，如仍需这些调用请重新发起。"
    )
    assert P.downgraded_notice(2) == (
        "本轮声明了多个 execute 子任务，只放开第一个；其余 2 个已按 research 只读执行"
        "（并行写同一目录有冲突风险）。需要串行执行请下一轮再派。"
    )


def test_计划硬约束提示_字段回退():
    # 元素是 dict：text 优先、退回 step（否则进度行会出现空步骤）
    assert P.plan_remaining_notice([{"text": "步骤A"}, {"step": "步骤B"}]) == (
        "【计划硬约束】以下步骤尚未完成：步骤A；步骤B\n"
        "请继续完成它们，不要提前输出最终回答；"
        "若某步确实无需执行或已完成，先用 todo_update "
        "complete 标记为已完成（无需执行的用 remove 删除），"
        "再结束。"
    )


def test_计划确认提示():
    assert P.plan_confirmed_notice(["读取文档", "汇总指标"]) == (
        "用户已确认执行计划（共 2 步），已退出计划模式并播种任务清单，"
        "请立即按顺序开始执行（不要重新规划）：\n"
        "1. 读取文档\n2. 汇总指标\n"
        "每完成一步立即用 todo_update complete 标记；"
        "有未完成步骤时不要提前给出最终回答。"
    )


def test_hook与工具失败提示():
    assert P.hook_context_notice(["[a] ctx1", "[b] ctx2"]) == (
        "PostToolUse hook 附加信息：\n[a] ctx1\n[b] ctx2"
    )
    assert P.tool_failure_notice("bash", "boom") == (
        "工具 bash 执行失败：boom\n"
        "这是一次可重试的失败，不消耗你的工具调用预算。"
        "请换一种方式继续完成任务，不要就此停止：\n"
        "1. 先 list_dir / read_file / grep_search 确认路径与现状；\n"
        "2. 按错误信息修正参数（例如改用工作目录内的相对路径，或先创建父目录）；\n"
        "3. 若目标确实不可达（如在工作目录之外），向用户说明限制，"
        "并给出可落地的替代方案（写入工作目录内、修改工作目录等），"
        "询问用户后再继续。\n"
        "任务未完成前，请持续尝试合理的替代方法。"
    )


def test_写后验证提示_通过与失败():
    assert P.verify_pass_notice(["### f1  (cmd1)\nOK"]) == (
        "写后自动验证结果（全部通过）：\n### f1  (cmd1)\nOK\n\n可以继续后续步骤。"
    )
    assert P.verify_fail_notice(["L1", "L2"], 1, 2, 1) == (
        "写后自动验证失败（1 个文件，第 2 次失败）：\nL1\n\nL2\n\n"
        "请根据上面的错误修复文件（edit_file/write_file）并等待复验，还有 1 次复验机会。"
    )
    # 重试用尽：提示改为"如实说明"
    assert P.verify_fail_notice(["L1"], 1, 2, 0).endswith(
        "已到验证重试上限。请停止继续修复，向用户如实说明验证未通过的原因、影响范围与建议。"
    )


def test_计划进度提示():
    assert P.plan_progress_notice(1, 3, [{"text": "当前步"}, {"text": "尾步"}]) == (
        "计划进度：已完成 1/3 步。\n当前步骤：当前步\n剩余步骤：当前步；尾步\n"
        "硬约束：剩余步骤尚未完成，请继续按顺序执行；不要提前输出最终回答。"
    )
    assert P.plan_progress_notice(3, 3, []) == (
        "计划进度：已完成 3/3 步。\n剩余：无，可以基于已获取的信息作答。"
    )


def test_强制收尾提示_两分支():
    assert P.budget_exhausted_notice(True) == (
        "本轮项目任务的工具调用预算已用尽，但任务尚未完成。"
        "请给出【进度汇报】：已完成了哪些步骤、还有哪些未完成、"
        "下一步计划做什么；并明确告诉用户：回复“继续”即可接着做。"
        "不要假装任务已完成。"
    )
    assert P.budget_exhausted_notice(False) == (
        "你已达到本轮允许的工具调用次数上限。"
        "请基于已经获取的信息直接给出最终回答；"
        "如果信息仍然不足，请明确告诉用户缺少什么，不要继续调用工具。"
    )
    assert P.failure_limit_notice(True) == (
        "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
        "请给出【进度汇报】：任务卡在哪一步、失败原因、用户需要做什么"
        "（例如修改工作目录、提供权限、确认替代路径）；"
        "并告诉用户回复“继续”即可接着做。不要再尝试调用工具。"
    )
    assert P.failure_limit_notice(False) == (
        "你已连续多次遇到工具执行失败（超过允许的失败重试上限）。"
        "请基于已经获得的信息给出最终回答，向用户如实说明："
        "任务卡在哪一步、失败原因、以及用户需要做什么（例如修改工作目录、"
        "提供权限、确认替代路径）。不要再尝试调用工具。"
    )


# ---------------- 终止条件纯函数（真值表） ----------------

def test_强制收尾判定():
    from app.agent.nodes.tools import _should_force_final

    assert _should_force_final(current=12, threshold=12, forced_final=False, status="ok")
    assert not _should_force_final(current=11, threshold=12, forced_final=False, status="ok")
    assert not _should_force_final(current=12, threshold=12, forced_final=True, status="ok")
    assert not _should_force_final(current=12, threshold=12, forced_final=False, status="stopped")


def test_末次调用提示判定():
    from app.agent.nodes.agent import _should_warn_last_call

    base = dict(has_tools=True, plan_steps=["a"], remaining_calls=1, forced_final=False, warned=False)
    assert _should_warn_last_call(**base)
    assert not _should_warn_last_call(**{**base, "has_tools": False})
    assert not _should_warn_last_call(**{**base, "plan_steps": []})
    assert not _should_warn_last_call(**{**base, "remaining_calls": 2})
    assert not _should_warn_last_call(**{**base, "forced_final": True})
    assert not _should_warn_last_call(**{**base, "warned": True})


def test_xml重试判定():
    from app.agent.nodes.agent import _should_retry_xml
    from app.agent.utils import _XML_TOOL_MARKERS

    marker = list(_XML_TOOL_MARKERS)[0]
    assert _should_retry_xml(
        tool_calls=[], merged_text=f"{marker}xx", forced_final=False, retry_count=0
    )
    assert not _should_retry_xml(
        tool_calls=[], merged_text=f"{marker}xx", forced_final=False, retry_count=2
    )
    assert not _should_retry_xml(
        tool_calls=[{"name": "bash"}], merged_text=f"{marker}xx", forced_final=False, retry_count=0
    )
    assert not _should_retry_xml(
        tool_calls=[], merged_text="普通文本", forced_final=False, retry_count=0
    )


def test_计划提交推回判定():
    from app.agent.nodes.agent import _should_push_plan_submit
    from app.agent.utils import _XML_TOOL_MARKERS

    marker = list(_XML_TOOL_MARKERS)[0]
    base = dict(
        plan_only=True,
        tool_calls=[],
        merged_text="计划如下：1. 新建页面",
        status="ok",
        push_count=0,
    )
    assert _should_push_plan_submit(**base)
    assert not _should_push_plan_submit(**{**base, "plan_only": False})
    assert not _should_push_plan_submit(
        **{**base, "tool_calls": [{"name": "exit_plan_mode"}]}
    )
    assert not _should_push_plan_submit(**{**base, "merged_text": "   "})
    assert not _should_push_plan_submit(
        **{**base, "merged_text": f"{marker}xx"}
    ), "XML 标记场景交由 xml 重试处理，两条提示不叠加"
    assert not _should_push_plan_submit(**{**base, "status": "stopped"})
    assert not _should_push_plan_submit(**{**base, "push_count": 3})
