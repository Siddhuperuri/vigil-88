"""The rule that earns `persistence/`'s S608 lint exemption (pyproject.toml).

S608 flags every f-string containing SQL, and this layer has many: statements are assembled
from column tuples declared in the module and from runs of `?` whose count comes from
`len(values)`. The lint cannot tell that apart from interpolating a caller's value, so it is
switched off for the layer — and these tests enforce the real rule instead, which is stronger:

  * a caller's **value** is only ever bound as a parameter, never spliced into statement text;
  * a caller's **identifier** (a sort key) must come from a fixed table, and anything else is
    refused rather than passed through;
  * LIKE wildcards inside a value are escaped, so a search for `cam_1` means `cam_1`;
  * and all of that holds against a real database, not just against the generated string.

A `# noqa` could not express any of this. If someone later interpolates a value, these fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.core.errors import PersistenceError
from vigil.domain.enums import EventType, IncidentStatus, Severity
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.queries import (
    SORT_COLUMNS,
    IncidentFilter,
    build_where,
    clamp_page_size,
    keyset_clause,
    order_by,
)
from vigil.persistence.repositories.incidents import IncidentRepository

# Payloads that would end a statement, start a comment, or close a quote if they were ever
# spliced into SQL rather than bound.
HOSTILE = (
    "'; DROP TABLE incidents; --",
    '" OR 1=1 --',
    "cam-1' UNION SELECT sha256 FROM evidence --",
    "\\'; DELETE FROM evidence WHERE 1=1; --",
    "%' OR '1'='1",
    "a' /* comment */ OR 'x'='x",
)
# Fragments that must never appear in generated statement text as a result of a value.
FORBIDDEN_FRAGMENTS = ("DROP", "DELETE", "UNION", "--", "/*", ";")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(
        tmp_path / "vigil.sqlite3",
        config=StorageConfig(),
        clock=ManualClock(),
        logger=get_logger("test.db"),
        metrics=MetricsRegistry(),
    )
    db.open()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------- values stay parameters


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_hostile_value_never_reaches_statement_text(payload: str) -> None:
    clause = build_where(
        IncidentFilter(
            camera_ids=(payload,),
            zone_id=payload,
            search=payload,
        )
    )
    assert payload not in clause.sql
    # It is present, exactly once per use, as a bound parameter.
    assert any(payload in str(p) for p in clause.params)
    upper = clause.sql.upper()
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in upper


@pytest.mark.parametrize("payload", HOSTILE)
def test_placeholder_count_matches_parameter_count(payload: str) -> None:
    clause = build_where(
        IncidentFilter(
            camera_ids=(payload, "cam-2"),
            event_types=(EventType.INTRUSION, EventType.PERSON_DOWN),
            statuses=(IncidentStatus.ACTIVE,),
            min_severity=Severity.HIGH,
            since_wall_utc=datetime(2026, 1, 1, tzinfo=UTC),
            until_wall_utc=datetime(2026, 2, 1, tzinfo=UTC),
            zone_id=payload,
            search=payload,
        )
    )
    assert clause.sql.count("?") == len(clause.params)


def test_every_filter_field_contributes_only_placeholders() -> None:
    clause = build_where(IncidentFilter(camera_ids=("a", "b", "c")))
    assert clause.sql.count("?") == 3
    assert clause.params == ["a", "b", "c"]


def test_an_empty_filter_builds_no_clause() -> None:
    clause = build_where(IncidentFilter())
    assert clause.sql == ""
    assert clause.rendered == ""
    assert clause.params == []


# ---------------------------------------------------------------- identifiers are allow-listed


@pytest.mark.parametrize(
    "payload",
    [
        "confirmed_utc; DROP TABLE incidents",
        "severity DESC, (SELECT sha256 FROM evidence)",
        "../../etc/passwd",
        "",
        "CONFIRMED",  # right column, wrong case: still not in the table
    ],
)
def test_an_unknown_sort_key_is_refused_rather_than_interpolated(payload: str) -> None:
    with pytest.raises(PersistenceError, match="unknown sort key"):
        order_by(payload)


def test_known_sort_keys_produce_only_known_columns() -> None:
    for key, column in SORT_COLUMNS.items():
        rendered = order_by(key, descending=True, alias="incidents")
        assert rendered == f"ORDER BY incidents.{column} DESC, incidents.incident_id DESC"


def test_keyset_cursor_values_are_parameters() -> None:
    sql, params = keyset_clause(("'; DROP TABLE incidents; --", "x"), "confirmed", descending=True)
    assert sql.count("?") == 2
    assert "DROP" not in sql.upper()
    assert tuple(params) == ("'; DROP TABLE incidents; --", "x")


def test_keyset_rejects_an_unknown_sort_key() -> None:
    with pytest.raises(PersistenceError, match="unknown sort key"):
        keyset_clause(("a", "b"), "nonsense", descending=True)


def test_page_size_is_clamped_so_one_call_cannot_ask_for_the_table() -> None:
    assert clamp_page_size(None, default=50, maximum=200) == 50
    assert clamp_page_size(10_000, default=50, maximum=200) == 200
    assert clamp_page_size(0, default=50, maximum=200) == 1
    assert clamp_page_size(-5, default=50, maximum=200) == 1


# ---------------------------------------------------------------- LIKE means what it says


def test_like_wildcards_inside_a_value_are_escaped() -> None:
    clause = build_where(IncidentFilter(search="cam_1"))
    assert any("cam\\_1" in str(p) for p in clause.params)
    clause = build_where(IncidentFilter(search="50%"))
    assert any("50\\%" in str(p) for p in clause.params)


def test_a_zone_filter_does_not_match_a_longer_zone_name() -> None:
    clause = build_where(IncidentFilter(zone_id="dock"))
    assert clause.params == ['%"dock"%']  # quoted, so it cannot match "dockside"


def test_a_time_range_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(PersistenceError, match="ends before it starts"):
        IncidentFilter(
            since_wall_utc=datetime(2026, 2, 1, tzinfo=UTC),
            until_wall_utc=datetime(2026, 1, 1, tzinfo=UTC),
        )


# ---------------------------------------------------------------- against a real database


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_hostile_filter_runs_harmlessly_against_the_real_schema(
    database: Database, payload: str
) -> None:
    repository = IncidentRepository(database)
    filters = IncidentFilter(camera_ids=(payload,), search=payload, zone_id=payload)

    assert repository.count(filters) == 0
    page = repository.page(filters, limit=10)
    assert page.incidents == ()

    # The tables the payloads try to drop are still there.
    for table in ("incidents", "evidence", "candidate_events", "incident_transitions"):
        assert database.scalar(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608 - fixed names


def test_a_hostile_sort_key_never_reaches_the_database(database: Database) -> None:
    repository = IncidentRepository(database)
    with pytest.raises(PersistenceError, match="unknown sort key"):
        repository.page(IncidentFilter(), limit=10, sort="confirmed_utc; DROP TABLE incidents")
    assert database.scalar("SELECT COUNT(*) FROM incidents") == 0


def test_every_sort_key_executes_against_the_real_schema(database: Database) -> None:
    """Proves the allow-list names columns that exist: a typo here would be a 500, not a hole."""
    repository = IncidentRepository(database)
    for key in SORT_COLUMNS:
        assert repository.page(IncidentFilter(), limit=5, sort=key).incidents == ()
