# Dyvine

Dyvine is a Python 3.12 FastAPI service for asynchronous Douyin content
downloads, persistent operation tracking, and optional Cloudflare R2 archival.
It wraps the third-party `f2` Douyin SDK with a REST API for videos, image
galleries, livestreams, and user content.

Full project documentation is available in [docs/index.html](docs/index.html).
Architecture diagrams are available in
[docs/architecture/index.html](docs/architecture/index.html).

## Features

- Async download endpoints return an operation record immediately and continue
  work on tracked background tasks.
- Postgres operation state uses an asyncpg pool, per-replica heartbeats, and an
  orphan sweep, with the schema versioned by Alembic.
- API-key authentication is enabled by default on feature routers through the
  `X-API-Key` header.
- User-supplied output paths are jailed inside `DOUYIN_DOWNLOAD_ROOT`, including
  traversal and symlink-segment checks.
- Optional Cloudflare R2 archival supports uploads, metadata lookup, listing,
  and deletes.
- Watch Mode automatically monitors subscribed Douyin users and downloads their
  new posts and livestreams on configurable polling cadences.
- Operational surfaces include `/livez`, `/readyz`, `/startupz`, `/health`, and
  `/metrics`.

## Quick Start

Install dependencies:

```bash
uv sync --all-extras
```

Create a local environment file:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Populate at least:

```dotenv
API_DEBUG=false
SECURITY_API_KEY=<48+ bytes of entropy>
DOUYIN_COOKIE=<browser session cookie>
```

Run the API locally:

```bash
PYTHONPATH=src uv run uvicorn dyvine.main:app --reload
```

Useful local URLs:

| Surface | URL |
| --- | --- |
| Swagger UI | `http://localhost:8000/docs` |
| ReDoc | `http://localhost:8000/redoc` |
| OpenAPI JSON | `http://localhost:8000/api/v1/openapi.json` |
| Metrics | `http://localhost:8000/metrics` |

## Configuration

Configuration is environment-driven through `dyvine.core.settings`:

| Prefix | Purpose |
| --- | --- |
| `API_` | Server bind settings, CORS, API prefix, operation DB path |
| `SECURITY_` | Secret key, API key, and router auth gate |
| `DOUYIN_` | Cookie, headers, proxy, download root, livestream headers, local-retention mode |
| `DOUYIN_WATCH_` | Watch Mode polling cadences, subscription cap, dedupe window, backfill flag |
| `R2_` | Cloudflare R2 account, key, bucket, and endpoint |

`API_DEBUG=false` rejects placeholder production secrets. R2 is optional: by
default `/readyz` reports `not_ready` until every R2 field and `DOUYIN_COOKIE`
are set, but enabling local-retention mode
(`DOUYIN_RETAIN_LOCAL_DOWNLOADS=true`) keeps each download under
`DOUYIN_DOWNLOAD_ROOT/tasks/<task_id>`, drops R2 from the readiness gate, and
reports it as `disabled`. When R2 is left unconfigured the service retains
downloads implicitly rather than discarding them. `/readyz` also verifies that
the retained workspace can be created and written. `/health` remains
informational and returns `200 OK` even when dependencies are missing.

## Common Commands

| Task | Command |
| --- | --- |
| Install runtime dependencies | `uv sync` |
| Install development dependencies | `uv sync --all-extras` |
| Run the API | `make run` |
| Run tests | `make test` |
| Run coverage gate | `uv run pytest --cov=src/dyvine --cov-fail-under=80` |
| Lint | `make lint` |
| Format | `make format` |
| Clean local caches | `make clean` |

## API Examples

All feature-router requests require `X-API-Key` unless
`SECURITY_REQUIRE_API_KEY=false` is set behind another authenticated layer.

```bash
curl -H "X-API-Key: $SECURITY_API_KEY" \
  "http://localhost:8000/api/v1/users/USER_ID"

curl -X POST -H "X-API-Key: $SECURITY_API_KEY" \
  "http://localhost:8000/api/v1/posts/users/USER_ID/posts:download"

curl -X POST -H "X-API-Key: $SECURITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://live.douyin.com/123456789"}' \
  "http://localhost:8000/api/v1/livestreams/stream:download"
```

