"""Says whether this repository may produce a distributable release, and why not.

    python scripts/release_check.py                 # report; exit 0 (informational)
    python scripts/release_check.py --release       # exit 1 while any decision is unresolved
    python scripts/release_check.py --release --installer build/installer/<setup>.exe

It reads `release/decisions.yaml` and the evidence each decision needs. A decision recorded without
its evidence is still a blocker: writing "decided" in a file must not be enough to ship. Nothing
here signs, uploads, downloads or publishes anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = Path("release/decisions.yaml")
GPU_MANIFEST = Path("release/gpu-runtime.manifest.json")
UPDATER_PACKAGES = ("electron-updater", "update-electron-app", "@electron/updater")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Blocker:
    decision: str
    reason: str


def _load(root: Path) -> dict[str, dict[str, object]]:
    path = root / DECISIONS
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _licence(root: Path, entry: dict[str, object]) -> list[Blocker]:
    if entry.get("status") != "decided":
        return [Blocker("licence", "no project licence is declared (decision 1)")]
    spdx = str(entry.get("spdx") or "").strip()
    out: list[Blocker] = []
    if not spdx:
        out.append(Blocker("licence", "decided, but no SPDX identifier is recorded"))
    if not any((root / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")):
        out.append(
            Blocker("licence", "decided, but there is no LICENSE file at the repository root")
        )
    pyproject = root / "pyproject.toml"
    if spdx and pyproject.is_file() and spdx not in pyproject.read_text(encoding="utf-8"):
        out.append(Blocker("licence", f"{spdx} is not named in pyproject.toml"))
    return out


def _gpu_manifest_problems(root: Path) -> list[str]:
    path = root / GPU_MANIFEST
    if not path.is_file():
        return [f"{GPU_MANIFEST} does not exist"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{GPU_MANIFEST} is not valid JSON: {exc}"]
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        return [f"{GPU_MANIFEST} lists no files"]
    problems: list[str] = []
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            problems.append(f"file {i} is not an object")
            continue
        name = item.get("name", f"#{i}")
        if not str(item.get("url", "")).startswith("https://"):
            problems.append(f"{name}: url must be https")
        if not _SHA256.match(str(item.get("sha256", ""))):
            problems.append(f"{name}: sha256 must be 64 lowercase hex digits")
        size = item.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            problems.append(f"{name}: size_bytes must be a positive integer")
        review = item.get("licence_review")
        if not (isinstance(review, dict) and review.get("approved_by") and review.get("date")):
            problems.append(f"{name}: no licence review (approved_by and date) is recorded")
    return problems


def _gpu(root: Path, entry: dict[str, object]) -> list[Blocker]:
    status = entry.get("status")
    if status == "not_shipped":
        return []
    if status == "download":
        return [Blocker("gpu_runtime", p) for p in _gpu_manifest_problems(root)]
    return [Blocker("gpu_runtime", "NVIDIA runtime policy is not decided (decision 2)")]


def _signing(entry: dict[str, object], installer: Path | None) -> list[Blocker]:
    if entry.get("status") != "signed":
        return [
            Blocker("signing", "the installer is unsigned; no certificate is chosen (decision 3)")
        ]
    if not str(entry.get("subject") or "").strip():
        return [Blocker("signing", "signed, but the expected certificate subject is not recorded")]
    if installer is None:
        return [Blocker("signing", "signed, but no --installer was given to verify")]
    return _verify_signature(installer, str(entry["subject"]))


def _verify_signature(installer: Path, subject: str) -> list[Blocker]:
    if not installer.is_file():
        return [Blocker("signing", f"{installer} does not exist")]
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "[pscustomobject]@{status=[string]$s.Status; subject=$s.SignerCertificate.Subject} "
        "| ConvertTo-Json -Compress"
    )
    try:
        run = subprocess.run(  # noqa: S603 - fixed program; the path is passed as an argument
            ["powershell", "-NoProfile", "-Command", script, str(installer)],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        result = json.loads(run.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [Blocker("signing", f"the signature could not be checked: {exc}")]
    if result.get("status") != "Valid":
        return [Blocker("signing", f"Authenticode status is {result.get('status')!r}, not Valid")]
    if subject not in str(result.get("subject") or ""):
        return [Blocker("signing", "the certificate subject is not the one recorded")]
    return []


def _updater(root: Path, entry: dict[str, object]) -> list[Blocker]:
    status = entry.get("status")
    package = root / "desktop" / "package.json"
    declared: list[str] = []
    if package.is_file():
        data = json.loads(package.read_text(encoding="utf-8"))
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        declared = [name for name in UPDATER_PACKAGES if name in deps]
    if status == "not_shipped":
        if declared:
            return [Blocker("updater", f"no updater was chosen, but desktop depends on {declared}")]
        return []
    if status == "enabled":
        out: list[Blocker] = []
        if not str(entry.get("feed_url") or "").startswith("https://"):
            out.append(Blocker("updater", "enabled, but feed_url is not an https URL"))
        if not str(entry.get("public_key_fingerprint") or "").strip():
            out.append(
                Blocker("updater", "enabled, but no update-signing key fingerprint is recorded")
            )
        if not declared:
            out.append(Blocker("updater", "enabled, but desktop depends on no updater package"))
        return out
    out = [Blocker("updater", "no update mechanism exists and none is chosen (decision 4)")]
    if declared:  # an updater that arrived before its key and host: the thing this gate is for
        out.append(Blocker("updater", f"desktop depends on {declared} before decision 4 is made"))
    return out


def blockers(root: Path = ROOT, installer: Path | None = None) -> list[Blocker]:
    decisions = _load(root)
    return [
        *_licence(root, decisions.get("licence", {})),
        *_gpu(root, decisions.get("gpu_runtime", {})),
        *_signing(decisions.get("signing", {}), installer),
        *_updater(root, decisions.get("updater", {})),
    ]


def report(found: list[Blocker]) -> str:
    if not found:
        return "No release blockers recorded."
    lines = ["NOT DISTRIBUTABLE. Unresolved:"]
    lines += [f"  - [{b.decision}] {b.reason}" for b in found]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    release = "--release" in argv
    installer = Path(argv[argv.index("--installer") + 1]) if "--installer" in argv else None
    found = blockers(ROOT, installer)
    print(report(found))
    return 1 if release and found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
