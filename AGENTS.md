# AGENTS.md

Contributor guide for automated agents working on Dyvine. Human-facing
documentation lives in `README.md` (concise entry point) and
`docs/index.html` (full reference); this file states the working
agreements agents must follow. Keep all three synchronized when
tools, configuration, or behavior change.

## Project

Dyvine is a Python 3.12+ hermes-native plugin for async Douyin content
downloads (35 tools: users, posts, livestreams, queue, Feishu delivery,
profiles), persistent operation tracking in Postgres, and optional
Cloudflare R2 archival. It wraps the third-party `f2` Douyin SDK.
There is no HTTP surface: `plugin.yaml` + `register(ctx)` is the only
interface, distributed as a directory plugin (Git URL) and as a pip
package (`hermes_agent.plugins` entry point).

## Commands

```bash
uv sync --all-extras          # install dev dependencies
make test                     # pytest
make coverage                 # pytest with the 80% gate
make lint                     # ruff + mypy src/dyvine src/dyvine_hermes
make format                   # black + isort
make doctor                   # hermes plugins doctor --ci (needs docker)
uv run alembic upgrade head   # apply schema (same DATABASE_URL first)
```

CI (`ci.yml`) gates merges on pytest + coverage gate, black/isort/ruff/
mypy, an Alembic upgrade/downgrade round-trip, and `hermes plugins
doctor --ci` inside the hermes-agent image. Security scanning
(`security.yml`) audits locked dependencies weekly and per-PR.

## Source Layout

| Path | Purpose |
| --- | --- |
| `plugin.yaml` | Hermes manifest: tools, hooks, env, dependencies |
| `__init__.py` | Directory-plugin entry: re-exports `register` |
| `src/dyvine/` | Engine: settings, Postgres repos, services, schemas |
| `src/dyvine_hermes/` | Plugin shell: `register`, tool table, lazy engine |
| `src/dyvine_hermes/skills/` | Bundled `dyvine:dyvine-ops` skill (SOP + tool manual) |
| `alembic/` | Operator-side schema migrations |
| `scripts/migrate_hermes_state_to_pg.py` | One-shot legacy-state migration |
| `tests/` | Pytest suite mirroring the source tree |

## Invariants (do not break silently)

- Idle-quiet plugin: importing either entry (`dyvine_hermes` or the
  repo-root shim) performs zero network I/O and opens zero database
  connections; the engine boots on the first tool call only
  (pinned by `tests/test_import_time_network.py` and the doctor run,
  which blocks sockets during registration).
- Registry contract: every tool handler returns a JSON **string**
  (`as_tool_handler` serializes at the boundary); dict/list returns
  become `tool_result_contract` errors.
- `plugin.yaml` `provides_tools` mirrors `TOOL_SPECS` exactly, and the
  bundled skill manual names every tool (both pinned by
  `tests/hermes/test_plugin.py`).
- `pyproject.toml` dependencies stay in sync with `plugin.yaml`
  `python_dependencies` (plus operator-side `alembic`): hermes
  installs entry-point plugins from the pep-621 bounds.
- Dependency floors admit the Hermes Agent pins (core
  `python-dotenv==1.2.2`, bedrock extra `boto3==1.42.89`; pinned by
  `tests/hermes/test_plugin.py`). Hermes resolves a directory plugin's
  `pyproject.toml` `[project].dependencies` against its own pins,
  refuses a union with no solution, and `hermes update` disables such a
  plugin. Never raise a floor past a Hermes pin.
- Postgres is the only state store; tools are single-shot and
  idempotent; periodic work is driven by hermes cron, never by
  in-plugin loops. No resident connections, no background threads
  that outlive a tool call except operation-scoped download tasks
  whose state lands in Postgres first.
- Tool results are JSON strings; error messages stay human-readable
  and secret-free (the registry renders propagated `DyvineError`s).

## Deployment Notes

- `f2` 0.0.1.7 publishes `==` pins (`httpx==0.27.2`, `pydantic==2.9.*`,
  `websockets<13`, `protobuf==5.28.3`, ...), so the Hermes resolver
  still refuses the install; `[tool.uv] override-dependencies` lifts
  them for uv only. Operators install the locked packages missing from
  the Hermes venv with `uv pip install --no-config --no-deps` and must
  repeat it after every `hermes update` (commands in
  `docs/index.html#hermes-dependencies`).
- `playwright==1.62.0` needs Chromium revision 1234. Hosts with a slow
  Playwright CDN install it from a mirror by setting
  `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST` (`docs/index.html#chromium`).

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
- `pytest` runs with `--import-mode=importlib` because the repo root
  must carry an `__init__.py` (hermes directory plugins require
  `plugin.yaml` + `__init__.py` side by side); the default prepend
  mode would shadow `src/dyvine`. Test basenames must stay unique.
  When the checkout directory name is not a valid identifier
  (hyphenated clones or worktrees), pytest imports the root shim as a
  plain `__init__` module with no `__path__`, so the shim must keep
  working without one (pinned by `tests/hermes/test_plugin.py`).
- Before finishing: run `make format`, `make lint`, and `uv run pytest`;
  for behavior changes also run the coverage gate above.
