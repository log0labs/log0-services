"""Webhook signature check."""

import hashlib
import hmac

from app.security.webhook_signature import verify_github_signature

BODY = b'{"a": 1}'


def _sign(secret: str, body: bytes = BODY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_the_right_signature_passes() -> None:
    assert verify_github_signature("s", BODY, _sign("s"))


def test_anything_else_fails() -> None:
    assert not verify_github_signature("s", BODY, _sign("other"))
    assert not verify_github_signature("s", b"changed", _sign("s"))
    assert not verify_github_signature("s", BODY, _sign("s").removeprefix("sha256="))
    assert not verify_github_signature("s", BODY, "")
    assert not verify_github_signature("s", BODY, None)
