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

# doctor tracks the :latest hermes release, same as CI's doctor job: the
# catalog reviews against that release and plugin.yaml's requires_hermes
# floor is the compatibility claim. Override for a pinned local check, e.g.
# `make doctor HERMES_AGENT_IMAGE=<pinned-tag-or-digest>`.
HERMES_AGENT_IMAGE ?= nousresearch/hermes-agent:latest

doctor:
	docker run --rm -v "$(CURDIR)":/plugin:ro $(HERMES_AGENT_IMAGE) plugins doctor --ci /plugin

clean:
	find . -path ./.venv -prune -o -path ./.git -prune -o -type f -name "*.pyc" -delete
	find . -path ./.venv -prune -o -path ./.git -prune -o -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -path ./.venv -prune -o -path ./.git -prune -o -type d -name "*.egg-info" -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage .coverage.* htmlcov coverage.xml dist build
