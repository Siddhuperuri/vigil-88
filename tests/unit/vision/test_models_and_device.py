from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import BinaryIO

import pytest

from vigil.core.clock import ManualClock
from vigil.core.errors import CapabilityError
from vigil.vision.device import (
    CPU_PROVIDER,
    CUDA_PROVIDER,
    nvidia_bin_dirs,
    prepare_cuda_runtime,
    resolve_device,
)
from vigil.vision.models import (
    KNOWN_MODELS,
    MANIFEST_NAME,
    ModelSpec,
    fetch_model,
    load_manifest,
    register_model,
    sha256_file,
    verify_model,
    weights_filename,
)

PAYLOAD = b"fake onnx weights " * 1000
SPEC = ModelSpec(
    name="toy",
    url="https://example.invalid/toy.onnx",
    license="Apache-2.0",
    input_size_px=416,
    size_bytes=len(PAYLOAD),
    family="yolox",
    version="1",
)


def opener_for(data: bytes, calls: list[str] | None = None):  # type: ignore[no-untyped-def]
    def opener(url: str) -> BinaryIO:
        if calls is not None:
            calls.append(url)
        return io.BytesIO(data)

    return opener


# ------------------------------------------------------------------ registry


def test_the_registry_is_what_the_user_approved() -> None:
    assert set(KNOWN_MODELS) == {"yolox_nano", "yolox_tiny", "yolox_s"}
    for spec in KNOWN_MODELS.values():
        assert spec.url.startswith("https://github.com/Megvii-BaseDetection/YOLOX/")
        assert spec.license == "Apache-2.0" and spec.family == "yolox"
    assert {s.input_size_px for s in KNOWN_MODELS.values()} == {416, 640}
    assert sum(s.size_bytes for s in KNOWN_MODELS.values()) == 59_737_071


@pytest.mark.parametrize(
    ("given", "expected"), [("yolox_s", "yolox_s.onnx"), ("custom.onnx", "custom.onnx")]
)
def test_weights_filename(given: str, expected: str) -> None:
    assert weights_filename(given) == expected


@pytest.mark.parametrize("bad", ["", "../x", "a/b.onnx", "a\\b.onnx", "C:x.onnx", "..", "."])
def test_weights_must_be_a_bare_name(bad: str) -> None:
    with pytest.raises(CapabilityError, match="not a path"):
        weights_filename(bad)


# ------------------------------------------------------------------ fetch + manifest


def test_fetch_downloads_verifies_size_and_records_a_manifest_entry(
    tmp_path: Path, clock: ManualClock
) -> None:
    entry = fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD))
    assert (tmp_path / "toy.onnx").read_bytes() == PAYLOAD
    assert entry.sha256 == hashlib.sha256(PAYLOAD).hexdigest() and entry.size_bytes == len(PAYLOAD)
    assert entry.source == SPEC.url and entry.license == "Apache-2.0"
    raw = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert raw["version"] == 1 and raw["models"]["toy.onnx"]["sha256"] == entry.sha256
    assert not list(tmp_path.glob("*.part"))  # no partial file left behind


def test_fetch_is_idempotent_and_does_not_redownload(tmp_path: Path, clock: ManualClock) -> None:
    calls: list[str] = []
    first = fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD, calls))
    second = fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD, calls))
    assert first == second and len(calls) == 1


def test_a_short_download_is_rejected_and_leaves_nothing_behind(
    tmp_path: Path, clock: ManualClock
) -> None:
    with pytest.raises(CapabilityError, match="expected .* bytes, received"):
        fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD[:-5]))
    assert not (tmp_path / "toy.onnx").exists() and not list(tmp_path.glob("*.part"))
    assert load_manifest(tmp_path) == {}


def test_an_oversized_download_is_stopped_early(tmp_path: Path, clock: ManualClock) -> None:
    with pytest.raises(CapabilityError, match="exceeded"):
        fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD + b"extra"))
    assert not (tmp_path / "toy.onnx").exists()


def test_a_network_failure_is_a_clean_capability_error(tmp_path: Path, clock: ManualClock) -> None:
    def failing(url: str) -> BinaryIO:
        raise OSError("connection reset")

    with pytest.raises(CapabilityError, match="could not download toy"):
        fetch_model(SPEC, tmp_path, clock=clock, opener=failing)
    assert not list(tmp_path.glob("*.part"))


