"""``get_service_traffic``: how much a service wrote, the volume a verdict needs before it can say anything."""

from datetime import timedelta

import pytest

from app.tools.logs import MAX_WINDOW, get_service_traffic
from tests.test_log_tools import T0, TENANT, FakeBackend


@pytest.mark.asyncio
async def test_traffic_is_the_count_of_the_services_log_lines_in_the_window() -> None:
    backend = FakeBackend([{"n": 4321}])

    traffic = await get_service_traffic(backend, tenant_id=TENANT, service_name="payment-service", start=T0, end=T0 + timedelta(minutes=30))

    assert traffic == 4321
    sql, tenant, parameters = backend.calls[0]
    assert tenant == TENANT  # the tenant is bound by the backend, never written into the query
    assert "{service:String}" in sql and "payment-service" not in sql  # the service name is a bound parameter
    assert parameters == {"service": "payment-service", "start": T0, "end": T0 + timedelta(minutes=30)}


@pytest.mark.asyncio
async def test_a_service_that_wrote_nothing_has_zero_traffic() -> None:
    assert await get_service_traffic(FakeBackend([]), tenant_id=TENANT, service_name="x", start=T0, end=T0 + timedelta(minutes=1)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [timedelta(0), timedelta(minutes=-5), MAX_WINDOW + timedelta(seconds=1)])
async def test_a_window_that_makes_no_sense_is_refused(window: timedelta) -> None:
    with pytest.raises(ValueError):
        await get_service_traffic(FakeBackend([{"n": 1}]), tenant_id=TENANT, service_name="x", start=T0, end=T0 + window)
