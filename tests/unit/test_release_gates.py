"""The release gate: a distributable build cannot be produced while a decision is open."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from scripts import release_check as rc

from tests.conftest import REPO_ROOT


def tree(root: Path, decisions: dict[str, object], **files: str) -> Path:
    (root / "release").mkdir(parents=True, exist_ok=True)
    (root / rc.DECISIONS).write_text(yaml.safe_dump(decisions), encoding="utf-8")
    for rel, text in files.items():
        target = root / rel.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def names(found: list[rc.Blocker]) -> set[str]:
    return {b.decision for b in found}


GOOD_MANIFEST = {
    "files": [
        {
            "name": "cudnn",
            "url": "https://example.test/cudnn.zip",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "licence_review": {"approved_by": "owner", "date": "2026-11-03"},
        }
    ]
}


def test_the_repository_is_not_distributable_and_says_all_four_decisions() -> None:
    found = rc.blockers(REPO_ROOT)
    assert names(found) == {"licence", "gpu_runtime", "signing", "updater"}


def test_release_mode_exits_nonzero_while_any_decision_is_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert rc.main(["--release"]) == 1
    assert "NOT DISTRIBUTABLE" in capsys.readouterr().out


def test_report_mode_is_informational() -> None:
    assert rc.main([]) == 0


def test_a_missing_decisions_file_blocks_everything(tmp_path: Path) -> None:
    assert names(rc.blockers(tmp_path)) == {"licence", "gpu_runtime", "signing", "updater"}


def test_writing_decided_is_not_enough_for_the_licence(tmp_path: Path) -> None:
    root = tree(tmp_path, {"licence": {"status": "decided", "spdx": "MIT"}})
    reasons = [b.reason for b in rc._licence(root, {"status": "decided", "spdx": "MIT"})]
    assert any("LICENSE file" in r for r in reasons)


def test_a_licence_with_its_evidence_passes(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {"licence": {"status": "decided", "spdx": "MIT"}},
        LICENSE="MIT licence text",
        **{"pyproject.toml": 'license = "MIT"\n'},
    )
    assert rc._licence(root, {"status": "decided", "spdx": "MIT"}) == []


def test_cpu_only_is_a_decision_that_passes(tmp_path: Path) -> None:
    assert rc._gpu(tmp_path, {"status": "not_shipped"}) == []


def test_a_gpu_download_needs_a_manifest(tmp_path: Path) -> None:
    assert names(rc._gpu(tmp_path, {"status": "download"})) == {"gpu_runtime"}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"url": "http://example.test/x"}, "https"),
        ({"sha256": "XYZ"}, "sha256"),
        ({"size_bytes": 0}, "size_bytes"),
        ({"licence_review": {}}, "licence review"),
    ],
)
def test_every_gpu_file_needs_https_a_hash_a_size_and_a_licence_review(
    tmp_path: Path, change: dict[str, object], fragment: str
) -> None:
    item = {**GOOD_MANIFEST["files"][0], **change}
    root = tree(
        tmp_path, {}, **{"release__gpu-runtime.manifest.json": json.dumps({"files": [item]})}
    )
    problems = rc._gpu_manifest_problems(root)
    assert problems and fragment in problems[0]


def test_a_complete_gpu_manifest_passes(tmp_path: Path) -> None:
    root = tree(tmp_path, {}, **{"release__gpu-runtime.manifest.json": json.dumps(GOOD_MANIFEST)})
    assert rc._gpu(root, {"status": "download"}) == []


def test_a_manifest_that_is_not_json_is_a_blocker(tmp_path: Path) -> None:
    root = tree(tmp_path, {}, **{"release__gpu-runtime.manifest.json": "{nope"})
    assert "not valid JSON" in rc._gpu_manifest_problems(root)[0]


def test_signing_needs_a_subject_and_an_installer_to_verify() -> None:
    assert rc._signing({"status": "signed"}, None)[0].reason.startswith("signed, but the expected")
    assert "--installer" in rc._signing({"status": "signed", "subject": "CN=X"}, None)[0].reason


def test_an_unsigned_or_missing_installer_does_not_verify(tmp_path: Path) -> None:
    missing = rc._signing({"status": "signed", "subject": "CN=X"}, tmp_path / "none.exe")
    assert "does not exist" in missing[0].reason
    unsigned = tmp_path / "setup.exe"
    unsigned.write_bytes(b"MZ not signed")
    result = rc._signing({"status": "signed", "subject": "CN=X"}, unsigned)
    assert names(result) == {"signing"}


def test_an_updater_dependency_before_decision_four_is_a_blocker(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {},
        **{"desktop__package.json": json.dumps({"dependencies": {"electron-updater": "1"}})},
    )
    reasons = [b.reason for b in rc._updater(root, {"status": "undecided"})]
    assert any("before decision 4" in r for r in reasons)
    assert rc._updater(root, {"status": "not_shipped"})[0].reason.startswith(
        "no updater was chosen"
    )


def test_no_updater_by_decision_passes_and_an_enabled_one_needs_its_key(tmp_path: Path) -> None:
    root = tree(tmp_path, {}, **{"desktop__package.json": json.dumps({"dependencies": {}})})
    assert rc._updater(root, {"status": "not_shipped"}) == []
    enabled = rc._updater(root, {"status": "enabled", "feed_url": "http://x"})
    assert len(enabled) == 3


def test_the_installer_is_named_development_only_unless_released() -> None:
    config = (REPO_ROOT / "desktop" / "electron-builder.yml").read_text(encoding="utf-8")
    assert "artifactName: VIGIL-88-DEV-ONLY-Setup-" in config
