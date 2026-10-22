"""Ordered teardown (01 §4, §8).

Shutdown order is not housekeeping; each step below unblocks the next, and getting the order
wrong produces a hang or a torn write rather than an error anyone can read. So it lives in one
small file with the reasoning attached:

  1. **Ask every worker to stop.** Cooperative, because a thread blocked in a decode cannot be
     interrupted safely (ADR 0011).
  2. **Close every frame channel and the bounded queues.** A capture worker waiting to hand a
     frame to a full queue is waiting on a condition nobody will signal once the consumer has
     stopped; closing it is what releases it. The same applies to the inference worker, which
     blocks on a full analysis queue for a PACED camera (01 §9) -- so that queue is closed here
     too, which is what makes the lossless handoff safe to wait on without a timeout.
  3. **Close the stream hub**, which wakes connected viewers so the HTTP server can finish
     rather than holding the process open for a browser.
  4. **Join within the budget**, and name any straggler. A straggler is reported, never waited
     on forever: `docs/LIMITATIONS.md` records that a blocked webcam read cannot be cancelled.
  5. **Flush and close the response stack** -- but only when nothing is still running, because
     a response worker mid-write would be writing to a connection about to close.
  6. **Close the detector**, then NVML last, and only when no worker could still be reading it.

The result is a report rather than a log line, so a caller (the CLI, a test, the selftest) can
decide what an unclean shutdown means for its exit code.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from vigil.core.clock import Clock
from vigil.core.errors import VigilError
from vigil.core.protocols.logger import LoggerLike
from vigil.core.units import MS_PER_S, NS_PER_S, ns_to_ms
from vigil.ingest.buffers import FrameChannel
from vigil.observability.metrics import MetricsRegistry
from vigil.pipeline.response_stack import ResponseStack
from vigil.pipeline.snapshot import ShutdownReport
from vigil.pipeline.workers import ThreadWorker


def stop_workers(
    workers: Sequence[ThreadWorker],
    channels: Sequence[FrameChannel],
    *,
    close_hub: Callable[[], None] | None,
    close_queues: Sequence[Callable[[], None]] = (),
) -> None:
    """Steps 1 to 3: request a stop, then release anything a worker could be blocked on."""
    for worker in workers:
        worker.request_stop()
    for channel in channels:
        channel.close()
    for close in close_queues:
        close()
    if close_hub is not None:
        close_hub()


def join_workers(
    workers: Sequence[ThreadWorker], *, timeout_ms: int, clock: Clock
) -> tuple[str, ...]:
    """Step 4. Returns the names of workers still running when the budget ran out."""
    deadline_ns = clock.monotonic_ns() + int(timeout_ms / MS_PER_S * NS_PER_S)
    stragglers: list[str] = []
    for worker in workers:
        remaining_s = max(0.0, (deadline_ns - clock.monotonic_ns()) / NS_PER_S)
        if not worker.join(remaining_s):
            stragglers.append(worker.name)
    return tuple(stragglers)


def close_response(
    response: ResponseStack | None,
    stragglers: Sequence[str],
    *,
    logger: LoggerLike,
    metrics: MetricsRegistry,
) -> int:
    """Step 5. Returns the number of queued ticks written during the flush.

    With a straggler the database is left open deliberately: a torn write is worse than a file
    handle that the process exit will close anyway.
    """
    if response is None:
        return 0
    if stragglers:
        logger.error(
            "response_not_flushed",
            stragglers=list(stragglers),
            reason="a worker did not stop, so the incident store was left open",
        )
        return 0
    drained = 0
    try:
        drained = response.worker.flush()
    except VigilError as exc:
        metrics.counter("vigil_response_errors_total", step="shutdown").inc()
        logger.error("response_flush_failed", error=exc.message)
    if drained:
        logger.info("response_flushed_at_shutdown", ticks=drained)
    response.close()
    return drained


def report(
    stragglers: Sequence[str], *, started_ns: int, clock: Clock, logger: LoggerLike
) -> ShutdownReport:
    result = ShutdownReport(
        clean=not stragglers,
        stragglers=tuple(stragglers),
        duration_ms=ns_to_ms(clock.monotonic_ns() - started_ns),
    )
    if stragglers:
        logger.error("shutdown incomplete", stragglers=list(stragglers))
    else:
        logger.info("application stopped", duration_ms=round(result.duration_ms, 1))
    return result
