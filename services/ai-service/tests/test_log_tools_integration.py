"""Log tools against the real ClickHouse (needs ai_reader from step 2.1)."""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import clickhouse_connect
import pytest
import pytest_asyncio

from app.config import get_settings
from app.integrations.clickhouse import LogStore
from app.tools.logs import get_cooccurring_errors, get_error_timeline, search_logs

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="ClickHouse integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
COLUMNS = [
    "event_id", "tenant_id", "service_name", "environment", "timestamp",
    "level", "message", "message_template", "fingerprint", "trace_id", "schema_version",
]


def _row(tenant: UUID, minute: int, service: str, level: str, message: str, fp: str) -> list:
    return [
        str(uuid4()), str(tenant), service, "prod", T0 + timedelta(minutes=minute),
        level, message, message, fp, "", "1",
    ]


@pytest_asyncio.fixture
async def tenants() -> AsyncGenerator[tuple[UUID, UUID]]:
    """Tenant A: 3 x fp-pay at +1m, 1 x fp-pay at +3m, 2 db errors, 1 INFO. Tenant B: 1 row."""
    a, b = uuid4(), uuid4()
    rows = [
        _row(a, 1, "payment", "ERROR", "Gateway Timeout calling bank", "fp-pay"),
        _row(a, 1, "payment", "ERROR", "Gateway Timeout calling bank", "fp-pay"),
        _row(a, 1, "payment", "ERROR", "Gateway Timeout calling bank", "fp-pay"),
        _row(a, 3, "payment", "ERROR", "Gateway Timeout calling bank", "fp-pay"),
        _row(a, 2, "db", "FATAL", "connection pool exhausted", "fp-db"),
        _row(a, 2, "db", "FATAL", "connection pool exhausted", "fp-db"),
        _row(a, 2, "api", "INFO", "request ok", "fp-ok"),
        _row(b, 1, "payment", "ERROR", "Gateway Timeout calling bank", "fp-pay"),
    ]
    admin = await clickhouse_connect.get_async_client(
        host="localhost", port=8123, username="log0", password="log0", database="log0"
    )
    await admin.insert("log_events", rows, column_names=COLUMNS)
    try:
        yield a, b
    finally:
        await admin.command(
            "ALTER TABLE log_events DELETE WHERE tenant_id IN ({a:String}, {b:String})",
            parameters={"a": str(a), "b": str(b)},
            settings={"mutations_sync": 1},
        )
        await admin.close()


@pytest_asyncio.fixture
async def store() -> AsyncGenerator[LogStore]:
    s = LogStore(get_settings())
    yield s
    await s.close()


async def test_timeline_counts_per_minute(store: LogStore, tenants) -> None:
    a, b = tenants
    out = await get_error_timeline(
        store, tenant_id=a, fingerprint="fp-pay", start=T0, end=T0 + timedelta(minutes=4)
    )
    assert [x.count for x in out.buckets] == [0, 3, 0, 1, 0]
    assert out.total == 4 and out.peak.bucket_start == T0 + timedelta(minutes=1)
    other = await get_error_timeline(
        store, tenant_id=b, fingerprint="fp-pay", start=T0, end=T0 + timedelta(minutes=4)
    )
    assert other.total == 1  # tenant B only sees its own row


async def test_timeline_larger_bucket_and_naive_datetimes(store: LogStore, tenants) -> None:
    a, _ = tenants
    out = await get_error_timeline(
        store,
        tenant_id=a,
        fingerprint="fp-pay",
        start=T0.replace(tzinfo=None),
        end=(T0 + timedelta(minutes=14)).replace(tzinfo=None),
        bucket="5m",
    )
    assert [x.count for x in out.buckets] == [4, 0, 0]


async def test_cooccurring_excludes_target_and_info(store: LogStore, tenants) -> None:
    a, _ = tenants
    out = await get_cooccurring_errors(
        store, tenant_id=a, start=T0, end=T0 + timedelta(hours=1), exclude_fingerprint="fp-pay"
    )
    assert [(e.service_name, e.fingerprint, e.count) for e in out] == [("db", "fp-db", 2)]
    everything = await get_cooccurring_errors(store, tenant_id=a, start=T0, end=T0 + timedelta(hours=1))
    assert [e.fingerprint for e in everything] == ["fp-pay", "fp-db"]  # ordered by count


async def test_search_filters(store: LogStore, tenants) -> None:
    a, _ = tenants
    window = {"start": T0, "end": T0 + timedelta(hours=1)}
    by_text = await search_logs(store, tenant_id=a, query="gateway timeout", **window)
    assert len(by_text.lines) == 4 and not by_text.limit_reached
    assert by_text.lines[0].timestamp >= by_text.lines[-1].timestamp  # newest first
    assert len((await search_logs(store, tenant_id=a, service="db", **window)).lines) == 2
    assert len((await search_logs(store, tenant_id=a, level="info", **window)).lines) == 1
    capped = await search_logs(store, tenant_id=a, limit=2, **window)
    assert len(capped.lines) == 2 and capped.limit_reached


async def test_search_never_crosses_tenants(store: LogStore, tenants) -> None:
    _, b = tenants
    window = {"start": T0, "end": T0 + timedelta(hours=1)}
    assert len((await search_logs(store, tenant_id=b, **window)).lines) == 1
    assert (await search_logs(store, tenant_id=uuid4(), **window)).lines == []


async def test_hostile_text_is_just_text(store: LogStore, tenants) -> None:
    a, _ = tenants
    out = await search_logs(
        store,
        tenant_id=a,
        query="x') OR 1=1 --",
        start=T0,
        end=T0 + timedelta(hours=1),
    )
    assert out.lines == []
