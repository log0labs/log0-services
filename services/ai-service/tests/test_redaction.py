"""Step 5.6: redaction of secrets and personal data. Every fake secret is built from pieces so that no secret
scanner (or reader) mistakes this file for a leak."""

import time

import pytest

from app.security.redaction import MARKER, luhn_ok, redact, redact_text, redact_value

GH = "ghp_" + "a1B2c3D4e5" * 4
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK = "xoxb-" + "123456789012-abcdefghijkl"
STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
GOOGLE = "AIza" + "SyA-1234567890abcdefghijklmnopqrstu"
OPENAI = "sk-" + "proj-abcdefghijklmnopqrstuvwx"
GROQ = "gsk_" + "abcdefghijklmnopqrstuvwxyz0123"
HF = "hf_" + "abcdefghijklmnopqrstuvwxyz"
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJ1c2VySWQiOiIxMjMifQ" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r"
VISA = "4111 1111 1111 1111"  # a well-known test number


def marked(kind: str) -> str:
    return MARKER.format(kind)


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        (GH, "github_token"),
        (AWS, "aws_key"),
        (SLACK, "slack_token"),
        (STRIPE, "stripe_key"),
        (GOOGLE, "google_key"),
        (OPENAI, "api_key"),
        (GROQ, "api_key"),
        (HF, "api_key"),
        (JWT, "jwt"),
    ],
)
def test_recognisable_token_shapes_are_replaced_and_labelled(secret, kind):
    result = redact(f"calling the API with {secret} failed with 401")
    assert result.text == f"calling the API with {marked(kind)} failed with 401"
    assert result.counts == {kind: 1}


def test_private_keys_are_removed_whole_even_when_truncated():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"key:\n{key}\nnext line").text == f"key:\n{marked('private_key')}\nnext line"
    cut = redact("log: -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\nmore base64")
    assert cut.text == f"log: {marked('private_key')}" and "MIIE" not in cut.text


def test_passwords_in_connection_strings_keep_the_scheme_user_and_host():
    text = "connecting to postgresql://app_user:s3cr3tPass@db.internal:5432/log0 failed"
    assert redact(text).text == f"connecting to postgresql://app_user:{marked('password')}@db.internal:5432/log0 failed"
    assert redact("redis://:onlypass@cache:6379").counts == {}  # no user part: the generic rules below decide


@pytest.mark.parametrize(
    "line",
    [
        "password=hunter22",
        'password: "hunter22"',
        '{"password": "hunter22"}',
        "DB_PASSWORD='hunter22'",
        "api_key = abcdef123456",
        "x-api-key: abcdef123456",
        "client_secret=abcdef123456",
        "refresh_token: abcdef123456",
        "AWS_SECRET_ACCESS_KEY=abcdefghij1234567890",
        "private_key=abcdef123456",
    ],
)
def test_name_value_secrets_hide_only_the_value(line):
    result = redact(line)
    assert "hunter22" not in result.text and "abcdef123456" not in result.text and "abcdefghij1234567890" not in result.text
    assert result.counts == {"secret": 1}
    name = line.split("=")[0].split(":")[0].strip().strip("{").strip('"')
    assert name.split("'")[0].strip('"') in result.text  # the name stays: the line still says what it was


def test_quotes_and_the_separator_survive():
    assert redact('{"password": "hunter22", "user": "bob"}').text == f'{{"password": "{marked("secret")}", "user": "bob"}}'
    assert redact("token = abcdef123456 next").text == f"token = {marked('secret')} next"


def test_authorization_headers():
    assert redact("Authorization: Bearer abcdef0123456789xyz").text == f"Authorization: Bearer {marked('bearer_token')}"
    basic = redact("Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==")
    assert "dXNlcjpw" not in basic.text and basic.counts


def test_emails_and_cards():
    assert redact("sent to jane.doe+test@example.co.uk now").text == f"sent to {marked('email')} now"
    assert redact(f"card {VISA} declined").text == f"card {marked('card')} declined"
    assert redact("card 4111111111111111 declined").text == f"card {marked('card')} declined"


# ------------------------------------------------------------ what must NOT be touched

ORDINARY = [
    "java.net.SocketTimeoutException: Connection timeout after 30000ms calling payment gateway",
    "\tat com.log0.payment.PaymentProcessor.charge(PaymentProcessor.java:120)",
    "request id 3f2a9c1e-77aa-4b1c-9d00-0123456789ab took 1234 ms",
    "max_tokens=4000 temperature=0.2",
    "token expired, please log in again",
    "the password field is required",
    "password_policy=strict",
    "token_count=12",
    "Basic authentication is not supported here",
    "Bearer authentication failed",
    "epoch 1700000000000 and 1700000000123456",
    "user=bob status=500 path=/api/v1/incidents",
    "SELECT * FROM users WHERE id = 42",
    "com.log0.Foo@1a2b3c failed",
    "version 1.2.3 build 4567",
    "4111 1111 1111 1112 is not a card number",  # fails the Luhn check
    "key=short",  # too short to be a credential
]


