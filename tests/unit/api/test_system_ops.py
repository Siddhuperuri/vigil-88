"""Logs and storage: bounded, read-only, and never reveal where the data directory is."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.api.accounts_rig import GOOD, Rig, make_user, set_up, signed_in
from vigil.api.system_info import MAX_WALK_ENTRIES, TAIL_BYTES, tail_lines

# ------------------------------------------------------------------ the tail reader


def write_log(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_a_missing_log_is_reported_as_missing_not_as_empty_success(tmp_path: Path) -> None:
    out = tail_lines(tmp_path / "none.jsonl", lines=10, level=None)
    assert out.file_bytes is None and out.lines == []


def test_the_newest_lines_come_back_oldest_first(tmp_path: Path) -> None:
    log = tmp_path / "v.jsonl"
    write_log(log, [{"event": f"e{i}", "level": "info"} for i in range(10)])
    out = tail_lines(log, lines=3, level=None)
    assert [r["event"] for r in out.lines] == ["e7", "e8", "e9"]


def test_a_level_filter_keeps_that_level_and_above(tmp_path: Path) -> None:
    log = tmp_path / "v.jsonl"
    write_log(
        log,
        [
            {"event": "a", "level": "debug"},
            {"event": "b", "level": "warning"},
            {"event": "c", "level": "error"},
        ],
    )
    assert [r["event"] for r in tail_lines(log, lines=10, level="warning").lines] == ["b", "c"]


def test_a_huge_log_costs_the_same_and_the_cut_first_line_is_dropped(tmp_path: Path) -> None:
    log = tmp_path / "v.jsonl"
    filler = {"event": "x" * 200, "level": "info"}
    write_log(log, [filler] * (TAIL_BYTES // 150) + [{"event": "last", "level": "info"}])
    out = tail_lines(log, lines=500, level=None)
    assert out.truncated_to_tail and out.lines[-1]["event"] == "last"
    assert all("unparsed" not in r for r in out.lines)  # no half-line leaked through


def test_a_line_that_is_not_json_is_kept_and_marked(tmp_path: Path) -> None:
    log = tmp_path / "v.jsonl"
    log.write_text('{"event":"ok","level":"info"}\nTraceback (most recent call last)\n', "utf-8")
    out = tail_lines(log, lines=10, level=None)
    assert out.lines[1]["unparsed"] is True


def test_the_walk_ceiling_is_a_sane_number() -> None:
    assert MAX_WALK_ENTRIES >= 10_000


# ------------------------------------------------------------------ over HTTP


def test_logs_are_for_administrators_and_storage_for_anyone_who_may_view(rig: Rig) -> None:
    set_up(rig)
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        assert o.get("/api/v1/system/logs").status_code == 403
        assert o.get("/api/v1/system/storage").status_code == 200
    with signed_in(rig, "alice") as a:
        body = a.get("/api/v1/system/logs", params={"lines": 5}).json()
        assert body["returned"] <= 5


def test_storage_reports_sizes_and_never_a_path(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        body = a.get("/api/v1/system/storage").json()
    assert body["disk_free_bytes"] > 0 and body["database_bytes"] > 0
    assert body["evidence_truncated"] is False
    assert not any(isinstance(v, str) and (":\\" in v or v.startswith("/")) for v in body.values())


def test_bad_log_parameters_are_refused(rig: Rig) -> None:
    set_up(rig)
    with signed_in(rig, "alice") as a:
        assert a.get("/api/v1/system/logs", params={"lines": 100000}).status_code == 422
        assert a.get("/api/v1/system/logs", params={"level": "loud"}).status_code == 422
