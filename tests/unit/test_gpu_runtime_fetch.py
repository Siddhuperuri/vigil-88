"""The NVIDIA runtime download refuses unless approved and keeps no unverified file."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from scripts import gpu_runtime

PAYLOAD = b"not really a cuda library" * 100


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "served"
    root.mkdir()
    (root / "lib.bin").write_bytes(PAYLOAD)
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/lib.bin"
    httpd.shutdown()
    httpd.server_close()


def item(url: str, **change: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "lib.bin",
        "url": url,
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "size_bytes": len(PAYLOAD),
    }
    return {**base, **change}


def test_a_file_that_matches_is_moved_into_place(server: str, tmp_path: Path) -> None:
    out = gpu_runtime.fetch(item(server), tmp_path / "target")
    assert out.read_bytes() == PAYLOAD
    assert not list((tmp_path / "target").glob("*.part"))


def test_a_wrong_hash_leaves_nothing_behind(server: str, tmp_path: Path) -> None:
    with pytest.raises(gpu_runtime.Refused, match="SHA-256"):
        gpu_runtime.fetch(item(server, sha256="0" * 64), tmp_path / "target")
    assert list((tmp_path / "target").iterdir()) == []


def test_more_bytes_than_recorded_stops_the_download(server: str, tmp_path: Path) -> None:
    with pytest.raises(gpu_runtime.Refused, match="more bytes"):
        gpu_runtime.fetch(item(server, size_bytes=10), tmp_path / "target")
    assert list((tmp_path / "target").iterdir()) == []


def test_a_name_cannot_climb_out_of_the_target(server: str, tmp_path: Path) -> None:
    out = gpu_runtime.fetch(item(server, name="../../escape.bin"), tmp_path / "target")
    assert out.parent == tmp_path / "target"


def test_verify_file_checks_size_before_hash(tmp_path: Path) -> None:
    f = tmp_path / "x"
    f.write_bytes(b"abc")
    with pytest.raises(gpu_runtime.Refused, match="size"):
        gpu_runtime.verify_file(f, sha256="0" * 64, size_bytes=4)
    gpu_runtime.verify_file(f, sha256=hashlib.sha256(b"abc").hexdigest(), size_bytes=3)


def test_nothing_is_downloaded_while_the_decision_is_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gpu_runtime.main([str(tmp_path / "m.json"), str(tmp_path / "t"), "--yes"])
    assert code == 2
    assert "not approved" in capsys.readouterr().out
    assert not (tmp_path / "t").exists()
