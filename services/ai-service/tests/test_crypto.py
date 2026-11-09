"""security.crypto: master keys, context binding, rotation, and nothing leaking by accident."""

import logging
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.config import Settings
from app.security.crypto import (
    CryptoNotConfiguredError,
    DecryptionError,
    EncryptedSecret,
    SecretBox,
    UnknownKeyError,
    last4_of,
    secret_box_from_settings,
    secret_context,
)

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
API_KEY = "gsk_live_0123456789abcdefABCD"
CONTEXT = secret_context(TENANT_A, "provider_config", "investigation")


def _key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox({"k1": _key()}, "k1")


# ------------------------------------------------------------------------- round trip


def test_a_secret_comes_back_exactly_as_it_went_in(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    plain = box.decrypt(stored.ciphertext, key_id=stored.key_id, context=CONTEXT)
    assert plain.get_secret_value() == API_KEY


def test_unusual_secrets_survive(box: SecretBox) -> None:
    for secret in ("x", "ghp_" + "a" * 200, "pass wörd with spaces and üñí", "line\nbreak"):
        stored = box.encrypt(secret, context=CONTEXT)
        assert box.decrypt(stored.ciphertext, key_id=stored.key_id, context=CONTEXT).get_secret_value() == secret


def test_the_ciphertext_does_not_contain_the_secret_and_differs_every_time(box: SecretBox) -> None:
    first = box.encrypt(API_KEY, context=CONTEXT)
    second = box.encrypt(API_KEY, context=CONTEXT)
    assert API_KEY not in first.ciphertext and "gsk_live" not in first.ciphertext
    assert first.ciphertext != second.ciphertext  # a fresh random IV: equal secrets cannot be spotted


def test_what_is_stored_carries_the_key_id_and_the_last_four(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    assert (stored.key_id, stored.last4) == ("k1", "ABCD")


def test_an_empty_secret_is_refused(box: SecretBox) -> None:
    with pytest.raises(ValueError, match="empty"):
        box.encrypt("", context=CONTEXT)


# ---------------------------------------------------------------------------- last four


def test_last4_shows_the_tail_of_long_secrets_and_hides_short_ones() -> None:
    assert last4_of("sk-abcdefghijklmnop") == "mnop"
    assert last4_of("exactly12chr") == "2chr"
    assert last4_of("shortsecret") == "****"  # 11 characters: four of them would be too much
    assert last4_of("abc") == "****"


# ------------------------------------------------------------------------------ context


def test_a_ciphertext_cannot_be_decrypted_for_another_tenant_or_purpose(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    for other in (
        secret_context(TENANT_B, "provider_config", "investigation"),  # another tenant
        secret_context(TENANT_A, "provider_config", "triage"),  # another purpose
        secret_context(TENANT_A, "repository_connection", "investigation"),  # another table
    ):
        with pytest.raises(DecryptionError, match="different context"):
            box.decrypt(stored.ciphertext, key_id="k1", context=other)


@pytest.mark.parametrize("bad", ["", "a\x00b"])
def test_a_bad_context_is_a_programming_error(box: SecretBox, bad: str) -> None:
    with pytest.raises(ValueError, match="context"):
        box.encrypt(API_KEY, context=bad)
    with pytest.raises(ValueError, match="context"):
        box.decrypt("whatever", key_id="k1", context=bad)


def test_the_context_helper_names_tenant_kind_and_name() -> None:
    assert secret_context(TENANT_A, "repository_connection", "acme/shop") == f"{TENANT_A}|repository_connection|acme/shop"


# ---------------------------------------------------------------------------- tampering


def test_a_changed_ciphertext_is_rejected_not_decrypted_to_garbage(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    last = stored.ciphertext[-1]
    tampered = stored.ciphertext[:-1] + ("A" if last != "A" else "B")
    with pytest.raises(DecryptionError):
        box.decrypt(tampered, key_id="k1", context=CONTEXT)


@pytest.mark.parametrize("junk", ["", "not a token", "gAAAAA"])
def test_junk_in_the_column_is_a_decryption_error(box: SecretBox, junk: str) -> None:
    with pytest.raises(DecryptionError):
        box.decrypt(junk, key_id="k1", context=CONTEXT)


def test_another_master_key_cannot_read_it() -> None:
    stored = SecretBox({"k1": _key()}, "k1").encrypt(API_KEY, context=CONTEXT)
    stranger = SecretBox({"k1": _key()}, "k1")  # same id, different key
    with pytest.raises(DecryptionError):
        stranger.decrypt(stored.ciphertext, key_id="k1", context=CONTEXT)


def test_a_key_id_this_process_does_not_have_is_its_own_error(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    with pytest.raises(UnknownKeyError, match="k9"):
        box.decrypt(stored.ciphertext, key_id="k9", context=CONTEXT)


# ---------------------------------------------------------------------------- rotation


def test_after_rotation_old_secrets_still_decrypt_and_new_ones_use_the_new_key() -> None:
    old_key, new_key = _key(), _key()
    old_box = SecretBox({"k1": old_key}, "k1")
    stored_before = old_box.encrypt(API_KEY, context=CONTEXT)

    rotated_box = SecretBox({"k1": old_key, "k2": new_key}, "k2")
    assert rotated_box.decrypt(stored_before.ciphertext, key_id="k1", context=CONTEXT).get_secret_value() == API_KEY
    assert rotated_box.encrypt("another-long-secret-value", context=CONTEXT).key_id == "k2"
    assert rotated_box.needs_rotation("k1") and not rotated_box.needs_rotation("k2")


def test_rotate_re_encrypts_with_the_active_key_and_keeps_the_secret_and_context() -> None:
    old_key, new_key = _key(), _key()
    stored = SecretBox({"k1": old_key}, "k1").encrypt(API_KEY, context=CONTEXT)
    box = SecretBox({"k1": old_key, "k2": new_key}, "k2")

    rotated = box.rotate(stored.ciphertext, key_id=stored.key_id, context=CONTEXT)

    assert rotated.key_id == "k2" and rotated.ciphertext != stored.ciphertext and rotated.last4 == stored.last4
    assert box.decrypt(rotated.ciphertext, key_id="k2", context=CONTEXT).get_secret_value() == API_KEY
    only_new = SecretBox({"k2": new_key}, "k2")  # the old key can now be retired
    assert only_new.decrypt(rotated.ciphertext, key_id="k2", context=CONTEXT).get_secret_value() == API_KEY


def test_rotating_with_the_wrong_context_changes_nothing() -> None:
    box = SecretBox({"k1": _key()}, "k1")
    stored = box.encrypt(API_KEY, context=CONTEXT)
    with pytest.raises(DecryptionError):
        box.rotate(stored.ciphertext, key_id="k1", context=secret_context(TENANT_B, "provider_config", "investigation"))


# ------------------------------------------------------------------------ configuration


def test_a_box_needs_keys_an_active_id_from_them_and_valid_key_material() -> None:
    with pytest.raises(CryptoNotConfiguredError, match="SECRETS_KEYS"):
        SecretBox({}, "k1")
    with pytest.raises(CryptoNotConfiguredError, match="SECRETS_ACTIVE_KEY_ID"):
        SecretBox({"k1": _key()}, "k2")
    with pytest.raises(CryptoNotConfiguredError, match="not a valid Fernet key") as caught:
        SecretBox({"k1": "this-is-not-a-fernet-key-but-secret"}, "k1")
    assert "this-is-not" not in str(caught.value)  # the bad value is never echoed


def test_secret_str_keys_are_accepted() -> None:
    box = SecretBox({"k1": SecretStr(_key())}, "k1")
    assert box.active_key_id == "k1"


def test_the_box_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _key()
    monkeypatch.setenv("SECRETS_KEYS", f'{{"k1": "{key}"}}')
    monkeypatch.setenv("SECRETS_ACTIVE_KEY_ID", "k1")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert list(settings.secrets_keys) == ["k1"]
    assert key not in repr(settings)  # SecretStr: the key does not show in the settings repr
    box = secret_box_from_settings(settings)
    stored = box.encrypt(API_KEY, context=CONTEXT)
    assert box.decrypt(stored.ciphertext, key_id="k1", context=CONTEXT).get_secret_value() == API_KEY


def test_unset_keys_leave_the_service_startable_but_the_box_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRETS_KEYS", raising=False)
    monkeypatch.delenv("SECRETS_ACTIVE_KEY_ID", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.secrets_keys == {} and settings.secrets_active_key_id == ""
    with pytest.raises(CryptoNotConfiguredError):
        secret_box_from_settings(settings)


# ------------------------------------------------------------------------ no accidents


def test_decrypted_secrets_print_hidden(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    plain = box.decrypt(stored.ciphertext, key_id="k1", context=CONTEXT)
    assert API_KEY not in str(plain) and API_KEY not in repr(plain) and API_KEY not in f"{plain}"


def test_the_stored_form_hides_its_ciphertext_in_repr(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    assert stored.ciphertext not in repr(stored)
    assert "ABCD" in repr(stored) and "k1" in repr(stored)
    assert isinstance(stored, EncryptedSecret)


def test_the_box_does_not_show_its_keys_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    key = _key()
    secret_box = SecretBox({"k1": key}, "k1")
    with caplog.at_level(logging.DEBUG):
        stored = secret_box.encrypt(API_KEY, context=CONTEXT)
        secret_box.decrypt(stored.ciphertext, key_id="k1", context=CONTEXT)
    assert key not in repr(secret_box)
    assert caplog.records == []


def test_error_messages_never_contain_the_secret_or_the_ciphertext(box: SecretBox) -> None:
    stored = box.encrypt(API_KEY, context=CONTEXT)
    other = secret_context(TENANT_B, "provider_config", "investigation")
    with pytest.raises(DecryptionError) as wrong_context:
        box.decrypt(stored.ciphertext, key_id="k1", context=other)
    with pytest.raises(DecryptionError) as tampered:
        box.decrypt(stored.ciphertext[:-3] + "AAA", key_id="k1", context=CONTEXT)
    for error in (wrong_context.value, tampered.value):
        assert API_KEY not in str(error) and stored.ciphertext not in str(error)
