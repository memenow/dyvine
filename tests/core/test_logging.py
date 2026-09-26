"""Tests for structured logging and request-scoped context."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dyvine.core import logging as dyvine_logging
from dyvine.core.logging import ContextLogger, JSONFormatter


@pytest.fixture(autouse=True)
def _reset_logging_context_vars() -> None:
    """Reset module-level logging ContextVars between tests.

    ``ContextLogger`` stores the correlation ID and context dict in
    module-level ``ContextVar`` instances so they propagate across logger
    instances. Clearing them before each test avoids cross-test bleed.
    """
    dyvine_logging._correlation_id_var.set(None)
    dyvine_logging._context_var.set(None)


# ── JSONFormatter ────────────────────────────────────────────────────────


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    """Test helper for this module."""
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
    """Verify JSON formatter basic output."""
    fmt = JSONFormatter()
    record = _make_record("hi")
    output = fmt.format(record)
    data = json.loads(output)
    assert data["message"] == "hi"
    assert data["level"] == "INFO"
    assert "timestamp" in data
    assert "logger" in data


def test_json_formatter_includes_exception_info() -> None:
    """Verify JSON formatter includes exception info."""
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
    """Verify JSON formatter includes correlation ID."""
    fmt = JSONFormatter()
    record = _make_record("ctx")
    record.correlation_id = "abc-123"  # type: ignore[attr-defined]
    output = fmt.format(record)
    data = json.loads(output)
    assert data["correlation_id"] == "abc-123"


# ── ContextLogger ────────────────────────────────────────────────────────


def test_context_logger_init() -> None:
    """Verify context logger init."""
    cl = ContextLogger("mylogger")
    assert cl.correlation_id is None
    assert cl.context == {}


def test_context_logger_set_correlation_id() -> None:
    """Verify context logger set correlation ID."""
    cl = ContextLogger("test")
    cl.set_correlation_id("cid-1")
    assert cl.correlation_id == "cid-1"


def test_context_logger_add_context_returns_self() -> None:
    """Verify context logger add context returns self."""
    cl = ContextLogger("test")
    result = cl.add_context(k="v")
    assert result is cl


def test_context_logger_add_context_stores_values() -> None:
    """Verify context logger add context stores values."""
    cl = ContextLogger("test")
    cl.add_context(a=1, b=2)
    assert cl.context == {"a": 1, "b": 2}


def test_context_logger_log_includes_correlation_id() -> None:
    """Verify context logger log includes correlation ID."""
    cl = ContextLogger("test.corr")
    cl.set_correlation_id("cid-test")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg")
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["correlation_id"] == "cid-test"


def test_context_logger_log_includes_context() -> None:
    """Verify context logger log includes context."""
    cl = ContextLogger("test.ctx")
    cl.add_context(env="dev")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg")
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["env"] == "dev"


@pytest.mark.asyncio
async def test_track_time_logs_duration() -> None:
    """Verify track time logs duration."""
    cl = ContextLogger("test.time")
    with patch.object(cl.logger, "log") as mock_log:
        async with cl.track_time("op"):
            pass
        assert mock_log.called
        call_args = mock_log.call_args
        assert "duration_ms" in call_args[1]["extra"]


@pytest.mark.asyncio
async def test_track_memory_logs_memory_diff() -> None:
    """Verify track memory logs memory diff."""
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
    """Verify context logger exception sets exc info."""
    cl = ContextLogger("test.exc")
    with patch.object(cl.logger, "log") as mock_log:
        cl.exception("fail")
        _, kwargs = mock_log.call_args
        assert kwargs["exc_info"] is True


@pytest.mark.asyncio
async def test_context_logger_uses_task_local_context() -> None:
    """Verify context logger uses task local context."""
    cl = ContextLogger("test.contextvars")

    async def emit(correlation_id: str) -> str | None:
        """Test helper for test_context_logger_uses_task_local_context."""
        cl.set_correlation_id(correlation_id)
        await asyncio.sleep(0)
        return cl.correlation_id

    correlation_one, correlation_two = await asyncio.gather(
        emit("cid-1"), emit("cid-2")
    )
    assert correlation_one == "cid-1"
    assert correlation_two == "cid-2"


def test_context_logger_shares_correlation_id_across_instances() -> None:
    """Correlation IDs are shared across ``ContextLogger`` instances.

    The caller sets a single correlation ID that every logger (including
    those instantiated by background tasks in different modules) must see,
    so the module-level ``ContextVar`` is shared across instances.
    """
    # The autouse ``_reset_logging_context_vars`` fixture already clears the
    # module-level ContextVars between tests, so no explicit teardown is
    # required here.
    first = ContextLogger("test.shared.one")
    second = ContextLogger("test.shared.two")

    first.set_correlation_id("cid-shared")
    assert second.correlation_id == "cid-shared"

    first.add_context(tenant="acme")
    assert second.context["tenant"] == "acme"


def test_context_logger_does_not_mutate_caller_extra() -> None:
    """The caller's ``extra`` dict must be copied, never written into.

    Writing ``correlation_id`` into the caller's mapping leaks one
    request's ID into the next when the dict is reused.
    """
    cl = ContextLogger("test.nomutate")
    cl.set_correlation_id("cid-1")
    caller_extra = {"k": "v"}
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg", extra=caller_extra)
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["correlation_id"] == "cid-1"
        assert kwargs["extra"]["k"] == "v"
    assert caller_extra == {"k": "v"}


def test_context_logger_accepts_explicit_none_extra() -> None:
    """An explicit ``extra=None`` must not crash the emit path."""
    cl = ContextLogger("test.noneextra")
    cl.set_correlation_id("cid-1")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg", extra=None)
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["correlation_id"] == "cid-1"


def test_context_logger_rejects_reserved_extra_keys() -> None:
    """Reserved ``LogRecord`` keys fail fast instead of at emit time."""
    cl = ContextLogger("test.reserved")
    with pytest.raises(ValueError, match="collides"):
        cl.info("msg", extra={"msg": "hijack"})
    with pytest.raises(ValueError, match="collides"):
        cl.add_context(levelname="hijack")


def test_context_logger_per_call_extra_wins_over_context() -> None:
    """An explicit per-call key beats the ambient context value."""
    cl = ContextLogger("test.precedence")
    cl.add_context(env="ambient")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg", extra={"env": "explicit"})
        _, kwargs = mock_log.call_args
        assert kwargs["extra"]["env"] == "explicit"


def test_context_logger_forwards_stacklevel() -> None:
    """Records must point at the caller, not the wrapper internals."""
    cl = ContextLogger("test.stacklevel")
    with patch.object(cl.logger, "log") as mock_log:
        cl.info("msg")
        _, kwargs = mock_log.call_args
        assert kwargs["stacklevel"] == 3


@pytest.mark.asyncio
async def test_track_time_logs_failure_and_reraises() -> None:
    """A failing operation must not leave a success record behind."""
    cl = ContextLogger("test.timefail")
    with patch.object(cl.logger, "log") as mock_log:
        with pytest.raises(RuntimeError, match="nope"):
            async with cl.track_time("op"):
                raise RuntimeError("nope")
        level, msg = mock_log.call_args[0][:2]
        extra = mock_log.call_args[1]["extra"]
        assert level == logging.ERROR
        assert msg == "op failed"
        assert extra["error"] == "nope"
        assert "duration_ms" in extra
        assert mock_log.call_args[1]["exc_info"] is True


@pytest.mark.asyncio
async def test_track_memory_logs_failure_and_reraises() -> None:
    """Memory tracking reports failures without masking the original error."""
    mock_process = MagicMock()
    mem_start = MagicMock()
    mem_start.rss = 100 * 1024 * 1024
    mock_process.memory_info = MagicMock(side_effect=[mem_start, OSError("gone")])

    cl = ContextLogger("test.memfail")
    with (
        patch("psutil.Process", return_value=mock_process),
        patch.object(cl.logger, "log") as mock_log,
    ):
        with pytest.raises(ValueError, match="original"):
            async with cl.track_memory("op"):
                raise ValueError("original")
        level, msg = mock_log.call_args[0][:2]
        extra = mock_log.call_args[1]["extra"]
        assert level == logging.ERROR
        assert msg == "op failed"
        assert extra["error"] == "original"
        assert extra["memory_error"] == "gone"


@pytest.fixture
def _preserve_root_logger() -> Iterator[logging.Logger]:
    """Save and restore root handlers/level around ``setup_logging``."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_dyvine_owned", False):
                root.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level)


