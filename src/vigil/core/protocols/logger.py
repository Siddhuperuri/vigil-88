"""The slice of a structured logger that engines and modules are allowed to depend on."""

from __future__ import annotations

from typing import Protocol


class LoggerLike(Protocol):
    def bind(self, **context: object) -> LoggerLike: ...

    def debug(self, event: str, **context: object) -> None: ...

    def info(self, event: str, **context: object) -> None: ...

    def warning(self, event: str, **context: object) -> None: ...

    def error(self, event: str, **context: object) -> None: ...
