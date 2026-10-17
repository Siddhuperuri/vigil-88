"""The mirror in `config/schema/taxonomy.py` must match `domain/enums.py` exactly.

This test is the whole justification for that file existing. `config/` is L1 and cannot import
L2 `domain/` (01 §5), but config validation still has to reject an unknown event type at
startup, so the names are mirrored -- and the mirror is only safe if drift fails loudly.

If you added an event type or a severity band and landed here: add it to
`src/vigil/config/schema/taxonomy.py` too.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vigil.config.schema.alerts import AlertsConfig
from vigil.config.schema.severity import SeverityConfig
from vigil.config.schema.taxonomy import EVENT_TYPE_NAMES, SEVERITY_NAMES
from vigil.domain.enums import EventType, Severity


def test_event_type_names_mirror_the_enum_exactly() -> None:
    assert {e.value for e in EventType} == EVENT_TYPE_NAMES


def test_severity_names_mirror_the_enum_in_band_order() -> None:
    assert tuple(s.name for s in sorted(Severity)) == SEVERITY_NAMES


def test_every_mirrored_severity_name_resolves_back_to_the_enum() -> None:
    assert [Severity[name] for name in SEVERITY_NAMES] == sorted(Severity)


def test_config_still_rejects_an_unknown_event_type() -> None:
    """The behaviour the mirror exists to preserve: validation at construction, not at load."""
    with pytest.raises(ValidationError, match="unknown event types"):
        SeverityConfig(base_by_type={"not_an_event": 0.5})


def test_config_accepts_every_real_event_type() -> None:
    config = SeverityConfig(base_by_type={e.value: 0.5 for e in EventType})
    assert len(config.base_by_type) == len(EventType)


def test_alerts_min_severity_accepts_every_band() -> None:
    for name in SEVERITY_NAMES:
        assert AlertsConfig(min_severity=name).min_severity == name  # type: ignore[arg-type]
