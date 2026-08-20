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
        editable[key] = mask_key(value) if key.endswith("api_key") else value
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
    return {"ok": True, "overrides": merged}


@router.get("/settings/models")
def list_chat_models() -> dict:
    """拉取当前激活对话供应商支持的模型列表（OpenAI 兼容 GET /models）。"""
    from ..network import make_httpx_client
    from ..runtime_config import chat_provider_config

    cfg = chat_provider_config(get_settings()) or {}
    if not cfg.get("api_key"):
        return {"ok": False, "models": [], "error": "未配置对话模型 API Key"}
    base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    try:
        with make_httpx_client(timeout=15) as client:
            resp = client.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
            )
        resp.raise_for_status()
        data = resp.json()
        models = [
            {"id": m.get("id") or m.get("name") or ""}
            for m in (data.get("data") or [])
            if isinstance(m, dict) and (m.get("id") or m.get("name"))
        ]
        return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)[:200]}
