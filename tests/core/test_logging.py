from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from dyvine.core.logging import ContextLogger, JSONFormatter

# ── JSONFormatter ────────────────────────────────────────────────────────


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    return record


def test_json_formatter_basic_output() -> None:
    fmt = JSONFormatter()
    record = _make_record("hi")
    output = fmt.format(record)
    data = json.loads(output)
    assert data["message"] == "hi"
    assert data["level"] == "INFO"
    assert "timestamp" in data
    assert "logger" in data


def test_json_formatter_includes_exception_info() -> None:
    fmt = JSONFormatter()
    try:
        raise ValueError("test-err")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
        record = _make_record("err", logging.ERROR, exc_info)
        output = fmt.format(record)
    data = json.loads(output)
    assert "exception" in data
    assert data["exception"]["type"] == "ValueError"


def test_json_formatter_includes_correlation_id() -> None:
    fmt = JSONFormatter()
    record = _make_record("ctx")
    record.correlation_id = "abc-123"  # type: ignore[attr-defined]
    output = fmt.format(record)
    data = json.loads(output)
    assert data["correlation_id"] == "abc-123"


# ── ContextLogger ────────────────────────────────────────────────────────


def test_context_logger_init() -> None:
    cl = ContextLogger("mylogger")
    assert cl.correlation_id is None
    assert cl.context == {}


def test_context_logger_set_correlation_id() -> None:
    cl = ContextLogger("test")
    cl.set_correlation_id("cid-1")
    assert cl.correlation_id == "cid-1"


def test_context_logger_add_context_returns_self() -> None:
    cl = ContextLogger("test")
    result = cl.add_context(k="v")
    assert result is cl


def test_context_logger_add_context_stores_values() -> None:
    cl = ContextLogger("test")
    cl.add_context(a=1, b=2)
    assert cl.context == {"a": 1, "b": 2}


def test_context_logger_log_includes_correlation_id() -> None:
    cl = ContextLogger("test.corr")
    cl.set_correlation_id("cid-test")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg")
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["correlation_id"] == "cid-test"


def test_context_logger_log_includes_context() -> None:
    cl = ContextLogger("test.ctx")
    cl.add_context(env="dev")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg")
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["env"] == "dev"


@pytest.mark.asyncio
async def test_track_time_logs_duration() -> None:
    cl = ContextLogger("test.time")
    with patch.object(cl.logger, "log") as mock_log:
        async with cl.track_time("op"):
            pass
        assert mock_log.called
        call_args = mock_log.call_args
        assert "duration_ms" in call_args[1]["extra"]


@pytest.mark.asyncio
async def test_track_memory_logs_memory_diff() -> None:
    mock_process = MagicMock()
    mem_start = MagicMock()
    mem_start.rss = 100 * 1024 * 1024
    mem_end = MagicMock()
    mem_end.rss = 110 * 1024 * 1024
    mock_process.memory_info = MagicMock(side_effect=[mem_start, mem_end])

    cl = ContextLogger("test.mem")
    with (
        patch("psutil.Process", return_value=mock_process),
        patch.object(cl.logger, "log") as mock_log,
    ):
        async with cl.track_memory("op"):
            pass
        assert mock_log.called
        extra = mock_log.call_args[1]["extra"]
        assert "memory_diff_mb" in extra
        assert "total_memory_mb" in extra


def test_context_logger_exception_sets_exc_info() -> None:
    cl = ContextLogger("test.exc")
    with patch.object(cl.logger, "log") as mock_log:
        cl.exception("fail")
        _, kwargs = mock_log.call_args
        assert kwargs["exc_info"] is True


@pytest.mark.asyncio
async def test_context_logger_uses_task_local_context() -> None:
    cl = ContextLogger("test.contextvars")

    async def emit(correlation_id: str) -> str | None:
        cl.set_correlation_id(correlation_id)
        await asyncio.sleep(0)
        return cl.correlation_id

    correlation_one, correlation_two = await asyncio.gather(
        emit("cid-1"), emit("cid-2")
    )
    assert correlation_one == "cid-1"
    assert correlation_two == "cid-2"
