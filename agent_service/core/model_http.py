"""用户模型出站策略：允许域名、公共 IP、HTTPS、固定连接 IP、禁止重定向。"""
from __future__ import annotations
import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit
import httpx
from .errors import BadRequest


def validate_url(value: str) -> str:
    try:
        url = urlsplit(value)
        hosts = {item.strip().lower() for item in os.getenv('ALLOWED_MODEL_HOSTS', '').split(',') if item.strip()}
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or (url.port not in (None, 443)) or url.hostname.lower() not in hosts):
            raise ValueError('unsupported endpoint')
        return url.hostname
    except ValueError as exc:
        raise BadRequest('模型服务地址未被允许') from exc


def public_ip(host: str) -> str:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise ValueError('no addresses')
        for value in addresses:
            address = ipaddress.ip_address(value)
            mapped = getattr(address, 'ipv4_mapped', None)
            if not address.is_global or address.is_multicast or (mapped and not mapped.is_global):
                raise ValueError('non-public address')
        return addresses[0]
    except (OSError, ValueError) as exc:
        raise BadRequest('模型端点不可达或地址不安全') from exc


def pinned_request(request: httpx.Request, host: str, address: str) -> httpx.Request:
    # Host / SNI 保留原域名，TCP 连接只使用本次验证过的 IP，避免第二次解析。
    extensions = {**request.extensions, 'sni_hostname': host}
    headers = request.headers.copy()
    headers['Host'] = host
    return httpx.Request(request.method, request.url.copy_with(host=address), headers=headers,
                         stream=request.stream, extensions=extensions)


class PublicTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = validate_url(str(request.url))
        return super().handle_request(pinned_request(request, host, public_ip(host)))


class AsyncPublicTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = validate_url(str(request.url))
        address = await asyncio.wait_for(asyncio.to_thread(public_ip, host), timeout=5)
        return await super().handle_async_request(pinned_request(request, host, address))


def model_clients():
    timeout = httpx.Timeout(60, connect=5)
    return (httpx.Client(transport=PublicTransport(), timeout=timeout, follow_redirects=False, trust_env=False),
            httpx.AsyncClient(transport=AsyncPublicTransport(), timeout=timeout, follow_redirects=False, trust_env=False))
