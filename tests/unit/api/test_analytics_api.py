"""The analytics and export routes over a real server and a real database.

What is checked here is the boundary, not the arithmetic (that is in `tests/unit/analytics/`):
does the response echo the window and filter it actually used, does a bad parameter get a 422
rather than a wrong number, and can a caller influence where a file is written.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.unit.response.test_lifecycle import CONTEXT, _assessment, _event, _ulid
from vigil.analytics import pdf
from vigil.api.app import create_app
from vigil.api.server import ApiServer
from vigil.config.loader import LoadedConfig
from vigil.core.clock import ManualClock
from vigil.domain.enums import EventType
from vigil.domain.observation import SubjectKey
from vigil.observability.logging import get_logger
from vigil.persistence.queries import ALL_INCIDENTS
from vigil.pipeline.app import Application
from vigil.pipeline.response_stack import ResponseStack
from vigil.pipeline.streaming import StreamHub

MakeConfig = Callable[..., LoadedConfig]
WALL = datetime(2026, 3, 1, 2, 41, tzinfo=UTC)


@pytest.fixture
def api(make_config: MakeConfig) -> Iterator[tuple[httpx.Client, ResponseStack, Path]]:
    loaded = make_config({"cameras": []})
    application = Application(loaded.settings, loaded.paths, clock=ManualClock(wall_start=WALL))
    application.start()
    response = application.response
    assert response is not None
    hub = StreamHub()
    server = ApiServer(
        create_app(application, hub), host="127.0.0.1", port=0, logger=get_logger("test.api")
    )
    server.start()
    try:
        with httpx.Client(base_url=f"{server.url}/api/v1", timeout=20) as client:
            yield (client, response, loaded.paths.exports_dir)
    finally:
        server.stop()
        hub.close()
        application.stop()


def _seed(response: ResponseStack, *, count: int = 3) -> None:
    """Incidents strictly before WALL.

    The default window ends at the clock's `wall_utc()`, which is WALL, and a window is
    closed-open so that adjacent windows neither gap nor double-count. An incident confirmed at
    exactly WALL is therefore outside it -- correct, and easy to seed into by accident.
    """
    for index in range(count):
        response.manager.record(
            _event(
                candidate_id=_ulid(index + 1),
                at=WALL - timedelta(hours=index + 1),
                subject=SubjectKey("track", str(index + 1)),
                event_type=EventType.INTRUSION if index % 2 == 0 else EventType.PERSON_DOWN,
            ),
            _assessment(),
            CONTEXT,
        )


# ---------------------------------------------------------------- summary


def test_an_empty_store_summarises_to_zero_with_caveats(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    body = client.get("/analytics/summary").json()
    assert body["incidents_total"] == 0
    assert body["by_event_type"] == []
    assert body["earliest_record_utc"] is None
    assert any("fact about the window" in c for c in body["caveats"])


def test_the_summary_echoes_the_window_and_bucket_it_used(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    body = client.get("/analytics/summary", params={"days": 3, "bucket": "hour"}).json()
    assert body["bucket"] == "hour"
    assert body["since_utc"] < body["until_utc"]
    assert body["utc_offset_minutes"] == 0
    # The window is three days of hours, and every bucket is present.
    assert len(body["trend"]) >= 72


def test_the_summary_reconciles_with_the_breakdowns(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=4)
    body = client.get("/analytics/summary", params={"days": 7}).json()
    assert body["incidents_total"] == 4
    for key in ("by_event_type", "by_severity", "by_status", "by_camera"):
        assert sum(c["count"] for c in body[key]) == 4
    assert sum(c["count"] for c in body["trend"]) == 4


def test_a_filter_narrows_the_summary(api: tuple[httpx.Client, ResponseStack, Path]) -> None:
    client, response, _exports = api
    _seed(response, count=4)
    body = client.get("/analytics/summary", params={"days": 7, "event_type": "person_down"}).json()
    assert body["incidents_total"] == 2
    assert [c["key"] for c in body["by_event_type"]] == ["person_down"]


def test_unavailable_durations_are_null_not_zero(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    """A dashboard cannot tell a zero from an absence, so the wire has to."""
    client, response, _exports = api
    _seed(response, count=2)
    body = client.get("/analytics/summary", params={"days": 7}).json()
    acknowledgement = body["acknowledgement"]
    assert acknowledgement["available"] is False
    assert acknowledgement["count"] == 0
    assert acknowledgement["mean_ms"] is None
    assert acknowledgement["p95_ms"] is None
    # Detection latency, by contrast, is always computable from two stored timestamps.
    assert body["detection_latency"]["available"] is True
    assert body["detection_latency"]["mean_ms"] > 0


def test_every_metric_arrives_with_its_definition(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    body = client.get("/analytics/summary").json()
    definitions = body["metric_definitions"]
    assert definitions["dismissal_rate"]["population"]
    assert "not the detector's false-positive rate" in definitions["dismissal_rate"]["definition"]
    assert definitions["camera_availability"]["unit"] == "seconds"


def test_a_local_offset_is_accepted_and_echoed(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    body = client.get("/analytics/summary", params={"utc_offset_minutes": -240}).json()
    assert body["utc_offset_minutes"] == -240


@pytest.mark.parametrize(
    "params",
    [
        {"event_type": "nonsense"},
        {"status": "sideways"},
        {"min_severity": "URGENT"},
        {"bucket": "fortnight"},
        {"days": 0},
        {"utc_offset_minutes": 10_000},
    ],
)
def test_a_bad_parameter_is_a_422_not_a_wrong_number(
    api: tuple[httpx.Client, ResponseStack, Path], params: dict[str, object]
) -> None:
    client, _response, _exports = api
    assert client.get("/analytics/summary", params=params).status_code == 422


def test_an_inverted_window_is_refused(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    reply = client.get(
        "/analytics/summary",
        params={"since": "2026-03-02T00:00:00Z", "until": "2026-03-01T00:00:00Z"},
    )
    assert reply.status_code == 422
    assert "end after it starts" in reply.json()["detail"]


# ---------------------------------------------------------------- report


def test_the_report_route_includes_incidents_with_their_timeline(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=2)
    body = client.get("/analytics/report", params={"days": 7}).json()
    assert body["schema"] == "vigil-88.report.v1"
    assert len(body["incidents"]) == 2
    timeline = body["incidents"][0]["timeline"]
    assert [t["to_status"] for t in timeline] == ["candidate", "confirmed"]
    assert body["incidents"][0]["explanation"]["trigger_summary"]


def test_the_report_row_limit_is_respected_and_declared(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=4)
    body = client.get("/analytics/report", params={"days": 7, "rows": 2}).json()
    assert len(body["incidents"]) == 2
    assert body["coverage"]["truncated"] is True
    assert body["summary"]["incidents_total"] == 4


# ---------------------------------------------------------------- export


def test_a_csv_export_downloads_with_a_filename(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=2)
    reply = client.get("/analytics/export", params={"format": "csv", "days": 7})
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/csv")
    assert "attachment" in reply.headers["content-disposition"]
    rows = [r for r in reply.text.splitlines() if not r.startswith("#")]
    assert len(list(csv.DictReader(io.StringIO("\n".join(rows))))) == 2


def test_a_json_export_downloads_and_parses(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=1)
    reply = client.get("/analytics/export", params={"format": "json", "days": 7})
    assert reply.status_code == 200
    assert json.loads(reply.text)["summary"]["incidents_total"] == 1


def test_an_export_is_not_saved_unless_asked(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    """Downloading is the default; writing to the filesystem is opt-in."""
    client, response, exports = api
    _seed(response, count=1)
    reply = client.get("/analytics/export", params={"days": 7})
    assert "x-export-saved-as" not in {k.lower() for k in reply.headers}
    assert not list(exports.glob("*.csv"))


def test_a_saved_export_lands_inside_the_exports_directory(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, exports = api
    _seed(response, count=1)
    reply = client.get(
        "/analytics/export", params={"days": 7, "save": True, "name": "march-review"}
    )
    assert reply.status_code == 200
    saved = reply.headers["x-export-saved-as"]
    assert saved == "march-review.csv"
    written = exports / saved
    assert written.is_file()
    assert written.resolve().is_relative_to(exports.resolve())


@pytest.mark.parametrize(
    "name", ["../escape", "../../etc/passwd", "sub/dir", "C:/evil", "with space", "dotted.name"]
)
def test_a_hostile_export_name_is_replaced_not_obeyed(
    api: tuple[httpx.Client, ResponseStack, Path], name: str
) -> None:
    """Validated rather than sanitised: an unusable name is replaced outright, so there is no
    partially-cleaned string to reason about."""
    client, response, exports = api
    _seed(response, count=1)
    reply = client.get("/analytics/export", params={"days": 7, "save": True, "name": name})
    assert reply.status_code == 200
    saved = reply.headers["x-export-saved-as"]
    assert saved.endswith(".csv")
    assert "/" not in saved
    assert "\\" not in saved
    assert ".." not in saved
    files = list(exports.rglob("*"))
    assert all(f.resolve().is_relative_to(exports.resolve()) for f in files)


def test_an_unknown_export_format_is_refused(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    assert client.get("/analytics/export", params={"format": "docx"}).status_code == 422


def test_a_pdf_export_downloads_as_a_pdf(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, _exports = api
    _seed(response, count=1)
    reply = client.get("/analytics/export", params={"format": "pdf", "days": 7})
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("application/pdf")
    assert reply.content.startswith(b"%PDF-")
    assert reply.content.rstrip().endswith(b"%%EOF")


def test_an_unavailable_format_is_a_503_that_says_how_to_install_it(
    api: tuple[httpx.Client, ResponseStack, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503, not 422: the request was valid and the format is real. What is missing is a
    capability of this deployment, and the answer names the command that adds it."""
    client, _response, _exports = api
    monkeypatch.setattr(pdf, "is_available", lambda: False)
    reply = client.get("/analytics/export", params={"format": "pdf"})
    assert reply.status_code == 503
    assert pdf.INSTALL_HINT in reply.json()["detail"]


