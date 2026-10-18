"""图片理解（可选）：OpenAI 兼容视觉模型，把收到的图片转为文字描述。

- 触发：收到图片消息且配置了 llm.vision_model（DeepSeek 无视觉模型，可配
  Qwen-VL / GLM-4V 等 OpenAI 兼容视觉接口；base_url/api_key 留空复用对话模型配置）
- 图片来源：消息 meta 里的 image_path（本地文件）或 image_url（http/https）
- 失败安全：任何错误由调用方兜底为占位草稿，绝不阻断消息流程
- 安全约束：描述结果仍然只是一份待确认草稿，是否回复、回复什么由人工决定
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_SUPPORTED_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

_PROMPT = (
    "你是聊天助手的图片理解模块。请用不超过两句话的简体中文描述这张图片的关键内容"
    "（主体、文字信息、是否涉及转账/红包/二维码/验证码等需要人工注意的元素）。"
    "只输出描述文字，不要任何前缀或解释。"
)


def resolve_image_source(meta: dict | None) -> str:
    """从消息 meta 中取图片源（image_path / image_url），无则返回空串。"""
    meta = meta or {}
    for section in meta.values():
        if isinstance(section, dict):
            for key in ("image_path", "image_url"):
                v = str(section.get(key) or "").strip()
                if v:
                    return v
    for key in ("image_path", "image_url"):
        v = str(meta.get(key) or "").strip()
        if v:
            return v
    return ""


def _to_data_url(source: str) -> str:
    """本地文件 → base64 data URL；http(s) 链接原样返回。"""
    low = source.lower()
    if low.startswith(("http://", "https://")):
        return source
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"图片文件不存在: {source}")
    mime = _SUPPORTED_MIME.get(path.suffix.lower(), "image/png")
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{raw}"


class VisionDescriber:
    """调用 OpenAI 兼容视觉模型生成图片描述。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def describe(self, image_source: str) -> str:
        """返回图片描述文字；失败抛异常（由调用方兜底）。"""
        data_url = _to_data_url(image_source)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
            "temperature": 0.2,
        }
        resp = await self._client.post(
            self.base_url + "/chat/completions", json=payload, headers=headers
        )
        resp.raise_for_status()
        text = str(resp.json()["choices"][0]["message"]["content"]).strip()
        if not text:
            raise ValueError("视觉模型返回空描述")
        return text[:500]

    async def aclose(self) -> None:
        await self._client.aclose()
