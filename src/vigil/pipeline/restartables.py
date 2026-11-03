"""Which workers can be restarted on their own, and which cannot (pipeline/supervision.py)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from vigil.pipeline.camera_runtime import CameraRuntime
from vigil.pipeline.supervision import Entry, WorkerSupervisor
from vigil.pipeline.workers import ThreadWorker

# These hold state that swapping a thread would corrupt or lose.
NOT_RESTARTABLE: Mapping[str, str] = {
    "inference": "the detector owns the GPU and the batch in flight; restart the engine instead",
    "analysis": "analysis holds every camera's tracking state; restart the engine instead",
    "response": "it holds confirmed events not yet stored; restart the engine instead",
}
REPLAY_REFUSAL = "a replayed file cannot be restarted mid-run; restart the engine instead"


def register_restartables(
    supervisor: WorkerSupervisor,
    *,
    system_workers: list[ThreadWorker],
    cameras: Mapping[str, CameraRuntime],
    rebuild_capture: Callable[[CameraRuntime], ThreadWorker],
) -> None:
    """Describe every worker to the supervisor, once they have all been built."""
    for index, worker in enumerate(system_workers):
        refusal = NOT_RESTARTABLE.get(worker.name)

        def _install(fresh: ThreadWorker, i: int = index) -> None:
            system_workers[i] = fresh

        def _rebuild(i: int = index) -> ThreadWorker:
            return system_workers[i].fresh()

        def _get(i: int = index) -> ThreadWorker:
            return system_workers[i]

        entry = Entry(
            name=worker.name,
            get=_get,
            install=_install,
            rebuild=None if refusal else _rebuild,
        )
        if refusal:
            entry.refusal = refusal
        supervisor.register(entry)
    for camera_id, runtime in cameras.items():
        if runtime.worker is None:
            continue

        def _install_capture(fresh: ThreadWorker, rt: CameraRuntime = runtime) -> None:
            rt.worker = fresh

        def _rebuild_capture(rt: CameraRuntime = runtime) -> ThreadWorker:
            return rebuild_capture(rt)

        def _get_capture(rt: CameraRuntime = runtime) -> ThreadWorker | None:
            return rt.worker

        supervisor.register(
            Entry(
                name=f"capture:{camera_id}",
                get=_get_capture,
                install=_install_capture,
                rebuild=None if runtime.lossless else _rebuild_capture,
                refusal=REPLAY_REFUSAL,
            )
        )
