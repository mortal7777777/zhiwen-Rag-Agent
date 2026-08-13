"""生命周期 Hooks：PreToolUse / PostToolUse 用户脚本回调。

设计（对齐 Claude Code hooks 思路）：
- 配置存 app_meta 键 `hooks`：[{name, command, events:[pre_tool_use, post_tool_use], timeout}]；
- hook 进程 stdin 收到 JSON {hook_event_name, tool_name, tool_input}；
- stdout 输出 JSON {decision: approve|deny|ignore, reason, additional_context}；
- PreToolUse 返回 deny 时阻止工具执行；PostToolUse 的 additional_context 回填模型。
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess

from .db import repository as repo

logger = logging.getLogger(__name__)


def load_hooks(db) -> list[dict]:
    if db is None:
        return []
    try:
        raw = repo.get_meta(db, "hooks")
        data = json.loads(raw) if raw else []
    except Exception as exc:
        logger.warning("读取 hooks 配置失败：%s", exc)
        return []
    if not isinstance(data, list):
        return []
    return [
        h
        for h in data
        if isinstance(h, dict) and str(h.get("command") or "").strip()
    ]


def _run_one(
    hook: dict,
    event: str,
    tool_name: str,
    payload: dict,
) -> dict:
    name = str(hook.get("name") or hook.get("command") or "hook")
    input_json = json.dumps(
        {
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": payload,
        },
        ensure_ascii=False,
    )
    timeout = max(1, int(hook.get("timeout") or 15))
    try:
        # 不经过 shell，避免注入/管道副作用；复杂脚本请自行包装成可执行命令
        parts = shlex.split(str(hook.get("command")), posix=False)
        proc = subprocess.run(
            parts,
            input=input_json,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "").strip()
        try:
            result = json.loads(out) if out else {}
        except json.JSONDecodeError:
            result = {"raw": out[:500]}
        return {
            "name": name,
            "decision": str(result.get("decision") or "ignore"),
            "reason": str(result.get("reason") or ""),
            "additional_context": str(result.get("additional_context") or ""),
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "decision": "ignore",
            "reason": f"hook 超时（>{timeout}s）",
            "additional_context": "",
            "exit_code": None,
        }
    except Exception as exc:
        return {
            "name": name,
            "decision": "ignore",
            "reason": str(exc),
            "additional_context": "",
            "exit_code": None,
        }


def run_hooks(
    db,
    event: str,
    tool_name: str,
    payload: dict,
) -> list[dict]:
    """运行订阅了该事件的所有 hooks，返回结果列表。"""
    results = []
    for hook in load_hooks(db):
        if event not in (hook.get("events") or []):
            continue
        result = _run_one(hook, event, tool_name, payload)
        results.append(result)
        logger.info(
            "hook %s %s %s -> %s",
            result["name"],
            event,
            tool_name,
            result["decision"],
        )
    return results
