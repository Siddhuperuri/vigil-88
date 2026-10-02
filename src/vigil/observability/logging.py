"""Structured logging (01 §7.2).

JSON lines to a rotating file; human-readable console. Context is bound, not formatted into
messages. A redaction processor strips credential patterns from every record, including
records emitted through stdlib `logging` by lower layers.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import TextIO

import structlog

from vigil.config.schema.system import LoggingConfig
from vigil.core.protocols.logger import LoggerLike
from vigil.core.redaction import redact_value

ROOT_LOGGER_NAME = "vigil"
_HANDLER_TAG = "_vigil_handler"
_BYTES_PER_MB = 1024 * 1024

EventDict = MutableMapping[str, object]


def redact_processor(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact sensitive keys and credential-looking text."""
    for key in list(event_dict):
        event_dict[key] = redact_value(event_dict[key], key=key)
    return event_dict


def _shared_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
        structlog.processors.StackInfoRenderer(),
    ]


def _console_renderer(fmt: str, stream: TextIO) -> structlog.typing.Processor:
    if fmt == "json":
        return structlog.processors.JSONRenderer()
    colors = fmt == "pretty" or (fmt == "auto" and stream.isatty())
    return structlog.dev.ConsoleRenderer(colors=colors)


def _formatter(renderer: structlog.typing.Processor) -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )


def shutdown_logging() -> None:
    """Remove and close every handler this module installed. Idempotent."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            stream = getattr(handler, "stream", None)
            if stream is None or not getattr(stream, "closed", False):
                handler.flush()  # a stream the host already closed (a test runner) cannot flush
            handler.close()


def configure_logging(
    cfg: LoggingConfig,
    *,
    log_file: Path | None,
    console_stream: TextIO | None = None,
) -> None:
    """Idempotent: reconfiguring replaces the handlers installed by a previous call."""
    shutdown_logging()
    stream = console_stream if console_stream is not None else sys.stderr

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(cfg.level)
    root.propagate = False

    if cfg.console:
        console = logging.StreamHandler(stream)
        console.setFormatter(_formatter(_console_renderer(cfg.console_format, stream)))
        setattr(console, _HANDLER_TAG, True)
        root.addHandler(console)

    if cfg.file_enabled and log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=cfg.rotate_mb * _BYTES_PER_MB,
            backupCount=cfg.retain_files,
            encoding="utf-8",
        )
        file_handler.setFormatter(_formatter(structlog.processors.JSONRenderer()))
        setattr(file_handler, _HANDLER_TAG, True)
        root.addHandler(file_handler)


def get_logger(name: str = ROOT_LOGGER_NAME, **context: object) -> LoggerLike:
    """A bound logger under the `vigil` hierarchy."""
    qualified = name if name.startswith(ROOT_LOGGER_NAME) else f"{ROOT_LOGGER_NAME}.{name}"
    logger: LoggerLike = structlog.stdlib.get_logger(qualified).bind(**context)
    return logger


def bind_context(**context: object) -> None:
    structlog.contextvars.bind_contextvars(**context)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
