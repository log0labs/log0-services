"""The MCP endpoint over HTTP (``/mcp``, streamable HTTP), behind the same JWT as the rest of the API.

Every request must carry ``Authorization: Bearer <access token>``. The token is verified with the shared
secret and its tenant becomes ``current_user`` for the duration of that request, so tools see only that
tenant. The server runs *stateless* (no session kept between requests), which means one client's identity
can never leak into another's session.

DNS-rebinding protection (a check of the Host header meant for servers that trust the local network) is
off: this endpoint trusts a bearer token, not network position, and is reached through a public hostname.
"""

from __future__ import annotations

import json
from typing import Any

import jwt
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from app.mcp.server import current_user
from app.security.jwt import decode_access_token


class BearerAuthMiddleware:
    """Pure ASGI middleware: verify the bearer token, set ``current_user``, or answer 401."""

    def __init__(self, app: ASGIApp, *, secret: str) -> None:
        self.app = app
        self._secret = secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = next((v for k, v in scope["headers"] if k == b"authorization"), b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        user = None
        if scheme.lower() == "bearer" and token:
            try:
                user = decode_access_token(token.strip(), self._secret)
            except jwt.PyJWTError:
                user = None
        if user is None:
            await _unauthorized(send)
            return
        marker = current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(marker)


async def _unauthorized(send: Send) -> None:
    body = json.dumps({"detail": "Not authenticated"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_http_app(server: MCPServer, *, secret: str) -> ASGIApp:
    inner = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuthMiddleware(inner, secret=secret)


class LazyMount:
    """Routed at ``/mcp`` when the app is created; the real endpoint is plugged in at start-up (it needs the
    backends the lifespan builds). Until then, or when MCP is switched off, every request is a plain 404."""

    def __init__(self) -> None:
        self.target: Any = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.target is None or scope["type"] != "http":
            if scope["type"] == "http":
                body = b'{"detail":"Not Found"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return
        await self.target(scope, receive, send)
