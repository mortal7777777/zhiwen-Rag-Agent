"""后端 HTTP 调用与本地会话/历史文件读写。

所有请求都走 http_json（统一超时/错误处理），主循环只消费返回的结构。
从 cli_agent.py 拆出（2026-10-08 物理拆分）：只做移动，未改逻辑。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from cli.term import paint, safe_text
from cli.format import fmt_num, todo_mark


SESSION_FILE = ".zhiwen_session.json"

# 最近一次上下文统计（供状态行 ctx_meter 使用）。定义在本层是因为写入方
# fetch_context_stats 在下层；由 cli_agent 导入同一个 dict 对象读取（只做
# 原地更新，不重新绑定，因此两边始终看到同一份数据）。
LAST_CONTEXT: dict = {"stats": None}


HISTORY_FILE = ".zhiwen_history.json"


def auth_headers() -> dict:
    """后端启用 ZHIWEN_API_TOKEN 时需要携带的请求头（未设置则为空）。"""
    token = os.environ.get("ZHIWEN_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def http_error_detail(exc: urllib.error.HTTPError) -> str:
    """读后端 JSON detail，避免 409 只显示成 "HTTP Error 409: Conflict"。"""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, str) and detail.strip():
            return f"HTTP {exc.code}: {detail.strip()}"
    except Exception:
        pass
    return f"HTTP {exc.code}: {exc.reason}"


def http_json(base_url: str, method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict | list:
    """通用 JSON 请求辅助（/context /compact /rewind 等新命令共用）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    headers.update(auth_headers())
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data if method == "POST" else None,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_backend(base_url: str, timeout: float = 2.0) -> dict | None:
    """CLI 启动预检：后端不可达返回 None（不抛异常）。"""
    try:
        data = http_json(base_url, "GET", "/api/health", timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(paint(
                "后端已启用 API Token 认证：请设置环境变量 ZHIWEN_API_TOKEN（与后端一致）后重试。",
                "red",
            ))
        return None
    except Exception:  # noqa: BLE001 - 预检失败统一转成 None
        return None
    return data if isinstance(data, dict) else {}


def fetch_conversations(base_url: str) -> list[dict]:
    """服务端全部会话（按最近更新排序，最多 200 条）。"""
    req = urllib.request.Request(
        f"{base_url}/api/conversations", headers=auth_headers()
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        items = json.loads(resp.read().decode("utf-8"))
    return items if isinstance(items, list) else []


def fetch_conversation_titles(base_url: str) -> dict[int, str]:
    try:
        items = fetch_conversations(base_url)
        return {int(c.get("id")): str(c.get("title") or "新对话") for c in items}
    except Exception:
        return {}


def upload_attachment(base_url: str, path) -> dict:
    """把本地文件上传到 /api/agent/attachments，返回附件元数据（含 id）。

    纯标准库 multipart：附件只用于下一轮上下文注入，不进入知识库。
    """
    from pathlib import Path

    p = Path(path)
    boundary = f"----zhiwen{uuid.uuid4().hex}"
    name = p.name.replace('"', "_")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    headers.update(auth_headers())
    req = urllib.request.Request(
        f"{base_url}/api/agent/attachments",
        data=head + p.read_bytes() + tail,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    items = payload.get("attachments") or []
    if not items:
        raise RuntimeError("服务端未返回附件 ID")
    return items[0]


def fetch_conversation_messages(base_url: str, conversation_id: int, limit: int = 6) -> list[dict]:
    """拉取会话最近的若干条消息（用于 /resume 后展示上下文）。"""
    try:
        url = f"{base_url}/api/conversations/{conversation_id}/messages"
        req = urllib.request.Request(url, headers=auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        if not isinstance(items, list):
            return []
        # 只取最近的 N 条（消息按 id 升序返回）
        return items[-limit:]
    except Exception:
        return []


def fetch_pending(base_url: str, conversation_id: int) -> dict:
    """挂起/运行状态：{pending, interrupted, active, created_at, ops, plan}。

    审批卡打开期间轮询它：permission_timeout 到点后服务端会自动拒绝并继续，
    客户端必须感知到 pending 消失，不能把一个已不存在的决定再提交上去。
    """
    try:
        data = http_json(
            base_url, "GET", f"/api/agent/pending/{conversation_id}", timeout=5
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fetch_model_label(base_url: str) -> str | None:
    try:
        req = urllib.request.Request(
            f"{base_url}/api/settings", headers=auth_headers()
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
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
        req = urllib.request.Request(
            f"{base_url}/api/todos/{conversation_id}", headers=auth_headers()
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            todos = json.loads(resp.read().decode("utf-8")).get("todos") or []
    except Exception as exc:
        print(paint(f"读取任务清单失败：{exc}", "red"))
        return
    if not todos:
        print(paint("当前没有任务清单。", "dim"))
        return
    print(paint("任务清单", "cyan", bold=True))
    for t in todos:
        mark = todo_mark(t.get("done"))
        color = "green" if t.get("done") else "dim"
        print(paint(f"  {mark} {safe_text(t.get('text', ''))}", color))


def fetch_runs(base_url: str, conversation_id: int | None, limit: int = 1) -> list[dict]:
    """会话的运行记录（最近在前；含 token_usage / cache_hit_rate / 估算成本）。"""
    if not conversation_id:
        return []
    try:
        url = f"{base_url}/api/runs?conversation_id={conversation_id}&limit={limit}"
        req = urllib.request.Request(url, headers=auth_headers())
        with urllib.request.urlopen(req, timeout=8) as resp:
            runs = json.loads(resp.read().decode("utf-8")) or []
        return runs if isinstance(runs, list) else []
    except Exception:
        return []


def fetch_run_metrics(base_url: str, conversation_id: int | None) -> dict:
    """最近一次运行的完整指标（用量 + 缓存命中率 + 估算成本 + 耗时）。"""
    runs = fetch_runs(base_url, conversation_id, limit=1)
    return runs[0] if runs else {}


def fetch_usage(base_url: str, conversation_id: int | None, limit: int = 1) -> dict:
    if not conversation_id:
        return {}
    usage: dict = {}
    for run in fetch_runs(base_url, conversation_id, limit=limit):
        u = run.get("token_usage") or {}
        for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
            usage[key] = int(usage.get(key) or 0) + int(u.get(key) or 0)
    return usage


def show_cost(base_url: str, conversation_id: int | None) -> None:
    """本会话累计用量：tokens / 缓存命中率 / 估算成本。"""
    if not conversation_id:
        print(paint("还没有会话，先提问。", "yellow"))
        return
    runs = fetch_runs(base_url, conversation_id, limit=200)
    if not runs:
        print(paint("暂无用量记录。", "dim"))
        return
    total = {"in": 0, "out": 0, "hit": 0, "miss": 0, "cost": 0.0, "calls": 0}
    for run in runs:
        usage = run.get("token_usage") or {}
        total["in"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        total["out"] += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        total["hit"] += int(usage.get("cache_hit_tokens") or 0)
        total["miss"] += int(usage.get("cache_miss_tokens") or 0)
        total["calls"] += int(usage.get("llm_calls") or 0)
        total["cost"] += float(run.get("estimated_cost_yuan") or 0)
    hit_all = total["hit"] + total["miss"]
    rate = f"{total['hit'] / hit_all * 100:.0f}%" if hit_all else "-"
    print(
        paint(
            f"本会话用量（最近 {len(runs)} 次运行 · {total['calls']} 次 LLM 调用）",
            "cyan",
            bold=True,
        )
    )
    print(
        paint(
            f"   tokens 输入 {fmt_num(total['in'])} · 输出 {fmt_num(total['out'])}"
            f" · 缓存命中 {rate}",
            "dim",
        )
    )
    print(
        paint(
            f"   估算成本 {total['cost']:.4f} 元（按模型单价与峰谷时段估算，非账单）",
            "dim",
        )
    )


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
    """下一轮实际发送量占预算的百分比（优先用 sent_estimate_tokens）。"""
    if not stats:
        return 0
    try:
        sent = stats.get("sent_estimate_tokens")
        tokens = int(sent if sent is not None else stats.get("estimated_tokens") or 0)
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
    headers: dict = {}
    headers.update(auth_headers())
    req = urllib.request.Request(
        f"{base_url}/api/agent/cancel/{run_id}", data=b"", method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False
