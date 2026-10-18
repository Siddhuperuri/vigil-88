"""Filters and the SQL they become (05 §5).

One place builds incident `WHERE` clauses, and both the incident repository and the analytics
layer use it. That is not DRY for its own sake: a filter that meant something slightly different
in the list and in the summary would make the console's numbers disagree with its rows, and
"analytics match persisted records and the selected filters" is the property this exists to
hold.

**Values are always parameters.** Nothing in this module interpolates a caller's value into SQL.
The only text this module puts into a statement is a column name chosen from a fixed table
defined here, and a sort direction from an enum — so a hostile filter value cannot become
syntax. `vigil.persistence` has no other query builder.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from vigil.core.errors import PersistenceError
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.persistence.mappers import to_utc_text

# Sortable columns, by the name the API accepts. A name not in this table is rejected, so no
# caller-supplied text ever reaches a statement.
SORT_COLUMNS: dict[str, str] = {
    "confirmed": "confirmed_utc",
    "first_observed": "first_observed_utc",
    "last_observed": "last_observed_utc",
    "severity": "severity",
    "severity_score": "severity_score",
    "confidence": "confidence_ratio",
    "duration": "duration_ms",
}
DEFAULT_SORT = "confirmed"

OPEN_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.CANDIDATE,
        IncidentStatus.CONFIRMED,
        IncidentStatus.ACTIVE,
        IncidentStatus.RESOLVING,
    }
)
TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset(
    {
        IncidentStatus.RESOLVED,
        IncidentStatus.DISMISSED,
        IncidentStatus.EXPIRED,
        IncidentStatus.SUPPRESSED,
    }
)


@dataclass(frozen=True, slots=True)
class IncidentFilter:
    """What an operator narrowed the list to. Every field is optional and ANDed."""

    camera_ids: tuple[str, ...] = ()
    event_types: tuple[EventType, ...] = ()
    statuses: tuple[IncidentStatus, ...] = ()
    min_severity: Severity | None = None
    since_wall_utc: datetime | None = None
    until_wall_utc: datetime | None = None
    zone_id: str | None = None
    search: str | None = None
    open_only: bool = False
    has_evidence: bool | None = None

    def __post_init__(self) -> None:
        if (
            self.since_wall_utc is not None
            and self.until_wall_utc is not None
            and self.until_wall_utc < self.since_wall_utc
        ):
            raise PersistenceError("the filter's time range ends before it starts")

    @property
    def describes_everything(self) -> bool:
        return self == IncidentFilter()


# The no-op filter, as a shared frozen singleton. A default argument of `IncidentFilter()`
# would construct one per call site at import time, which is harmless but which ruff rightly
# refuses to let pass unexamined (B008).
ALL_INCIDENTS = IncidentFilter()


@dataclass(slots=True)
class WhereClause:
    sql: str
    params: list[object] = field(default_factory=list)

    def and_(self, fragment: str, *params: object) -> None:
        self.sql = fragment if not self.sql else f"{self.sql} AND {fragment}"
        self.params.extend(params)

    @property
    def rendered(self) -> str:
        return f" WHERE {self.sql}" if self.sql else ""


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def build_where(filters: IncidentFilter, *, alias: str = "") -> WhereClause:
    """Turn a filter into a parameterised clause. `alias` prefixes columns for a join."""
    prefix = f"{alias}." if alias else ""
    clause = WhereClause("")
    if filters.camera_ids:
        clause.and_(
            f"{prefix}camera_id IN ({_placeholders(len(filters.camera_ids))})",
            *filters.camera_ids,
        )
    if filters.event_types:
        clause.and_(
            f"{prefix}event_type IN ({_placeholders(len(filters.event_types))})",
            *[t.value for t in filters.event_types],
        )
    statuses = tuple(filters.statuses)
    if filters.open_only:
        statuses = (
            tuple(sorted(OPEN_STATUSES, key=lambda s: s.value))
            if not statuses
            else tuple(s for s in statuses if s in OPEN_STATUSES)
        )
        if not statuses:
            clause.and_("1 = 0")  # the caller asked for open incidents of a terminal status
            return clause
    if statuses:
        clause.and_(
            f"{prefix}status IN ({_placeholders(len(statuses))})", *[s.value for s in statuses]
        )
    if filters.min_severity is not None:
        clause.and_(f"{prefix}severity >= ?", int(filters.min_severity))
    if filters.since_wall_utc is not None:
        clause.and_(f"{prefix}confirmed_utc >= ?", to_utc_text(filters.since_wall_utc))
    if filters.until_wall_utc is not None:
        clause.and_(f"{prefix}confirmed_utc < ?", to_utc_text(filters.until_wall_utc))
    if filters.zone_id is not None:
        # zone_ids is a JSON array; the quoted form makes "dock" not match "dockside".
        clause.and_(f"{prefix}zone_ids_json LIKE ?", f'%"{_escape_like(filters.zone_id)}"%')
    if filters.search:
        term = f"%{_escape_like(filters.search)}%"
        clause.and_(
            f"({prefix}display_ref LIKE ? ESCAPE '\\' OR {prefix}location LIKE ? ESCAPE '\\' "
            f"OR {prefix}operator_note LIKE ? ESCAPE '\\' OR {prefix}camera_id LIKE ? ESCAPE '\\')",
            term,
            term,
            term,
            term,
        )
    if filters.has_evidence is not None:
        exists = (
            "EXISTS (SELECT 1 FROM evidence e WHERE e.incident_id = "
            f"{prefix or 'incidents.'}incident_id)"
        )
        clause.and_(exists if filters.has_evidence else f"NOT {exists}")
    return clause


def _escape_like(value: str) -> str:
    """LIKE treats `%` and `_` as wildcards; an operator searching for `cam_1` means `cam_1`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def order_by(sort: str, *, descending: bool = True, alias: str = "") -> str:
    column = SORT_COLUMNS.get(sort)
    if column is None:
        known = ", ".join(sorted(SORT_COLUMNS))
        raise PersistenceError(f"unknown sort key {sort!r}; known keys: {known}")
    prefix = f"{alias}." if alias else ""
    direction = "DESC" if descending else "ASC"
    # The id is the tiebreaker so paging is stable when two incidents share a timestamp; ULIDs
    # sort by creation time, so it is also the right secondary order.
    return f"ORDER BY {prefix}{column} {direction}, {prefix}incident_id {direction}"


def clamp_page_size(requested: int | None, *, default: int, maximum: int) -> int:
    if requested is None:
        return min(default, maximum)
    return max(1, min(requested, maximum))


def keyset_clause(
    cursor: tuple[str, str] | None, sort: str, *, descending: bool, alias: str = ""
) -> tuple[str, Sequence[object]]:
    """`(sort_value, incident_id)` from the last row of the previous page.

    Keyset rather than OFFSET: an incident list is written to while it is paged through, and
    OFFSET silently skips or repeats rows when that happens (08 §10).
    """
    if cursor is None:
        return ("", ())
    column = SORT_COLUMNS.get(sort)
    if column is None:
        raise PersistenceError(f"unknown sort key {sort!r}")
    prefix = f"{alias}." if alias else ""
    comparison = "<" if descending else ">"
    sql = f"({prefix}{column}, {prefix}incident_id) {comparison} (?, ?)"
    return (sql, cursor)
