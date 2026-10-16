"""访问控制：IP 白名单（无登录/无鉴权方案）。

设计（用户约定）：
- 服务默认仅允许本机访问（127.0.0.1 / ::1）
- 需要局域网访问时，在配置 allowed_ips 中加入对应 IP（支持 "*" 全放行）
- 中间件拦截 HTTP 与 WebSocket 两类请求；非白名单 IP：HTTP 403，WS 直接断开
- 若未来部署到反向代理之后，真实来源由代理控制；同机代理的来源 IP 是 127.0.0.1，天然在白名单内
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def normalize_ip(ip: str) -> str:
    """IPv4-mapped IPv6 归一化：::ffff:127.0.0.1 -> 127.0.0.1"""
    ip = (ip or "").strip()
    if ip.startswith("::ffff:"):
        return ip[7:]
    return ip


def ip_allowed(ip: str, allowed: Iterable[str]) -> bool:
    ip = normalize_ip(ip)
    for a in allowed:
        a = normalize_ip(str(a))
        if a == "*" or a == ip:
            return True
    return False


class IPAllowlistMiddleware:
    """纯 ASGI 中间件：同时作用于 http 与 websocket scope。"""

    def __init__(self, app, allowed_ips: Iterable[str]) -> None:
        self.app = app
        self.allowed = [normalize_ip(str(x)) for x in (allowed_ips or [])]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client") or ("", 0)
            ip = normalize_ip(client[0])
            if not ip_allowed(ip, self.allowed):
                logger.warning("[security] 拒绝非白名单来源: %s (%s)", ip, scope["type"])
                if scope["type"] == "http":
                    from starlette.responses import JSONResponse
                    resp = JSONResponse(
                        {"detail": f"IP {ip} 不在访问白名单内"}, status_code=403
                    )
                    await resp(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)
