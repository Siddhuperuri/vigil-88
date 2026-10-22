"""The analytics query builders, against hostile input and against their own shape.

`pyproject.toml` exempts `src/vigil/analytics/**` from ruff's S608 (SQL in an f-string) on the
stated grounds that "the interpolated text is always either a column tuple declared in the same
module or a run of `?` placeholders". `tests/unit/persistence/test_query_safety.py` pays for that
exemption on the persistence layer. **Nothing paid for it on this layer**, which P6's security
audit found: the exemption was inherited rather than earned.

Two kinds of check, because the two risks are different:

  * **behavioural** — hostile filter values go through the real builders against the real schema
    and must arrive as bound parameters, leaving the database intact;
  * **structural** — an AST walk asserting that every interpolated column and expression at
    every call site is a literal in this module's own source. That is the check that survives
    someone threading an API query parameter through `_grouped()` next year, which is the only
    way this exemption could actually become unsafe.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vigil.analytics import queries as analytics_queries
from vigil.analytics.queries import AnalyticsQueries, Bucket, Window
from vigil.config.schema.storage import StorageConfig
from vigil.core.clock import ManualClock
from vigil.domain.enums import EventType
from vigil.observability.logging import get_logger
from vigil.observability.metrics import MetricsRegistry
from vigil.persistence.database import Database
from vigil.persistence.queries import IncidentFilter

# The payloads a reviewer would reach for. Each must survive as data.
HOSTILE = [
    "'; DROP TABLE incidents; --",
    '" OR 1=1 --',
    "cam-a' UNION SELECT name FROM sqlite_master --",
    "1); DELETE FROM evidence WHERE 1=1; --",
    "%' OR evidence_status LIKE '%",
    "\\'; PRAGMA writable_schema=1; --",
]

# Every interpolated fragment these two helpers may receive. Derived from the module, not typed
# out here, so a new grouping dimension cannot be added without this list noticing.
INTERPOLATING_HELPERS = {"_grouped": 0, "_durations": 0}


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


def window() -> Window:
    return Window(
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 2, 1, tzinfo=UTC),
    )


@pytest.fixture
def queries(database: Database) -> AnalyticsQueries:
    return AnalyticsQueries(database)


# ------------------------------------------------------------------ behavioural


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_hostile_camera_id_is_bound_not_interpolated(
    queries: AnalyticsQueries, payload: str
) -> None:
    result = queries.total(window(), IncidentFilter(camera_ids=(payload,)))
    assert result == 0  # it matched nothing, because it is a value and not SQL


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_hostile_search_term_leaves_the_schema_intact(
    queries: AnalyticsQueries, database: Database, payload: str
) -> None:
    queries.total(window(), IncidentFilter(search=payload))
    queries.by_camera(window(), IncidentFilter(search=payload))
    queries.trend(window(), Bucket.DAY, IncidentFilter(search=payload))
    tables = {
        str(row["name"])
        for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "incidents" in tables
    assert "evidence" in tables


@pytest.mark.parametrize("payload", HOSTILE)
def test_a_hostile_zone_filter_is_bound(queries: AnalyticsQueries, payload: str) -> None:
    assert queries.total(window(), IncidentFilter(zone_id=payload)) == 0


def test_every_grouping_dimension_executes_against_the_real_schema(
    queries: AnalyticsQueries,
) -> None:
    """If a column name were ever wrong or injectable, the statement would not run at all."""
    w = window()
    filters = IncidentFilter(event_types=(EventType.INTRUSION,))
    assert queries.by_event_type(w, filters) == ()
    assert queries.by_camera(w, filters) == ()
    assert queries.by_status(w, filters) == ()
    assert queries.by_severity(w, filters) == ()


@pytest.mark.parametrize("bucket", list(Bucket))
def test_every_bucket_executes_against_the_real_schema(
    queries: AnalyticsQueries, bucket: Bucket
) -> None:
    """The one numeric interpolation in this module is `_PREFIX[bucket]`, keyed by an enum. WEEK
    and any local offset take the Python path instead, so both branches are covered here."""
    assert queries.trend(window(), bucket, IncidentFilter()) is not None


@pytest.mark.parametrize("bucket", list(Bucket))
def test_a_local_offset_still_executes(queries: AnalyticsQueries, bucket: Bucket) -> None:
    offset = Window(
        since=datetime(2026, 1, 1, tzinfo=UTC),
        until=datetime(2026, 2, 1, tzinfo=UTC),
        utc_offset_minutes=330,
    )
    assert queries.trend(offset, bucket, IncidentFilter()) is not None


def test_the_bucket_prefix_table_covers_every_bucket_it_claims_to() -> None:
    """A bucket with no prefix must take the Python path, not raise a KeyError at query time on
    a code path an operator triggers from the viewer."""
    prefixes = analytics_queries._PREFIX
    assert all(isinstance(v, int) for v in prefixes.values())
    for bucket in Bucket:
        if bucket not in prefixes:
            assert bucket is Bucket.WEEK, (
                f"{bucket} has no SQL prefix and is not the documented Python-path exception"
            )


# ------------------------------------------------------------------ structural


def _module_tree() -> ast.Module:
    return ast.parse(Path(inspect.getfile(analytics_queries)).read_text(encoding="utf-8"))


def _calls_to(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


@pytest.mark.parametrize(("helper", "position"), sorted(INTERPOLATING_HELPERS.items()))
def test_every_interpolated_fragment_is_a_literal_in_this_module(
    helper: str, position: int
) -> None:
    """The check that keeps the S608 exemption honest as the module changes.

    An f-string containing SQL is safe only while everything interpolated into it is a constant
    this module wrote. A caller-supplied column name would be an injection point that no amount
    of parameter binding elsewhere would close, and ruff has been told not to look.
    """
    calls = _calls_to(_module_tree(), helper)
    assert calls, f"{helper} is no longer called; update this test or delete the exemption"
    for call in calls:
        fragment = call.args[position] if len(call.args) > position else None
        assert isinstance(fragment, ast.Constant), (
            f"{helper} is called at line {call.lineno} with a non-literal fragment: only a "
            f"literal written in this module may be interpolated into SQL"
        )
        assert isinstance(fragment.value, str)


def test_the_extra_clause_of_a_duration_query_is_also_a_literal() -> None:
    """`_durations(extra=...)` is spliced into the WHERE clause, so it has the same rule."""
    for call in _calls_to(_module_tree(), "_durations"):
        for keyword in call.keywords:
            if keyword.arg == "extra":
                assert isinstance(keyword.value, ast.Constant)
