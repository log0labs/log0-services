"""The request models: what is accepted, normalised and refused before any code runs."""

import pytest
from pydantic import ValidationError

from app.schemas.ai_config import (
    AiSettingIn,
    RepositoryIn,
    ServiceRepositoryIn,
    normalize_path_prefix,
)

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("", ""),
        ("/", ""),
        ("services/payment", "services/payment"),
        ("/services/payment/", "services/payment"),
        ("  services/payment  ", "services/payment"),
        ("a.b/c-d_e", "a.b/c-d_e"),
        (".github/workflows", ".github/workflows"),
        ("..hidden/x", "..hidden/x"),  # a folder that merely starts with dots is fine
    ],
)
def test_path_prefixes_are_normalised(raw: str, clean: str) -> None:
    assert normalize_path_prefix(raw) == clean


@pytest.mark.parametrize("raw", ["..", "../x", "x/..", "a/../b", ".", "a/./b", "a//b", "a\\b", "a\x00b", "x" * 501])
def test_path_prefixes_that_could_leave_the_checkout_are_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="relative folder"):
        normalize_path_prefix(raw)


def test_secrets_in_requests_do_not_show_in_repr_or_dumps() -> None:
    setting = AiSettingIn(provider="groq", model="m", apiKey="gsk_live_0123456789abcdefABCD")  # type: ignore[call-arg]
    repo = RepositoryIn(owner="acme", name="shop", token=TOKEN)  # type: ignore[arg-type]
    for model in (setting, repo):
        assert "gsk_live" not in repr(model) and TOKEN not in repr(model)
        assert "gsk_live" not in str(model.model_dump()) and TOKEN not in str(model.model_dump())
        assert "gsk_live" not in model.model_dump_json() and TOKEN not in model.model_dump_json()


def test_requests_accept_camel_case_and_refuse_unknown_fields() -> None:
    parsed = AiSettingIn.model_validate({"provider": "groq", "model": "m", "maxTokens": 100})
    assert parsed.max_tokens == 100 and parsed.api_key is None
    with pytest.raises(ValidationError):
        AiSettingIn.model_validate({"provider": "groq", "model": "m", "baseUrl": "http://x"})
    with pytest.raises(ValidationError):
        ServiceRepositoryIn.model_validate({"repositoryId": "not-a-uuid"})


def test_unknown_provider_error_lists_the_supported_ones() -> None:
    with pytest.raises(ValidationError, match="groq"):
        AiSettingIn.model_validate({"provider": "skynet", "model": "m"})


@pytest.mark.parametrize("owner", ["acme", "Acme-Corp", "a", "a1-b2", "x" * 39])
def test_valid_github_owners(owner: str) -> None:
    assert RepositoryIn(owner=owner, name="shop", token=TOKEN).owner == owner  # type: ignore[arg-type]


@pytest.mark.parametrize("owner", ["", "-acme", "ac me", "acme/x", "../x", "a_b", "x" * 40, "ac.me"])
def test_invalid_github_owners(owner: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryIn(owner=owner, name="shop", token=TOKEN)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["shop", "my.repo", "my_repo-2", ".github", "x" * 100])
def test_valid_repository_names(name: str) -> None:
    assert RepositoryIn(owner="acme", name=name, token=TOKEN).name == name  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b", "a?b", "x" * 101])
def test_invalid_repository_names(name: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryIn(owner="acme", name=name, token=TOKEN)  # type: ignore[arg-type]
