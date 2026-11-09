"""``/api/v1/ai-settings``: which LLM a tenant uses for each purpose, with its own key.

Anyone signed in can read the settings; only an ADMIN can change them or test a key. A purpose
with no saved row shows the platform's default (``source: "platform"``). The API key is
encrypted before it is stored and is never returned: the response only has ``keyLast4``.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_connection_checker, get_optional_secret_box
from app.config import Settings, get_settings
from app.db.session import get_db
from app.llm.connection_check import ConnectionChecker
from app.llm.purpose import Purpose
from app.llm.resolver import PlatformLlmResolver
from app.schemas.ai_config import (
    AiSettingIn,
    AiSettingOut,
    AiSettingSource,
    ConnectionCheckOut,
)
from app.security.crypto import CryptoError, SecretBox, secret_context
from app.security.jwt import CurrentUser, get_current_user, require_admin
from app.store import provider_config as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai-settings", tags=["ai-settings"])

_NO_BOX = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="Secret storage is not configured on this server",
)


def _tenant_out(row: store.ProviderConfig) -> AiSettingOut:
    return AiSettingOut(
        purpose=Purpose(row.purpose),
        source=AiSettingSource.TENANT,
        provider=row.provider,
        model=row.model,
        key_last4=row.key_last4,
        temperature=row.temperature,
        max_tokens=row.max_tokens,
        updated_at=row.updated_at,
    )


def _platform_out(settings: Settings, purpose: Purpose) -> AiSettingOut:
    spec = settings.llm_models[purpose]
    return AiSettingOut(purpose=purpose, source=AiSettingSource.PLATFORM, provider=spec.provider, model=spec.model)


@router.get("", response_model=list[AiSettingOut])
async def list_ai_settings(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[AiSettingOut]:
    """One entry per purpose: the tenant's own setting, or the platform default."""
    saved = {row.purpose: row for row in await store.list_for_tenant(session, tenant_id=user.tenant_id)}
    return [_tenant_out(saved[p.value]) if p.value in saved else _platform_out(settings, p) for p in Purpose]


@router.put("/{purpose}", response_model=AiSettingOut)
async def save_ai_setting(
    purpose: Purpose,
    body: AiSettingIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    box: Annotated[SecretBox | None, Depends(get_optional_secret_box)],
) -> AiSettingOut:
    """Save the tenant's setting for a purpose.

    Key handling: a new ``apiKey`` replaces the stored one; no ``apiKey`` keeps the stored key
    as long as the provider is unchanged; a changed provider without a key clears it (a key for
    another provider is useless), so the platform's key for that provider is used instead.
    """
    context = secret_context(admin.tenant_id, "provider_config", purpose.value)
    if body.api_key is not None:
        if box is None:
            raise _NO_BOX
        key = box.encrypt(body.api_key.get_secret_value(), context=context)
    else:
        existing = await store.get(session, tenant_id=admin.tenant_id, purpose=purpose.value)
        key = existing.stored_key() if existing and existing.provider == body.provider else None

    row = await store.upsert(
        session,
        tenant_id=admin.tenant_id,
        purpose=purpose.value,
        provider=body.provider,
        model=body.model,
        key=key,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        updated_by=admin.user_id,
    )
    logger.info("ai setting saved: tenant=%s purpose=%s provider=%s", admin.tenant_id, purpose.value, body.provider)
    return _tenant_out(row)


@router.delete("/{purpose}", status_code=status.HTTP_204_NO_CONTENT)
async def reset_ai_setting(
    purpose: Purpose,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Forget the tenant's setting (and its key): the purpose uses the platform default again."""
    if not await store.delete_one(session, tenant_id=admin.tenant_id, purpose=purpose.value):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{purpose}/test", response_model=ConnectionCheckOut)
async def test_ai_setting(
    purpose: Purpose,
    body: AiSettingIn,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    box: Annotated[SecretBox | None, Depends(get_optional_secret_box)],
    checker: Annotated[ConnectionChecker, Depends(get_connection_checker)],
) -> ConnectionCheckOut:
    """Send one tiny prompt with these values (nothing is saved) and report how it went.

    The key used is, in order: the ``apiKey`` in the body, the key saved for this purpose (same
    provider), the platform's key for the provider. The key never appears in the response.
    """
    key = body.api_key.get_secret_value() if body.api_key is not None else None

    if key is None:
        saved = await store.get(session, tenant_id=admin.tenant_id, purpose=purpose.value)
        stored = saved.stored_key() if saved and saved.provider == body.provider else None
        if stored is not None:
            if box is None:
                raise _NO_BOX
            try:
                context = secret_context(admin.tenant_id, "provider_config", purpose.value)
                key = box.decrypt(stored.ciphertext, key_id=stored.key_id, context=context).get_secret_value()
            except CryptoError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The saved key can no longer be read; enter it again",
                ) from None

    if key is None:
        try:
            key = PlatformLlmResolver(settings).api_key_for(body.provider).get_secret_value()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No API key is available for {body.provider!r}: send apiKey",
            ) from None

    result = await checker(body.provider, body.model, key)
    return ConnectionCheckOut(ok=result.ok, provider=body.provider, model=body.model, latency_ms=result.latency_ms, error=result.error)
