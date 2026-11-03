"""What an operator needs to judge whether the machine is healthy: recent logs and storage.

Both are read-only and bounded. The log tail reads at most `TAIL_BYTES` from the end of the file,
so a log of any size costs the same; storage walks the evidence folder with a ceiling on entries and
says `truncated` when it bites, rather than stalling a request on a huge tree. No absolute path
leaves the machine: a client is told sizes and file names, never where the data directory is.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from vigil.api.session import audit_event, requires
from vigil.api.system_info import (
    MAX_LINES,
    LogsOut,
    StorageOut,
    storage_of,
    tail_lines,
)
from vigil.auth.roles import Permission
from vigil.config.paths import ResolvedPaths
from vigil.pipeline.app import Application


class WorkerOut(BaseModel):
    name: str
    alive: bool
    crashed: bool
    error: str | None
    heartbeat_age_ms: float | None
    restartable: bool


class RestartOut(BaseModel):
    ok: bool
    detail: str


def build_router(*, paths: ResolvedPaths, application: Application | None = None) -> APIRouter:
    router = APIRouter(prefix="/system", tags=["system"])

    if application is not None:

        @router.get(
            "/workers", response_model=list[WorkerOut], dependencies=[requires(Permission.VIEW)]
        )
        def workers() -> list[WorkerOut]:
            """Every worker with whether it can be restarted on its own."""
            return [
                WorkerOut(
                    name=w.name,
                    alive=w.alive,
                    crashed=w.crashed,
                    error=w.error,
                    heartbeat_age_ms=w.heartbeat_age_ms,
                    restartable=application.supervisor.restartable(w.name),
                )
                for w in application.snapshot().workers
            ]

        @router.get("/diagnostics", dependencies=[requires(Permission.DIAGNOSTICS)])
        def diagnostics(request: Request) -> StreamingResponse:
            """A zip an administrator can send to get help: versions, health, redacted
            configuration and logs. Never frames, evidence, accounts or credentials."""
            from vigil.api.diagnostics import build_bundle

            archive, manifest = build_bundle(application)

            def chunks() -> Iterator[bytes]:
                try:
                    while block := archive.read(1024 * 1024):
                        yield block
                finally:
                    archive.close()

            audit_event(request, "diagnostics.exported", detail={"files": len(manifest) + 1})
            return StreamingResponse(
                chunks(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": 'attachment; filename="vigil88-diagnostics.zip"',
                    "Cache-Control": "no-store",
                },
            )

        @router.post(
            "/workers/{name}/restart",
            response_model=RestartOut,
            dependencies=[requires(Permission.CONFIGURE)],
        )
        def restart_worker(name: str, request: Request) -> RestartOut:
            """Replace one worker. Refused, with the reason, when that would be unsafe."""
            if name not in application.supervisor.names():
                raise HTTPException(status_code=404, detail=f"no worker named {name!r}")
            result = application.supervisor.restart(name)
            audit_event(
                request,
                "worker.restart",
                target=name,
                detail={"ok": result.ok, "detail": result.detail},
            )
            return RestartOut(ok=result.ok, detail=result.detail)

    @router.get("/logs", response_model=LogsOut, dependencies=[requires(Permission.DIAGNOSTICS)])
    def logs(
        lines: Annotated[int, Query(ge=1, le=MAX_LINES)] = 200,
        level: Annotated[str | None, Query(pattern="^(debug|info|warning|error|critical)$")] = None,
    ) -> LogsOut:
        """Recent engine log records. Redaction happens where they are written, not here."""
        return tail_lines(paths.log_file, lines=lines, level=level)

    @router.get("/storage", response_model=StorageOut, dependencies=[requires(Permission.VIEW)])
    def storage() -> StorageOut:
        return storage_of(paths)

    return router
