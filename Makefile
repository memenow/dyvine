.PHONY: help install dev test lint format clean run

help:
	@echo "Available commands:"
	@echo "  install   Install dependencies"
	@echo "  dev       Install development dependencies"
	@echo "  test      Run tests"
	@echo "  lint      Run linting"
	@echo "  format    Format code"
	@echo "  clean     Clean temporary files"
	@echo "  run       Run the application"

install:
	uv sync

dev:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy .

format:
	uv run black .
	uv run isort .

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache

run:
	uv run uvicorn src.dyvine.main:app --reload