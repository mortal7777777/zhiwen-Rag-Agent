"""运行时设置接口：免重启切换模型/供应商/主题参数。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..agent.agent import AgentService
from ..config import get_settings
from ..db import get_db
from ..rag.service import RAGService
from ..runtime_config import (
    EDITABLE_KEYS,
    READONLY_KEYS,
    effective,
    get_active_provider,
    get_overrides,
    get_providers,
    load_overrides,
    mask_key,
    save_overrides,
)
from .deps import get_agent_service, get_service

router = APIRouter(tags=["settings"])


@router.get("/settings")
def get_settings_view(db: Session = Depends(get_db)) -> dict:
    """读取生效配置（API Key 脱敏）+ 供应商清单（cc-switch 式）。"""
    settings = get_settings()
    load_overrides(db)
    editable = {}
    for key in EDITABLE_KEYS:
        value = effective(settings, key)
        if key.endswith("api_key"):
            value = mask_key(value)
        elif key == "providers" and isinstance(value, list):
            # 供应商列表内嵌 api_key：同样脱敏，否则 GET /settings 会把所有
            # 密钥明文返回（前端原样回传时后端按 **** 前缀还原旧值）
            value = [_mask_provider(p) for p in value]
        editable[key] = value
    readonly = {key: str(getattr(settings, key, "")) for key in READONLY_KEYS}
    overrides = get_overrides()
    providers = [_mask_provider(p) for p in get_providers(settings)]
    return {
        "editable": editable,
        "readonly": readonly,
        "providers": providers,
        "active_chat_provider": overrides.get(
            "active_chat_provider",
            (get_active_provider(settings, "chat") or {}).get("id"),
        ),
        "active_vision_provider": overrides.get(
            "active_vision_provider",
            (get_active_provider(settings, "vision") or {}).get("id"),
        ),
    }


def _mask_provider(provider: dict) -> dict:
    """返回脱敏后的供应商配置（API Key 只留后 4 位）。"""
    out = dict(provider)
    out["api_key"] = mask_key(provider.get("api_key"))
    return out


@router.put("/settings")
def update_settings(
    payload: dict,
    db: Session = Depends(get_db),
    agent: AgentService = Depends(get_agent_service),
    service: RAGService = Depends(get_service),
) -> dict:
    """保存设置并立即生效（清空模型缓存，下次请求用新配置）。"""
    updates = payload.get("updates") if isinstance(payload, dict) else None
    merged = save_overrides(db, updates or {}, settings=get_settings())
    agent.refresh()
    service.refresh()
    # 响应脱敏:providers 里的 api_key 只留后 4 位(与 GET 一致),
    # 避免保存设置后响应把完整密钥明文带回浏览器/日志
    masked = dict(merged)
    if isinstance(masked.get("providers"), list):
        masked["providers"] = [_mask_provider(p) for p in masked["providers"]]
    for key in list(masked):
        if key.endswith("api_key"):
            masked[key] = mask_key(masked[key])
    return {"ok": True, "overrides": masked}


# Anthropic 兼容子路径：剥离后可回到同一厂商的 OpenAI 兼容根端点
# （照搬 cc-switch services/model_fetch.rs 的 strip_compat_suffix 列表，
#  长后缀在前，避免 /anthropic 提前匹配掉 /api/anthropic）
_COMPAT_SUFFIXES = ("/api/anthropic", "/apps/anthropic", "/anthropic")


def _models_candidates(cfg: dict) -> list[tuple[str, dict]]:
    """按序生成模型列表候选请求（URL + 鉴权头）。

    1. 格式对应端点：openai → {base}/models（Bearer）；
       anthropic → {base}/v1/models（x-api-key，Anthropic 官方形状）；
    2. 剥离 Anthropic 兼容子路径后的 OpenAI 兼容端点：{root}/models、
       {root}/v1/models——DeepSeek /anthropic 未实现模型列表，但根端点可用
       （cc-switch 的 DeepSeek 预设同样直接指向 https://api.deepseek.com/models）。
    """
    from ..runtime_config import provider_api_format

    base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    api_key = cfg.get("api_key") or ""
    auth = {"Authorization": f"Bearer {api_key}"}
    if provider_api_format(cfg) == "anthropic":
        candidates = [
            (
                f"{base}/v1/models",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        ]
    else:
        candidates = [(f"{base}/models", auth)]
    root = base
    for suffix in _COMPAT_SUFFIXES:
        if base.endswith(suffix):
            root = base[: -len(suffix)]
            break
    if root != base:
        candidates.append((f"{root}/models", auth))
        candidates.append((f"{root}/v1/models", auth))
    return candidates


@router.get("/settings/models")
def list_chat_models() -> dict:
    """拉取当前激活对话供应商支持的模型列表（候选端点按序尝试）。

    全部远端候选不可用（如 DeepSeek /anthropic 未实现模型列表且根端点
    也不可达）时，回退到供应商配置里保存的模型名，不把裸 404 甩给前端。
    """
    from ..network import make_httpx_client
    from ..runtime_config import chat_provider_config, provider_api_format

    cfg = chat_provider_config(get_settings()) or {}
    if not cfg.get("api_key"):
        return {"ok": False, "models": [], "error": "未配置对话模型 API Key"}
    last_error = ""
    with make_httpx_client(timeout=15) as client:
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
                    return {"ok": True, "models": models}
                last_error = f"{url} 未返回模型"
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                last_error = f"HTTP {status}" if status else str(exc)[:120]
                continue
    fallback = [{"id": m} for m in (cfg.get("models") or []) if m]
    if fallback:
        return {
            "ok": True,
            "models": fallback,
            "note": f"未拉到在线模型列表（{last_error}），已返回配置中保存的模型名",
        }
    if provider_api_format(cfg) == "anthropic":
        return {
            "ok": False,
            "models": [],
            "error": "该供应商未提供模型列表接口，请手动填写模型名",
        }
    return {"ok": False, "models": [], "error": last_error}
