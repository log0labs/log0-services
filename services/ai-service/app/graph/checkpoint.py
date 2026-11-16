"""The Postgres checkpointer: durable graph state, one thread per investigation.

After every step LangGraph writes the whole state to Postgres under a ``thread_id`` (we use
the investigation id). That is what lets a run survive a restart and pause for human
review and resume days later. The tables live in the ``ai`` schema next to ours.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

from app.config import Settings
from app.graph.state import checkpoint_serde

CHECKPOINT_SCHEMA = "ai"


def checkpoint_conninfo(settings: Settings) -> str:
    """A libpq connection string for DATABASE_URL with ``search_path`` set to the ai schema.

    LangGraph creates its tables without a schema name, so they land in the first schema
    of the search path. ``make_conninfo`` quotes every value, so a password containing
    spaces or ``@`` cannot break the string.
    """
    url = make_url(settings.database_url)
    return make_conninfo(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        options=f"-c search_path={CHECKPOINT_SCHEMA}"
    )


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncGenerator[AsyncPostgresSaver]:
    """Open the saver, create its tables if missing, close the connection on exit."""
    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(settings), serde=checkpoint_serde()
    ) as saver:
        await saver.setup()  # idempotent: records applied migrations in checkpoint_migrations
        yield saver
