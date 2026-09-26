"""Structured logging primitives.

Exposes a `ContextLogger` wrapper that:

- Stores correlation IDs and ad-hoc key/value context in module-level
  `contextvars.ContextVar` slots so they propagate naturally across
  asyncio tasks, including background work spawned via
  `BackgroundTaskRegistry`.
- Emits log records as JSON via `JSONFormatter` (file + stdout) and a
  human-readable formatter for console output when `API_DEBUG=true`.
- Provides `track_time` / `track_memory` async context managers that
  log a single completion record with elapsed milliseconds or RSS
  delta, suitable for wrapping route handlers.

`setup_logging` configures the root logger with a `TimedRotatingFileHandler`
keyed on UTC midnight so the active filename `dyvine.log` rotates
cleanly across day boundaries.
"""

import contextvars
import json
import logging
import logging.handlers
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import psutil

from .settings import settings

# Module-level ContextVars shared by every ``ContextLogger`` instance.
#
# Logging context (correlation IDs and arbitrary key/value pairs) is associated
# with the current asyncio Task / contextvars ``Context`` rather than a
# particular logger. Using module-level variables means a correlation ID set in
# a tool-call wrapper propagates to background tasks and to logger instances
# defined in other modules, as long as those tasks inherit the same context.
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dyvine_correlation_id", default=None
)
_context_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "dyvine_logging_context", default=None
)


def set_correlation_id(correlation_id: str | None) -> None:
    """Module-level setter so non-logger callers can update the ContextVar.

    Background-task scheduling needs to overwrite the inherited
    correlation ID with a fresh value before the coroutine runs so logs
    emitted by long-lived downloads do not appear under the spawning
    request's identifier.
    """
    _correlation_id_var.set(correlation_id)


def clear_logging_context() -> None:
    """Reset the request-scoped logging context dict."""
    _context_var.set({})


