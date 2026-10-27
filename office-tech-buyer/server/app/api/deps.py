"""API 层公共依赖：客户端 IP 解析。"""

from __future__ import annotations

from fastapi import Request

from ..config import settings


def client_ip(request: Request) -> str:
    """解析真实客户端 IP。

    仅在显式信任代理头时读取 ``X-Forwarded-For`` / ``X-Real-IP``，
    否则一律使用 TCP 对端地址，避免日志被伪造。
    """
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "-"
