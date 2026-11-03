"""The diagnostics bundle: useful for support, and incapable of carrying a secret or a path."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
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
from vigil.api.diagnostics import PathScrubber
from vigil.version import __version__

URL = "/api/v1/system/diagnostics"
WEBHOOK_SECRET = "WEBHOOK-TOKEN-ZZ41"
LOG_PASSWORD = "hunter2hunter2"
LOG_BEARER = "Bearer abcdefghijklmnopqrstuvwx"
LOG_CREDENTIAL_URL_PASSWORD = "pw12345"
PROFILE = "realperson"
FRAME_MARKER = b"\xff\xd8\xff\xe0FRAME-BYTES-MUST-NOT-LEAK"


@pytest.fixture
def rig(make_config: MakeConfig, tmp_path: Path) -> Iterator[Rig]:
    extra = {
        "cameras": [{"camera_id": "bench", "source": {"kind": "synthetic"}}],
        "alerts.channels": ["console", "webhook"],
        "alerts.webhook_url": f"https://hooks.example.test/in/{WEBHOOK_SECRET}",
    }
    with build_rig(make_config, tmp_path, extra=extra) as made:
        set_up(made)
        paths = made.app.paths
        paths.log_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            {"event": "fine", "level": "info"},
            {"event": f"login failed password={LOG_PASSWORD}", "level": "warning"},
            {"event": f"sent header {LOG_BEARER}", "level": "info"},
            {
                "event": f"open rtsp://admin:{LOG_CREDENTIAL_URL_PASSWORD}@10.0.0.7/s",
                "level": "info",
            },
            {
                "event": f"read C:\\Users\\{PROFILE}\\AppData\\file.txt and {paths.data_dir}",
                "level": "info",
            },
            {
                "event": "ok",
                "level": "info",
                "api_key": "KEYVALUE-1234567890",
                "path": str(paths.data_dir),
            },
        ]
        paths.log_file.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        evidence = paths.evidence_dir / "bench" / "incident"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "snapshot_raw.jpg").write_bytes(FRAME_MARKER)
        yield made


def fetch(rig: Rig) -> tuple[zipfile.ZipFile, dict[str, str]]:
    with signed_in(rig, "alice") as a:
        reply = a.get(URL)
    assert reply.status_code == 200, reply.text
    archive = zipfile.ZipFile(io.BytesIO(reply.content))
    return archive, dict(reply.headers)


def everything(archive: zipfile.ZipFile) -> str:
    return "\n".join(archive.read(n).decode("utf-8") for n in archive.namelist())


def test_only_an_administrator_may_download_it(rig: Rig) -> None:
    make_user(rig, "oscar", "operator")
    with signed_in(rig, "oscar", GOOD + "?") as o:
        assert o.get(URL).status_code == 403


def test_it_has_the_expected_members_and_downloads_as_a_file(rig: Rig) -> None:
    archive, headers = fetch(rig)
    assert set(archive.namelist()) == {
        "README.txt",
        "versions.json",
        "health.json",
        "config.redacted.json",
        "metrics.json",
        "storage.json",
        "logs/vigil.tail.jsonl",
        "MANIFEST.json",
    }
    assert headers["content-type"] == "application/zip" and headers["cache-control"] == "no-store"
    assert "vigil88-diagnostics.zip" in headers["content-disposition"]


def test_it_says_what_it_is_and_what_it_leaves_out(rig: Rig) -> None:
    archive, _ = fetch(rig)
    assert json.loads(archive.read("versions.json"))["vigil"] == __version__
    manifest = json.loads(archive.read("MANIFEST.json"))
    assert (
        "evidence files" in manifest["left_out"] and "absolute file paths" in manifest["left_out"]
    )
    assert "Read it before you send it" in archive.read("README.txt").decode()


def test_the_manifest_hashes_match_the_contents(rig: Rig) -> None:
    archive, _ = fetch(rig)
    for entry in json.loads(archive.read("MANIFEST.json"))["files"]:
        data = archive.read(entry["file"])
        assert hashlib.sha256(data).hexdigest() == entry["sha256"] and len(data) == entry["bytes"]


def test_health_and_configuration_are_there_to_be_useful(rig: Rig) -> None:
    archive, _ = fetch(rig)
    health = json.loads(archive.read("health.json"))
    assert health["state"] == "running" and health["degradation"]["step"] == 0
    assert [c["camera_id"] for c in health["cameras"]] == ["bench"]
    assert any(w["name"] == "inference" for w in health["workers"])
    config = json.loads(archive.read("config.redacted.json"))
    assert config["cameras"][0]["camera_id"] == "bench" and "alerts" in config
    assert "counters" in json.loads(archive.read("metrics.json"))


@pytest.mark.parametrize(
    "secret",
    [
        WEBHOOK_SECRET,
        LOG_PASSWORD,
        "abcdefghijklmnopqrstuvwx",
        LOG_CREDENTIAL_URL_PASSWORD,
        "KEYVALUE-1234567890",
        PROFILE,
    ],
)
def test_no_planted_secret_or_name_survives_anywhere(rig: Rig, secret: str) -> None:
    archive, _ = fetch(rig)
    assert secret not in everything(archive)


def test_no_absolute_path_to_the_data_or_config_folder_survives(rig: Rig) -> None:
    archive, _ = fetch(rig)
    text = everything(archive).lower()
    for folder in (rig.app.paths.data_dir, rig.app.paths.config_dir):
        assert str(folder).lower() not in text and folder.as_posix().lower() not in text
    assert "<data_dir>" in text


def test_no_frame_and_nothing_from_the_evidence_folder_is_included(rig: Rig) -> None:
    archive, _ = fetch(rig)
    assert not [
        n for n in archive.namelist() if n.lower().endswith((".jpg", ".jpeg", ".png", ".mp4"))
    ]
    assert all(b"FRAME-BYTES" not in archive.read(n) for n in archive.namelist())


def test_the_webhook_is_reported_only_as_set_or_not(rig: Rig) -> None:
    archive, _ = fetch(rig)
    assert json.loads(archive.read("config.redacted.json"))["alerts"]["webhook_url"] == "<set>"


def test_no_account_or_audit_content_is_included(rig: Rig) -> None:
    archive, _ = fetch(rig)
    text = everything(archive)
    assert (
        "alice" not in text
        and "argon2" not in text
        and "audit" not in text.lower().replace("the audit log", "")
    )


def test_downloading_it_is_audited(rig: Rig) -> None:
    fetch(rig)
    with signed_in(rig, "alice") as a:
        actions = [e["action"] for e in a.get("/api/v1/audit", params={"limit": 10}).json()]
    assert "diagnostics.exported" in actions


def test_a_missing_log_file_does_not_stop_the_bundle(rig: Rig) -> None:
    rig.app.paths.log_file.unlink()
    archive, _ = fetch(rig)
    assert archive.read("logs/vigil.tail.jsonl") == b""


# ------------------------------------------------------------------ the scrubber on its own


def test_the_scrubber_replaces_named_folders_and_any_user_profile(tmp_path: Path) -> None:
    scrub = PathScrubber({"data_dir": tmp_path / "data"})
    inside = f"opened {tmp_path / 'data' / 'x.db'} and C:\\Users\\Someone\\Desktop\\a.txt"
    out = scrub(inside)
    assert "<data_dir>" in out and "Someone" not in out and "<user_profile>" in out
    assert scrub("/home/someone/file") == "<user_profile>/file"
    assert scrub("nothing to hide") == "nothing to hide"


def test_the_scrubber_walks_nested_values(tmp_path: Path) -> None:
    scrub = PathScrubber({"config_dir": tmp_path})
    out = scrub.deep({"a": [str(tmp_path / "x")], "b": 3, "c": ("y",)})
    assert out["b"] == 3 and out["c"] == ["y"]  # type: ignore[index]
    assert out["a"][0].startswith("<config_dir>")  # type: ignore[index]
