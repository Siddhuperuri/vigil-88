"""`vigil openapi` feeds the console's generated types, so it must be stable and complete."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vigil.cli.main import app

runner = CliRunner()


def _document(tmp_path: Path, name: str) -> str:
    out = tmp_path / name
    result = runner.invoke(app, ["openapi", "--out", str(out)])
    assert result.exit_code == 0, result.output
    return out.read_text(encoding="utf-8")


def test_the_document_is_valid_json_with_the_account_and_incident_routes(tmp_path: Path) -> None:
    schema = json.loads(_document(tmp_path, "a.json"))
    paths = schema["paths"]
    for route in (
        "/api/v1/auth/login",
        "/api/v1/auth/status",
        "/api/v1/incidents",
        "/api/v1/users",
    ):
        assert route in paths, f"{route} missing: the console would be generated against a lie"


def test_two_runs_produce_the_same_bytes_so_the_staleness_gate_is_not_noisy(tmp_path: Path) -> None:
    assert _document(tmp_path, "a.json") == _document(tmp_path, "b.json")


def test_it_writes_to_standard_output_when_no_file_is_named() -> None:
    result = runner.invoke(app, ["openapi"])
    assert result.exit_code == 0 and json.loads(result.stdout)["info"]["title"] == "VIGIL-88"
