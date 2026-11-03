"""Camera, zone and credential administration over real HTTP, and the apply contract."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
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
from vigil.ingest import credentials as creds

SYNTH = {"camera_id": "bench", "name": "Bench", "source": {"kind": "synthetic"}}
RUNNING = {"camera_id": "lobby", "source": {"kind": "synthetic"}}
SQUARE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
ZONES = {"zones": [{"zone_id": "yard", "kind": "restricted", "polygon": SQUARE, "criticality": 3}]}


@pytest.fixture
def started(make_config: MakeConfig, tmp_path: Path) -> Iterator[Rig]:
    """An engine that already has one inline camera, and can restart itself."""
    with build_rig(make_config, tmp_path, extra={"cameras": [RUNNING]}, can_restart=True) as made:
        yield made


def entries(rig: Rig, admin: object, action: str) -> list[dict[str, object]]:
    got = admin.get("/api/v1/audit", params={"limit": 200, "action": action}).json()  # type: ignore[attr-defined]
    return list(got)


# ------------------------------------------------------------------ cameras


def test_the_list_shows_the_running_camera_as_defined_outside_the_console(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        cams = a.get("/api/v1/config/cameras").json()
    assert [c["config"]["camera_id"] for c in cams] == ["lobby"]
    assert cams[0]["editable"] is False and cams[0]["pending"] is None
    assert cams[0]["credential_stored"] is None


def test_adding_a_camera_saves_it_at_once_and_reports_it_pending_until_a_restart(
    started: Rig,
) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        made = a.post("/api/v1/config/cameras", json=SYNTH)
        assert made.status_code == 201 and made.json()["pending"] == "added"
        assert (started.config_dir / "cameras" / "bench.yaml").is_file()
        pending = a.get("/api/v1/config/pending").json()
        assert pending["pending"] is True and pending["can_restart"] is True
        assert pending["changes"] == [{"kind": "camera", "target": "bench", "change": "added"}]
        listed = {c["config"]["camera_id"]: c for c in a.get("/api/v1/config/cameras").json()}
        assert listed["bench"]["editable"] is True and listed["bench"]["state"] is None


def test_a_duplicate_id_and_an_invalid_camera_are_refused_with_the_reason(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        assert a.post("/api/v1/config/cameras", json=RUNNING).status_code == 409
        bad = a.post("/api/v1/config/cameras", json={**SYNTH, "camera_id": "BAD ID"})
        assert bad.status_code == 422
        inline = a.post(
            "/api/v1/config/cameras",
            json={**SYNTH, "source": {"kind": "rtsp", "url": "rtsp://u:p@cam.local/live"}},
        )
        assert inline.status_code == 422 and "credentials" in inline.text
        assert not (started.config_dir / "cameras").exists() or not list(
            (started.config_dir / "cameras").glob("*.yaml")
        )


def test_editing_and_removing_a_file_camera_is_reported_and_audited_with_before_and_after(
    started: Rig,
) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        a.post("/api/v1/config/cameras", json=SYNTH)
        changed = a.put(
            "/api/v1/config/cameras/bench", json={**SYNTH, "priority": 9, "name": "Dock"}
        )
        assert changed.status_code == 200 and changed.json()["config"]["priority"] == 9
        assert (
            a.put("/api/v1/config/cameras/bench", json={**SYNTH, "camera_id": "other"}).status_code
            == 422
        )
        assert a.delete("/api/v1/config/cameras/bench").status_code == 204
        assert a.delete("/api/v1/config/cameras/bench").status_code == 404
        trail = entries(started, a, "camera.changed")
        assert '"before"' in str(trail[0]["detail"]) and '"after"' in str(trail[0]["detail"])
        assert entries(started, a, "camera.added") and entries(started, a, "camera.removed")


def test_an_inline_camera_cannot_be_edited_or_removed_through_the_console(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        assert a.put("/api/v1/config/cameras/lobby", json=RUNNING).status_code == 409
        assert a.delete("/api/v1/config/cameras/lobby").status_code == 404
        assert (
            a.put(
                "/api/v1/config/cameras/ghost", json={**RUNNING, "camera_id": "ghost"}
            ).status_code
            == 404
        )


def test_only_an_administrator_may_change_configuration_and_a_viewer_may_look(started: Rig) -> None:
    set_up(started)
    make_user(started, "oscar", "operator")
    make_user(started, "vera", "viewer")
    for name in ("oscar", "vera"):
        with signed_in(started, name, GOOD + "?") as u:
            assert u.get("/api/v1/config/cameras").status_code == 200
            assert u.get("/api/v1/config/pending").status_code == 200
            assert u.post("/api/v1/config/cameras", json=SYNTH).status_code == 403
            assert u.get("/api/v1/config/devices").status_code == 403
            assert u.post("/api/v1/config/apply").status_code == 403
            assert (
                u.put(
                    "/api/v1/config/credentials/gate", json={"username": "u", "password": "p"}
                ).status_code
                == 403
            )


# ------------------------------------------------------------------ test a source


def write_video(path: Path) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), 10.0, (64, 48))
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 20, np.uint8))
    writer.release()


def test_testing_a_synthetic_and_a_video_source_reports_what_was_granted(
    started: Rig, tmp_path: Path
) -> None:
    set_up(started)
    clip = tmp_path / "clip.mp4"
    write_video(clip)
    with signed_in(started, "alice") as a:
        ok = a.post("/api/v1/config/cameras/test", json={"kind": "synthetic"}).json()
        assert ok["ok"] is True and ok["width_px"] == 320 and ok["backend"] == "synthetic"
        video = a.post(
            "/api/v1/config/cameras/test", json={"kind": "video_file", "path": str(clip)}
        ).json()
        assert video["ok"] is True and (video["width_px"], video["height_px"]) == (64, 48)
        # Nothing was saved by testing.
        assert a.get("/api/v1/config/pending").json()["pending"] is False


def test_a_source_that_cannot_open_says_why_and_does_not_reveal_the_folder(
    started: Rig, tmp_path: Path
) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        gone = a.post(
            "/api/v1/config/cameras/test",
            json={"kind": "video_file", "path": str(tmp_path / "nope.mp4")},
        ).json()
    assert (
        gone["ok"] is False
        and "nope.mp4" in gone["message"]
        and str(tmp_path) not in gone["message"]
    )


def test_a_device_already_in_use_by_a_running_camera_is_not_opened_a_second_time(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    webcam = {
        "camera_id": "desk",
        "source": {"kind": "webcam", "device_index": 0},
        "enabled": False,
    }
    with build_rig(make_config, tmp_path, extra={"cameras": [{**webcam, "enabled": True}]}) as rig:
        set_up(rig)
        with signed_in(rig, "alice") as a:
            r = a.post(
                "/api/v1/config/cameras/test", json={"kind": "webcam", "device_index": 0}
            ).json()
    assert r["ok"] is False and "already in use" in r["message"] and "'desk'" in r["message"]


def test_the_device_list_is_a_structure_that_never_opens_a_camera(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        body = a.get("/api/v1/config/devices").json()
    assert set(body) == {"available", "reason", "devices"}
    for d in body["devices"]:
        assert set(d) == {"index", "name", "backend"} and d["name"]


# ------------------------------------------------------------------ zones


def test_saving_zones_writes_the_file_points_the_camera_at_it_and_reports_it_pending(
    started: Rig,
) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        a.post("/api/v1/config/cameras", json=SYNTH)
        before = a.get("/api/v1/config/cameras/bench/zones").json()
        assert before["zones"]["zones"] == [] and before["editable"] is True
        saved = a.put("/api/v1/config/cameras/bench/zones", json=ZONES)
        assert saved.status_code == 200 and saved.json()["zones_ref"] == "bench"
        assert (started.config_dir / "zones" / "bench.zones.yaml").is_file()
        cams = {c["config"]["camera_id"]: c for c in a.get("/api/v1/config/cameras").json()}
        assert cams["bench"]["config"]["zones_ref"] == "bench"
        again = a.get("/api/v1/config/cameras/bench/zones").json()
        assert [z["zone_id"] for z in again["zones"]["zones"]] == ["yard"]
        kinds = {c["kind"] for c in a.get("/api/v1/config/pending").json()["changes"]}
        assert "zones" in kinds


@pytest.mark.parametrize(
    "zone",
    [
        {"zone_id": "bow", "kind": "restricted", "polygon": [[0, 0], [1, 1], [1, 0], [0, 1]]},
        {"zone_id": "out", "kind": "restricted", "polygon": [[0, 0], [2, 0], [2, 2]]},
        {"zone_id": "line", "kind": "crossing_line", "polygon": [[0, 0], [1, 0], [1, 1]]},
    ],
)
def test_a_zone_the_engine_would_refuse_is_refused_at_the_boundary(
    started: Rig, zone: dict[str, object]
) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        a.post("/api/v1/config/cameras", json=SYNTH)
        assert (
            a.put("/api/v1/config/cameras/bench/zones", json={"zones": [zone]}).status_code == 422
        )
        assert not (started.config_dir / "zones" / "bench.zones.yaml").exists()


def test_zones_of_an_inline_or_missing_camera_cannot_be_written(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        assert a.put("/api/v1/config/cameras/lobby/zones", json=ZONES).status_code == 409
        assert a.put("/api/v1/config/cameras/ghost/zones", json=ZONES).status_code == 404
        assert a.get("/api/v1/config/cameras/ghost/zones").status_code == 404


# ------------------------------------------------------------------ credentials


class Vault:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Vault:
    v = Vault()
    monkeypatch.setattr(
        creds, "store_credentials", lambda ref, u, p: v.items.__setitem__(ref, (u, p))
    )
    monkeypatch.setattr(creds, "has_credentials", lambda ref, **_k: ref in v.items)
    monkeypatch.setattr(creds, "delete_credentials", lambda ref: v.items.pop(ref, None) is not None)
    return v


def test_a_credential_is_stored_by_name_and_the_secret_is_never_returned_or_audited(
    started: Rig, vault: Vault
) -> None:
    set_up(started)
    secret = "zzTOPSECRETzz"
    with signed_in(started, "alice") as a:
        assert a.get("/api/v1/config/credentials/gate").json() == {"ref": "gate", "stored": False}
        put = a.put(
            "/api/v1/config/credentials/gate", json={"username": "admin", "password": secret}
        )
        assert put.status_code == 200 and put.json() == {"ref": "gate", "stored": True}
        assert secret not in put.text
        assert a.get("/api/v1/config/credentials/gate").json()["stored"] is True
        audit = a.get("/api/v1/audit", params={"limit": 500}).text
        assert secret not in audit and "credential.stored" in audit
        assert a.delete("/api/v1/config/credentials/gate").status_code == 204
        assert a.get("/api/v1/config/credentials/gate").json()["stored"] is False
    assert vault.items == {}


def test_a_camera_reports_whether_its_named_credential_is_stored(
    started: Rig, vault: Vault
) -> None:
    set_up(started)
    cam = {
        "camera_id": "gate",
        "source": {"kind": "rtsp", "url": "rtsp://cam.local/live", "credential_ref": "gate"},
    }
    with signed_in(started, "alice") as a:
        made = a.post("/api/v1/config/cameras", json=cam).json()
        assert made["credential_stored"] is False
        a.put("/api/v1/config/credentials/gate", json={"username": "u", "password": "p"})
        listed = {c["config"]["camera_id"]: c for c in a.get("/api/v1/config/cameras").json()}
        assert listed["gate"]["credential_stored"] is True


def test_a_bad_credential_name_is_refused_and_a_missing_keyring_says_how_to_fix_it(
    started: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigil.core.errors import SourceError

    set_up(started)

    def no_keyring(*_a: object, **_k: object) -> None:
        raise SourceError(
            "keyring is not installed: install the credentials extra", retryable=False
        )

    monkeypatch.setattr(creds, "store_credentials", no_keyring)
    with signed_in(started, "alice") as a:
        assert a.put(
            "/api/v1/config/credentials/Bad Name", json={"username": "u", "password": "p"}
        ).status_code in (404, 422)
        r = a.put("/api/v1/config/credentials/gate", json={"username": "u", "password": "p"})
        assert r.status_code == 501 and "credentials extra" in r.text


# ------------------------------------------------------------------ applying


def test_apply_restarts_through_the_supervisor_and_says_what_it_applied(started: Rig) -> None:
    set_up(started)
    with signed_in(started, "alice") as a:
        a.post("/api/v1/config/cameras", json=SYNTH)
        r = a.post("/api/v1/config/apply")
        assert r.status_code == 202 and r.json()["changes"][0]["target"] == "bench"
        assert entries(started, a, "config.apply_requested")
    assert started.restarts == ["restart"]


def test_an_engine_with_no_supervisor_refuses_to_restart_rather_than_going_down(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    with build_rig(make_config, tmp_path, can_restart=False) as rig:
        set_up(rig)
        with signed_in(rig, "alice") as a:
            assert a.get("/api/v1/config/pending").json()["can_restart"] is False
            r = a.post("/api/v1/config/apply")
    assert r.status_code == 409 and "not running under the desktop app" in r.text
    assert rig.restarts == []


def test_a_file_camera_deleted_since_start_is_reported_as_removed(
    make_config: MakeConfig, tmp_path: Path
) -> None:
    """The engine started with this camera and its file; deleting the file is a pending removal."""
    from vigil.config import writer

    def seed(config_dir: Path) -> None:
        writer.write_camera(config_dir, writer.validate_camera(SYNTH))

    with build_rig(make_config, tmp_path, extra={"cameras": [SYNTH]}, seed=seed) as rig:
        set_up(rig)
        with signed_in(rig, "alice") as a:
            assert a.get("/api/v1/config/pending").json()["pending"] is False
            assert a.delete("/api/v1/config/cameras/bench").status_code == 204
            changes = a.get("/api/v1/config/pending").json()["changes"]
            listed = [c["config"]["camera_id"] for c in a.get("/api/v1/config/cameras").json()]
    assert changes == [{"kind": "camera", "target": "bench", "change": "removed"}]
    assert (
        "bench" not in listed
    )  # a removed camera is shown by /pending, not as if it were still there
