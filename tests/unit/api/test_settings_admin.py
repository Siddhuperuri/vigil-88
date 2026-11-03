"""Alert policy, export and import: live where safe, validated in full, reversible, secret-free."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from vigil.config.settings_store import BACKUPS_DIR

CAMERA = {
    "camera_id": "bench",
    "name": "Bench",
    "source": {"kind": "rtsp", "url": "rtsp://192.0.2.10/stream", "credential_ref": "bench-cam"},
    "zones_ref": "bench",
}
SQUARE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
ZONES = {"zones": [{"zone_id": "yard", "kind": "restricted", "polygon": SQUARE}]}
WEBHOOK = "https://hooks.example.test/in?key=SENTINEL-NOT-FOR-EXPORT"
RULE = {"name": "gate", "camera": "bench", "min_severity": "LOW"}
NIGHT = {"name": "night", "schedule": {"start": "22:00", "end": "06:00"}}
IMPORT = "/api/v1/settings/import"


@pytest.fixture
def rig(make_config: MakeConfig, tmp_path: Path) -> Iterator[Rig]:
    def seed(config_dir: Path) -> None:
        writer.write_camera(config_dir, writer.validate_camera(CAMERA))
        writer.write_zone_set(config_dir, "bench", writer.validate_zone_set(ZONES))
        writer.write_module_params(config_dir, "intrusion", {"min_track_hits": 4})
        (config_dir / "vigil.local.yaml").write_text("logging:\n  level: INFO\n", encoding="utf-8")

    extra = {
        "cameras": [],
        "alerts.channels": ["console", "webhook"],
        "alerts.webhook_url": WEBHOOK,
        "modules.enabled": ["intrusion"],
    }
    with build_rig(make_config, tmp_path, extra=extra, seed=seed) as made:
        set_up(made)
        yield made


def post(client: Any, doc: object, **params: object) -> Any:
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return client.post(
        IMPORT, content=body, params=params, headers={"content-type": "application/json"}
    )


def exported(client: Any) -> dict[str, Any]:
    return client.get("/api/v1/settings/export").json()  # type: ignore[no-any-return]


def fresh_document(**change: Any) -> dict[str, Any]:
    base = {
        "format": "vigil88-settings",
        "schema_version": 1,
        "cameras": [],
        "zones": {},
        "module_params": {},
    }
    return {**base, **change}


# ------------------------------------------------------------------ the alert policy


def test_the_policy_is_readable_by_anyone_who_may_view_and_lists_channels_read_only(
    rig: Rig,
) -> None:
    make_user(rig, "vera", "viewer")
    with signed_in(rig, "vera", GOOD + "?") as v:
        out = v.get("/api/v1/settings/alerts").json()
        assert out["channels"] == ["console", "webhook"] and out["policy"]["rules"] == []
        assert v.put("/api/v1/settings/alerts", json=out["policy"]).status_code == 403


def test_saving_the_policy_takes_effect_at_once_and_is_kept_beside_other_settings(rig: Rig) -> None:
    body = {"enabled": True, "min_severity": "HIGH", "rules": [RULE], "quiet_hours": [NIGHT]}
    with signed_in(rig, "alice") as a:
        out = a.put("/api/v1/settings/alerts", json=body)
        assert out.status_code == 200 and out.json()["applied_live"] is True
    in_force = rig.app.settings.alerts
    assert in_force.min_severity == "HIGH" and in_force.rules[0].name == "gate"
    local = (rig.config_dir / "vigil.local.yaml").read_text(encoding="utf-8")
    assert "level: INFO" in local and "min_severity: HIGH" in local and "night" in local
    assert "SENTINEL" not in local  # the webhook is not part of the policy and is never copied


def test_the_second_save_keeps_the_first_as_a_backup(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        a.put("/api/v1/settings/alerts", json={"min_severity": "HIGH"})
        a.put("/api/v1/settings/alerts", json={"min_severity": "LOW"})
    saved = list((rig.config_dir / BACKUPS_DIR).rglob("*vigil.local.yaml"))
    assert saved and any("min_severity: HIGH" in p.read_text(encoding="utf-8") for p in saved)


def test_a_policy_that_does_not_fit_this_installation_is_refused_and_nothing_changes(
    rig: Rig,
) -> None:
    before = (rig.config_dir / "vigil.local.yaml").read_text(encoding="utf-8")
    bad_channel = {"rules": [{"name": "x", "channels": ["jsonl"]}]}  # jsonl is not enabled here
    bad_type = {"rules": [{"name": "x", "event_types": ["nonsense"]}]}
    with signed_in(rig, "alice") as a:
        for body in (bad_channel, bad_type, {"unknown_key": 1}):
            assert a.put("/api/v1/settings/alerts", json=body).status_code == 422
    assert (rig.config_dir / "vigil.local.yaml").read_text(encoding="utf-8") == before
    assert rig.app.settings.alerts.rules == ()


def test_an_operator_may_not_change_the_policy(rig: Rig) -> None:
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        assert o.put("/api/v1/settings/alerts", json={}).status_code == 403
        assert o.get("/api/v1/settings/export").status_code == 403


# ------------------------------------------------------------------ export


def test_an_export_carries_the_settings_and_none_of_the_secrets(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        text = a.get("/api/v1/settings/export").text
        doc = json.loads(text)
        header = a.get("/api/v1/settings/export").headers
    assert doc["format"] == "vigil88-settings" and doc["schema_version"] == 1
    assert [c["camera_id"] for c in doc["cameras"]] == ["bench"]
    assert doc["zones"]["bench"]["zones"][0]["zone_id"] == "yard"
    assert doc["module_params"] == {"intrusion": {"min_track_hits": 4}}
    assert "SENTINEL" not in text and "webhook" not in text
    assert doc["cameras"][0]["source"]["credential_ref"] == "bench-cam"  # a name, never the pair
    assert "attachment" in header["content-disposition"] and header["cache-control"] == "no-store"


def test_an_export_can_be_imported_back_as_no_change(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        doc = exported(a)
        plan = post(a, doc).json()
    assert {c["change"] for c in plan["changes"]} == {"same"} and plan["applied"] is False


# ------------------------------------------------------------------ import: the plan


def test_a_dry_run_writes_nothing_and_says_what_would_happen(rig: Rig) -> None:
    doc = fresh_document(
        cameras=[{"camera_id": "newcam", "source": {"kind": "synthetic"}}],
        alerts={"min_severity": "LOW"},
    )
    with signed_in(rig, "alice") as a:
        plan = post(a, doc).json()
    assert plan["applied"] is False and plan["backup"] is None
    kinds = {(c["kind"], c["target"], c["change"]) for c in plan["changes"]}
    assert ("camera", "newcam", "add") in kinds and ("alerts", "alert policy", "replace") in kinds
    assert not (rig.config_dir / "cameras" / "newcam.yaml").exists()


def test_a_plan_warns_about_what_will_not_work_here(rig: Rig) -> None:
    doc = fresh_document(
        cameras=[
            {
                "camera_id": "door",
                "source": {
                    "kind": "rtsp",
                    "url": "rtsp://192.0.2.9/s",
                    "credential_ref": "door-cam",
                },
            },
            {"camera_id": "desk", "source": {"kind": "webcam", "device_index": 1}},
        ]
    )
    with signed_in(rig, "alice") as a:
        warnings = post(a, doc).json()["warnings"]
    assert any("door-cam" in w and "never exported" in w for w in warnings)
    assert any("webcam" in w and "device list" in w for w in warnings)


# ------------------------------------------------------------------ import: applying


def test_applying_adds_and_replaces_with_a_backup_and_keeps_everything_else(rig: Rig) -> None:
    doc = fresh_document(
        cameras=[
            {**CAMERA, "name": "Renamed"},
            {"camera_id": "newcam", "source": {"kind": "synthetic"}},
        ],
        zones={
            "bench": ZONES
            | {"zones": [{"zone_id": "dock", "kind": "restricted", "polygon": SQUARE}]}
        },
        module_params={"intrusion": {"min_track_hits": 6}},
        alerts={"min_severity": "HIGH", "rules": [RULE]},
    )
    with signed_in(rig, "alice") as a:
        out = post(a, doc, dry_run="false").json()
    assert out["applied"] is True and out["restart_needed"] is True
    assert out["backup"].startswith("backups/import-")
    backups = list((rig.config_dir / out["backup"]).iterdir())
    assert any("cameras__bench.yaml" in p.name for p in backups)  # the replaced file was saved
    assert writer.read_camera(rig.config_dir, "bench").name == "Renamed"  # type: ignore[union-attr]
    assert writer.read_camera(rig.config_dir, "newcam") is not None
    assert "dock" in (rig.config_dir / "zones" / "bench.zones.yaml").read_text(encoding="utf-8")
    assert "min_track_hits: 6" in (rig.config_dir / "modules" / "intrusion.yaml").read_text(
        encoding="utf-8"
    )
    assert rig.app.settings.alerts.min_severity == "HIGH"  # the policy applied live
    assert "level: INFO" in (rig.config_dir / "vigil.local.yaml").read_text(encoding="utf-8")


def test_importing_never_deletes_what_the_file_does_not_mention(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        post(
            a,
            fresh_document(cameras=[{"camera_id": "extra", "source": {"kind": "synthetic"}}]),
            dry_run="false",
        )
    assert writer.read_camera(rig.config_dir, "bench") is not None
    assert (rig.config_dir / "zones" / "bench.zones.yaml").is_file()


def test_a_failed_write_puts_everything_back(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    original = (rig.config_dir / "cameras" / "bench.yaml").read_text(encoding="utf-8")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(writer, "write_zone_set", fail)
    doc = fresh_document(
        cameras=[
            {**CAMERA, "name": "Renamed"},
            {"camera_id": "newcam", "source": {"kind": "synthetic"}},
        ],
        zones={
            "bench": ZONES
            | {"zones": [{"zone_id": "dock", "kind": "restricted", "polygon": SQUARE}]}
        },
    )
    with signed_in(rig, "alice") as a:
        out = post(a, doc, dry_run="false")
    assert out.status_code == 500 and "restored" in out.json()["detail"]
    assert (rig.config_dir / "cameras" / "bench.yaml").read_text(encoding="utf-8") == original
    assert not (rig.config_dir / "cameras" / "newcam.yaml").exists()


def test_importing_the_same_thing_twice_changes_nothing_the_second_time(rig: Rig) -> None:
    doc = fresh_document(cameras=[{"camera_id": "newcam", "source": {"kind": "synthetic"}}])
    with signed_in(rig, "alice") as a:
        first = post(a, doc, dry_run="false").json()
        second = post(a, doc, dry_run="false").json()
    assert first["restart_needed"] is True
    assert second["restart_needed"] is False and second["backup"] is None


# ------------------------------------------------------------------ import: what is refused


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        (b"{not json", "not valid JSON"),
        (b'{"format": "something-else", "schema_version": 1}', "not a VIGIL-88 settings file"),
        (b'{"format": "vigil88-settings"}', "no schema version"),
        (b'{"format": "vigil88-settings", "schema_version": 99}', "newer version"),
        (b'{"format": "vigil88-settings", "schema_version": 0}', "not valid"),
    ],
)
def test_a_file_that_is_not_ours_or_is_too_new_is_refused_in_plain_words(
    rig: Rig, document: bytes, fragment: str
) -> None:
    with signed_in(rig, "alice") as a:
        out = post(a, document)
    assert out.status_code == 422 and fragment in out.json()["detail"]["message"]


@pytest.mark.parametrize(
    "change",
    [
        {"surprise": 1},  # an unknown top-level key is an error, not something ignored
        {"alerts": {"webhook_url": "https://example.test/x"}},  # the webhook is never imported
        {"cameras": [{"camera_id": "x", "source": {"kind": "rtsp", "url": "rtsp://u:p@host/s"}}]},
        {"cameras": [{"camera_id": "Bad Id!", "source": {"kind": "synthetic"}}]},
        {"zones": {"../escape": {}}},
        {"module_params": {"no_such_module": {"a": 1}}},
        {"module_params": {"intrusion": {"base_signal": 7}}},
        {"module_params": {"intrusion": {"typo": 1}}},
        {"alerts": {"rules": [{"name": "r", "channels": ["jsonl"]}]}},
    ],
)
def test_a_document_with_anything_wrong_in_it_changes_nothing(
    rig: Rig, change: dict[str, Any]
) -> None:
    before = sorted(p.name for p in rig.config_dir.rglob("*") if p.is_file())
    with signed_in(rig, "alice") as a:
        out = post(a, fresh_document(**change), dry_run="false")
    assert out.status_code == 422, out.text
    assert sorted(p.name for p in rig.config_dir.rglob("*") if p.is_file()) == before


def test_the_same_camera_twice_is_refused(rig: Rig) -> None:
    cam = {"camera_id": "dup", "source": {"kind": "synthetic"}}
    with signed_in(rig, "alice") as a:
        assert post(a, fresh_document(cameras=[cam, cam])).status_code == 422


def test_a_file_over_the_size_limit_is_refused_before_it_is_read(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        out = post(a, b" " * (1024 * 1024 + 10))
    assert out.status_code == 413


def test_imports_and_exports_are_audited_without_their_content(rig: Rig) -> None:
    with signed_in(rig, "alice") as a:
        exported(a)
        post(
            a,
            fresh_document(cameras=[{"camera_id": "newcam", "source": {"kind": "synthetic"}}]),
            dry_run="false",
        )
        actions = [e["action"] for e in a.get("/api/v1/audit", params={"limit": 20}).json()]
        everything = a.get("/api/v1/audit", params={"limit": 20}).text
    assert "settings.exported" in actions and "settings.imported" in actions
    assert "SENTINEL" not in everything
