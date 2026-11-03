"""One incident as a zip: its record, its evidence files, and a manifest saying what each is.

The point of a bundle is that someone outside this system can check it. Every file carries the hash
recorded when it was captured and the hash found now; a file that no longer matches is included and
marked, and a file that is gone is listed as missing rather than left out, so the bundle never
claims more than it holds (05 §6). Nothing here is signed: the hashes show a file differs from what
was recorded, not that the recording itself is genuine.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

from vigil.core.errors import PersistenceError
from vigil.domain.evidence import Evidence
from vigil.response.paths import resolve_relative

SPOOL_BYTES = 32 * 1024 * 1024  # in memory up to this, then on disk
README = """VIGIL-88 incident bundle

incident.json   the incident as the console shows it
MANIFEST.json   every evidence file: the hash recorded at capture, the hash found now, and a status
evidence/       the files themselves

status is one of
  ok         the file matches the hash recorded at capture
  mismatch   the file differs from what was recorded; it is included so it can be examined
  missing    the file is no longer on disk (retention removed it, or it was lost)

The hashes show whether a file changed after capture. They do not show that the recording is
genuine, and nothing in this bundle is signed.
"""


def _copy_hashed(path: Path, archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            dest.write(chunk)
    return digest.hexdigest()


def build_bundle(
    *, incident_json: str, records: Iterable[Evidence], evidence_root: Path
) -> tuple[IO[bytes], list[dict[str, Any]]]:
    """A rewound temporary file holding the zip, and the manifest entries."""
    out = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115 - returned to the caller
    manifest: list[dict[str, Any]] = []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("incident.json", incident_json)
        for record in records:
            entry: dict[str, Any] = {
                "evidence_id": record.evidence_id,
                "kind": record.kind.value,
                "media_type": record.media_type,
                "recorded_sha256": record.sha256,
                "found_sha256": None,
                "status": "missing",
                "file": None,
            }
            try:
                path = resolve_relative(evidence_root, record.relative_path)
            except PersistenceError:
                path = None
            if path is not None and path.is_file():
                name = f"evidence/{record.evidence_id}-{path.name}"
                # Hash the very bytes that go into the archive, so a file changing mid-read cannot
                # leave a hash that describes something other than what was written.
                try:
                    found = _copy_hashed(path, archive, name)
                except OSError:
                    found = None
                if found is not None:
                    entry.update(
                        found_sha256=found,
                        status="ok" if found == record.sha256 else "mismatch",
                        file=name,
                    )
            manifest.append(entry)
        archive.writestr("MANIFEST.json", json.dumps({"files": manifest}, indent=2))
        archive.writestr("README.txt", README)
    out.seek(0)
    return out, manifest
