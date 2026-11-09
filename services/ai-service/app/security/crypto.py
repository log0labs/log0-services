"""Encrypt the secrets a tenant gives us (LLM API keys, repository tokens) before they are stored.

The database only ever holds ciphertext (``api_key_ciphertext``, ``token_ciphertext``). This
module is the single place that turns a secret into ciphertext and back.

* **Fernet** (from ``cryptography``) does the work: AES-128-CBC for secrecy plus an HMAC, so a
  ciphertext that was altered in any way fails to decrypt instead of producing garbage.
* **Master keys** come from settings (``SECRETS_KEYS``), each with an id (``key_id``). New
  secrets are encrypted with the active key; the id is stored next to the ciphertext, so old
  secrets still decrypt after a new key becomes active. That is key rotation without downtime.
* **Context binding**: every secret is encrypted together with a context string (for example
  the tenant and the purpose). Decrypting with a different context fails. A ciphertext copied
  from one row to another, or one tenant's key pasted into another tenant's row, is refused
  even though the master key is the same.
* **Nothing leaks by accident**: decrypted values are returned as ``SecretStr`` (it prints as
  ``**********``), ``EncryptedSecret`` hides its ciphertext in ``repr``, and no error message
  contains a secret or a ciphertext.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.config import Settings

_SEPARATOR = "\x00"  # context and secret are joined with it; a context may not contain it
_HIDDEN_LAST4 = "****"
_MIN_LENGTH_FOR_LAST4 = 12  # shorter secrets would reveal too much of themselves


class CryptoError(Exception):
    """Base class. Messages never contain secrets, ciphertexts or keys."""


class CryptoNotConfiguredError(CryptoError):
    """No usable master key is configured (SECRETS_KEYS / SECRETS_ACTIVE_KEY_ID)."""


class UnknownKeyError(CryptoError):
    """The ciphertext was made with a master key id this process does not have."""


class DecryptionError(CryptoError):
    """Wrong key, wrong context, or the ciphertext was altered."""


@dataclass(frozen=True)
class EncryptedSecret:
    """What gets stored: the ciphertext, the id of the master key, and the safe-to-show tail."""

    ciphertext: str = field(repr=False)
    key_id: str
    last4: str


def last4_of(secret: str) -> str:
    """The tail the console may show ("...a1b2"); ``****`` for short secrets."""
    return secret[-4:] if len(secret) >= _MIN_LENGTH_FOR_LAST4 else _HIDDEN_LAST4


class SecretBox:
    """Encrypts and decrypts with the configured master keys."""

    def __init__(self, keys: Mapping[str, SecretStr | str], active_key_id: str) -> None:
        if not keys:
            raise CryptoNotConfiguredError("SECRETS_KEYS is empty: set at least one master key")
        if active_key_id not in keys:
            raise CryptoNotConfiguredError("SECRETS_ACTIVE_KEY_ID is not one of the ids in SECRETS_KEYS")
        self._fernets: dict[str, Fernet] = {}
        for key_id, key in keys.items():
            raw = key.get_secret_value() if isinstance(key, SecretStr) else key
            try:
                self._fernets[key_id] = Fernet(raw.encode())
            except ValueError:  # not 32 url-safe base64 bytes; do not echo the value
                raise CryptoNotConfiguredError(f"master key {key_id!r} is not a valid Fernet key") from None
        self._active = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active

    def encrypt(self, secret: str, *, context: str) -> EncryptedSecret:
        """Encrypt ``secret`` with the active key, bound to ``context``."""
        if not secret:
            raise ValueError("cannot encrypt an empty secret")
        _check_context(context)
        token = self._fernets[self._active].encrypt(f"{context}{_SEPARATOR}{secret}".encode())
        return EncryptedSecret(ciphertext=token.decode(), key_id=self._active, last4=last4_of(secret))

    def decrypt(self, ciphertext: str, *, key_id: str, context: str) -> SecretStr:
        """Decrypt with the key that made it; fails unless the context is the one it was bound to."""
        _check_context(context)
        fernet = self._fernets.get(key_id)
        if fernet is None:
            raise UnknownKeyError(f"no master key with id {key_id!r}")
        try:
            plain = fernet.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, UnicodeError):
            raise DecryptionError("could not decrypt: wrong key, or the stored value was altered") from None
        stored_context, separator, secret = plain.partition(_SEPARATOR)
        if not separator or stored_context != context:
            raise DecryptionError("could not decrypt: the secret belongs to a different context")
        return SecretStr(secret)

    def needs_rotation(self, key_id: str) -> bool:
        """True for a secret that was encrypted with a key that is no longer the active one."""
        return key_id != self._active

    def rotate(self, ciphertext: str, *, key_id: str, context: str) -> EncryptedSecret:
        """Re-encrypt a stored secret with the active key (same context, new ciphertext)."""
        secret = self.decrypt(ciphertext, key_id=key_id, context=context).get_secret_value()
        return self.encrypt(secret, context=context)


def _check_context(context: str) -> None:
    if not context or _SEPARATOR in context:
        raise ValueError("context must be a non-empty string without NUL characters")


def secret_context(tenant_id: UUID, kind: str, name: str) -> str:
    """The context string for a stored secret: who it belongs to and what it is for.

    ``secret_context(tenant, "provider_config", "investigation")`` or
    ``secret_context(tenant, "repository_connection", "acme/shop")`` (the id of a new connection
    does not exist yet when its token is encrypted, so the repository's owner/name is used).
    """
    return f"{tenant_id}|{kind}|{name}"


def secret_box_from_settings(settings: Settings) -> SecretBox:
    """Build the box from SECRETS_KEYS / SECRETS_ACTIVE_KEY_ID; raises when they are not set."""
    return SecretBox(settings.secrets_keys, settings.secrets_active_key_id)