def test_setup_logging_preserves_foreign_handlers(_preserve_root_logger) -> None:
    """Host-owned handlers must survive ``setup_logging``."""
    from dyvine.core.logging import setup_logging

    foreign = logging.StreamHandler()
    logging.getLogger().addHandler(foreign)
    setup_logging()
    assert foreign in logging.getLogger().handlers
    assert foreign not in [
        h for h in logging.getLogger().handlers if getattr(h, "_dyvine_owned", False)
    ]
    logging.getLogger().removeHandler(foreign)


def test_setup_logging_is_idempotent(_preserve_root_logger) -> None:
    """Repeat calls reconcile owned handlers instead of duplicating."""
    from dyvine.core.logging import setup_logging

    setup_logging()
    setup_logging()
    owned = [
        h for h in logging.getLogger().handlers if getattr(h, "_dyvine_owned", False)
    ]
    assert len(owned) == 1  # console only; no file handler without log_dir


def test_setup_logging_writes_file_only_with_log_dir(
    _preserve_root_logger: logging.Logger, tmp_path: Path
) -> None:
    """File rotation is opt-in via an explicit directory."""
    from dyvine.core.logging import setup_logging

    setup_logging(log_dir=tmp_path / "logs")
    assert (tmp_path / "logs" / "dyvine.log").exists()


