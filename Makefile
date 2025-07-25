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
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	mypy .

format:
	black .
	isort .

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache

run:
	uvicorn src.dyvine.main:app --reload