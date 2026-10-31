"""Unit tests for app.integrations.clickhouse (no ClickHouse needed)."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from app.config import get_settings
from app.integrations.clickhouse import ClickHouseError, LogStore

TENANT = UUID("11111111-1111-4111-8111-111111111111")
SQL = "SELECT service_name, count() AS n FROM log_events WHERE tenant_id = {tenant_id:String} GROUP BY service_name"


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._error = error

    async def query(self, sql: str, parameters: dict):
        self.calls.append((sql, parameters))
        if self._error:
            raise self._error
        return SimpleNamespace(column_names=("service_name", "n"), result_rows=[("api", 3), ("db", 1)])


def _store(client: FakeClient) -> LogStore:
    store = LogStore(get_settings())
    store._client = client  # skip the real connection
    return store


@pytest.mark.asyncio
async def test_query_binds_tenant_and_returns_dicts() -> None:
    client = FakeClient()
    rows = await _store(client).query(SQL, tenant_id=TENANT, parameters={"limit": 5})
    assert rows == [{"service_name": "api", "n": 3}, {"service_name": "db", "n": 1}]
    sql, params = client.calls[0]
    assert sql == SQL  # SQL text is never rewritten or interpolated
    assert params == {"limit": 5, "tenant_id": str(TENANT)}


@pytest.mark.asyncio
async def test_rejects_query_without_tenant_placeholder() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        await _store(FakeClient()).query("SELECT 1", tenant_id=TENANT)


@pytest.mark.asyncio
async def test_rejects_caller_supplied_tenant_param() -> None:
    with pytest.raises(ValueError, match="do not pass"):
        await _store(FakeClient()).query(SQL, tenant_id=TENANT, parameters={"tenant_id": "other"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO log_events (event_id) VALUES ('x') -- {tenant_id:String}",
        "DROP TABLE log_events -- {tenant_id:String}",
        "ALTER TABLE log_events DELETE WHERE tenant_id = {tenant_id:String}",
    ],
)
async def test_rejects_non_select(sql: str) -> None:
    client = FakeClient()
    with pytest.raises(ValueError, match="SELECT"):
        await _store(client).query(sql, tenant_id=TENANT)
    assert client.calls == []


@pytest.mark.asyncio
async def test_rejects_multiple_statements() -> None:
    sql = "SELECT 1 WHERE {tenant_id:String} != ''; DROP TABLE log_events"
    with pytest.raises(ValueError, match="multiple"):
        await _store(FakeClient()).query(sql, tenant_id=TENANT)


@pytest.mark.asyncio
async def test_trailing_semicolon_is_allowed() -> None:
    rows = await _store(FakeClient()).query(SQL + ";", tenant_id=TENANT)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_driver_errors_become_clickhouse_error() -> None:
    store = _store(FakeClient(error=DatabaseError("boom")))
    with pytest.raises(ClickHouseError, match="boom"):
        await store.query(SQL, tenant_id=TENANT)
