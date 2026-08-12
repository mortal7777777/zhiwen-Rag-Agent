"""视觉接口：SenseNova 识图 / 状态查询 / 模型列表。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..rag.vision import SenseNovaVision
from ..runtime_config import vision_provider_config
from ..schemas import VisionDescribeRequest, VisionDescribeResponse

router = APIRouter(tags=["vision"])


def _vision() -> SenseNovaVision:
    return SenseNovaVision(get_settings())


@router.post("/vision/describe", response_model=VisionDescribeResponse)
def describe(request: VisionDescribeRequest) -> VisionDescribeResponse:
    """一次调用识别最多 6 张图片，返回文字描述（供识图工具/前端预览使用）。"""
    try:
        answer = _vision().describe_images(
            request.images,
            question=request.question,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"识图失败：{exc}")
    return VisionDescribeResponse(
        answer=answer,
        model=(vision_provider_config(get_settings()) or {}).get("model")
        or "sensenova-6.8-flash-lite",
    )


@router.get("/vision/status")
def status() -> dict:
    """查看视觉能力配置状态（是否配 key、模型、是否自动识图）。"""
    settings = get_settings()
    provider = vision_provider_config(settings) or {}
    return {
        "configured": bool(provider.get("api_key")),
        "model": provider.get("model") or settings.sensenova_model,
        "base_url": provider.get("base_url") or settings.sensenova_base_url,
        "auto_describe": settings.vision_auto_describe,
        "main_model_vision": settings.main_model_vision,
        "max_images": settings.vision_max_images,
    }


@router.get("/vision/models")
def models() -> dict:
    """查询平台模型列表（未配 key 或接口无权限时返回空列表）。"""
    vision = _vision()
    if not vision.configured:
        return {"configured": False, "models": []}
    try:
        return {"configured": True, "models": vision.list_models()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"查询模型列表失败：{exc}")