@pytest.mark.parametrize("line", ORDINARY)
def test_ordinary_log_lines_come_out_unchanged(line):
    result = redact(line)
    assert result.text == line and result.counts == {}


def test_a_stack_trace_survives_intact():
    trace = "Caused by: java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms"
    assert redact_text(trace) == trace


# ------------------------------------------------------------------- behaviour

def test_several_secrets_in_one_text_are_all_counted():
    result = redact(f"{GH} and {AWS} and password=hunter22 and a@b.io")
    assert result.counts == {"github_token": 1, "aws_key": 1, "secret": 1, "email": 1}
    assert result.total == 4


def test_redacting_twice_changes_and_counts_nothing_more():
    once = redact(f"{GH} password=hunter22 jane@example.com postgresql://u:pw123456@h/db")
    twice = redact(once.text)
    assert twice.text == once.text and twice.counts == {}


def test_empty_and_plain_text():
    assert redact("").text == "" and redact("").counts == {}
    assert redact("nothing to hide here").total == 0


def test_redact_value_walks_nested_data_and_leaves_keys_and_numbers():
    value = {"message": f"key {GH}", "n": 5, "items": [{"text": "mail me at a@b.io"}, "ok", 7, None], "password": "hunter22"}
    out = redact_value(value)
    assert out["message"] == f"key {marked('github_token')}" and out["n"] == 5
    assert out["items"][0]["text"] == f"mail me at {marked('email')}" and out["items"][1:] == ["ok", 7, None]
    assert out["password"] == "hunter22"  # a bare value under a key is not text with a name in it: leave structure alone


@pytest.mark.parametrize(
    "number",
    ["4111111111111111", "5500 0000 0000 0004", "378282246310005", "6011-1111-1111-1117"],
)
def test_luhn_accepts_valid_test_numbers(number):
    assert luhn_ok(number.replace("-", " "))


@pytest.mark.parametrize("number", ["4111111111111112", "123", "12345678901234567890123"])
def test_luhn_rejects_the_rest(number):
    assert not luhn_ok(number)


HOSTILE = {
    "letters": "a" * 1_000_000,
    "equals": "=" * 1_000_000,
    "password-word": "password" * 100_000,
    "name-equals": "a=" * 500_000,
    "at-signs": "a@" * 300_000,
    "unclosed-keys": "-----BEGIN PRIVATE KEY-----" * 50_000,
    "digit-groups": "1234 " * 200_000,
}


@pytest.mark.parametrize("garbage", list(HOSTILE.values()), ids=list(HOSTILE))
def test_hostile_input_cannot_make_a_match_slow(garbage):
    started = time.monotonic()
    redact(garbage)
    assert time.monotonic() - started < 5


def test_a_secret_in_the_middle_of_a_very_long_text_is_still_found():
    filler = "ordinary log line without anything secret in it\n" * 6000  # ~290k characters: several chunks
    text = filler + "password=hunter22xyz\n" + filler
    result = redact(text)
    assert "hunter22xyz" not in result.text and result.counts == {"secret": 1}
    assert result.text.count("ordinary log line") == 12000


def test_a_secret_near_a_chunk_boundary_is_found():
    from app.security.redaction import CHUNK

    line = "x" * 98 + "\n"
    text = line * (CHUNK // len(line) - 1) + "api_key=abcdef123456789\n" + line * 2000
    assert "abcdef123456789" not in redact(text).text


def test_a_bearer_token_with_underscores_is_hidden():
    """Found by the evaluation harness: the demo shop logs `Authorization: Bearer sk_live_demo_<digits>`, and the
    Bearer rule's character class had no underscore, so the live key went to the model."""
    key = "sk_live_" + "demo_0123456789abcdef"
    line = f"gateway rejected request POST http://127.0.0.1:8092/fake-gateway/charge with Authorization: Bearer {key}"
    out = redact(line)
    assert key not in out.text and "[REDACTED:" in out.text
    assert out.text.startswith("gateway rejected request POST http://127.0.0.1:8092/fake-gateway/charge with Authorization:")


def test_stripe_style_keys_with_underscores_in_the_tail_are_hidden_anywhere():
    key = "sk_live_" + "demo_0123456789abcdef"
    assert redact(f"STRIPE={key} and {key}").text.count("[REDACTED:") >= 2
    assert redact("sk_test_" + "a1b2c3d4e5f6a7b8c9").counts == {"stripe_key": 1}
