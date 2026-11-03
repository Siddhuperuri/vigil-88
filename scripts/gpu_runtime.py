"""Fetches the NVIDIA runtime as an explicit, hash-verified download. Refuses without approval.

    python scripts/gpu_runtime.py <manifest.json> <target dir> [--yes]

This is preparation, not a decision. It does nothing until `release/decisions.yaml` says the
runtime is distributed by download (plan decision 2) AND the manifest records, for every file, an
https URL, an exact size, a SHA-256 and a named person's licence review. It downloads nothing
without `--yes`, never follows a redirect to another scheme, stops at the recorded size, and
moves a file into place only after its hash matches. A file that fails verification is deleted.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from scripts import release_check

CHUNK = 1 << 20


class Refused(Exception):
    """The download was not allowed or did not verify."""


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise Refused(f"redirect to a non-https address was refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify_file(path: Path, *, sha256: str, size_bytes: int) -> None:
    """Raise `Refused` unless the file is exactly the recorded size and hash."""
    if path.stat().st_size != size_bytes:
        raise Refused(f"{path.name}: size {path.stat().st_size} is not the recorded {size_bytes}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise Refused(f"{path.name}: SHA-256 does not match the manifest")


def fetch(item: dict[str, object], target: Path) -> Path:
    name = Path(str(item["name"])).name  # a name, never a path: nothing escapes the target
    size = int(str(item["size_bytes"]))
    part = target / f"{name}.part"
    final = target / name
    target.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_HttpsOnly)
    try:
        with opener.open(str(item["url"]), timeout=60) as reply, part.open("wb") as out:
            received = 0
            while chunk := reply.read(CHUNK):
                received += len(chunk)
                if received > size:
                    raise Refused(f"{name}: more bytes arrived than the recorded {size}")
                out.write(chunk)
        verify_file(part, sha256=str(item["sha256"]), size_bytes=size)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(final)
    return final


def main(argv: list[str]) -> int:
    manifest_path, target = Path(argv[0]), Path(argv[1])
    blocked = [b for b in release_check.blockers() if b.decision == "gpu_runtime"]
    decisions = release_check._load(release_check.ROOT).get("gpu_runtime", {})
    if decisions.get("status") != "download" or blocked:
        print("Refused: the NVIDIA runtime is not approved for download.")
        for b in blocked:
            print(f"  - {b.reason}")
        return 2
    if "--yes" not in argv:
        print("Approved by the manifest, but nothing is downloaded without --yes.")
        return 3
    for item in json.loads(manifest_path.read_text(encoding="utf-8"))["files"]:
        try:
            print(f"fetched {fetch(item, target)}")
        except Refused as exc:
            print(f"Refused: {exc}")
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
