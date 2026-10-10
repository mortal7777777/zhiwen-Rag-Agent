"""后端 HTTP 调用与本地会话/历史文件读写。

所有请求都走 http_json（统一超时/错误处理），主循环只消费返回的结构。
从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from cli.term import paint, safe_text
from cli.format import fmt_tokens


SESSION_FILE = ".zhiwen_session.json"

# 最近一次上下文统计（供状态行 ctx_meter 使用）。定义在本层是因为写入方
# fetch_context_stats 在下层；由 cli_agent 导入同一个 dict 对象读取（只做
# 原地更新，不重新绑定，因此两边始终看到同一份数据）。
LAST_CONTEXT: dict = {"stats": None}


HISTORY_FILE = ".zhiwen_history.json"


def http_json(base_url: str, method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict | list:
    """通用 JSON 请求辅助（/context /compact /rewind 等新命令共用）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data if method == "POST" else None,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_conversation_titles(base_url: str) -> dict[int, str]:
    try:
        with urllib.request.urlopen(f"{base_url}/api/conversations", timeout=10) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        return {int(c.get("id")): str(c.get("title") or "新对话") for c in items}
    except Exception:
        return {}


def fetch_conversation_messages(base_url: str, conversation_id: int, limit: int = 6) -> list[dict]:
    """拉取会话最近的若干条消息（用于 /resume 后展示上下文）。"""
    try:
        url = f"{base_url}/api/conversations/{conversation_id}/messages"
        with urllib.request.urlopen(url, timeout=10) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        if not isinstance(items, list):
            return []
        # 只取最近的 N 条（消息按 id 升序返回）
        return items[-limit:]
    except Exception:
        return []


def fetch_model_label(base_url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{base_url}/api/settings", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        active = data.get("active_chat_provider") or ""
        for p in data.get("providers") or []:
            if p.get("id") == active:
                return p.get("model") or active
        return active or None
    except Exception:
        return None


def fetch_todos(base_url: str, conversation_id: int | None) -> None:
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/todos/{conversation_id}", timeout=10
        ) as resp:
            todos = json.loads(resp.read().decode("utf-8")).get("todos") or []
    except Exception as exc:
        print(paint(f"读取任务清单失败：{exc}", "red"))
        return
    if not todos:
        print(paint("当前没有任务清单。", "dim"))
        return
    print(paint("任务清单", "cyan", bold=True))
    for t in todos:
        mark = "[x]" if t.get("done") else "[ ]"
        color = "green" if t.get("done") else "dim"
        print(paint(f"  {mark} {safe_text(t.get('text', ''))}", color))


def fetch_usage(base_url: str, conversation_id: int | None, limit: int = 1) -> dict:
    if not conversation_id:
        return {}
    try:
        url = f"{base_url}/api/runs?conversation_id={conversation_id}&limit={limit}"
        with urllib.request.urlopen(url, timeout=4) as resp:
            runs = json.loads(resp.read().decode("utf-8")) or []
        usage: dict = {}
        for run in runs:
            u = run.get("token_usage") or {}
            for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
                usage[key] = int(usage.get(key) or 0) + int(u.get(key) or 0)
        return usage
    except Exception:
        return {}


def show_cost(base_url: str, conversation_id: int | None) -> None:
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    usage = fetch_usage(base_url, conversation_id, limit=200)
    if not usage:
        print(paint("暂无用量记录。", "dim"))
        return
    text = fmt_tokens(usage) or "暂无"
    print(paint(f"本会话 tokens：{text}", "dim"))


def fetch_context_stats(base_url: str, conversation_id: int | None) -> dict | None:
    """拉取上下文占用统计；失败静默（状态行缺上下文不影响使用）。"""
    if not conversation_id:
        return None
    try:
        stats = http_json(base_url, "GET", f"/api/agent/context/{conversation_id}", timeout=8)
        LAST_CONTEXT["stats"] = stats
        return stats
    except Exception:
        return None


def ctx_meter(stats: dict | None, width: int = 10) -> str:
    """紧凑上下文表：上下文 [██░░░░░░░░] 12%（token 占预算比例）。"""
    if not stats:
        return ""
    pct = context_pct(stats)
    filled = int(width * pct / 100)
    return f"上下文 {'█' * filled}{'░' * (width - filled)} {pct}%"


def context_pct(stats: dict | None) -> int:
    if not stats:
        return 0
    try:
        tokens = int(stats.get("estimated_tokens") or 0)
        budget = int(stats.get("token_budget") or 0)
        return min(100, round(100 * tokens / budget)) if budget else 0
    except Exception:
        return 0


def save_session(cwd: str, conversation_id: int) -> None:
    """按目录记录最近会话（最多 10 条，最近在前）。"""
    path = os.path.join(cwd, SESSION_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    history = [
        h
        for h in (data.get("history") or [])
        if h.get("conversation_id") != conversation_id
    ]
    history.insert(0, {"conversation_id": conversation_id, "updated_at": time.time()})
    data["history"] = history[:10]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def load_session_history(cwd: str) -> list[dict]:
    try:
        with open(os.path.join(cwd, SESSION_FILE), encoding="utf-8") as f:
            return (json.load(f) or {}).get("history") or []
    except Exception:
        return []


def load_history(cwd: str) -> list[str]:
    try:
        with open(os.path.join(cwd, HISTORY_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data if isinstance(x, str)][-200:]
    except Exception:
        return []


def save_history(cwd: str, history: list[str]) -> None:
    try:
        with open(os.path.join(cwd, HISTORY_FILE), "w", encoding="utf-8") as f:
            json.dump(history[-200:], f, ensure_ascii=False)
    except Exception:
        pass


def _cancel_run(base_url: str, run_id: str | None) -> bool:
    if not run_id:
        return False
    req = urllib.request.Request(
        f"{base_url}/api/agent/cancel/{run_id}", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
