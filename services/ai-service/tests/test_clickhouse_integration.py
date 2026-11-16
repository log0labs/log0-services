"""Integration tests against the real ClickHouse (needs ai_reader user)."""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import clickhouse_connect
import pytest
import pytest_asyncio

from app.config import get_settings
from app.integrations.clickhouse import ClickHouseError, LogStore

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="ClickHouse integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

COLUMNS = [
    "event_id", "tenant_id", "service_name", "environment", "timestamp",
    "level", "message", "message_template", "fingerprint", "trace_id", "schema_version",
]


def _row(tenant: UUID, service: str) -> list:
    return [
        str(uuid4()), str(tenant), service, "prod", datetime.now(UTC),
        "ERROR", "boom", "boom", "fp-1", "", "1",
    ]


@pytest_asyncio.fixture
async def seeded() -> AsyncGenerator[tuple[UUID, UUID]]:
    """Two tenants with one log row each, written by the admin user; cleaned up after."""
    tenant_a, tenant_b = uuid4(), uuid4()
    admin = await clickhouse_connect.get_async_client(
        host="localhost", port=8123, username="log0", password="log0", database="log0"
    )
    await admin.insert(
        "log_events",
        [_row(tenant_a, "svc-a"), _row(tenant_b, "svc-b")],
        column_names=COLUMNS,
    )
    try:
        yield tenant_a, tenant_b
    finally:
        await admin.command(
            "ALTER TABLE log_events DELETE WHERE tenant_id IN ({a:String}, {b:String})",
            parameters={"a": str(tenant_a), "b": str(tenant_b)},
            settings={"mutations_sync": 1},
        )
        await admin.close()


@pytest_asyncio.fixture
async def store() -> AsyncGenerator[LogStore]:
    s = LogStore(get_settings())
    yield s
    await s.close()


SQL = "SELECT service_name FROM log_events WHERE tenant_id = {tenant_id:String}"


async def test_each_tenant_sees_only_own_rows(store: LogStore, seeded) -> None:
    tenant_a, tenant_b = seeded
    assert await store.query(SQL, tenant_id=tenant_a) == [{"service_name": "svc-a"}]
    assert await store.query(SQL, tenant_id=tenant_b) == [{"service_name": "svc-b"}]


async def test_unknown_tenant_gets_nothing(store: LogStore, seeded) -> None:
    assert await store.query(SQL, tenant_id=uuid4()) == []


async def test_extra_parameters_are_bound_not_interpolated(store: LogStore, seeded) -> None:
    tenant_a, _ = seeded
    sql = SQL + " AND service_name = {svc:String}"
    hostile = "svc-a' OR '1'='1"
    assert await store.query(sql, tenant_id=tenant_a, parameters={"svc": hostile}) == []
    assert len(await store.query(sql, tenant_id=tenant_a, parameters={"svc": "svc-a"})) == 1


async def test_database_blocks_writes_even_if_guard_bypassed(store: LogStore) -> None:
    client = await store._get_client()
    with pytest.raises(Exception, match="Not enough privileges"):
        await client.command("INSERT INTO log_events (event_id) VALUES ('x')")


async def test_server_error_is_wrapped(store: LogStore) -> None:
    sql = "SELECT * FROM no_such_table WHERE tenant_id = {tenant_id:String}"
    with pytest.raises(ClickHouseError):
        await store.query(sql, tenant_id=uuid4())


async def test_ping(store: LogStore) -> None:
    assert await store.ping() is True
