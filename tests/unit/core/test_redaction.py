from __future__ import annotations

from typing import ClassVar

import pytest

from vigil.core.redaction import (
    MAX_INLINE_BYTES,
    REDACTED,
    contains_inline_credentials,
    is_sensitive_key,
    redact_text,
    redact_value,
    summarise_opaque,
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


# ------------------------------------------------------------------ image data (01 §10)
#
# "No full image frames in logs" held until P6 only because no caller happened to log one --
# `redact_value` passed bytes and arrays straight through. That is a statement about today's
# callers, and the caller that matters is the one added next year. A single 1080p frame is
# 6.2 MB, so the operational argument agrees with the privacy one.


def test_a_jpeg_sized_blob_is_summarised_rather_than_rendered() -> None:
    blob = b"\xff\xd8\xff\xe0" + b"\x00" * 60_000
    rendered = redact_value({"clip": blob})
    assert isinstance(rendered, dict)
    assert rendered["clip"] == f"<bytes: {len(blob)} bytes>"
    assert "\xff" not in str(rendered["clip"])


@pytest.mark.parametrize("kind", [bytes, bytearray, memoryview])
def test_every_bytes_like_type_is_covered(kind: type) -> None:
    value = kind(b"x" * 1000)
    summarised = summarise_opaque(value)
    assert summarised is not None
    assert "1000 bytes" in summarised


def test_a_short_blob_stays_readable() -> None:
    """A hash, a nonce or a short header is useful in a log line and is not image data."""
    assert summarise_opaque(b"deadbeef") is None
    assert redact_value({"sha": b"deadbeef"}) == {"sha": b"deadbeef"}


def test_the_threshold_is_where_it_says_it_is() -> None:
    assert summarise_opaque(b"x" * MAX_INLINE_BYTES) is None
    assert summarise_opaque(b"x" * (MAX_INLINE_BYTES + 1)) is not None


def test_a_decoded_frame_is_summarised_by_its_shape() -> None:
    """Duck-typed on `shape`, because `core/` must not import numpy (the domain-purity contract)
    and the thing being guarded against is any array-like, not one library's type."""

    class FakeFrame:
        shape = (1080, 1920, 3)

    rendered = redact_value({"pixels": FakeFrame()})
    assert isinstance(rendered, dict)
    assert rendered["pixels"] == "<FakeFrame shape=(1080, 1920, 3)>"


def test_a_real_numpy_array_is_summarised() -> None:
    np = pytest.importorskip("numpy")
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    assert summarise_opaque(frame) == "<ndarray shape=(48, 64, 3)>"


def test_an_array_interface_without_a_shape_is_still_caught() -> None:
    class Exotic:
        __array_interface__: ClassVar[dict[str, object]] = {"shape": (4,), "typestr": "|u1"}

    assert summarise_opaque(Exotic()) == "<Exotic array>"


def test_an_ordinary_object_with_no_shape_is_left_alone() -> None:
    """The guard must not swallow the structured context a log line exists to carry."""
    assert summarise_opaque(42) is None
    assert summarise_opaque("a string") is None
    assert summarise_opaque({"a": 1}) is None
    assert summarise_opaque(None) is None


def test_a_class_object_is_not_mistaken_for_an_array() -> None:
    """A class whose *instances* have a shape has `shape` as a descriptor, not a value."""

    class HasShapeAttribute:
        shape: tuple[int, ...]

    assert summarise_opaque(HasShapeAttribute) is None


def test_image_data_nested_inside_a_structure_is_still_summarised() -> None:
    nested = {"evidence": {"frames": [b"y" * 5000, b"z" * 5000]}}
    rendered = redact_value(nested)
    assert rendered == {"evidence": {"frames": ["<bytes: 5000 bytes>", "<bytes: 5000 bytes>"]}}


def test_a_sensitive_key_still_wins_over_the_summary() -> None:
    """Order matters: a 4 KB token must be `***`, not `<bytes: 4096 bytes>`."""
    assert redact_value({"api_token": b"t" * 4096}) == {"api_token": REDACTED}
