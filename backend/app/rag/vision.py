"""视觉理解服务：日日新 SenseNova 多模态模型（识图 / OCR / 图片问答）。

为什么单独做一个轻量模块而不是引入重型 SDK：
- 本项目只需要"图片 -> 文字描述"这一个能力，直接走官方 HTTP API 最轻量；
- 支持 data URL（前端粘贴/上传的图片）与本地文件（PDF 扫描页 OCR）两种输入；
- 与主模型解耦：主模型（DeepSeek V4 Flash）没有视觉能力时自动走这里。

接口说明（SenseNova Token Plan，OpenAI 兼容格式）：
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
messages[].content 为数组，元素可以是
  {"type": "text", "text": "..."}
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
注意：Token Plan 网关对参数较严格，只发送官方列出的字段（model/messages/
max_tokens/temperature 等），不要带 response_format 等未列出的字段。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from ..runtime_config import effective, vision_provider_config

logger = logging.getLogger(__name__)


def _strip_data_url(image_data: str) -> str:
    """去掉 data URL 前缀，只保留 base64 部分。"""
    if "," in image_data and image_data.lstrip().startswith("data:"):
        return image_data.split(",", 1)[1].strip()
    return image_data.strip()


class SenseNovaVision:
    """SenseNova 多模态视觉客户端。"""

    def __init__(self, settings) -> None:
        self.settings = settings

    @property
    def provider(self) -> dict:
        """当前视觉供应商配置（含明文 API Key）。"""
        return vision_provider_config(self.settings) or {}

    @property
    def configured(self) -> bool:
        return bool(self.provider.get("api_key"))

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.provider.get('api_key')}",
        }

    def chat(
        self,
        messages: list[dict],
        *,
        max_new_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: int | None = None,
    ) -> str:
        """调用 SenseNova chat-completions，返回助手文本。兼容两种响应结构。"""
        if not self.configured:
            raise RuntimeError("未配置视觉模型 API Key，无法使用视觉能力")
        payload: dict[str, Any] = {
            "model": self.provider.get("model") or "sensenova-6.8-flash-lite",
            "messages": messages,
            "max_tokens": max_new_tokens,
            "temperature": temperature,
            "stream": False,
        }
        base = self.provider.get("base_url") or "https://token.sensenova.cn/v1"
        url = f"{base.rstrip('/')}/chat/completions"
        # 代理回退直连：系统代理未启动时不至于 WinError 10061
        from ..network import make_httpx_client

        with make_httpx_client(
            timeout=timeout or effective(self.settings, "vision_timeout", 120)
        ) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
        if resp.status_code != 200:
            detail = resp.text[:500]
            logger.warning("SenseNova 请求失败：%s %s", resp.status_code, detail)
            raise RuntimeError(
                f"SenseNova 请求失败（{resp.status_code}）：{detail}"
            )
        data = resp.json()
        # Token Plan 返回 OpenAI 兼容结构：choices[0].message.content
        # 兼容旧平台结构：data.choices[0].message
        root = data.get("data") if isinstance(data.get("data"), dict) else data
        choices = root.get("choices") or []
        if not choices:
            raise RuntimeError(f"SenseNova 返回异常：{resp.text[:300]}")
        message = choices[0].get("message")
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                # 多模态响应里 content 可能是片段数组
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("text")
                ]
                return "".join(parts)
            return str(content or "")
        return ""

    def _content_parts(
        self,
        image_data_urls: list[str],
        question: str,
    ) -> list[dict]:
        parts: list[dict] = []
        max_images = int(effective(self.settings, "vision_max_images", 6))
        for image in image_data_urls[:max_images]:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image},
                }
            )
        parts.append({"type": "text", "text": question})
        return parts

    def describe_images(
        self,
        image_data_urls: list[str],
        question: str = "请详细描述这张图片的内容，包括可见的文字、图表、人物、场景等，便于后续基于你的描述回答问题。",
    ) -> str:
        """一次调用识别多张图片，返回文字描述。"""
        messages = [
            {
                "role": "user",
                "content": self._content_parts(image_data_urls, question),
            }
        ]
        return self.chat(messages).strip()

    def describe_image_file(
        self,
        file_path: Path | str,
        question: str = "请把这张图片/页面中的内容完整识别出来，包括所有文字和图表信息。",
    ) -> str:
        """读取本地图片文件并识别（用于 PDF 扫描页 OCR 等场景）。"""
        file_path = Path(file_path)
        raw = file_path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        mime = mimetypes.guess_type(file_path.name)[0] or "image/png"
        return self.describe_image_bytes(raw, file_path.name, question)

    def describe_image_bytes(
        self,
        raw: bytes,
        filename: str = "image.png",
        question: str = "请把这张图片/页面中的内容完整识别出来，包括所有文字和图表信息。",
    ) -> str:
        """直接识别图片字节（用于渲染后的 PDF 页面等场景）。"""
        encoded = base64.b64encode(raw).decode("ascii")
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        return self.describe_images([f"data:{mime};base64,{encoded}"], question)

    def list_models(self) -> list[str]:
        """查询平台可用模型列表（部分账号可能有权限限制）。"""
        if not self.configured:
            return []
        base = self.provider.get("base_url") or "https://token.sensenova.cn/v1"
        url = f"{base.rstrip('/')}/models"
        from ..network import make_httpx_client

        with make_httpx_client(timeout=15) as client:
            resp = client.get(url, headers=self._headers())
        if resp.status_code != 200:
            return []
        data = resp.json()
        root = data.get("data", data)
        if isinstance(root, list):
            return [m.get("id") or m.get("name") or "" for m in root if isinstance(m, dict)]
        return []
