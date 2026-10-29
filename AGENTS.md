# Repository Guidelines

## Project Structure & Module Organization

Dyvine is a Python 3.12+ Hermes plugin for Douyin automation, not an HTTP
service. `src/dyvine/` owns the engine: `core/` configuration and utilities,
`db/` PostgreSQL repositories, `schemas/` validation, and `services/` business
logic. `src/dyvine_hermes/` owns registration, tool adapters, weekly orchestration,
and bundled skills. `plugin.yaml` and root `__init__.py` support directory
installation. Keep migrations in `alembic/`, operator utilities in `scripts/`,
mirrored tests in `tests/`, and public HTML documentation in `docs/`.

## Build, Test, and Development Commands

- `uv sync --all-extras --frozen`: install locked runtime and development dependencies.
- `uv build --wheel`: build the distributable wheel in `dist/`.
- `make test`: run pytest; PostgreSQL integration tests require Docker.
- `make coverage`: test both packages with an 80% coverage gate.
- `make lint`: run Ruff and mypy, including the root plugin shim.
- `make format`: apply Black and isort; use `uv run black --check .` and
  `uv run isort --check-only .` for nonmutating checks.
- `make doctor`: validate registration in the Dockerized Hermes runtime.

Follow [README.md](README.md#quick-start-development) for local PostgreSQL,
configuration, and migrations; invoke tools through Hermes.

## Coding Style & Naming Conventions

Use four-space indentation, 88-character lines, and typed function signatures.
Use `snake_case` for modules/functions, `PascalCase` for classes, and
`UPPER_SNAKE_CASE` for constants. Write English comments explaining contracts
or non-obvious behavior. Follow `pyproject.toml`; isort uses Black's profile.

## Testing Guidelines

Use pytest and pytest-asyncio. Name files `test_*.py` and functions `test_*`;
keep basenames unique and do not add `tests/**/__init__.py`. Preserve importlib
mode. Inject `tests/fake_repos.py` fakes for unit tests; database tests use
disposable PostgreSQL containers. Add regression tests for behavior changes.
Run focused tests first, then relevant suites and coverage.

## Dependency & Runtime Contracts

Regenerate `uv.lock` with uv; do not hand-edit it or commit generated build
artifacts. Preserve dependency bounds and Hermes/f2 compatibility; synchronize
`pyproject.toml` with `plugin.yaml`. Keep imports and registration free of
network/database I/O. Tool handlers return JSON strings; keep `TOOL_SPECS`,
manifest entries, schemas, and the bundled manual synchronized.

## Commit & Pull Request Guidelines

History favors concise English imperative subjects. Branch from `main`, keep
changes scoped, and describe purpose, related issues, and validation in PRs.
Update affected tests and documentation together; CI must pass before merging.
Never commit cookies, credentials, real account data, logs, or downloads;
keep `.env.example` placeholder-only.
