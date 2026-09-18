# AGENTS.md

Contributor guide for automated agents working on Dyvine. Human-facing
documentation lives in `README.md` (concise entry point) and
`docs/index.html` (full reference); this file states the working
agreements agents must follow. Keep all three synchronized when
commands, configuration, probes, or deployment behavior change.

## Project

Dyvine is a Python 3.12+ FastAPI service for async Douyin content
downloads, persistent operation tracking in Postgres, and optional
Cloudflare R2 archival. It wraps the third-party `f2` Douyin SDK.

## Commands

```bash
uv sync --all-extras          # install dev dependencies
make run                      # run the API (uvicorn, reload)
make test                     # pytest
make lint                     # ruff + mypy src/dyvine
make format                   # black + isort
uv run pytest --cov=src/dyvine --cov-fail-under=80   # coverage gate
uv run alembic upgrade head   # apply schema (same DATABASE_URL first)
kubectl kustomize k8s/overlays/production > /dev/null  # manifest check
```

CI (`ci.yml`) gates merges on the coverage gate, black/isort/ruff/mypy,
an Alembic upgrade/downgrade round-trip, Kustomize renders, and a
container scan. There is no CD pipeline and no releases: production
ships by manual `kubectl` rollout (see `README.md` Deployment).

## Source Layout

| Path | Purpose |
| --- | --- |
| `src/dyvine/main.py` | FastAPI app, middleware, routers, probes, metrics |
| `src/dyvine/core/` | Settings, logging, dependency container, path safety |
| `src/dyvine/db/` | Postgres repositories, session factory, janitor, health tracker |
| `src/dyvine/middleware/` | Token-bucket rate limiting |
| `src/dyvine/routers/` | User, post, livestream, and watch HTTP endpoints |
| `src/dyvine/services/` | SDK orchestration, background work, R2 storage, watch scheduling, Argus webSign signing |
| `src/dyvine/schemas/` | Pydantic request and response models |
| `scripts/dyvine_batch/` | Batch user-download CLI (`serial` / `concurrent`) |
| `tests/` | Pytest suite mirroring the source tree |

## Invariants (do not break silently)

- Idle-quiet database: `DATABASE_POOL_CLASS=null` (default) holds zero
  idle connections; read-only lookups and probes never touch the
  stores — only real task execution opens connections
  (pinned by `tests/test_stateless_contract.py`).
- `/readyz` reports the last-known database state (`unknown` /
  `available` / `unavailable` plus `operation_store_checked_at`) and
  never probes the database itself; `unknown` counts as ready.
- Single replicas run boot recovery plus a daily retention purge only;
  the janitor heartbeat/sweep loop runs on multi-replica deployments
  or an explicit `DATABASE_JANITOR_INTERVAL_SECONDS`.
- Watch loops run on exactly one watcher replica (`WATCH_ENABLED=true`);
  API replicas serve subscription CRUD with no loops.
- Cluster replicas share one database, so `k8s/` uses the `queue` pool;
  the `null` code default optimizes single processes instead.
- Public API routes and the error envelope are stable; behavior changes
  need a regression test first.

## Branching and PRs

Branch from `main` (`feature/*`, `chore/*`, or `docs/*` prefix), make
one scoped change per branch, keep docs and tests in the same commit
series as the code they describe, and open a pull request back to
`main`. CI must pass before merge.

## Working Rules

- Use standard American English for code, identifiers, comments, and
  docs. Docstrings and comments explain contracts, invariants, and
  non-obvious reasons — never narrate syntax.
- Public documentation is static HTML under `docs/` with
  `docs/index.html` as the entry point; no build step is required to
  read it. `README.md`, `.env.example`, `docs/index.html`, and this
  file stay synchronized.
- Tests mirror the source tree (`tests/...` matches `src/dyvine/...`);
  unit tests inject in-memory fakes (`tests/fake_repos.py`), and the
  contract suites in `tests/db/` run identical assertions against both
  backends. Keep coverage at or above 80%.
- Never commit secrets, credentials, cookies, real user IDs, logs,
  downloads, or local environment files. `.env.example` carries
  placeholder values only; copy it to `.env` (ignored) for local runs.
  `.agents/plans/` holds local working notes and stays untracked.
- Before finishing: run `make format`, `make lint`, and `uv run pytest`;
  for behavior changes also run the coverage gate above.
