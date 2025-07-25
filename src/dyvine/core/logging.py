"""Logging configuration for Dyvine."""

import json
import logging
import logging.handlers
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from .settings import settings


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
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

        for attr in ["correlation_id", "extra"]:
            if hasattr(record, attr):
                log_data[attr] = getattr(record, attr)

        return json.dumps(log_data)


def setup_logging() -> None:
    """Configure application logging."""
    level = logging.DEBUG if settings.debug else logging.INFO

    # Ensure logs directory exists
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    # File handler with rotation
    log_file = logs_dir / f"dyvine-{datetime.now():%Y-%m-%d}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"  # 10MB
    )
    file_handler.setFormatter(JSONFormatter())
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
    root_logger.addHandler(console_handler)


class ContextLogger:
    """Logger with context and performance tracking."""

    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)
        self.correlation_id: str | None = None
        self.context: dict[str, Any] = {}

    def set_correlation_id(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id

    def add_context(self, **kwargs: Any) -> "ContextLogger":
        self.context.update(kwargs)
        return self

    @asynccontextmanager
    async def track_time(self, operation: str) -> AsyncGenerator[None, None]:
        start = perf_counter()
        try:
            yield
        finally:
            duration_ms = (perf_counter() - start) * 1000
            self.info(
                f"{operation} completed", extra={"duration_ms": round(duration_ms, 2)}
            )

    @asynccontextmanager
    async def track_memory(self, operation: str) -> AsyncGenerator[None, None]:
        import psutil

        process = psutil.Process()
        start_mem = process.memory_info().rss
        try:
            yield
        finally:
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
        exc_info: bool = False,
        **kwargs: Any,
    ) -> None:
        extra = kwargs.pop("extra", {})
        if self.correlation_id:
            extra["correlation_id"] = self.correlation_id
        if self.context:
            extra.update(self.context)
        self.logger.log(level, msg, *args, exc_info=exc_info, extra=extra, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)