def test_json_formatter_emits_flattened_context_fields() -> None:
    """End-to-end: context fields forwarded via extra must reach JSON output."""
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    std_logger = logging.getLogger("test.e2e-context")
    std_logger.addHandler(handler)
    std_logger.setLevel(logging.INFO)
    try:
        ctx = ContextLogger("test.e2e-context")
        ctx.set_correlation_id("cid-e2e")
        ctx.add_context(user_id="user-1", operation_id="op-1")
        try:
            ctx.info("hello")
        finally:
            ctx.set_correlation_id(None)
            ctx.clear_context()
    finally:
        std_logger.removeHandler(handler)
    data = json.loads(stream.getvalue().strip())
    assert data["correlation_id"] == "cid-e2e"
    assert data["user_id"] == "user-1"
    assert data["operation_id"] == "op-1"


async def test_track_memory_reports_diff_on_failure_when_read_succeeds() -> None:
    """A failed op still reports its memory delta when the closing read works."""
    mock_process = MagicMock()
    mem_start = MagicMock()
    mem_start.rss = 100 * 1024 * 1024
    mem_end = MagicMock()
    mem_end.rss = 110 * 1024 * 1024
    mock_process.memory_info = MagicMock(side_effect=[mem_start, mem_end])

    cl = ContextLogger("test.memfailok")
    with (
        patch("psutil.Process", return_value=mock_process),
        patch.object(cl.logger, "log") as mock_log,
    ):
        with pytest.raises(ValueError, match="original"):
            async with cl.track_memory("op"):
                raise ValueError("original")
        level, msg = mock_log.call_args[0][:2]
        extra = mock_log.call_args[1]["extra"]
        assert level == logging.ERROR
        assert msg == "op failed"
        assert extra["error"] == "original"
        assert "memory_diff_mb" in extra
        assert "total_memory_mb" in extra
