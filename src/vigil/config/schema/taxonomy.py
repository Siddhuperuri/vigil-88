"""The domain taxonomy, mirrored for configuration validation.

**This file duplicates `domain/enums.py` on purpose, and the duplication is enforced.**

`config/` is L1 and `domain/` is L2, so configuration cannot import the enums (01 §5). But
config validation still has to reject `severity.base_by_type: {not_an_event: 0.5}` at startup
with a precise message, because an invalid config is a startup failure and never a silent
fallback (01 §7.1). The two requirements conflict, and there are only three ways out:

  1. import upward anyway -- which is what the layering contract exists to prevent, and which
     `lint-imports` had been failing on;
  2. move the check into the loader -- but then constructing `SeverityConfig` directly, as every
     test and every programmatic caller does, would no longer validate at all;
  3. mirror the names here and prove the mirror is correct.

Option 3 is taken. The risk it carries is drift, so that risk is closed by a test:
`tests/unit/config/test_taxonomy.py` asserts these sets are exactly the enum members. Adding an
event type without updating this file fails that test, which is a better failure than either of
the alternatives -- it is loud, immediate, and names the file to change.
"""

from __future__ import annotations

# Mirrors domain.enums.EventType.
EVENT_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "fire",
        "smoke",
        "vehicle_collision",
        "person_down",
        "crowd_anomaly",
        "intrusion",
        "restricted_area_entry",
        "abandoned_object",
        "dangerous_motion",
        "vehicle_anomaly",
    }
)

# Mirrors domain.enums.Severity, in band order.
SEVERITY_NAMES: tuple[str, ...] = ("INFO", "LOW", "MODERATE", "HIGH", "CRITICAL")
