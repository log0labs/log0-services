"""The M3 tables (ai.provider_config, ai.repository_connection, ai.service_repository).

Integration tests: they need Postgres with the migrations applied (``alembic upgrade head``).
They pin what the design relies on: one config per tenant per purpose, one repository mapping
per tenant per service, a tenant column on every table, secrets stored only as ciphertext, and
a database-level refusal to link one tenant's service to another tenant's repository.
"""

import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import provider_config, repository_connection, service_repository

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT_A = UUID("31000000-0000-4000-8000-00000000000a")
TENANT_B = UUID("31000000-0000-4000-8000-00000000000b")
USER = UUID("31000000-0000-4000-8000-0000000000c1")
TABLES: tuple[Table, ...] = (provider_config, repository_connection, service_repository)


async def _wipe(session: AsyncSession) -> None:
    for tenant in (TENANT_A, TENANT_B):
        for name in ("service_repository", "repository_connection", "provider_config"):
            await session.execute(text(f"DELETE FROM ai.{name} WHERE tenant_id = :t"), {"t": tenant})
    await session.commit()


@pytest_asyncio.fixture
async def session(db_session: AsyncSession) -> AsyncGenerator[AsyncSession]:
    await _wipe(db_session)
    yield db_session
    await db_session.rollback()  # a failed insert leaves the transaction unusable
    await _wipe(db_session)


def _config(tenant: UUID = TENANT_A, purpose: str = "investigation", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": tenant,
        "purpose": purpose,
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "updated_by": USER,
    }
    return row | overrides


def _repo(tenant: UUID = TENANT_A, name: str = "shop", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": tenant,
        "owner": "acme",
        "name": name,
        "token_ciphertext": "gAAAAA-not-a-real-token",
        "key_id": "k1",
        "created_by": USER,
    }
    return row | overrides


async def _add_repo(session: AsyncSession, tenant: UUID = TENANT_A, name: str = "shop") -> UUID:
    result = await session.execute(insert(repository_connection).values(**_repo(tenant, name)).returning(repository_connection.c.id))
    return result.scalar_one()


async def _rejects(session: AsyncSession, statement: Any) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(statement)
    await session.rollback()


# ------------------------------------------------------------------------- the shape


async def test_the_core_definitions_match_the_real_columns(session: AsyncSession) -> None:
    for table in TABLES:
        rows = await session.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'ai' AND table_name = :t"),
            {"t": table.name},
        )
        assert {r[0] for r in rows} == {c.name for c in table.columns}, table.name


async def test_every_table_has_a_required_tenant_column(session: AsyncSession) -> None:
    for table in TABLES:
        row = (
            await session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'ai' AND table_name = :t AND column_name = 'tenant_id'"
                ),
                {"t": table.name},
            )
        ).one()
        assert row[0] == "NO", table.name


async def test_secrets_exist_only_as_ciphertext_columns() -> None:
    names = {c.name for table in TABLES for c in table.columns}
    assert {"api_key_ciphertext", "token_ciphertext"} <= names
    assert not names & {"api_key", "token", "secret", "password"}  # no plaintext column to fill by mistake


# ---------------------------------------------------------------------- provider_config


async def test_one_config_per_tenant_and_purpose_but_other_tenants_and_purposes_are_free(session: AsyncSession) -> None:
    await session.execute(insert(provider_config).values(**_config()))
    await session.execute(insert(provider_config).values(**_config(purpose="triage")))
    await session.execute(insert(provider_config).values(**_config(tenant=TENANT_B)))
    await session.commit()

    await _rejects(session, insert(provider_config).values(**_config(model="another")))


async def test_a_config_row_gets_its_defaults_and_stores_what_it_is_given(session: AsyncSession) -> None:
    await session.execute(
        insert(provider_config).values(
            **_config(
                base_url="https://llm.example.com/v1",
                api_key_ciphertext="gAAAAA-x",
                key_id="k1",
                key_last4="a1b2",
                temperature=0.2,
                max_tokens=2000,
            )
        )
    )
    await session.commit()
    row = (await session.execute(select(provider_config).where(provider_config.c.tenant_id == TENANT_A))).one()._mapping
    assert (row["provider"], row["model"], row["key_last4"], row["max_tokens"]) == ("groq", "openai/gpt-oss-120b", "a1b2", 2000)
    assert float(row["temperature"]) == 0.2 and row["updated_at"] is not None


@pytest.mark.parametrize("purpose", ["chat", "", "Investigation", "embedding"])
async def test_only_the_four_known_purposes_are_accepted(session: AsyncSession, purpose: str) -> None:
    await _rejects(session, insert(provider_config).values(**_config(purpose=purpose)))


@pytest.mark.parametrize("purpose", ["summary", "triage", "investigation", "evaluation"])
async def test_each_known_purpose_is_accepted(session: AsyncSession, purpose: str) -> None:
    await session.execute(insert(provider_config).values(**_config(purpose=purpose)))
    await session.commit()


