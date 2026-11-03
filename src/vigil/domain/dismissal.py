"""Why an operator dismissed an incident: a small fixed vocabulary, so the answer can be counted.

A dismissal is the only measurement of "the system was wrong" that exists here (06 §9), and a
free-text reason cannot be counted. The code is stored at the front of the transition's reason,
`[code] text`, so no schema changes and a reason written by an older client still reads as plain
text. It is a label an operator chose, not a verdict: nobody has checked what was actually in the
picture.
"""

from __future__ import annotations

import re
from types import MappingProxyType

REASONS = MappingProxyType(
    {
        "nothing_there": "Nothing was there",
        "harmless": "Something was there, but harmless",
        "authorised": "A known or authorised person",
        "camera_artifact": "Camera artefact (glare, shadow, compression)",
        "wrong_zone": "The zone is drawn wrongly",
        "duplicate": "Duplicate of another incident",
        "other": "Another reason",
    }
)

NOT_GIVEN = "not_given"
_PATTERN = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.DOTALL)


def encode(code: str, text: str | None) -> str:
    """`[code] text`. Raises ValueError for a code outside the vocabulary."""
    if code not in REASONS:
        raise ValueError(f"unknown dismissal reason {code!r}")
    detail = (text or "").strip()
    return f"[{code}] {detail}" if detail else f"[{code}] {REASONS[code]}"


def decode(reason: str | None) -> str:
    """The code at the front of a stored reason, or `not_given` for anything else."""
    match = _PATTERN.match(reason or "")
    if match is not None and match.group(1) in REASONS:
        return match.group(1)
    return NOT_GIVEN
