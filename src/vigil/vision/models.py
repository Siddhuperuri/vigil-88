"""Model registry, manifest and integrity checking (03 §6, §7).

Rules:
  * Weights are never committed and never downloaded implicitly. `fetch_model` runs only
    when an operator invokes it, from a fixed registry of HTTPS URLs.
  * Every model file must be listed in `<models_dir>/MANIFEST.json` with its SHA-256,
    licence and source. A file that is unlisted, missing or whose hash differs from the
    manifest is a hard `CapabilityError`, never a warning.
  * Upstream YOLOX publishes no checksums, so the first fetch is trust-on-first-use: the hash
    is recorded then and verified on every load afterwards.
  * `weights` in configuration is a bare file name inside `models_dir`, never a path.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from vigil.core.clock import Clock
from vigil.core.errors import CapabilityError

MANIFEST_NAME = "MANIFEST.json"
MANIFEST_VERSION = 1
_CHUNK_BYTES = 1024 * 1024
_FETCH_TIMEOUT_S = 30
_YOLOX_RELEASE = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    url: str
    license: str
    input_size_px: int
    size_bytes: int
    family: str
    version: str

    @property
    def filename(self) -> str:
        return f"{self.name}.onnx"


def _yolox(name: str, size_px: int, size_bytes: int) -> ModelSpec:
    return ModelSpec(
        name=name,
        url=f"{_YOLOX_RELEASE}/{name}.onnx",
        license="Apache-2.0",
        input_size_px=size_px,
        size_bytes=size_bytes,
        family="yolox",
        version="0.1.1rc0",
    )


KNOWN_MODELS: Mapping[str, ModelSpec] = {
    m.name: m
    for m in (
        _yolox("yolox_nano", 416, 3_659_407),
        _yolox("yolox_tiny", 416, 20_219_662),
        _yolox("yolox_s", 640, 35_858_002),
    )
}


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    file: str
    sha256: str
    size_bytes: int
    source: str  # the URL it came from, or "registered locally"
    license: str
    recorded_utc: str


@dataclass(frozen=True, slots=True)
class VerifiedModel:
    path: Path
    entry: ManifestEntry


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(models_dir: Path) -> Path:
    return models_dir / MANIFEST_NAME


def load_manifest(models_dir: Path) -> dict[str, ManifestEntry]:
    path = manifest_path(models_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {name: ManifestEntry(**fields) for name, fields in raw["models"].items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CapabilityError(
            f"{path.name} is unreadable or malformed ({type(exc).__name__}); refusing to trust it",
            context={"path": str(path)},
        ) from exc


def _write_manifest(models_dir: Path, entries: Mapping[str, ManifestEntry]) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "models": {
            name: {
                "file": e.file,
                "sha256": e.sha256,
                "size_bytes": e.size_bytes,
                "source": e.source,
                "license": e.license,
                "recorded_utc": e.recorded_utc,
            }
            for name, e in sorted(entries.items())
        },
    }
    target = manifest_path(models_dir)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def _stamp(clock: Clock) -> str:
    wall: datetime = clock.wall_utc()
    return wall.isoformat()


def _validate_filename(weights: str) -> str:
    """`weights` must be a bare name: no separators, no drive, no traversal."""
    if not weights or weights != Path(weights).name or weights in {".", ".."} or ":" in weights:
        raise CapabilityError(
            f"vision.weights must be a file name inside models_dir, not a path: {weights!r}"
        )
    return weights


def weights_filename(weights: str) -> str:
    """`yolox_s` -> `yolox_s.onnx`; an explicit `*.onnx` name is used as given."""
    name = _validate_filename(weights)
    return name if name.endswith(".onnx") else f"{name}.onnx"


def verify_model(models_dir: Path, weights: str) -> VerifiedModel:
    """Find the file, require a manifest entry, and check size and SHA-256. Raises on any gap."""
    filename = weights_filename(weights)
    path = models_dir / filename
    hint = (
        f"fetch it with `vigil models fetch {weights}`"
        if weights in KNOWN_MODELS
        else f"register it with `vigil models register {filename}`"
    )
    if not path.is_file():
        raise CapabilityError(
            f"model file {filename} not found in {models_dir}; {hint}", context={"file": filename}
        )
    entry = load_manifest(models_dir).get(filename)
    if entry is None:
        raise CapabilityError(
            f"{filename} is not listed in {MANIFEST_NAME}, so its integrity cannot be "
            f"checked; {hint}",
            context={"file": filename},
        )
    actual_size = path.stat().st_size
    if actual_size != entry.size_bytes:
        raise CapabilityError(
            f"{filename} is {actual_size} bytes but the manifest records {entry.size_bytes}",
            context={"file": filename},
        )
    actual = sha256_file(path)
    if actual != entry.sha256:
        raise CapabilityError(
            f"{filename} failed its integrity check: sha256 {actual[:16]}... does not match "
            f"the manifest ({entry.sha256[:16]}...). The file changed after it was recorded",
            context={"file": filename},
        )
    return VerifiedModel(path, entry)


def register_model(
    models_dir: Path, filename: str, *, license: str, source: str, clock: Clock
) -> ManifestEntry:
    """Record an existing local file (trust-on-first-use)."""
    name = weights_filename(filename)
    path = models_dir / name
    if not path.is_file():
        raise CapabilityError(f"cannot register {name}: not found in {models_dir}")
    entry = ManifestEntry(
        file=name,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        source=source,
        license=license,
        recorded_utc=_stamp(clock),
    )
    entries = load_manifest(models_dir)
    entries[name] = entry
    _write_manifest(models_dir, entries)
    return entry


Opener = Callable[[str], BinaryIO]


def _default_opener(url: str) -> BinaryIO:
    if not url.startswith("https://"):
        raise CapabilityError(f"refusing to download over a non-HTTPS URL: {url}")
    # The scheme is checked above and the URL comes from the fixed registry.
    return urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_S)  # type: ignore[no-any-return]  # noqa: S310


def fetch_model(
    spec: ModelSpec, models_dir: Path, *, clock: Clock, opener: Opener | None = None
) -> ManifestEntry:
    """Download one registry model, verify its size, and record its SHA-256.

    Safe to re-run: a file that already verifies is left alone. A partial or wrong-sized
    download is deleted and reported; it never reaches `models_dir` under its final name.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / spec.filename
    existing = load_manifest(models_dir).get(spec.filename)
    if target.is_file() and existing is not None:
        verify_model(models_dir, spec.name)
        return existing

    open_url = opener or _default_opener
    part = target.with_suffix(".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with open_url(spec.url) as response, part.open("wb") as out:
            while chunk := response.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > spec.size_bytes:
                    raise CapabilityError(
                        f"{spec.name}: download exceeded the expected {spec.size_bytes} bytes"
                    )
                digest.update(chunk)
                out.write(chunk)
        if total != spec.size_bytes:
            raise CapabilityError(
                f"{spec.name}: expected {spec.size_bytes} bytes, received {total}"
            )
        part.replace(target)
    except OSError as exc:
        raise CapabilityError(f"could not download {spec.name}: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)

    entry = ManifestEntry(
        file=spec.filename,
        sha256=digest.hexdigest(),
        size_bytes=total,
        source=spec.url,
        license=spec.license,
        recorded_utc=_stamp(clock),
    )
    entries = load_manifest(models_dir)
    entries[spec.filename] = entry
    _write_manifest(models_dir, entries)
    return entry