@pytest.mark.parametrize(
    "key_columns",
    [
        {"api_key_ciphertext": "gAAAAA-x"},  # ciphertext without key id and last4
        {"api_key_ciphertext": "gAAAAA-x", "key_id": "k1"},  # no last4
        {"key_id": "k1", "key_last4": "a1b2"},  # key id and last4 without a ciphertext
    ],
)
async def test_a_key_is_stored_complete_or_not_at_all(session: AsyncSession, key_columns: dict[str, str]) -> None:
    await _rejects(session, insert(provider_config).values(**_config(**key_columns)))


async def test_a_config_without_any_key_is_allowed(session: AsyncSession) -> None:
    await session.execute(insert(provider_config).values(**_config(provider="ollama", base_url="http://localhost:11434")))
    await session.commit()


@pytest.mark.parametrize("overrides", [{"temperature": -0.1}, {"temperature": 2.5}, {"max_tokens": 0}, {"max_tokens": -5}])
async def test_nonsense_generation_settings_are_refused(session: AsyncSession, overrides: dict[str, Any]) -> None:
    await _rejects(session, insert(provider_config).values(**_config(**overrides)))


# ---------------------------------------------------------------- repository_connection


async def test_a_repository_connection_gets_an_id_and_the_github_defaults(session: AsyncSession) -> None:
    repo_id = await _add_repo(session)
    await session.commit()
    row = (await session.execute(select(repository_connection).where(repository_connection.c.id == repo_id))).one()._mapping
    assert (row["provider"], row["default_branch"], row["owner"], row["name"]) == ("github", "main", "acme", "shop")
    assert row["created_at"] is not None


async def test_the_same_repository_cannot_be_connected_twice_by_a_tenant_but_another_tenant_may(session: AsyncSession) -> None:
    await _add_repo(session, TENANT_A, "shop")
    await _add_repo(session, TENANT_A, "billing")  # a different repository: fine
    await _add_repo(session, TENANT_B, "shop")  # same repository, other tenant: fine
    await session.commit()

    await _rejects(session, insert(repository_connection).values(**_repo(TENANT_A, "shop")))


async def test_only_github_is_accepted_as_a_provider_for_now(session: AsyncSession) -> None:
    await _rejects(session, insert(repository_connection).values(**_repo(provider="gitlab")))


@pytest.mark.parametrize("missing", ["token_ciphertext", "key_id", "created_by", "owner", "name"])
async def test_a_repository_connection_needs_its_token_and_identity(session: AsyncSession, missing: str) -> None:
    row = _repo()
    del row[missing]
    await _rejects(session, insert(repository_connection).values(**row))


# ----------------------------------------------------------------- service_repository


async def test_one_repository_mapping_per_tenant_and_service(session: AsyncSession) -> None:
    repo_a = await _add_repo(session, TENANT_A)
    repo_b = await _add_repo(session, TENANT_B)
    await session.execute(
        insert(service_repository).values(tenant_id=TENANT_A, service_name="payment-service", repository_id=repo_a, path_prefix="services/payment")
    )
    await session.execute(insert(service_repository).values(tenant_id=TENANT_A, service_name="auth-service", repository_id=repo_a))
    await session.execute(insert(service_repository).values(tenant_id=TENANT_B, service_name="payment-service", repository_id=repo_b))
    await session.commit()

    await _rejects(session, insert(service_repository).values(tenant_id=TENANT_A, service_name="payment-service", repository_id=repo_a))

    row = (
        await session.execute(
            select(service_repository).where(service_repository.c.tenant_id == TENANT_A, service_repository.c.service_name == "auth-service")
        )
    ).one()._mapping
    assert row["path_prefix"] == ""  # the default: the whole repository


async def test_a_service_cannot_point_at_another_tenants_repository(session: AsyncSession) -> None:
    other_tenants_repo = await _add_repo(session, TENANT_B)
    await session.commit()
    await _rejects(session, insert(service_repository).values(tenant_id=TENANT_A, service_name="payment-service", repository_id=other_tenants_repo))


async def test_a_service_cannot_point_at_a_repository_that_does_not_exist(session: AsyncSession) -> None:
    await _rejects(session, insert(service_repository).values(tenant_id=TENANT_A, service_name="payment-service", repository_id=uuid4()))


async def test_disconnecting_a_repository_removes_the_service_mappings_that_used_it(session: AsyncSession) -> None:
    repo_id = await _add_repo(session)
    await session.execute(insert(service_repository).values(tenant_id=TENANT_A, service_name="payment-service", repository_id=repo_id))
    await session.commit()

    await session.execute(text("DELETE FROM ai.repository_connection WHERE id = :id"), {"id": repo_id})
    await session.commit()

    left = (await session.execute(select(service_repository).where(service_repository.c.tenant_id == TENANT_A))).all()
    assert left == []
