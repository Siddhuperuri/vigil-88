from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from vigil.config.schema.system import LoggingConfig
from vigil.observability.logging import (
    ROOT_LOGGER_NAME,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    shutdown_logging,
)


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]


def setup(level: str = "INFO", **kw: object) -> io.StringIO:
    stream = io.StringIO()
    configure_logging(
        LoggingConfig(level=level, console_format="json", file_enabled=False, **kw),  # type: ignore[arg-type]
        log_file=None,
        console_stream=stream,
    )
    return stream


def test_records_are_json_with_standard_fields() -> None:
    out = setup()
    get_logger("test").info("hello", camera_id="cam-a", frames=3)
    (rec,) = lines(out)
    assert rec["event"] == "hello" and rec["level"] == "info"
    assert rec["camera_id"] == "cam-a" and rec["frames"] == 3
    assert rec["logger"] == "vigil.test"
    assert str(rec["timestamp"]).endswith("Z")  # UTC ISO-8601


def test_bound_context_is_attached_to_every_record() -> None:
    out = setup()
    log = get_logger("test", stage="ingest").bind(camera_id="cam-a")
    log.info("one")
    log.info("two", extra="x")
    one, two = lines(out)
    assert one["stage"] == two["stage"] == "ingest"
    assert one["camera_id"] == two["camera_id"] == "cam-a"
    assert "extra" not in one and two["extra"] == "x"


def test_contextvars_are_merged_and_clearable() -> None:
    out = setup()
    bind_context(correlation_id="abc")
    get_logger("t").info("with")
    clear_context()
    get_logger("t").info("without")
    with_, without = lines(out)
    assert with_["correlation_id"] == "abc" and "correlation_id" not in without


def test_level_filtering() -> None:
    out = setup(level="WARNING")
    log = get_logger("t")
    log.info("quiet")
    log.warning("loud")
    assert [r["event"] for r in lines(out)] == ["loud"]


def test_credentials_are_redacted_from_messages_and_fields() -> None:
    out = setup()
    get_logger("t").warning(
        "open rtsp://admin:hunter2@10.0.0.5/s failed",
        password="hunter2",
        api_key="k-123",
        nested={"token": "t-9", "ok": 1},
        url="rtsp://u:p@h/x",
    )
    text = out.getvalue()
    for secret in ("hunter2", "k-123", "t-9", "u:p@"):
        assert secret not in text
    (rec,) = lines(out)
    assert rec["password"] == "***" and rec["nested"] == {"token": "***", "ok": 1}


def test_stdlib_logging_from_lower_layers_is_routed_and_redacted() -> None:
    out = setup()
    logging.getLogger("vigil.core.bus").warning("failed with token=abcd1234secret")
    (rec,) = lines(out)
    assert "abcd1234secret" not in json.dumps(rec) and rec["logger"] == "vigil.core.bus"


def test_exceptions_are_rendered() -> None:
    out = setup()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("t").error("failed", exc_info=True)
    (rec,) = lines(out)
    assert "RuntimeError: boom" in str(rec["exception"])


def test_reconfiguring_replaces_handlers_instead_of_stacking_them() -> None:
    def ours() -> int:
        return sum(
            hasattr(h, "_vigil_handler") for h in logging.getLogger(ROOT_LOGGER_NAME).handlers
        )

    setup()
    first = ours()
    out = setup()
    assert ours() == first == 1
    get_logger("t").info("once")
    assert len(lines(out)) == 1  # not duplicated


def test_shutdown_removes_every_handler_it_installed() -> None:
    setup()
    shutdown_logging()
    shutdown_logging()  # idempotent
    ours = [h for h in logging.getLogger(ROOT_LOGGER_NAME).handlers if hasattr(h, "_vigil_handler")]
    assert ours == []  # pytest's own capture handlers are not ours to remove


def test_file_handler_writes_json_lines_and_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "vigil.jsonl"
    configure_logging(LoggingConfig(console=False), log_file=target)
    get_logger("t").info("to-file", n=1)
    shutdown_logging()
    (rec,) = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
    assert rec["event"] == "to-file" and rec["n"] == 1


def test_console_can_be_disabled(tmp_path: Path) -> None:
    out = io.StringIO()
    configure_logging(
        LoggingConfig(console=False, file_enabled=False), log_file=None, console_stream=out
    )
    get_logger("t").error("silent")
    assert out.getvalue() == ""


def test_plain_console_format_is_human_readable() -> None:
    out = io.StringIO()
    configure_logging(
        LoggingConfig(console_format="plain", file_enabled=False), log_file=None, console_stream=out
    )
    get_logger("t").info("hello world", camera_id="cam-a")
    text = out.getvalue()
    assert "hello world" in text and "camera_id" in text and not text.lstrip().startswith("{")


@pytest.mark.parametrize("name", ["pipeline.app", "vigil.ingest", "vigil"])
def test_loggers_live_under_the_vigil_hierarchy(name: str) -> None:
    out = setup()
    get_logger(name).info("x")
    assert str(lines(out)[0]["logger"]).startswith("vigil")
