"""Persistence for ai.provider_config: a tenant's LLM choice per purpose, with its key encrypted.

Every function takes ``tenant_id`` and puts it in the WHERE clause, like the rest of the ``ai``
stores. The row type keeps the ciphertext out of ``repr`` so logging a row cannot leak it.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import provider_config
from app.security.crypto import EncryptedSecret


@dataclass(frozen=True)
class ProviderConfig:
    tenant_id: UUID
    purpose: str
    provider: str
    model: str
    api_key_ciphertext: str | None = field(repr=False)
    key_id: str | None
    key_last4: str | None
    temperature: float | None
    max_tokens: int | None
    updated_by: UUID
    updated_at: datetime

    def stored_key(self) -> EncryptedSecret | None:
        """The stored key as the crypto module's type (to keep it unchanged on an update)."""
        if self.api_key_ciphertext is None or self.key_id is None or self.key_last4 is None:
            return None
        return EncryptedSecret(self.api_key_ciphertext, self.key_id, self.key_last4)


def _to_config(row) -> ProviderConfig:
    m = row._mapping
    return ProviderConfig(
        tenant_id=m["tenant_id"],
        purpose=m["purpose"],
        provider=m["provider"],
        model=m["model"],
        api_key_ciphertext=m["api_key_ciphertext"],
        key_id=m["key_id"],
        key_last4=m["key_last4"],
        temperature=float(m["temperature"]) if m["temperature"] is not None else None,
        max_tokens=m["max_tokens"],
        updated_by=m["updated_by"],
        updated_at=m["updated_at"],
    )


async def list_for_tenant(session: AsyncSession, *, tenant_id: UUID) -> list[ProviderConfig]:
    result = await session.execute(select(provider_config).where(provider_config.c.tenant_id == tenant_id))
    return [_to_config(row) for row in result.all()]


async def get(session: AsyncSession, *, tenant_id: UUID, purpose: str) -> ProviderConfig | None:
    result = await session.execute(
        select(provider_config).where(provider_config.c.tenant_id == tenant_id, provider_config.c.purpose == purpose)
    )
    row = result.first()
    return _to_config(row) if row else None


async def upsert(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    purpose: str,
    provider: str,
    model: str,
    key: EncryptedSecret | None,
    temperature: float | None,
    max_tokens: int | None,
    updated_by: UUID,
) -> ProviderConfig:
    """Create or replace the (tenant, purpose) row. ``key=None`` stores no key."""
    values = {
        "provider": provider,
        "model": model,
        "api_key_ciphertext": key.ciphertext if key else None,
        "key_id": key.key_id if key else None,
        "key_last4": key.last4 if key else None,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "updated_by": updated_by,
    }
    statement = insert(provider_config).values(tenant_id=tenant_id, purpose=purpose, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[provider_config.c.tenant_id, provider_config.c.purpose],
        set_={**values, "updated_at": func.now()},
    ).returning(provider_config)
    result = await session.execute(statement)
    await session.commit()
    return _to_config(result.one())


async def delete_one(session: AsyncSession, *, tenant_id: UUID, purpose: str) -> bool:
    """True if a row was removed (the purpose now falls back to the platform settings)."""
    result = await session.execute(
        delete(provider_config).where(provider_config.c.tenant_id == tenant_id, provider_config.c.purpose == purpose)
    )
    await session.commit()
    return result.rowcount > 0
