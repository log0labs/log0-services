"""Verify that a webhook really comes from GitHub: HMAC-SHA256 of the raw body with the shared secret."""

import hashlib
import hmac

_PREFIX = "sha256="


def verify_github_signature(secret: str, body: bytes, header: str | None) -> bool:
    """True only if ``header`` (``X-Hub-Signature-256``) matches ``body``; compared in constant time."""
    if not header or not header.startswith(_PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix(_PREFIX), expected)
