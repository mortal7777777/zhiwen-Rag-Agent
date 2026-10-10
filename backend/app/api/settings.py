"""运行时设置接口：免重启切换模型/供应商/主题参数。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..agent.agent import AgentService
from ..config import get_settings
from ..db import get_db
from ..rag.service import RAGService
from ..runtime_config import (
    EDITABLE_KEYS,
    READONLY_KEYS,
    editable_schema,
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


@router.get("/settings/schema")
def get_settings_schema() -> dict:
    """可调项元数据（类型/区间/枚举/分组/展示名）。

    单一来源是 runtime_config._SPECS：新增一个可调项只改声明表，前端可据此
    渲染表单与做前端校验（不再需要三处同步）。不含任何敏感值。
    """
    return {"fields": editable_schema()}


@router.get("/settings/provider-presets")
def get_provider_presets() -> dict:
    """内置供应商预设（cc-switch 式"从预设开始"），只含非敏感字段。"""
    from ..provider_presets import PROVIDER_PRESETS

    return {"presets": PROVIDER_PRESETS}


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
    """返回脱敏后的供应商配置（API Key 只留后 4 位；敏感请求头同规则）。"""
    from ..runtime_config import is_sensitive_header

    out = dict(provider)
    out["api_key"] = mask_key(provider.get("api_key"))
    headers = provider.get("headers")
    if isinstance(headers, dict):
        out["headers"] = {
            name: (mask_key(val) or "" if is_sensitive_header(name) else val)
            for name, val in headers.items()
        }
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
    from ..runtime_config import normalize_base_url, provider_api_format

    fmt = provider_api_format(cfg)
    base = normalize_base_url(cfg.get("base_url"), fmt)
    api_key = cfg.get("api_key") or ""
    auth = {"Authorization": f"Bearer {api_key}"}
    if fmt == "anthropic":
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


@router.post("/settings/providers/test")
def test_provider_connection(payload: dict) -> dict:
    """测试单个供应商（支持未保存草稿；恒 HTTP 200，由 ok 判别成败）。

    payload = 完整供应商 cfg；api_key 为 `****` 掩码时，仅当 id 与 base_url
    主机都能对上已存记录才还原（见 provider_probe.restore_masked_key）。
    """
    from ..provider_probe import probe_provider

    cfg = payload if isinstance(payload, dict) else {}
    return probe_provider(cfg, get_providers(get_settings()))


@router.post("/settings/providers/models")
def list_provider_models(payload: dict) -> dict:
    """按指定供应商配置拉模型列表（修复"永远拉激活供应商"的既有 bug）。"""
    from ..provider_probe import probe_models, restore_masked_key

    cfg = payload if isinstance(payload, dict) else {}
    fixed, err = restore_masked_key(cfg, get_providers(get_settings()))
    if err:
        return {"ok": False, "models": [], "error": err}
    if not fixed.get("api_key"):
        return {"ok": False, "models": [], "error": "未配置 API Key"}
    return probe_models(fixed)


@router.post("/settings/providers/speedtest")
def speedtest_providers() -> dict:
    """对全部已启用 chat 供应商并发测延迟（输入是服务端已存配置，Key 不过浏览器）。"""
    from ..provider_probe import speedtest

    return {"ok": True, "results": speedtest(get_providers(get_settings()))}


@router.get("/settings/providers/export")
def export_providers(include_keys: int = 0) -> dict:
    """导出供应商配置（默认不含密钥；include_keys=1 带明文，仅本地使用）。"""
    from ..provider_io import export_payload

    return export_payload(bool(include_keys))


@router.post("/settings/providers/import")
def import_providers(
    payload: dict,
    db: Session = Depends(get_db),
    agent: AgentService = Depends(get_agent_service),
    service: RAGService = Depends(get_service),
) -> dict:
    """导入供应商配置（同 id 覆盖、空/掩码 Key 保留旧值、新 id 掩码 Key 拒收）。"""
    from ..provider_io import import_providers as run_import

    result = run_import(payload, db=db, settings=get_settings())
    if result.get("ok"):
        agent.refresh()
        service.refresh()
    return result


@router.post("/settings/switch-chat-provider")
def switch_chat_provider(
    payload: dict,
    db: Session = Depends(get_db),
    agent: AgentService = Depends(get_agent_service),
    service: RAGService = Depends(get_service),
) -> dict:
    """快捷切换激活对话供应商（聊天页头部下拉）。

    与 PUT /settings 的区别：只清对话缓存（refresh_chat_only），不触发
    RAGService 的嵌入/重排重载——本地 BGE 重新加载体感数秒，快捷切换要高频用。
    """
    provider_id = str((payload or {}).get("provider_id") or "").strip()
    if not provider_id:
        raise HTTPException(status_code=422, detail="缺少 provider_id")
    target = next(
        (
            p
            for p in get_providers(get_settings())
            if p.get("id") == provider_id and p.get("type") == "chat"
        ),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not target.get("enabled", True):
        raise HTTPException(status_code=409, detail="供应商未启用，请先在设置中开启")
    merged = save_overrides(
        db, {"active_chat_provider": provider_id}, settings=get_settings()
    )
    agent.refresh_chat_only()
    service.refresh_chat_only()
    return {
        "ok": True,
        "active_chat_provider": merged.get("active_chat_provider"),
        "model": target.get("model") or "",
    }
