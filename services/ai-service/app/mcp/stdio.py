"""Run log0's MCP server over stdio, for a desktop client on the same machine (Claude Desktop, Cursor).

    LOG0_MCP_TOKEN=<access token> uv run python -m app.mcp.stdio

stdout carries the protocol, so everything else (logs, errors) goes to stderr. The token is a normal log0
access token (``scripts/mcp_token.py`` makes one for local use): its tenant is the only tenant the tools can
see, and when it expires the tools stop working until the client is restarted with a new one.

Claude Desktop (``claude_desktop_config.json``):

    {"mcpServers": {"log0": {
        "command": "uv",
        "args": ["--directory", "<path to services/ai-service>", "run", "python", "-m", "app.mcp.stdio"],
        "env": {"LOG0_MCP_TOKEN": "<token>"}}}}
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import jwt

from app.config import get_settings
from app.db.session import create_db_engine, create_session_factory
from app.integrations.clickhouse import LogStore
from app.integrations.code_access import DbCodeAccess
from app.integrations.incident_db import IncidentReader
from app.integrations.incident_service import IncidentApiClient
from app.integrations.repo_cache import RepoCache
from app.mcp.server import McpBackends, build_mcp_server, current_deadline, current_user
from app.retrieval.embeddings import build_embedder
from app.retrieval.runbooks import RunbookSearcher
from app.security.crypto import secret_box_from_settings
from app.security.jwt import CurrentUser, decode_access_token

TOKEN_ENV = "LOG0_MCP_TOKEN"


def user_from_token(token: str | None, secret: str) -> tuple[CurrentUser, float]:
    """The user and the time (epoch seconds) the token stops being valid. Raises ``SystemExit`` with a plain message."""
    if not token:
        raise SystemExit(f"{TOKEN_ENV} is not set. Create a token with: uv run python scripts/mcp_token.py <tenant_id>")
    try:
        user = decode_access_token(token, secret)
        expires = float(jwt.decode(token, secret, algorithms=["HS256"])["exp"])
    except jwt.PyJWTError:
        raise SystemExit(f"{TOKEN_ENV} is not a valid, unexpired log0 token") from None
    return user, expires


async def serve(user: CurrentUser, expires_at: float) -> None:
    settings = get_settings()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    log_store = LogStore(settings)
    incident_reader = IncidentReader(settings)
    embedder = build_embedder(settings)
    box = secret_box_from_settings(settings) if settings.secrets_keys else None
    try:
        server = build_mcp_server(
            McpBackends(
                logs=log_store,
                incidents=IncidentApiClient(settings),
                history=incident_reader,
                session_factory=session_factory,
                runbooks=RunbookSearcher(session_factory, embedder),
                code=DbCodeAccess(
                    session_factory, box, RepoCache(settings), git_binary=settings.git_binary, rg_binary=settings.rg_binary
                ),
            )
        )
        current_user.set(user)
        current_deadline.set(expires_at)
        await server.run_stdio_async()
    finally:
        if hasattr(embedder, "close"):
            await embedder.close()
        await log_store.close()
        await incident_reader.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    user, expires_at = user_from_token(os.environ.get(TOKEN_ENV), settings.jwt_secret.get_secret_value())
    if expires_at <= time.time():
        raise SystemExit(f"{TOKEN_ENV} has expired")
    # psycopg's async mode cannot use the default Windows event loop
    asyncio.run(serve(user, expires_at), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


if __name__ == "__main__":
    main()
