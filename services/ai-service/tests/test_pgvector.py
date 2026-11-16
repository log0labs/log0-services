"""Step 5.1: the database can store and compare vectors.

Needs the real Postgres from docker compose (the pgvector image) with the migration applied.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.config import get_settings
from tests.conftest import requires_postgres

pytestmark = requires_postgres

COMPOSE = Path(__file__).resolve().parents[3] / "docker" / "docker-compose.yml"


@pytest.fixture
def conn():
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        yield connection
        connection.rollback()
    engine.dispose()


def test_extension_is_enabled(conn):
    version = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
    assert version is not None


def test_vectors_round_trip_as_text(conn):
    conn.execute(text("CREATE TEMP TABLE t (v vector(3))"))
    conn.execute(text("INSERT INTO t VALUES ('[1,2,3]')"))
    assert conn.execute(text("SELECT v::text FROM t")).scalar() == "[1,2,3]"


def test_cosine_distance_orders_by_direction_not_length(conn):
    conn.execute(text("CREATE TEMP TABLE t (name text, v vector(2))"))
    conn.execute(text("INSERT INTO t VALUES ('same direction, long', '[10,0]'), ('orthogonal', '[0,1]'), ('opposite', '[-1,0]')"))
    rows = conn.execute(text("SELECT name, v <=> '[1,0]' AS d FROM t ORDER BY d")).all()
    assert [r.name for r in rows] == ["same direction, long", "orthogonal", "opposite"]
    assert rows[0].d == pytest.approx(0.0)
    assert rows[1].d == pytest.approx(1.0)
    assert rows[2].d == pytest.approx(2.0)


def test_a_wrong_dimension_is_refused(conn):
    conn.execute(text("CREATE TEMP TABLE t (v vector(3))"))
    with pytest.raises(Exception, match="expected 3 dimensions"):
        conn.execute(text("INSERT INTO t VALUES ('[1,2]')"))


def test_the_compose_file_uses_the_pgvector_image():
    assert "image: pgvector/pgvector:pg16" in COMPOSE.read_text(encoding="utf-8")
