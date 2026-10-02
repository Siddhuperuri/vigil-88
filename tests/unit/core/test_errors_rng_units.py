from __future__ import annotations

import pytest

from vigil.core import errors
from vigil.core.rng import seeded_rng
from vigil.core.units import NS_PER_S, ms_to_ns, ms_to_s, ns_to_ms, s_to_ms


@pytest.mark.parametrize(
    "cls",
    [
        errors.ConfigError,
        errors.SourceError,
        errors.TransientSourceError,
        errors.InferenceError,
        errors.ModuleError,
        errors.PersistenceError,
        errors.CapabilityError,
        errors.LifecycleError,
    ],
)
def test_every_error_is_a_vigil_error(cls: type[errors.VigilError]) -> None:
    assert issubclass(cls, errors.VigilError)


def test_config_error_lists_every_issue() -> None:
    e = errors.ConfigError("invalid configuration", issues=["a: bad", "b: worse"])
    text = str(e)
    assert "invalid configuration" in text and "a: bad" in text and "b: worse" in text


def test_config_error_without_issues_is_just_the_message() -> None:
    assert str(errors.ConfigError("plain")) == "plain"


def test_error_context_is_read_only() -> None:
    e = errors.VigilError("x", context={"camera_id": "a"})
    assert e.context["camera_id"] == "a"
    with pytest.raises(TypeError):
        e.context["camera_id"] = "b"  # type: ignore[index]


def test_source_error_retryability() -> None:
    assert errors.SourceError("x").retryable is True
    assert errors.SourceError("x", retryable=False).retryable is False
    transient = errors.TransientSourceError("x")
    assert isinstance(transient, errors.SourceError) and transient.retryable


def test_seeded_rng_is_reproducible_and_streams_are_independent() -> None:
    a = [seeded_rng(1, "s").random() for _ in range(3)]
    assert a == [seeded_rng(1, "s").random() for _ in range(3)]
    assert seeded_rng(1, "s").random() != seeded_rng(1, "t").random()
    assert seeded_rng(1, "s").random() != seeded_rng(2, "s").random()


def test_unit_conversions_round_trip() -> None:
    assert ms_to_ns(1.5) == 1_500_000
    assert ns_to_ms(2_500_000) == 2.5
    assert NS_PER_S == 1_000_000_000
    assert s_to_ms(ms_to_s(1234.0)) == pytest.approx(1234.0)
