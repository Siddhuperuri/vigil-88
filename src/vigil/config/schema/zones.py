"""The zone file format (05 §4, 02 §2): `config/zones/<zones_ref>.zones.yaml`.

Polygons are in normalized [0, 1] coordinates, so a change of resolution (or a lower inference
tier) never invalidates a zone. A zone file is validated on its own, not as part of Settings:
an invalid one must not stop the application, it must leave that camera without the ZONES
capability and say why (04 §8).
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.core.geometry import Point, Polygon

# `crossing_line` is in the data model's ZoneKind but needs a line, not a polygon, and nothing
# consumes it before a later phase. It is accepted by the type so the error can name it, and
# refused below: a zone kind that parses but does nothing would be fake functionality.
ZoneKindName = Literal["restricted", "exclusion", "counting", "crossing_line", "region_of_interest"]
UNSUPPORTED_KINDS = frozenset({"crossing_line"})

ZONE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
ZONES_REF_PATTERN = ZONE_ID_PATTERN  # both are short slugs, never paths
_CLOCK_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_UTC_OFFSET_MINUTES = 14 * 60
# A zone smaller than this fraction of the frame is almost certainly a drawing mistake (and is
# smaller than the box of the objects it is meant to catch).
MIN_ZONE_AREA_RATIO = 1e-4
MAX_POLYGON_POINTS = 64


def parse_clock(text: str) -> int:
    """'HH:MM' to minutes after midnight."""
    match = _CLOCK_PATTERN.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not a time of day written HH:MM (00:00 to 23:59)")
    return int(match.group(1)) * 60 + int(match.group(2))


class ScheduleConfig(ConfigModel):
    """When a zone is in force. A window may wrap midnight: 18:00 to 06:00 is overnight."""

    start: str
    end: str
    utc_offset_minutes: int = Field(
        default=0, ge=-MAX_UTC_OFFSET_MINUTES, le=MAX_UTC_OFFSET_MINUTES
    )
    # Monday=0 .. Sunday=6 in the zone's local time. Empty means every day.
    weekdays: tuple[int, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, value: str) -> str:
        parse_clock(value)
        return value

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 0 <= d <= 6 for d in value):
            raise ValueError("weekdays must be numbers from 0 (Monday) to 6 (Sunday)")
        return value

    @model_validator(mode="after")
    def _non_empty_window(self) -> Self:
        if parse_clock(self.start) == parse_clock(self.end):
            raise ValueError("a schedule window must not be empty; omit the schedule for 'always'")
        return self


class ZoneConfig(ConfigModel):
    zone_id: str
    kind: ZoneKindName
    polygon: tuple[tuple[float, float], ...]
    label: str | None = None
    # 0..4, feeds the severity engine (06 §8).
    criticality: int = Field(default=0, ge=0, le=4)
    schedule: ScheduleConfig | None = None

    @field_validator("zone_id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not ZONE_ID_PATTERN.match(value):
            raise ValueError(
                f"invalid zone_id {value!r}: must match {ZONE_ID_PATTERN.pattern} "
                "(lowercase letters, digits, '_' and '-', starting alphanumeric)"
            )
        return value

    @field_validator("kind")
    @classmethod
    def _supported_kind(cls, value: str) -> str:
        if value in UNSUPPORTED_KINDS:
            raise ValueError(
                f"zone kind {value!r} is not implemented yet (see docs/LIMITATIONS.md); "
                "use restricted, exclusion, counting or region_of_interest"
            )
        return value

    @model_validator(mode="after")
    def _geometry(self) -> Self:
        if not 3 <= len(self.polygon) <= MAX_POLYGON_POINTS:
            raise ValueError(f"a polygon needs 3 to {MAX_POLYGON_POINTS} points")
        for x, y in self.polygon:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(
                    "polygon points are normalized coordinates and must lie within [0, 1]"
                )
        poly = self.to_polygon()
        if not poly.is_simple():
            raise ValueError(
                "the polygon crosses or touches itself, repeats a point or has no area; "
                "draw it as a simple outline"
            )
        if poly.area() < MIN_ZONE_AREA_RATIO:
            raise ValueError(f"the polygon covers less than {MIN_ZONE_AREA_RATIO:.2%} of the frame")
        return self

    def to_polygon(self) -> Polygon:
        return Polygon([Point(x, y) for x, y in self.polygon])

    @property
    def display_label(self) -> str:
        return self.label or self.zone_id


class ZoneSetConfig(ConfigModel):
    version: Literal[1] = 1
    zones: tuple[ZoneConfig, ...] = ()

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for z in self.zones:
            if z.zone_id in seen:
                raise ValueError(f"duplicate zone_id {z.zone_id!r}")
            seen.add(z.zone_id)
        return self