def test_the_formats_route_reports_what_this_install_can_produce(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    body = {row["format"]: row for row in client.get("/analytics/formats").json()}
    assert set(body) == {"json", "csv", "pdf"}
    assert body["json"]["available"] is True
    assert body["json"]["reason"] is None
    # The dev group installs the renderer, so PDF is available here; the reason field is what
    # the viewer shows when it is not.
    assert body["pdf"]["available"] is True


def test_the_formats_route_explains_an_unavailable_one(
    api: tuple[httpx.Client, ResponseStack, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _response, _exports = api
    monkeypatch.setattr(pdf, "is_available", lambda: False)
    body = {row["format"]: row for row in client.get("/analytics/formats").json()}
    assert body["pdf"]["available"] is False
    assert pdf.INSTALL_HINT in body["pdf"]["reason"]
    assert body["csv"]["available"] is True


def test_a_saved_pdf_export_lands_inside_the_exports_directory(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, response, exports = api
    _seed(response, count=1)
    reply = client.get(
        "/analytics/export",
        params={"days": 7, "format": "pdf", "save": True, "name": "march-print"},
    )
    assert reply.status_code == 200
    saved = reply.headers["x-export-saved-as"]
    assert saved == "march-print.pdf"
    written = exports / saved
    assert written.is_file()
    assert written.read_bytes().startswith(b"%PDF-")
    assert written.resolve().is_relative_to(exports.resolve())


def test_an_export_of_nothing_is_an_empty_table_not_an_error(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    client, _response, _exports = api
    reply = client.get("/analytics/export", params={"format": "csv"})
    assert reply.status_code == 200
    assert "incidents_total=0" in reply.text
    assert "display_ref" in reply.text


def test_an_exported_note_cannot_become_a_spreadsheet_formula(
    api: tuple[httpx.Client, ResponseStack, Path],
) -> None:
    """End to end: a note written through the API, exported, and inert in a spreadsheet."""
    client, response, _exports = api
    _seed(response, count=1)
    page = response.incidents.page(ALL_INCIDENTS, limit=1)
    incident_id = page.incidents[0].incident_id
    client.post(
        f"/incidents/{incident_id}/annotate",
        json={"note": "=cmd|' /C calc'!A0"},
    )
    reply = client.get("/analytics/export", params={"format": "csv", "days": 7})
    rows = [r for r in reply.text.splitlines() if not r.startswith("#")]
    parsed = list(csv.DictReader(io.StringIO("\n".join(rows))))
    note = parsed[0]["operator_note"]
    assert note.startswith("'=cmd")
    assert not note.startswith("=")