# Attribute names owned by ``logging.LogRecord`` itself. Anything else found
# on a record arrived through an ``extra=`` mapping and is forwarded into
# the JSON payload as structured context.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record into the JSON logging contract.

        Args:
            record: Standard library log record to serialize.

        Returns:
            JSON string containing the normalized log fields.
        """
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        # ``ContextLogger`` flattens request context (correlation_id,
        # user_id, operation_id, ...) into per-record attributes via
        # ``extra=``. Re-emit every attribute that is neither stdlib-owned
        # nor already normalized above; normalized keys always win on
        # collision. ``default=str`` keeps one exotic context value from
        # breaking log serialization.
        for key, value in vars(record).items():
            if key in _RESERVED_RECORD_ATTRS or key in log_data:
                continue
            log_data[key] = value

        return json.dumps(log_data, default=str)


def setup_logging(log_dir: str | Path | None = None) -> None:
    """Configure application logging.

    Always installs a console handler (human-readable when
    ``API_DEBUG=true``, JSON otherwise) and, only when ``log_dir`` is
    given, a :class:`logging.handlers.TimedRotatingFileHandler` keyed
    on UTC midnight so the active filename ``dyvine.log`` rotates
    cleanly across day boundaries. There is intentionally no default
    log directory: a CWD-relative ``logs/`` folder would scatter files
    depending on how the host process was launched.

    Only handlers owned by this module (tagged on install) are ever
    removed or closed, so host frameworks (uvicorn/pytest/...) keep
    their handlers and repeat calls reconcile instead of duplicating.
    """
    level = logging.DEBUG if settings.debug else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        if getattr(handler, "_dyvine_owned", False):
            root_logger.removeHandler(handler)
            handler.close()

    if log_dir is not None:
        resolved_dir = Path(log_dir).expanduser()
        resolved_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            resolved_dir / "dyvine.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setFormatter(JSONFormatter())
        file_handler._dyvine_owned = True  # type: ignore[attr-defined]
        root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    if settings.debug:
        console_handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        console_handler.setFormatter(JSONFormatter())
    console_handler._dyvine_owned = True  # type: ignore[attr-defined]
    root_logger.addHandler(console_handler)


class ContextLogger:
    """Logger with context and performance tracking."""

    def __init__(self, name: str) -> None:
        """Create a context-aware wrapper around a named logger.

        Args:
            name: Logger name passed to ``logging.getLogger``.
        """
        self.logger = logging.getLogger(name)

    @property
    def correlation_id(self) -> str | None:
        """Expose the current request-scoped correlation ID."""
        return _correlation_id_var.get()

    @property
    def context(self) -> dict[str, Any]:
        """Expose the current request-scoped logging context."""
        return dict(_context_var.get() or {})

    def set_correlation_id(self, correlation_id: str | None) -> None:
        """Store the current correlation ID for this context.

        Args:
            correlation_id: Request or background-task correlation ID, or ``None``.
        """
        _correlation_id_var.set(correlation_id)

    def add_context(self, **kwargs: Any) -> "ContextLogger":
        """Merge key/value pairs into the current logging context.

        Returns:
            This logger wrapper, allowing fluent context setup.

        Raises:
            ValueError: If a key collides with a ``logging.LogRecord``
                attribute -- the stdlib would raise ``KeyError`` later
                at emit time, so fail fast here instead.
        """
        for key in kwargs:
            if key in _RESERVED_RECORD_ATTRS:
                raise ValueError(
                    f"Logging context key {key!r} collides with a "
                    "logging.LogRecord attribute"
                )
        context = dict(_context_var.get() or {})
        context.update(kwargs)
        _context_var.set(context)
        return self

    def clear_context(self) -> None:
        """Reset request-scoped logging context."""
        _context_var.set({})

    @asynccontextmanager
    async def track_time(self, operation: str) -> AsyncGenerator[None, None]:
        """Log elapsed time for an asynchronous operation.

        Success logs ``<operation> completed`` at INFO; failure logs
        ``<operation> failed`` at ERROR with the exception attached and
        re-raises, so a failed operation never leaves a success record.

        Args:
            operation: Operation label included in the completion log record.
        """
        start = perf_counter()
        try:
            yield
        except BaseException as exc:
            duration_ms = (perf_counter() - start) * 1000
            self.error(
                f"{operation} failed",
                extra={
                    "duration_ms": round(duration_ms, 2),
                    "error": str(exc) or exc.__class__.__name__,
                },
                exc_info=True,
            )
            raise
        else:
            duration_ms = (perf_counter() - start) * 1000
            self.info(
                f"{operation} completed", extra={"duration_ms": round(duration_ms, 2)}
            )

    @asynccontextmanager
    async def track_memory(self, operation: str) -> AsyncGenerator[None, None]:
        """Log RSS memory delta for an asynchronous operation.

        Success logs ``<operation> memory usage`` at INFO; failure logs
        ``<operation> failed`` at ERROR and re-raises. The closing
        ``memory_info()`` read is guarded so it can never mask the
        original exception.

        Args:
            operation: Operation label included in the memory log record.
        """
        process = psutil.Process()
        start_mem = process.memory_info().rss
        try:
            yield
        except BaseException as exc:
            extra: dict[str, Any] = {"error": str(exc) or exc.__class__.__name__}
            try:
                end_mem = process.memory_info().rss
            except Exception as mem_exc:
                extra["memory_error"] = str(mem_exc)
            else:
                extra["memory_diff_mb"] = round((end_mem - start_mem) / 1024 / 1024, 2)
                extra["total_memory_mb"] = round(end_mem / 1024 / 1024, 2)
            self.error(f"{operation} failed", extra=extra, exc_info=True)
            raise
        else:
            end_mem = process.memory_info().rss
            self.info(
                f"{operation} memory usage",
                extra={
                    "memory_diff_mb": round((end_mem - start_mem) / 1024 / 1024, 2),
                    "total_memory_mb": round(end_mem / 1024 / 1024, 2),
                },
            )

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        exc_info: bool | tuple[Any, ...] | None = False,
        stacklevel: int = 3,
        **kwargs: Any,
    ) -> None:
        """Emit a log record with the current correlation and context fields.

        The caller's ``extra`` mapping is copied, never mutated, so a
        reused dict cannot leak one request's correlation ID into the
        next. Per-call keys win over ambient context on collision;
        keys colliding with ``logging.LogRecord`` attributes raise
        ``ValueError`` (the stdlib would raise ``KeyError`` at emit
        time otherwise).

        Args:
            level: Standard library logging level.
            msg: Log message format string.
            *args: Positional arguments forwarded to the wrapped logger.
            exc_info: Exception info attached to the record.
            stacklevel: Frames skipped so the record points at the
                caller (default skips this wrapper).
            **kwargs: Additional keyword arguments forwarded to the wrapped logger.

        Raises:
            ValueError: If a merged ``extra`` key collides with a
                ``logging.LogRecord`` attribute.
        """
        caller_extra = kwargs.pop("extra", None)
        merged: dict[str, Any] = dict(_context_var.get() or {})
        correlation_id = _correlation_id_var.get()
        if correlation_id:
            merged["correlation_id"] = correlation_id
        if caller_extra:
            merged.update(caller_extra)
        for key in merged:
            if key in _RESERVED_RECORD_ATTRS:
                raise ValueError(
                    f"Logging extra key {key!r} collides with a "
                    "logging.LogRecord attribute"
                )
        self.logger.log(
            level,
            msg,
            *args,
            exc_info=exc_info,
            extra=merged,
            stacklevel=stacklevel,
            **kwargs,
        )

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a DEBUG message with the current context."""
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an INFO message with the current context."""
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a WARNING message with the current context."""
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an ERROR message with the current context."""
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an ERROR message with active exception information."""
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)