Poll operation status through the matching domain endpoint:

```bash
curl -H "X-API-Key: $SECURITY_API_KEY" \
  "http://localhost:8000/api/v1/posts/operations/OPERATION_ID"
```

Create a Watch Mode subscription to auto-download a user's new posts and
livestreams:

```bash
# Create a subscription (201 for new, 200 when one already exists for that user)
curl -X POST -H "X-API-Key: $SECURITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "SEC_USER_ID"}' \
  "http://localhost:8000/api/v1/watch"

# List all subscriptions
curl -H "X-API-Key: $SECURITY_API_KEY" \
  "http://localhost:8000/api/v1/watch"

# Delete a subscription (stop future checks; any recording already in progress
# runs to completion in its own background task)
curl -X DELETE -H "X-API-Key: $SECURITY_API_KEY" \
  "http://localhost:8000/api/v1/watch/SUBSCRIPTION_ID"
```

## Batch Download CLI

`scripts/dyvine_batch/` submits user downloads from a text file (one user
ID per line) and polls them to a summary report:

```bash
# Concurrent: submit all users, then poll together (recommended)
uv run python -m scripts.dyvine_batch concurrent users.txt --api-key KEY

# Serial: one user at a time
uv run python -m scripts.dyvine_batch serial users.txt --api-key KEY
```

Configuration precedence per knob is CLI flag, then `DYVINE_*`
environment (`DYVINE_API_KEY`, `DYVINE_API_URL`, `DYVINE_API_PREFIX`),
then the repo `.env` file (`SECURITY_API_KEY`, `API_HOST`/`API_PORT`),
then the default (`http://localhost:8000`, prefix `/api/v1`). Useful
flags: `--include-likes` (download liked posts too),
`--max-concurrent`, `--poll-interval`, `--timeout`. Exit code is `0`
unless configuration, input, or the run itself fails.

## Project Structure

| Path | Purpose |
| --- | --- |
| `src/dyvine/main.py` | FastAPI app, middleware, routers, probes, metrics |
| `src/dyvine/core/` | Settings, logging, dependency container, path safety |
| `src/dyvine/db/` | Postgres repositories, session factory, janitor (Alembic-versioned) |
| `src/dyvine/middleware/` | Token-bucket rate limiting |
| `src/dyvine/routers/` | User, post, livestream, and watch HTTP endpoints |
| `src/dyvine/services/` | Douyin SDK orchestration, background work, R2 storage, watch scheduling |
| `src/dyvine/schemas/` | Pydantic request and response models |
| `scripts/dyvine_batch/` | Batch user-download CLI (`serial` / `concurrent`) |
| `scripts/migrate_watch_to_pg.py` | One-shot SQLite → Postgres watch migration |
| `alembic/` | Database migration scripts and environment |
| `tests/` | Pytest suite mirroring the source tree |
| `docs/` | Static HTML project documentation and Mermaid architecture diagrams |
| `k8s/` | Base, production overlay, and migration-Job manifests |
| `.github/` | CI, image build, deployment, release, and security workflows |

## Development Notes

- Keep public documentation in static HTML under `docs/` and use
  `docs/index.html` as the entry point.
- Keep `README.md`, `AGENTS.md`, `CLAUDE.md`, `.env.example`, and
  `docs/index.html` synchronized when configuration, commands, probes, or
  deployment behavior changes.
- Runtime downloads, logs, and local credentials are intentionally ignored.
- Operation/watch state is multi-replica safe in Postgres, but per-task
  download workspaces live on a ReadWriteOnce volume. Do not scale beyond
  one replica until the workspaces move to shared storage.

## Contributing

1. Create a branch from `main`.
2. Make the scoped change and update docs/tests that describe or cover it.
3. Run `make format`, `make lint`, and `uv run pytest`.
4. For behavior changes, also run the coverage gate:
   `uv run pytest --cov=src/dyvine --cov-fail-under=80`.

## License

This project is licensed under Apache License 2.0. See [LICENSE](LICENSE).
