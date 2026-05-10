# Dyvine

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136+-green.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Type Hints](https://img.shields.io/badge/typing-mypy-green.svg)](http://mypy-lang.org/)
[![Tests](https://img.shields.io/badge/tests-pytest-orange.svg)](https://pytest.org)

[Overview](#overview) • [Quick Start](#quick-start) • [API Reference](#api-reference) • [Operations](#operations) • [Deployment](#deployment) • [Development](#development)

---

**Dyvine** is a production-grade REST API for Douyin (TikTok) content management. It delivers asynchronous downloads of videos, image galleries, livestreams, and user content with persistent operation tracking and optional Cloudflare R2 archival.

## Overview

Dyvine fronts the third-party `f2` Douyin SDK with a FastAPI service that adds:

- **Async, fire-and-forget downloads** — every long-running task returns an `operation_id` immediately and continues in a tracked background task that survives until the FastAPI lifespan drains it.
- **Persistent operation state** — a SQLite-backed `OperationStore` writes WAL-mode journal so progress, terminal status, and download paths survive process restarts.
- **API-key gated routers** — `X-API-Key` authentication protects every router endpoint by default; production builds refuse to boot with placeholder secrets.
- **Path-traversal jail** — user-supplied `output_path` values are resolved inside `DOUYIN_DOWNLOAD_ROOT` and rejected if they escape, including symlink redirection.
- **Bounded thread-pool executors** — R2 uploads, R2 `head_object` fan-out, SQLite writes, and audit-log writes run on dedicated, named pools so a burst in one IO domain cannot starve another.
- **Operational endpoints** — split `/livez`, `/readyz`, `/startupz` probes plus a Prometheus metrics endpoint at `/metrics`.

### Feature domains

| Domain       | Purpose                                                                  |
| ------------ | ------------------------------------------------------------------------ |
| Users        | Profile lookup and bulk download of a user's posts/likes to R2.          |
| Posts        | Per-post detail, paginated post listing, bulk download of all user posts.|
| Livestreams  | Active-room download by user ID or direct URL with deduplication.        |
| System       | Liveness/readiness/startup probes, health summary, Prometheus metrics.   |

## Quick Start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (recommended) or Python 3.12+
- Git
- A valid Douyin session cookie
- Optional: Cloudflare R2 credentials and a bucket for archival uploads

### Install

```bash
git clone https://github.com/memenow/dyvine.git
cd dyvine

# Install runtime + dev dependencies
uv sync --all-extras
```

### Configure

Copy `.env.example` to `.env` and populate the required fields:

```bash
cp .env.example .env
```

Minimum required settings for a non-debug deployment:

```dotenv
API_DEBUG=false
SECURITY_SECRET_KEY=<48+ bytes of entropy>
SECURITY_API_KEY=<48+ bytes of entropy>
DOUYIN_COOKIE=<browser session cookie>
```

Generate strong secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Optional R2 archival requires every R2 field — `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, and `R2_ENDPOINT`. The readiness probe (`/readyz`) fails until all R2 fields plus the Douyin cookie are set; the storage service auto-disables when any field is empty.

### Run

```bash
# Development (auto-reload, debug formatting)
uv run uvicorn src.dyvine.main:app --reload

# Production
uv run uvicorn src.dyvine.main:app --host 0.0.0.0 --port 8000 \
    --timeout-graceful-shutdown 25
```

The API exposes:

- Application:                 <http://localhost:8000>
- Swagger UI:                  <http://localhost:8000/docs>
- ReDoc:                       <http://localhost:8000/redoc>
- OpenAPI spec:                <http://localhost:8000/api/v1/openapi.json>
- Prometheus metrics:          <http://localhost:8000/metrics>

## API Reference

All router endpoints sit under the configurable prefix `API_PREFIX` (default `/api/v1`).

### Authentication

When `SECURITY_REQUIRE_API_KEY=true` (the default), every router request must carry the configured key in an `X-API-Key` header. Mismatches return `401 Unauthorized`. The check uses `hmac.compare_digest` to keep timing uniform.

```http
X-API-Key: <SECURITY_API_KEY>
```

Set `SECURITY_REQUIRE_API_KEY=false` only when fronting the API with mTLS, an authenticated gateway, or a service-mesh policy that already gates access.

### Endpoints

#### System (no API key required)

```http
GET /                       # Service metadata and feature list
GET /livez                  # Process liveness signal (200)
GET /readyz                 # Dependency-aware readiness (503 when any dep is missing)
GET /startupz               # Reports lifespan startup completion
GET /health                 # Aggregated runtime metrics (uptime, RSS, CPU); always 200
GET /metrics                # Prometheus exposition format
```

#### Users

```http
GET  /api/v1/users/{user_id}                       # User profile
POST /api/v1/users/{user_id}/content:download      # Bulk content download (posts/likes)
GET  /api/v1/users/operations/{operation_id}       # Poll bulk download status
```

#### Posts

```http
GET  /api/v1/posts/{post_id}                              # Single post detail
GET  /api/v1/posts/users/{user_id}/posts                  # Paginated user posts
POST /api/v1/posts/users/{user_id}/posts:download         # Bulk download all of a user's posts
GET  /api/v1/posts/operations/{operation_id}              # Poll bulk download status
```

The list endpoint accepts an opaque `page_token` query parameter. Echo the response's `next_page_token` back unchanged on the follow-up call; the server treats the token as a base64-encoded Douyin cursor and never as an offset.

#### Livestreams

```http
POST /api/v1/livestreams/users/{user_id}/stream:download  # Download by user ID
POST /api/v1/livestreams/stream:download                  # Download by URL or webcast ID
GET  /api/v1/livestreams/operations/{operation_id}        # Poll download status
```

The URL endpoint validates the host against an allowlist of `*.douyin.com` domains to prevent SSRF.

### Examples

```bash
# Profile lookup
curl -H "X-API-Key: $API_KEY" \
     "http://localhost:8000/api/v1/users/USER_ID"

# Schedule bulk post download (returns 202 with operation_id)
curl -X POST -H "X-API-Key: $API_KEY" \
     "http://localhost:8000/api/v1/posts/users/USER_ID/posts:download"

# Schedule a livestream download by URL
curl -X POST -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://live.douyin.com/123456789"}' \
     "http://localhost:8000/api/v1/livestreams/stream:download"

# Poll an operation
curl -H "X-API-Key: $API_KEY" \
     "http://localhost:8000/api/v1/livestreams/operations/OPERATION_ID"
```

### Response shape

Every async operation returns a standardized `OperationResponse`:

```json
{
  "operation_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "operation_type": "livestream_download",
  "subject_id": "123456789",
  "status": "pending",
  "message": "Livestream download scheduled",
  "progress": 0.0,
  "total_items": null,
  "completed_items": null,
  "downloaded_items": null,
  "download_path": "livestreams/123456789_live.flv",
  "error": null,
  "created_at": "2026-05-10T10:00:00Z",
  "updated_at": "2026-05-10T10:00:00Z"
}
```

Status values are drawn from a single shared `OperationStatus` enum: `pending`, `running`, `completed`, `partial`, `failed`. Bulk-download responses extend this with per-`PostType` counters and a `failed_count` field.

## Operations

### Persistent operation store

`src/dyvine/core/operations.py` owns a SQLite database (default path `data/douyin/state/operations.db`, override via `API_OPERATION_DB_PATH`). The store:

- Runs in WAL journaling mode so concurrent readers and a single writer share the database without serializing every read.
- Maintains per-thread reader connections for parallel polls and one shared writer connection guarded by a lock.
- Sweeps `pending` and `running` rows to `failed` on startup so a crashed deployment cannot leave stale "in-progress" rows behind.
- Dispatches every SQLite call onto a dedicated `sqlite_executor` (4 workers) so progress updates never block the event loop.

### Background task lifecycle

Long-running downloads are scheduled through a shared `BackgroundTaskRegistry` (see `src/dyvine/core/background.py`). On `SIGTERM` the FastAPI lifespan drains in-flight tasks (default 30 s) before reaping the executor pools, so a rolling restart never tears down an active R2 upload mid-flight.

### Health and probes

| Endpoint     | Purpose                                                                                       |
| ------------ | --------------------------------------------------------------------------------------------- |
| `/livez`     | Process liveness — always 200 if the event loop is responsive.                                |
| `/readyz`    | Returns 503 when any required dependency is missing: Douyin cookie, service container, operation store, R2. |
| `/startupz`  | 200 once the lifespan startup hook completes; 503 while the container is still wiring up.     |
| `/health`    | Always 200; reports uptime, RSS memory, CPU usage, and an informational dependency snapshot.  |
| `/metrics`   | Prometheus exposition format. Default counters track HTTP requests/duration and R2 upload metrics. |

The `/health` endpoint is informational only — `degraded` (e.g. R2 missing) returns 200. Use `/readyz` for dependency-aware gating.

### Concurrency model

`ServiceContainer` (in `src/dyvine/core/dependencies.py`) provisions four named thread-pool executors so each IO domain has an independent capacity ceiling:

| Pool                  | Workers | Purpose                                                                |
| --------------------- | ------- | ---------------------------------------------------------------------- |
| `dyvine-r2`           | 16      | R2 PUT/HEAD/DELETE/list operations.                                    |
| `dyvine-r2-head`      | 16      | Per-key `head_object` fan-out triggered inside `list_objects` hydration.|
| `dyvine-sqlite`       | 4       | OperationStore reads and writes.                                       |
| `dyvine-audit`        | 2       | Lifecycle audit-log writes.                                            |

## Deployment

### Docker

```bash
docker build -t dyvine:latest .

docker run -d \
  --name dyvine \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --env-file .env \
  --restart unless-stopped \
  dyvine:latest
```

Notes:

- The image is multi-stage and runs as non-root user `1000`. The runtime layer carries `python -m uvicorn` directly with `--timeout-graceful-shutdown 25` so the container fits inside the default Kubernetes 30 s grace period.
- The build step strips the `f2`-injected `black` binary from the production layer to keep the Trivy scan green.
- `apt-get upgrade` runs in both stages on every build to absorb upstream Debian security advisories. CI fails the image scan on HIGH/CRITICAL findings.

### Kubernetes (Kustomize)

Manifests live under `k8s/`:

```
k8s/
├── base/
│   ├── kustomization.yaml
│   └── core/
│       ├── namespace.yaml
│       ├── service-account.yaml
│       ├── configmap.yaml
│       ├── deployment.yaml
│       ├── service.yaml
│       ├── pvc.yaml             # 1Gi RWO claim for operation state
│       └── networkpolicy.yaml
└── overlays/
    └── production/
        ├── kustomization.yaml   # Patches deployment, hpa, pdb, ingress
        ├── ingress.yaml
        ├── hpa.yaml
        ├── pdb.yaml
        └── patches.yaml
```

Apply the base directly (development) or the production overlay:

```bash
# Development / staging — pin a real image first
kustomize edit set image \
  ghcr.io/memenow/dyvine=ghcr.io/memenow/dyvine:main-<sha>
kubectl apply -k k8s/base

# Production
kubectl apply -k k8s/overlays/production
```

The `dyvine-secrets` Secret is intentionally excluded from `base/kustomization.yaml`; the deploy workflows create it from GitHub secrets before the kustomize apply.

### Production constraints

- The default operation store is a pod-local SQLite file backed by a `ReadWriteOnce` PVC (`dyvine-operation-state`, 1 Gi). The base `Deployment` uses the `Recreate` strategy and a single replica because RWO volumes do not support attach to a surge pod, and a shared SQLite writer cannot be safely scaled horizontally.
- To scale beyond a single replica, replace the SQLite-backed `OperationStore` with a shared backend (e.g. PostgreSQL) before bumping `replicas` or enabling the production HPA.
- Place the API behind an ingress, gateway, or service mesh that enforces authentication and rate limiting in addition to (or instead of) the built-in API key.

## Development

### Test suite

```bash
uv run pytest                                  # All tests
uv run pytest --cov=src/dyvine                  # With coverage
uv run pytest tests/services/test_post_service.py
```

CI gates merges on `pytest --cov-fail-under=80`; the warning filter is set to `error` so any new `DeprecationWarning` from runtime code fails the run.

The test layout mirrors the source tree:

```
tests/
├── conftest.py                          # sys.path bootstrap + per-test isolation
├── test_main.py                         # App startup, probes, root metadata
├── test_dependencies.py                 # ServiceContainer wiring
├── test_utils.py
├── core/
│   ├── test_background.py
│   ├── test_decorators.py
│   ├── test_error_handlers.py
│   ├── test_exceptions.py
│   ├── test_logging.py
│   ├── test_operations.py
│   ├── test_path_safety.py
│   └── test_settings.py
├── routers/
│   ├── test_livestreams_router.py
│   ├── test_posts_router.py
│   └── test_users_router.py
├── schemas/
│   ├── test_schemas_livestreams.py
│   ├── test_schemas_posts.py
│   └── test_schemas_users.py
└── services/
    ├── test_lifecycle_service.py
    ├── test_livestream_service.py
    ├── test_post_service.py
    ├── test_storage_service.py
    ├── test_storage_service_extended.py
    └── test_user_service.py
```

### Quality gates

```bash
make format        # black + isort
make lint          # ruff + mypy
make test          # pytest
```

CI runs `black --check`, `isort --check-only`, `ruff check`, `mypy src/`, and the test matrix on Python 3.12 and 3.13. The container scan job (`container-scan`) builds the production image and asserts that `black` is not present in `/app/.venv/bin/`.

### Continuous integration

Workflows under `.github/workflows/`:

| Workflow            | Trigger                       | Purpose                                                              |
| ------------------- | ----------------------------- | -------------------------------------------------------------------- |
| `ci.yml`            | Push and PR to `main`         | Tests, quality gates, Trivy container scan, Docker build/push.       |
| `docker-build.yml`  | Reusable from `ci.yml`        | Multi-platform image build and push to `ghcr.io`.                    |
| `deploy-dev.yml`    | Push to `main`                | Apply `k8s/overlays/production` against the dev cluster (gated).     |
| `deploy-prod.yml`   | Tag push                      | Apply against the production cluster.                                |
| `release.yml`       | Tag push                      | GitHub release creation.                                             |
| `security.yml`      | Weekly schedule + manual      | Dependency audit (Bandit, Safety, Trivy).                            |

### Architecture documentation

Renderable architecture diagrams live under [`docs/architecture/`](docs/architecture/index.html) as standalone HTML pages with embedded Mermaid diagrams.

Internal notes intended for AI assistants and contributors live as Markdown alongside the source (`AGENTS.md`, `CLAUDE.md`, `.serena/memories/`).

## Security notes

- API key auth is enabled by default; the production composite settings validator refuses to boot with the placeholder `change-me-in-production` sentinel when `API_DEBUG=false`.
- The schema layer rejects URLs whose host is not on the `douyin.com` allowlist before the service issues any outbound HTTP request.
- Output paths are resolved inside the configured download root with both pre- and post-mkdir checks, plus an explicit symlink-segment scan that fires before `Path.resolve()` follows any indirection.
- `/health` and `/livez` never echo dependency state in a way that can be used to probe for missing credentials; only `/readyz` reports each dependency by name.

## Contributing

1. Fork the repository and create a feature branch from `main`.
2. Run `make format && make lint && make test` before pushing.
3. Open a pull request — CI will exercise the same gates plus the container scan.

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.
