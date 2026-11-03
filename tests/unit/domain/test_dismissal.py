"""The dismissal vocabulary: a leading code in the reason; anything else reads as not given."""

from __future__ import annotations

import pytest

from vigil.domain.dismissal import NOT_GIVEN, REASONS, decode, encode


def test_a_code_round_trips_with_and_without_text() -> None:
    assert decode(encode("harmless", "a cat on the dock")) == "harmless"
    assert encode("harmless", "a cat") == "[harmless] a cat"
    assert encode("wrong_zone", None) == f"[wrong_zone] {REASONS['wrong_zone']}"
    assert encode("other", "   ") == f"[other] {REASONS['other']}"


def test_an_unknown_code_is_refused_rather_than_stored() -> None:
    with pytest.raises(ValueError, match="unknown dismissal reason"):
        encode("because", "x")


@pytest.mark.parametrize(
    "text",
    [None, "", "contractors, known", "[unlisted] something", "[harmless", "harmless] x"],
)
def test_anything_without_a_known_leading_code_is_not_given(text: str | None) -> None:
    assert decode(text) == NOT_GIVEN


def test_free_text_that_merely_contains_a_code_is_not_a_code() -> None:
    assert decode("it was [harmless] really") == NOT_GIVEN
