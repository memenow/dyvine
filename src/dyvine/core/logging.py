"""Module for logging configuration in the Dyvine package.

This module provides structured JSON logging with the following features:
- Request correlation tracking
- Automatic log rotation
- Development/production formatting
- Error tracking
- Performance metrics
"""

import logging
import logging.handlers
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, AsyncGenerator
from contextlib import asynccontextmanager
from time import perf_counter

from .settings import settings

class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string."""
        log_object: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add exception info if present
        if record.exc_info:
            exc_type = record.exc_info[0]
            exc_value = record.exc_info[1]
            log_object["exception"] = {
                "type": exc_type.__name__ if exc_type else None,
                "message": str(exc_value) if exc_value else None,
                "traceback": self.formatException(record.exc_info) if record.exc_info else None
            }
            
        # Add extra fields from record
        if hasattr(record, "correlation_id"):
            log_object["correlation_id"] = record.correlation_id
            
        if hasattr(record, "extra"):
            log_object["extra"] = record.extra
            
        return json.dumps(log_object)

def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> logging.handlers.RotatingFileHandler:
    """Configure application logging and return the file handler.
    
    Returns:
        file_handler: The configured rotating file handler.
    """
    # Determine log level
    level = (
        getattr(logging, log_level.upper())
        if log_level
        else logging.DEBUG if settings.debug else logging.INFO
    )

    # Set up logging directory and file
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    if not log_file:
        timestamp = datetime.now().strftime("%Y-%m-%d")
        log_file = str(logs_dir / f"dyvine-{timestamp}.log")

    # Create JSON formatter
    json_formatter = JSONFormatter()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        if hasattr(handler, 'close'):
            handler.close()

    # Configure rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setFormatter(json_formatter)
    root_logger.addHandler(file_handler)

    # Configure console handler with appropriate formatting
    console_handler = logging.StreamHandler(sys.stdout)
    if settings.debug:
        # Use more readable format for development
        console_format = logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        console_handler.setFormatter(console_format)
    else:
        # Use JSON format for production
        console_handler.setFormatter(json_formatter)
    root_logger.addHandler(console_handler)

    # Log startup configuration
    logging.info(
        "Logging system initialized",
        extra={
            "log_level": logging.getLevelName(level),
            "log_file": log_file,
            "max_size": f"{max_bytes/1024/1024:.1f}MB",
            "backup_count": backup_count,
            "debug_mode": settings.debug
        }
    )
    return file_handler

class ContextLogger:
    """Logger with persistent context and performance tracking."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the ContextLogger.

        Args:
            logger: The pre-configured logger instance.
        """
        self.logger = logger
        self.correlation_id: Optional[str] = None
        self.context: Dict[str, Any] = {}
        self._timers: Dict[str, float] = {}

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set the correlation ID for request tracking."""
        self.correlation_id = correlation_id

    def add_context(self, **kwargs: Any) -> "ContextLogger":
        """Add persistent context to the logger."""
        self.context.update(kwargs)
        return self
        
    @asynccontextmanager
    async def track_time(self, operation: str) -> AsyncGenerator[None, None]:
        """Track the execution time of an operation."""
        start_time = perf_counter()
        try:
            yield
        finally:
            elapsed = perf_counter() - start_time
            self.info(
                f"{operation} completed",
                extra={
                    "operation": operation,
                    "duration_ms": round(elapsed * 1000, 2)
                }
            )

    @asynccontextmanager
    async def track_memory(self, operation: str) -> AsyncGenerator[None, None]:
        """Track the memory usage of an operation."""
        import psutil
        process = psutil.Process()
        start_memory = process.memory_info().rss
        try:
            yield
        finally:
            end_memory = process.memory_info().rss
            memory_diff = end_memory - start_memory
            self.info(
                f"{operation} memory usage",
                extra={
                    "operation": operation,
                    "memory_diff_mb": round(memory_diff / 1024 / 1024, 2),
                    "total_memory_mb": round(end_memory / 1024 / 1024, 2)
                }
            )

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        exc_info: bool = False,
        **kwargs: Any
    ) -> None:
        """Internal logging method that adds context."""
        extra = kwargs.pop("extra", {})
        
        # Add correlation ID if present
        if self.correlation_id:
            extra["correlation_id"] = self.correlation_id
            
        # Add persistent context if present
        if self.context:
            extra.update(self.context)
            
        # Add timing information if available
        if hasattr(self, "_timer_start"):
            extra["elapsed_ms"] = (datetime.now() - self._timer_start).total_seconds() * 1000
            
        self.logger.log(
            level, 
            msg, 
            *args, 
            exc_info=exc_info,
            extra=extra,
            **kwargs
        )
        
    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug message with context."""
        self._log(logging.DEBUG, msg, *args, **kwargs)
        
    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an info message with context."""
        self._log(logging.INFO, msg, *args, **kwargs)
        
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning message with context."""
        self._log(logging.WARNING, msg, *args, **kwargs)
        
    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an error message with context."""
        self._log(logging.ERROR, msg, *args, **kwargs)
        
    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log an exception message with context and traceback."""
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)
