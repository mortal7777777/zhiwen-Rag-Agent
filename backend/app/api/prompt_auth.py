"""提示词管理可选鉴权：设置 PROMPT_ADMIN_TOKEN 后，所有提示词/模板接口需带令牌。"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def require_prompt_admin(
    x_prompt_admin_token: str | None = Header(
        default=None, alias="X-Prompt-Admin-Token"
    ),
) -> None:
    expected = os.environ.get("PROMPT_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    candidate = (x_prompt_admin_token or "").encode("utf-8")
    if not candidate or not hmac.compare_digest(
        candidate, expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="提示词管理令牌无效或缺失")
