"""Unit tests for app.tools.logs (fake backend, no ClickHouse needed)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.tools.logs import (
    MESSAGE_MAX_CHARS,
    get_cooccurring_errors,
    get_error_timeline,
    search_logs,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeBackend:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, UUID, dict[str, Any]]] = []

    async def query(self, sql: str, *, tenant_id: UUID, parameters: dict[str, Any] | None = None):
        self.calls.append((sql, tenant_id, parameters or {}))
        return self.rows


# ------------------------------------------------------------------ timeline


@pytest.mark.asyncio
async def test_timeline_zero_fills_and_finds_peak() -> None:
    backend = FakeBackend(
        [
            {"bucket_start": T0 + timedelta(minutes=1), "n": 4},
            {"bucket_start": T0 + timedelta(minutes=3), "n": 9},
        ]
    )
    out = await get_error_timeline(
        backend, tenant_id=TENANT, fingerprint="fp", start=T0, end=T0 + timedelta(minutes=4)
    )
    assert [b.count for b in out.buckets] == [0, 4, 0, 9, 0]
    assert out.total == 13
    assert out.peak is not None and out.peak.bucket_start == T0 + timedelta(minutes=3)


@pytest.mark.asyncio
async def test_timeline_with_no_rows_has_no_peak() -> None:
    out = await get_error_timeline(
        FakeBackend(), tenant_id=TENANT, fingerprint="fp", start=T0, end=T0 + timedelta(minutes=2)
    )
    assert out.total == 0 and out.peak is None and len(out.buckets) == 3


@pytest.mark.asyncio
async def test_timeline_binds_values_and_aligns_window() -> None:
    backend = FakeBackend()
    hostile = "x' OR 1=1 --"
    await get_error_timeline(
        backend,
        tenant_id=TENANT,
        fingerprint=hostile,
        start=T0 + timedelta(seconds=40),
        end=T0 + timedelta(minutes=2, seconds=10),
        bucket="1m",
    )
    sql, tenant, params = backend.calls[0]
    assert tenant == TENANT
    assert hostile not in sql  # values are bound, never part of the SQL text
    assert params["fingerprint"] == hostile
    assert params["start"] == T0  # floored to the bucket boundary
    assert params["end"] == T0 + timedelta(minutes=3)  # exclusive end


@pytest.mark.asyncio
async def test_timeline_rejects_bad_input() -> None:
    kw = {"tenant_id": TENANT, "fingerprint": "fp"}
    with pytest.raises(ValueError, match="end must be after"):
        await get_error_timeline(FakeBackend(), start=T0, end=T0, **kw)
    with pytest.raises(ValueError, match="bucket"):
        await get_error_timeline(FakeBackend(), start=T0, end=T0 + timedelta(hours=1), bucket="2m", **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="buckets"):
        await get_error_timeline(FakeBackend(), start=T0, end=T0 + timedelta(days=2), bucket="1m", **kw)
    with pytest.raises(ValueError, match="at most"):
        await get_error_timeline(FakeBackend(), start=T0, end=T0 + timedelta(days=30), bucket="1h", **kw)


# ------------------------------------------------------------ co-occurring


@pytest.mark.asyncio
async def test_cooccurring_maps_rows_and_excludes_fingerprint() -> None:
    backend = FakeBackend(
        [
            {
                "service_name": "db",
                "fingerprint": "fp2",
                "message_template": "pool exhausted",
                "n": 7,
                "first_ts": T0,
                "last_ts": T0 + timedelta(minutes=1),
            }
        ]
    )
    out = await get_cooccurring_errors(
        backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1), exclude_fingerprint="fp1"
    )
    assert out[0].service_name == "db" and out[0].count == 7
    sql, _, params = backend.calls[0]
    assert "fingerprint != {exclude_fingerprint:String}" in sql
    assert params["exclude_fingerprint"] == "fp1" and params["max_rows"] == 20


@pytest.mark.asyncio
async def test_cooccurring_without_exclusion_has_no_exclusion_clause() -> None:
    backend = FakeBackend()
    await get_cooccurring_errors(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1))
    assert "exclude_fingerprint" not in backend.calls[0][0]
    assert "exclude_fingerprint" not in backend.calls[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 51])
async def test_cooccurring_limit_bounds(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await get_cooccurring_errors(
            FakeBackend(), tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1), limit=limit
        )


# ------------------------------------------------------------------ search


def _row(msg: str = "boom") -> dict[str, Any]:
    return {"timestamp": T0, "service_name": "api", "level": "ERROR", "message": msg, "fingerprint": "fp"}


@pytest.mark.asyncio
async def test_search_builds_only_requested_filters() -> None:
    backend = FakeBackend()
    await search_logs(backend, tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1))
    sql, _, params = backend.calls[0]
    assert "positionCaseInsensitive" not in sql and "service_name =" not in sql and "level =" not in sql
    assert set(params) == {"start", "end", "max_rows"}


@pytest.mark.asyncio
async def test_search_filters_are_bound_and_level_is_normalised() -> None:
    backend = FakeBackend()
    hostile = "'; DROP TABLE log_events; --"
    await search_logs(
        backend,
        tenant_id=TENANT,
        start=T0,
        end=T0 + timedelta(hours=1),
        query=hostile,
        service="api",
        level="error",
    )
    sql, _, params = backend.calls[0]
    assert hostile not in sql
    assert params["query"] == hostile and params["service"] == "api" and params["level"] == "ERROR"


@pytest.mark.asyncio
async def test_search_detects_limit_and_truncates_messages() -> None:
    rows = [_row("x" * (MESSAGE_MAX_CHARS + 50)), _row(), _row()]
    out = await search_logs(
        FakeBackend(rows), tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1), limit=2
    )
    assert out.limit_reached is True and len(out.lines) == 2
    assert out.lines[0].message_truncated and len(out.lines[0].message) == MESSAGE_MAX_CHARS
    assert not out.lines[1].message_truncated


@pytest.mark.asyncio
async def test_search_exact_fit_is_not_limit_reached() -> None:
    out = await search_logs(
        FakeBackend([_row(), _row()]), tenant_id=TENANT, start=T0, end=T0 + timedelta(hours=1), limit=2
    )
    assert out.limit_reached is False and len(out.lines) == 2


@pytest.mark.asyncio
async def test_search_rejects_bad_input() -> None:
    kw = {"tenant_id": TENANT, "start": T0, "end": T0 + timedelta(hours=1)}
    with pytest.raises(ValueError, match="limit"):
        await search_logs(FakeBackend(), limit=201, **kw)
    with pytest.raises(ValueError, match="level"):
        await search_logs(FakeBackend(), level="LOUD", **kw)
