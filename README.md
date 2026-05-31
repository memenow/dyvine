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
- SQLite operation state uses WAL mode, per-thread reader connections, and a
  single guarded writer.
- API-key authentication is enabled by default on feature routers through the
  `X-API-Key` header.
- User-supplied output paths are jailed inside `DOUYIN_DOWNLOAD_ROOT`, including
  traversal and symlink-segment checks.
- Optional Cloudflare R2 archival supports uploads, metadata lookup, listing,
  and deletes.
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
SECURITY_SECRET_KEY=<48+ bytes of entropy>
SECURITY_API_KEY=<48+ bytes of entropy>
DOUYIN_COOKIE=<browser session cookie>
```

Run the API locally:

```bash
uv run uvicorn src.dyvine.main:app --reload
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
| `DOUYIN_` | Cookie, headers, proxy settings, download root, livestream headers |
| `R2_` | Cloudflare R2 account, key, bucket, and endpoint |

`API_DEBUG=false` rejects placeholder production secrets. R2 is optional, but
`/readyz` reports `not_ready` until every R2 field and `DOUYIN_COOKIE` are set.
`/health` remains informational and returns `200 OK` even when dependencies are
missing.

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
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/api/v1/users/USER_ID"

curl -X POST -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/api/v1/posts/users/USER_ID/posts:download"

curl -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://live.douyin.com/123456789"}' \
  "http://localhost:8000/api/v1/livestreams/stream:download"
```

Poll operation status through the matching domain endpoint:

```bash
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/api/v1/posts/operations/OPERATION_ID"
```

## Project Structure

| Path | Purpose |
| --- | --- |
| `src/dyvine/main.py` | FastAPI app, middleware, routers, probes, metrics |
| `src/dyvine/core/` | Settings, logging, dependency container, operation store, path safety |
| `src/dyvine/routers/` | User, post, and livestream HTTP endpoints |
| `src/dyvine/services/` | Douyin SDK orchestration, background work, R2 storage |
| `src/dyvine/schemas/` | Pydantic request and response models |
| `tests/` | Pytest suite mirroring the source tree |
| `docs/` | Static HTML project documentation and Mermaid architecture diagrams |
| `k8s/` | Base and production overlay manifests |
| `.github/` | CI, image build, deployment, release, and security workflows |

## Development Notes

- Keep public documentation in static HTML under `docs/` and use
  `docs/index.html` as the entry point.
- Keep `README.md`, `AGENTS.md`, `CLAUDE.md`, `.env.example`, and
  `docs/index.html` synchronized when configuration, commands, probes, or
  deployment behavior changes.
- Runtime downloads, logs, SQLite files, WAL/SHM files, and local credentials
  are intentionally ignored.
- The default SQLite operation store is pod-local. Do not scale beyond one
  replica without replacing it with a shared backend.

## Contributing

1. Create a branch from `main`.
2. Make the scoped change and update docs/tests that describe or cover it.
3. Run `make format`, `make lint`, and `uv run pytest`.
4. For behavior changes, also run the coverage gate:
   `uv run pytest --cov=src/dyvine --cov-fail-under=80`.

## License

This project is licensed under Apache License 2.0. See [LICENSE](LICENSE).
