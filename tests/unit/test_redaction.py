import json

from social_media_favorites_archiver.safety.redaction import (
    StructuredLogger,
    redact_mapping,
    redact_text,
)


def test_redacts_fake_cookie_bearer_url_signature_key_and_private_identity() -> None:
    fake_values = (
        "session=fake-cookie-value",
        "fake-bearer-token",
        "FAKE-SIGNATURE-123",
        "sk-proj-FAKE-OPENAI-KEY-123456",
        "local-user",
        "person@example.invalid",
    )
    text = (
        "Cookie: session=fake-cookie-value\n"
        "Authorization: Bearer fake-bearer-token\n"
        "https://cdn.example.invalid/file?signature=FAKE-SIGNATURE-123&token=private\n"
        "key=sk-proj-FAKE-OPENAI-KEY-123456\n"
        "/Users/local-user/private and person@example.invalid"
    )

    redacted = redact_text(text)

    for value in fake_values:
        assert value not in redacted
    assert "[REDACTED]" in redacted
    assert "/Users/[REDACTED]/private" in redacted


def test_structured_redaction_never_serializes_raw_responses_or_environment_values() -> None:
    fake_secret = "fake-raw-private-value"
    payload = {
        "code": "adapter.failed",
        "raw_response": {"body": fake_secret},
        "environment": {"OPENAI_API_KEY": fake_secret},
        "safe_count": 2,
        "url": "https://example.invalid/item?x-signature=fake-signature",
    }

    sanitized = redact_mapping(payload)
    serialized = json.dumps(sanitized)
    event = StructuredLogger().event("adapter.failed", payload)

    assert fake_secret not in serialized
    assert fake_secret not in event
    assert sanitized["raw_response"] == "[REDACTED]"
    assert sanitized["environment"] == "[REDACTED]"
    assert sanitized["safe_count"] == 2