def test_non_https_urls_are_refused(tmp_path: Path, clock: ManualClock) -> None:
    http = ModelSpec("toy", "http://example.invalid/toy.onnx", "MIT", 416, 10, "yolox", "1")
    with pytest.raises(CapabilityError, match="non-HTTPS"):
        fetch_model(http, tmp_path, clock=clock)


# ------------------------------------------------------------------ verification


def test_a_fetched_model_verifies(tmp_path: Path, clock: ManualClock) -> None:
    fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD))
    assert verify_model(tmp_path, "toy").path == tmp_path / "toy.onnx"


def test_tampering_is_caught_even_when_the_size_is_unchanged(
    tmp_path: Path, clock: ManualClock
) -> None:
    fetch_model(SPEC, tmp_path, clock=clock, opener=opener_for(PAYLOAD))
    tampered = bytearray(PAYLOAD)
    tampered[10] ^= 1
    (tmp_path / "toy.onnx").write_bytes(bytes(tampered))
    with pytest.raises(CapabilityError, match="failed its integrity check"):
        verify_model(tmp_path, "toy")


def test_a_deleted_file_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(CapabilityError, match="vigil models fetch yolox_nano"):
        verify_model(tmp_path, "yolox_nano")
    with pytest.raises(CapabilityError, match="vigil models register mine.onnx"):
        verify_model(tmp_path, "mine.onnx")


def test_a_corrupt_manifest_is_never_trusted(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(CapabilityError, match="unreadable or malformed"):
        load_manifest(tmp_path)


def test_register_records_an_existing_file(tmp_path: Path, clock: ManualClock) -> None:
    (tmp_path / "mine.onnx").write_bytes(b"abc" * 100)
    entry = register_model(tmp_path, "mine", license="MIT", source="my export", clock=clock)
    assert entry.sha256 == sha256_file(tmp_path / "mine.onnx") and entry.source == "my export"
    assert verify_model(tmp_path, "mine.onnx").entry == entry
    with pytest.raises(CapabilityError, match="not found"):
        register_model(tmp_path, "absent", license="MIT", source="x", clock=clock)


def test_sha256_is_streamed_correctly(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    data = os.urandom(3 * 1024 * 1024 + 17)  # spans several read chunks
    big.write_bytes(data)
    assert sha256_file(big) == hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ device resolution

BOTH = (CUDA_PROVIDER, CPU_PROVIDER)


@pytest.mark.parametrize("wanted", ["auto", "cuda"])
def test_cuda_is_chosen_when_available(wanted: str) -> None:
    d = resolve_device(wanted, allow_cpu_fallback=False, providers=BOTH)  # type: ignore[arg-type]
    assert d.kind == "cuda" and not d.fell_back and CUDA_PROVIDER in d.reason


def test_cpu_request_never_touches_cuda_even_if_present() -> None:
    d = resolve_device("cpu", allow_cpu_fallback=False, providers=BOTH)
    assert d.kind == "cpu" and not d.fell_back


def test_auto_without_cuda_falls_back_and_explains() -> None:
    d = resolve_device("auto", allow_cpu_fallback=False, providers=(CPU_PROVIDER,))
    assert d.kind == "cpu" and d.fell_back and "onnx-gpu" in d.reason


def test_explicit_cuda_without_cuda_follows_the_fallback_flag() -> None:
    d = resolve_device("cuda", allow_cpu_fallback=True, providers=(CPU_PROVIDER,))
    assert d.kind == "cpu" and d.fell_back
    with pytest.raises(CapabilityError, match="allow_cpu_fallback is false"):
        resolve_device("cuda", allow_cpu_fallback=False, providers=(CPU_PROVIDER,))


def test_no_onnxruntime_at_all_is_explained() -> None:
    d = resolve_device("auto", allow_cpu_fallback=True, providers=())
    assert "onnxruntime not installed" in d.reason


def test_cuda_runtime_preparation_is_idempotent() -> None:
    first = prepare_cuda_runtime()
    assert prepare_cuda_runtime() == []  # nothing new the second time
    assert all(Path(p).is_dir() for p in first)
    assert {str(p) for p in nvidia_bin_dirs()} >= set(first)
