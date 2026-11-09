"""Request and response shapes for tenant LLM settings and repository connections.

Nothing in here can carry a stored secret out: the ``*Out`` models have no key or token field,
only the ``key_last4`` tail the console may show. Secrets come in as ``SecretStr`` (they print
as ``**********``) and are handed to ``security.crypto`` straight away.
"""

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr, field_validator

from app.llm.providers import get_provider
from app.llm.purpose import Purpose
from app.schemas.base import CamelModel

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # GitHub user / organization names
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_PATH_PREFIX = 500


def normalize_path_prefix(value: str) -> str:
    """A folder inside a repository: no leading or trailing slash, never leaving the checkout.

    ``""`` means the whole repository. ``..``, ``.``, empty segments (``a//b``), backslashes and
    NUL are refused: the code tools of 3.5 join this onto a checkout path, so it must not be
    able to climb out of it.
    """
    prefix = value.strip().strip("/")
    if len(prefix) > MAX_PATH_PREFIX or "\\" in prefix or "\x00" in prefix:
        raise ValueError("path prefix must be a relative folder like services/payment")
    if prefix and any(part in ("", ".", "..") for part in prefix.split("/")):
        raise ValueError("path prefix must be a relative folder like services/payment")
    return prefix


# ---------------------------------------------------------------------------- AI settings


class AiSettingSource(StrEnum):
    TENANT = "tenant"  # saved by the tenant in ai.provider_config
    PLATFORM = "platform"  # the platform's LLM_* settings: no row for this purpose


class AiSettingOut(CamelModel):
    purpose: Purpose
    source: AiSettingSource
    provider: str
    model: str
    key_last4: str | None = None  # only for a tenant-saved key
    temperature: float | None = None
    max_tokens: int | None = None
    updated_at: datetime | None = None


class AiSettingIn(CamelModel):
    """Used by PUT (save) and POST .../test (try without saving)."""

    model_config = ConfigDict(extra="forbid")  # an unsupported field (baseUrl) is an error, not ignored

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32000)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        get_provider(value)  # raises ValueError naming the supported providers
        return value


class ConnectionCheckOut(CamelModel):
    ok: bool
    provider: str
    model: str
    latency_ms: int
    error: str | None = None  # a short category (auth, model_not_found, ...), never the provider's message


# ----------------------------------------------------------------------------- repositories


class RepositoryIn(CamelModel):
    model_config = ConfigDict(extra="forbid")

    owner: str
    name: str
    token: SecretStr = Field(min_length=8, max_length=512)

    @field_validator("owner")
    @classmethod
    def _owner(cls, value: str) -> str:
        if not _OWNER.match(value):
            raise ValueError("owner is not a valid GitHub user or organization name")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _REPO_NAME.match(value) or value in (".", ".."):
            raise ValueError("name is not a valid GitHub repository name")
        return value


class RepositoryOut(CamelModel):
    id: UUID
    provider: str
    owner: str
    name: str
    default_branch: str
    created_at: datetime


class ServiceRepositoryIn(CamelModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    path_prefix: str = ""

    @field_validator("path_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        return normalize_path_prefix(value)


class ServiceRepositoryOut(CamelModel):
    service_name: str
    repository_id: UUID
    path_prefix: str
