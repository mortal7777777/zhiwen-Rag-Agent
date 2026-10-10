"""供应商配置探测：模型列表 / 最小对话 ping / 全部测速（设置页"测试连接"）。

安全要点（2026-10-10 评审）：掩码 Key（`****` 前缀）的还原只允许"同 id 且
base_url 主机一致"——否则把已存 Key 发往任意 base_url 就是一条一键外泄通道；
新草稿一律要求明文 Key。探测请求不占 `acquire_llm` 并发信号量（与生成任务
隔离），并发上限靠每次点击触发 + 前端禁用控制。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .runtime_config import build_chat_model

MAX_ERROR_CHARS = 200


def _host_of(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def restore_masked_key(cfg: dict, saved: list[dict] | None) -> tuple[dict, str | None]:
    """还原掩码 Key；返回 (cfg, error)，error 非空时调用方不得发出任何请求。"""
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key.startswith("****"):
        return cfg, None
    pid = str(cfg.get("id") or "").strip()
    old = next((p for p in (saved or []) if p.get("id") == pid), None)
    if not old:
        return cfg, "掩码 Key 仅适用于已保存的供应商，请重新输入 Key"
    if _host_of(old.get("base_url")) != _host_of(cfg.get("base_url")):
        return cfg, "供应商地址已变更，掩码 Key 不适用于新地址，请重新输入 Key"
    fixed = dict(cfg)
    fixed["api_key"] = str(old.get("api_key") or "").strip()
    return fixed, None


def _short_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status:
        body = getattr(getattr(exc, "response", None), "text", "") or ""
        suffix = f"：{body.strip()[:120]}" if body.strip() else ""
        return f"HTTP {status}{suffix}"[:MAX_ERROR_CHARS]
    return str(exc).strip()[:MAX_ERROR_CHARS]


def probe_models(cfg: dict, timeout: float = 15.0) -> dict:
    """拉模型列表（复用 /settings/models 的候选端点逻辑），非致命探测。"""
    from .api.settings import _models_candidates
    from .network import make_httpx_client

    if not cfg.get("api_key"):
        return {"ok": False, "models": [], "error": "未配置 API Key"}
    last_error = ""
    with make_httpx_client(timeout=timeout) as client:
        for url, headers in _models_candidates(cfg):
            try:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                models = [
                    {"id": m.get("id") or m.get("name") or ""}
                    for m in (data.get("data") or [])
                    if isinstance(m, dict) and (m.get("id") or m.get("name"))
                ]
                if models:
                    return {"ok": True, "models": models, "error": None}
                last_error = f"{url} 未返回模型"
            except Exception as exc:  # noqa: BLE001 - 探测端点：错误一律转成文案
                last_error = _short_error(exc)
    return {"ok": False, "models": [], "error": last_error}


def _close_llm(llm) -> None:
    """尽力关闭探测模型持有的 httpx 客户端（探测是一次性的，不能随点击累积）。"""
    if llm is None:
        return
    targets = [llm]
    inner = getattr(llm, "_inner", None)
    if inner is not None:
        targets.append(inner)
    for obj in targets:
        for attr in ("http_client", "http_async_client"):
            try:
                client = getattr(obj, attr)
            except Exception:  # noqa: BLE001
                continue
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        # anthropic SDK 客户端是 cached_property：只关已构造的，别触发构造
        sdk = obj.__dict__.get("_client") if hasattr(obj, "__dict__") else None
        if sdk is not None:
            try:
                sdk.close()
            except Exception:  # noqa: BLE001
                pass


def probe_chat(cfg: dict, timeout: float = 20.0) -> dict:
    """最小对话 ping：短超时、0 重试、关思考、max_tokens 只够回几个字。"""
    model = str(cfg.get("model") or "").strip()
    if not model:
        return {"ok": False, "latency_ms": None, "error": "未设置默认模型"}
    started = time.perf_counter()
    llm = None
    try:
        llm = build_chat_model(
            cfg,
            temperature=0.0,
            timeout=timeout,
            max_retries=0,
            max_tokens=64,
            thinking={"type": "disabled"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        llm.invoke("请只回复：pong")
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as exc:  # noqa: BLE001 - 探测端点：错误一律转成文案
        latency = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "latency_ms": latency, "error": _short_error(exc)}
    finally:
        _close_llm(llm)


def probe_provider(cfg: dict, saved: list[dict] | None = None) -> dict:
    """完整探测：Key 还原 → 模型列表（非致命）→ 最小对话 ping（决定性）。

    恒返回 200 风格的结构（对齐 /api/mcp/test 先例），由 `ok` 判别成败：
    `{ok, stage, latency_ms, models_count, models_error, error}`。
    """
    fixed, err = restore_masked_key(cfg, saved)
    if err:
        return {
            "ok": False,
            "stage": "key",
            "latency_ms": None,
            "models_count": None,
            "models_error": None,
            "error": err,
        }
    if not str(fixed.get("model") or "").strip():
        return {
            "ok": False,
            "stage": "model",
            "latency_ms": None,
            "models_count": None,
            "models_error": None,
            "error": "未设置默认模型",
        }
    models = probe_models(fixed)
    chat = probe_chat(fixed)
    return {
        "ok": chat["ok"],
        "stage": "" if chat["ok"] else "chat",
        "latency_ms": chat["latency_ms"],
        "models_count": len(models["models"]) if models["ok"] else None,
        "models_error": None if models["ok"] else models["error"],
        "error": chat["error"],
    }


def speedtest(saved: list[dict] | None, timeout: float = 15.0) -> list[dict]:
    """对全部已启用 chat 供应商并发测延迟（每项上界=httpx 超时）。

    输入是服务端已存配置（Key 不经过浏览器）；无 Key 的条目直接返回失败，不发请求。
    """
    targets = [
        p
        for p in (saved or [])
        if p.get("type") == "chat" and p.get("enabled", True)
    ]
    ordered: list[dict] = []
    if not targets:
        return ordered
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_speed_one, dict(p), timeout): dict(p) for p in targets}
        for fut in as_completed(futures):
            provider = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整场测速
                result = {"ok": False, "latency_ms": None, "error": _short_error(exc)}
            ordered.append(
                {
                    "id": provider.get("id"),
                    "name": provider.get("name") or provider.get("id"),
                    **result,
                }
            )
    order_index = {p.get("id"): i for i, p in enumerate(targets)}
    ordered.sort(key=lambda item: order_index.get(item["id"], 999))
    return ordered


def _speed_one(cfg: dict, timeout: float) -> dict:
    if not cfg.get("api_key"):
        return {"ok": False, "latency_ms": None, "error": "未配置 API Key"}
    return probe_chat(cfg, timeout=timeout)
