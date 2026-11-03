"""Per-camera thresholds: shown with real bounds, validated by the module, saved per camera."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.api.accounts_rig import (
    GOOD,
    MakeConfig,
    Rig,
    build_rig,
    make_user,
    set_up,
    signed_in,
)
from vigil.config import writer

CAMERA = {"camera_id": "bench", "name": "Bench", "source": {"kind": "synthetic"}}
INLINE = {"camera_id": "lobby", "source": {"kind": "synthetic"}}
URL = "/api/v1/config/cameras/bench/tuning"


@pytest.fixture
def rig(make_config: MakeConfig, tmp_path: Path) -> Iterator[Rig]:
    def seed(config_dir: Path) -> None:
        writer.write_camera(config_dir, writer.validate_camera(CAMERA))

    with build_rig(
        make_config,
        tmp_path,
        extra={"cameras": [CAMERA, INLINE], "modules.enabled": ["intrusion"]},
        seed=seed,
    ) as made:
        set_up(made)
        yield made


def by_name(modules: list[dict], module_id: str, name: str) -> dict:  # type: ignore[type-arg]
    module = next(m for m in modules if m["module_id"] == module_id)
    return next(p for p in module["params"] if p["name"] == name)


def test_every_module_is_listed_with_the_bounds_the_module_enforces(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        modules = a.get(URL).json()
    assert {"intrusion", "person_down", "abandoned_object", "line_crossing"} <= {
        m["module_id"] for m in modules
    }
    signal = by_name(modules, "intrusion", "base_signal")
    assert signal["kind"] == "number" and signal["default"] == 0.7
    assert signal["minimum"] == 0.5 and signal["exclusive_minimum"] is True
    assert signal["maximum"] == 1.0 and signal["exclusive_maximum"] is True
    hits = by_name(modules, "intrusion", "min_track_hits")
    assert hits["kind"] == "integer" and hits["minimum"] == 1
    assert by_name(modules, "intrusion", "object_classes")["kind"] == "list"


def test_a_viewer_may_read_but_not_change(rig: Rig) -> None:
    make_user(rig, "vera", "viewer")
    with signed_in(rig, "vera", GOOD + "?") as v:
        assert v.get(URL).status_code == 200
        assert v.put(f"{URL}/intrusion", json={"overrides": {}}).status_code == 403


def test_an_override_is_saved_on_that_camera_only_and_reported_pending(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        out = a.put(f"{URL}/intrusion", json={"overrides": {"base_signal": 0.72}})
        assert out.status_code == 200
        param = by_name(out.json(), "intrusion", "base_signal")
        assert param["override"] == 0.72 and param["has_override"] is True
        assert param["effective"] == 0.72 and param["default"] == 0.7
        assert next(m for m in out.json() if m["module_id"] == "intrusion")["pending"] is True
        saved = writer.read_camera(rig.config_dir, "bench")
        assert saved is not None and dict(saved.module_params["intrusion"]) == {"base_signal": 0.72}
        audited = a.get("/api/v1/audit", params={"action": "camera.tuned"}).json()
    assert json.loads(audited[0]["detail"])["after"] == {"base_signal": 0.72}


def test_clearing_overrides_returns_the_camera_to_the_shared_value(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        a.put(f"{URL}/intrusion", json={"overrides": {"base_signal": 0.72}})
        out = a.put(f"{URL}/intrusion", json={"overrides": {}}).json()
    param = by_name(out, "intrusion", "base_signal")
    assert param["has_override"] is False and param["effective"] == 0.7
    saved = writer.read_camera(rig.config_dir, "bench")
    assert saved is not None and "intrusion" not in saved.module_params


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_signal": 1.5},  # above the bound
        {"base_signal": 0.4},  # below the exclusive minimum
        {"no_such_parameter": 1},  # a typo is refused, not ignored
        {"min_track_hits": "many"},
    ],
)
def test_a_value_the_module_would_refuse_is_refused_and_nothing_is_written(
    rig: Rig, overrides: dict[str, object]
) -> None:
    before = (rig.config_dir / "cameras" / "bench.yaml").read_text(encoding="utf-8")
    with signed_in(rig, "alice") as a:
        out = a.put(f"{URL}/intrusion", json={"overrides": overrides})
    assert out.status_code == 422 and out.json()["detail"]["issues"]
    assert (rig.config_dir / "cameras" / "bench.yaml").read_text(encoding="utf-8") == before


def test_other_modules_checks_apply_to_the_merged_values(rig: Rig) -> None:
    """base_signal plus the bonuses must stay below 1.0: valid alone, invalid together."""
    with signed_in(rig, "alice") as a:
        out = a.put(f"{URL}/intrusion", json={"overrides": {"base_signal": 0.99}})
    assert out.status_code == 422


def test_the_edge_cases_are_refused_in_plain_words(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        assert (
            a.put(
                "/api/v1/config/cameras/lobby/tuning/intrusion", json={"overrides": {}}
            ).status_code
            == 409
        )
        assert (
            a.put(
                "/api/v1/config/cameras/ghost/tuning/intrusion", json={"overrides": {}}
            ).status_code
            == 404
        )
        assert a.put(f"{URL}/not_a_module", json={"overrides": {}}).status_code == 404
        assert a.put(f"{URL}/intrusion", json={"overrides": {}, "extra": 1}).status_code == 422
        too_many = {f"p{i}": 1 for i in range(100)}
        assert a.put(f"{URL}/intrusion", json={"overrides": too_many}).status_code == 422
        assert a.get("/api/v1/config/cameras/ghost/tuning").status_code == 404


def test_an_inline_camera_can_be_read(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        assert a.get("/api/v1/config/cameras/lobby/tuning").status_code == 200
