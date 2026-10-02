"""Runs the API on a background thread alongside the application (one process, one port)."""

from __future__ import annotations

import threading

import uvicorn
from fastapi import FastAPI

from vigil.core.errors import CapabilityError
from vigil.core.protocols.logger import LoggerLike

START_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 5.0
GRACEFUL_SHUTDOWN_S = 3  # lingering streams are cut after this, never waited on forever
_POLL_S = 0.02


class ApiServer:
    def __init__(self, app: FastAPI, *, host: str, port: int, logger: LoggerLike) -> None:
        self._log = logger.bind(stage="api")
        self._host, self._port = host, port
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, name="vigil-api", daemon=True)
        self._error: BaseException | None = None

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:  # noqa: BLE001 - thread boundary: surfaced by start()
            self._error = exc

    @property
    def port(self) -> int:
        """The bound port: differs from the requested one only when 0 (any free port) was asked."""
        for server in self._server.servers:
            for sock in server.sockets:
                return int(sock.getsockname()[1])
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}"

    def start(self) -> None:
        self._thread.start()
        waited = 0.0
        while not self._server.started:
            if not self._thread.is_alive():
                raise CapabilityError(
                    f"the API could not start on {self._host}:{self._port} "
                    f"(is the port already in use?): {self._error!r}"
                )
            if waited > START_TIMEOUT_S:
                self._server.should_exit = True
                raise CapabilityError("the API did not start within the timeout")
            threading.Event().wait(_POLL_S)
            waited += _POLL_S
        self._log.info("api listening", url=self.url)

    def stop(self) -> bool:
        """True if the server thread finished within the timeout. Idempotent."""
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(STOP_TIMEOUT_S)
        return not self._thread.is_alive()
