from __future__ import annotations

import pytest

from vigil.core.redaction import (
    REDACTED,
    contains_inline_credentials,
    is_sensitive_key,
    redact_text,
    redact_value,
)


@pytest.mark.parametrize(
    "url",
    ["rtsp://admin:hunter2@10.0.0.5/stream", "rtsp://token@host/x", "https://u:p@example.com"],
)
def test_detects_inline_credentials(url: str) -> None:
    assert contains_inline_credentials(url)


@pytest.mark.parametrize(
    "url", ["rtsp://10.0.0.5:554/stream", "https://example.com/a@b", "webcam:0", ""]
)
def test_clean_urls_are_not_flagged(url: str) -> None:
    assert not contains_inline_credentials(url)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "api_key",
        "apiKey",
        "x-api-key",
        "token",
        "Authorization",
        "client_secret",
        "db_passwd",
        "private_key",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["camera_id", "frames", "topic", "level", "path"])
def test_ordinary_keys_are_not_sensitive(key: str) -> None:
    assert not is_sensitive_key(key)


def test_redact_text_strips_url_userinfo_and_keeps_the_rest() -> None:
    out = redact_text("open rtsp://admin:hunter2@10.0.0.5:554/s failed")
    assert "hunter2" not in out and "admin" not in out
    assert "10.0.0.5:554/s" in out


def test_redact_text_strips_bearer_and_assignments() -> None:
    assert "abcdef123456" not in redact_text("Authorization: Bearer abcdef123456")
    assert "s3cr3t" not in redact_text("connecting with password=s3cr3t now")
    assert "tok123" not in redact_text("token: tok123")


def test_redact_value_recurses_and_masks_by_key() -> None:
    out = redact_value(
        {
            "user": "bob",
            "password": "x",
            "nested": {"api_key": "k", "n": 1},
            "urls": ["rtsp://a:b@h/x"],
        }
    )
    assert out == {
        "user": "bob",
        "password": REDACTED,
        "nested": {"api_key": REDACTED, "n": 1},
        "urls": [f"rtsp://{REDACTED}@h/x"],
    }


def test_redact_value_leaves_non_strings_alone() -> None:
    assert redact_value(5) == 5
    assert redact_value(None) is None
    assert redact_value(b"bytes") == b"bytes"
