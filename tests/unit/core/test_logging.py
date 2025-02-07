"""Unit tests for the logging module.

This module tests the logging functionality including:
- JSON formatting
- Context logging
- Performance tracking
- Error handling
"""

import json
import logging
import asyncio
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

from dyvine.core.logging import (
    JSONFormatter,
    ContextLogger,
    setup_logging
)

def test_json_formatter():
    """Test JSON formatter correctly formats log records."""
    formatter = JSONFormatter()
    
    # Create a test log record
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Test message",
        args=(),
        exc_info=None
    )
    
    # Format the record
    formatted = formatter.format(record)
    log_dict = json.loads(formatted)
    
    # Verify required fields
    assert log_dict["level"] == "INFO"
    assert log_dict["logger"] == "test_logger"
    assert log_dict["message"] == "Test message"
    assert log_dict["module"] == "test"
    assert log_dict["line"] == 1
    assert "timestamp" in log_dict

def test_json_formatter_with_exception():
    """Test JSON formatter correctly formats exception information."""
    formatter = JSONFormatter()
    
    try:
        raise ValueError("Test error")
    except ValueError as e:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Error occurred",
            args=(),
            exc_info=(ValueError, e, e.__traceback__)
        )
    
    formatted = formatter.format(record)
    log_dict = json.loads(formatted)
    
    assert log_dict["level"] == "ERROR"
    assert "exception" in log_dict
    assert log_dict["exception"]["type"] == "ValueError"
    assert log_dict["exception"]["message"] == "Test error"

def test_context_logger_initialization():
    """Test ContextLogger initialization."""
    logger = ContextLogger("test.logger")
    
    assert logger.logger.name == "test.logger"
    assert logger.correlation_id is None
    assert logger.context == {}

def test_context_logger_correlation_id():
    """Test setting and using correlation ID."""
    logger = ContextLogger("test.logger")
    correlation_id = "test-123"
    
    logger.set_correlation_id(correlation_id)
    assert logger.correlation_id == correlation_id

def test_context_logger_add_context():
    """Test adding context to logger."""
    logger = ContextLogger("test.logger")
    
    # Add context
    logger.add_context(user_id="123", action="test")
    
    assert logger.context == {
        "user_id": "123",
        "action": "test"
    }

@pytest.mark.asyncio
async def test_context_logger_track_time():
    """Test time tracking context manager."""
    logger = ContextLogger("test.logger")
    
    with patch.object(logger, 'info') as mock_info:
        async with logger.track_time("test_operation"):
            await asyncio.sleep(0.001)  # Small delay to ensure timing
        
        mock_info.assert_called_once()
        args, kwargs = mock_info.call_args
        assert args[0] == "test_operation completed"
        assert "duration_ms" in kwargs.get("extra", {})
        duration = kwargs["extra"]["duration_ms"]
        assert duration > 0  # Should have some duration

@pytest.mark.asyncio
async def test_context_logger_track_memory():
    """Test memory tracking context manager."""
    logger = ContextLogger("test.logger")
    
    with patch.object(logger, 'info') as mock_info:
        async with logger.track_memory("test_operation"):
            await asyncio.sleep(0.001)  # Small delay to ensure measurement
            # Allocate some memory to test tracking
            _ = [1] * 1000000
        
        mock_info.assert_called_once()
        args, kwargs = mock_info.call_args
        assert args[0] == "test_operation memory usage"
        assert "memory_diff_mb" in kwargs.get("extra", {})
        assert "total_memory_mb" in kwargs.get("extra", {})

def test_setup_logging(test_data_dir, cleanup_test_files):
    """Test logging setup configuration."""
    log_file = test_data_dir / "test.log"
    
    # Configure logging
    setup_logging(
        log_level="DEBUG",
        log_file=str(log_file),
        max_bytes=1024,
        backup_count=2
    )
    
    # Get root logger
    root_logger = logging.getLogger()
    
    # Verify configuration
    assert root_logger.level == logging.DEBUG
    assert len(root_logger.handlers) == 2  # File and console handlers
    
    # Test logging
    test_message = "Test log message"
    logging.info(test_message)
    
    # Verify log file
    assert log_file.exists()
    with open(log_file) as f:
        log_content = f.read()
        assert test_message in log_content

def test_logging_with_context():
    """Test logging with context information."""
    logger = ContextLogger("test.logger")
    logger.set_correlation_id("test-123")
    logger.add_context(user="test_user")
    
    with patch.object(logger.logger, 'log') as mock_log:
        logger.info("Test message", extra={"action": "test"})
        
        args, kwargs = mock_log.call_args
        assert args[0] == logging.INFO
        assert args[1] == "Test message"
        assert kwargs["extra"]["correlation_id"] == "test-123"
        assert kwargs["extra"]["user"] == "test_user"
        assert kwargs["extra"]["action"] == "test"
