.PHONY: help install dev test coverage lint format doctor clean

help:
	@echo "Available commands:"
	@echo "  install   Install dependencies"
	@echo "  dev       Install development dependencies"
	@echo "  test      Run tests"
	@echo "  coverage  Run tests with the 80% coverage gate"
	@echo "  lint      Run linting"
	@echo "  format    Format code"
	@echo "  doctor    Validate the plugin with hermes (needs docker)"
	@echo "  clean     Clean temporary files"

install:
	uv sync

dev:
	uv sync --all-extras

test:
	uv run pytest

coverage:
	uv run pytest --cov=src/dyvine --cov=src/dyvine_hermes --cov-fail-under=80

lint:
	uv run ruff check .
	uv run mypy src/dyvine src/dyvine_hermes __init__.py

format:
	uv run black .
	uv run isort .

doctor:
	docker run --rm -v $(PWD):/plugin:ro nousresearch/hermes-agent:latest plugins doctor --ci /plugin

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
