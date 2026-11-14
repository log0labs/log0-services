"""Hide secrets and personal data in text before it is shown to a language model.

Logs, stack traces, code, runbooks and past investigations can contain things nobody meant to share with a
model vendor: an API key printed by a debug line, a password in a connection string, a customer's e-mail.
Everything that becomes evidence, every tool result a model reads, and everything the MCP tools return goes
through ``redact`` first. A match is replaced by ``[REDACTED:<kind>]`` so the text stays readable ("the
request failed with password [REDACTED:secret]") and the kind says what was there.

Design choices:

* **A false positive costs a little context; a false negative leaks a secret.** The rules lean towards
  hiding. They are still narrow enough to leave stack traces, class names, ids and numbers alone (see the
  tests that check ordinary log lines come out unchanged).
* **Ordered, specific first.** Token formats with a recognisable shape (private keys, JWTs, GitHub, AWS,
  Slack, Stripe, Google, LLM keys) come before the generic ``name = value`` rule, so each match is labelled
  by what it is.
* **Idempotent.** The marker itself never matches a rule, so redacting twice changes nothing and counts
  nothing the second time (evidence is rebuilt from checkpoints).
* **Bounded patterns.** Every pattern has bounded repetition where it scans for a name, so a megabyte of
  garbage cannot make a match take quadratic time (there is a test with such input).

What it is not: it does not understand context. A password written in prose ("the password is hunter2") is
not caught; a value that is not credential-shaped and sits under an innocent name is not caught. It is one
layer next to the others (tenant scoping, read-only tools, no secrets in code tools), not a guarantee.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MARKER = "[REDACTED:{}]"
_VALUE_MIN = 6  # shorter values after "password=" are not worth hiding ("password=1"), and "token=ok" is noise

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]{0,30}PRIVATE KEY-----|\Z)")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_URL_CREDENTIALS = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.-]{0,20}://[^\s:/@]{1,100}:)(?P<secret>[^\s@/]{1,200})(?=@)")

# the literals a text must contain (lower case) for each token rule to be worth running
_TOKEN_TRIGGERS: dict[str, tuple[str, ...]] = {
    "github_token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "aws_key": ("akia", "asia", "agpa", "aida", "aroa"),
    "slack_token": ("xox",),
    "stripe_key": ("k_live_", "k_test_"),
    "google_key": ("aiza",),
    "api_key": ("sk-", "gsk_", "hf_"),
}

_TOKEN_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("api_key", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,})")),
)

# "Bearer abc123..." and "Basic dXNlcjpwYXNz": the value must contain a digit, so prose such as
# "Basic authentication" is left alone.
_AUTH_HEADER = re.compile(r"\b(?P<scheme>Bearer|Basic)\s+(?P<secret>(?=[A-Za-z0-9._~+/=-]{0,200}[0-9])[A-Za-z0-9._~+/=-]{12,200})", re.IGNORECASE)

_SECRET_WORDS = (
    r"password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|credentials?|authorization|token"
)
# name = value, name: value, "name": "value". The name must END with a secret word, so "max_tokens=4000" and
# "password_policy=strict" are not matched.
_NAME_VALUE = re.compile(
    rf"""(?P<name>[A-Za-z0-9_.-]{{0,40}}(?:{_SECRET_WORDS}))(?P<quote_after>["']?)(?P<sep>\s{{0,3}}[:=]\s{{0,3}})
        (?:"(?P<dq>[^"\n]{{{_VALUE_MIN},500}})"|'(?P<sq>[^'\n]{{{_VALUE_MIN},500}})'|(?P<bare>[^\s,;&"'}}\]]{{{_VALUE_MIN},500}}))""",
    re.IGNORECASE | re.VERBOSE,
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,5}\.[A-Za-z]{2,24}\b")
_CARD_GROUPED = re.compile(r"\b\d{4}([ -])\d{4}\1\d{4}\1\d{4}\b")
_CARD_PLAIN = re.compile(r"\b[3-6]\d{15}\b")


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Redaction:
    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _is_marker(value: str) -> bool:
    return value.startswith("[REDACTED:")


CHUNK = 100_000  # longer text is redacted piece by piece, cut at a line or word end where possible


def redact(text: str) -> Redaction:
    """The text with secrets and personal data replaced, and how many of each kind were hidden."""
    if len(text) <= CHUNK:
        return _redact(text)
    parts: list[str] = []
    counts: Counter[str] = Counter()
    position = 0
    while position < len(text):
        end = min(position + CHUNK, len(text))
        if end < len(text):
            cut = max(text.rfind(chr(10), position, end), text.rfind(" ", position, end))
            if cut > position + CHUNK // 2:
                end = cut + 1
        piece = _redact(text[position:end])
        parts.append(piece.text)
        counts.update(piece.counts)
        position = end
    return Redaction(text="".join(parts), counts=dict(counts))


def _redact(text: str) -> Redaction:
    counts: Counter[str] = Counter()
    lowered = text.lower()

    def sub(
        pattern: re.Pattern[str],
        kind: str,
        repl: Callable[[re.Match[str]], str] | None = None,
        *,
        needs: tuple[str, ...] = (),
    ) -> None:
        """Apply one rule, but only if the text contains one of the literals that rule needs: a plain substring
        test is far cheaper than a regular expression scan, and most text has none of them."""
        nonlocal text
        if needs and not any(literal in lowered for literal in needs):
            return

        def swap(match: re.Match[str]) -> str:
            replacement = repl(match) if repl else MARKER.format(kind)
            if replacement != match.group(0):
                counts[kind] += 1
            return replacement

        text = pattern.sub(swap, text)

    sub(_PRIVATE_KEY, "private_key", needs=("private key",))
    sub(_JWT, "jwt", needs=("eyj",))
    sub(_URL_CREDENTIALS, "password", lambda m: m.group("head") + MARKER.format("password") if not _is_marker(m.group("secret")) else m.group(0), needs=("://",))
    for kind, pattern in _TOKEN_SHAPES:
        sub(pattern, kind, needs=_TOKEN_TRIGGERS[kind])
    sub(_AUTH_HEADER, "bearer_token", lambda m: f"{m.group('scheme')} {MARKER.format('bearer_token')}", needs=("bearer", "basic"))

    def name_value(m: re.Match[str]) -> str:
        value = m.group("dq") or m.group("sq") or m.group("bare")
        if _is_marker(value) or value.lower() in ("bearer", "basic"):
            return m.group(0)  # already hidden, or the scheme word of an Authorization header handled above
        marker = MARKER.format("secret")
        if m.group("dq") is not None:
            shown = f'"{marker}"'
        elif m.group("sq") is not None:
            shown = f"'{marker}'"
        else:
            shown = marker
        return f"{m.group('name')}{m.group('quote_after')}{m.group('sep')}{shown}"

    sub(_NAME_VALUE, "secret", name_value, needs=("=", ":"))
    sub(_EMAIL, "email", needs=("@",))
    sub(_CARD_GROUPED, "card", lambda m: MARKER.format("card") if luhn_ok(m.group(0)) else m.group(0))
    sub(_CARD_PLAIN, "card", lambda m: MARKER.format("card") if luhn_ok(m.group(0)) else m.group(0))
    return Redaction(text=text, counts=dict(counts))


def redact_text(text: str) -> str:
    return redact(text).text


def redact_value(value: Any) -> Any:
    """Redact every string inside nested dicts and lists (keys are left alone). Other values pass through."""
    if isinstance(value, str):
        return redact(value).text
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value
