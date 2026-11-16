
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.mark.asyncio
async def test_health_up_when_database_up():
    app = create_app()
    app.state.db_engine = None

    with patch(
        "app.api.health._database_status",
        new=AsyncMock(return_value="UP"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "UP"
    assert body["database"]["status"] == "UP"


@pytest.mark.asyncio
async def test_health_503_when_database_down():
    app = create_app()
    app.state.db_engine = None

    with patch(
        "app.api.health._database_status",
        new=AsyncMock(return_value="DOWN"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "DOWN"
