"""Read-only ClickHouse access for the evidence tools.

Every query is tenant-scoped by construction: callers must reference the
``{tenant_id:String}`` placeholder and pass the tenant separately; the value is
bound by ClickHouse itself (server-side parameters), never formatted into SQL.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import clickhouse_connect
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError as _DriverError

from app.config import Settings

TENANT_PLACEHOLDER = "{tenant_id:String}"
_READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class ClickHouseError(Exception):
    """ClickHouse rejected or failed a query (message is safe to log)."""


class LogStore:
    """Thin async wrapper: lazy connection, tenant-bound read-only queries."""
    
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None
        self._lock = asyncio.Lock()
    
    async def _get_client(self) -> AsyncClient:
        """Connect on first use so a ClickHouse outage never blocks service startup."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    s = self._settings
                    url = urlparse(s.clickhouse_url)
                    password = (
                        s.clickhouse_password.get_secret_value()
                        if s.clickhouse_password is not None
                        else ""
                    )
                    self._client = await clickhouse_connect.get_async_client(
                        host=url.hostname or "localhost",
                        port=url.port or (8443 if url.scheme == "https" else 8123),
                        secure=url.scheme == "https",
                        username=s.clickhouse_user,
                        password=password,
                        database=s.clickhouse_database,
                        send_receive_timeout=s.clickhouse_timeout_seconds,
                    )
        return self._client
    
    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a tenant-scoped SELECT and return rows as dicts.

        ``sql`` must be a single SELECT/WITH statement that uses
        ``{tenant_id:String}``. Other values go in ``parameters`` and are bound
        server-side (``{start:DateTime64(3)}`` etc.).
        """
        params = dict(parameters or {})
        _check_query(sql, params)
        params["tenant_id"] = str(tenant_id)
        client = await self._get_client()
        try:
            result = await client.query(sql, parameters=params)
        except _DriverError as exc:
            raise ClickHouseError(str(exc)) from exc
        return[dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    
    async def ping(self) -> bool:
        """True if ClickHouse answers; never raises (for readiness checks)."""
        try:
            return bool(await (await self._get_client()).ping())
        except Exception:  # noqa: BLE001 - readiness probe must never raise
            return False
    
    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

def _check_query(sql: str, parameters: dict[str, Any]) -> None:
    if TENANT_PLACEHOLDER not in sql:
        raise ValueError(f"query must filter on {TENANT_PLACEHOLDER}")
    if "tenant_id" in parameters:
        raise ValueError("tenant_id is bound by LogStore; do not pass it in parameters")
    if not _READ_ONLY_START.match(sql):
        raise ValueError("only SELECT / WITH queries are allowed")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("multiple statements are not allowed")
