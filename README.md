# Dyvine

Dyvine is a hermes-native plugin for Douyin download automation: 35
tools covering users, posts, livestreams, download queues, Feishu group
delivery, and profile snapshots, with persistent operation tracking in
Postgres and optional Cloudflare R2 archival. It wraps the third-party
`f2` Douyin SDK. There is no HTTP surface.

Full project documentation is available in [docs/index.html](docs/index.html).
Architecture diagrams are available in
[docs/architecture/index.html](docs/architecture/index.html).

## Features

- 35 idempotent, single-shot tools: share-link resolution, profiles,
  social graph, post listing and bulk download (post/like/collection/
  music/mix/collects), comments, stats, feeds, livestream download at
  the highest available quality, live IM, queue and round management,
  Feishu per-account delivery, hermes-native notify, profile cache,
  and operation polling.
- Postgres is the only state store (asyncpg; Alembic-versioned):
  operations, download queue, seeds, send status, profiles, rounds,
  and watch subscriptions. Idle-quiet by design: importing the plugin
  performs zero network I/O and opens zero connections; the engine
  boots on the first tool call.
- Long downloads run as operation-scoped background tasks whose state
  lands in Postgres first: submit via a download tool, poll with the
  matching status tool, and survive gateway restarts via claim/heartbeat
  semantics.
- User-supplied output paths are jailed inside `DOUYIN_DOWNLOAD_ROOT`,
  including traversal and symlink-segment checks.
- Optional Cloudflare R2 archival supports uploads, metadata lookup,
  listing, and deletes.
- A bundled `dyvine:dyvine-ops` skill carries the batch-delivery SOP
  (paired batches, verification, cleanup) plus the tool manual.

## Install

From the hermes catalog (once listed):

```bash
hermes plugins install dyvine
```

From Git:

```bash
hermes plugins install <owner>/dyvine --enable
```

From pip (entry-point distribution):

```bash
pip install dyvine
# then enable `dyvine` in hermes (`hermes plugins enable dyvine`)
```

At install hermes prompts for the required environment:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dyvine
DOUYIN_COOKIE=<browser session cookie>
```

Feishu credentials are NOT plugin inputs: delivery reads the hermes
defaults (`FEISHU_APP_ID`/`FEISHU_APP_SECRET` from `~/.hermes/.env`) at
runtime. Channels other than Feishu groups go through hermes-native
messaging (`dyvine.notify.send` is a thin wrapper, not a channel).

Apply the schema once before first use (operator-side, from a repo
checkout with the same `DATABASE_URL`):

```bash
uv run alembic upgrade head
```

## Quick Start (development)

Install dependencies:

```bash
uv sync --all-extras
```

Create a local environment file:

```bash
cp .env.example .env
```

Populate at least:

```dotenv
API_DEBUG=false
DOUYIN_COOKIE=<browser session cookie>
DATABASE_URL=postgresql+asyncpg://dyvine:dyvine@localhost:5432/dyvine
```

Start a local Postgres and apply the schema:

```bash
docker run -d --name dyvine-pg \
  -e POSTGRES_USER=dyvine -e POSTGRES_PASSWORD=dyvine \
  -e POSTGRES_DB=dyvine -p 5432:5432 postgres:16
uv run alembic upgrade head
```

Validate the plugin the way CI does:

```bash
make doctor
```

## Configuration

Configuration is environment-driven through `dyvine.core.settings`:

| Prefix | Purpose |
| --- | --- |
| `API_` | Debug flag only (`API_DEBUG`); no HTTP server exists |
| `DATABASE_` | Postgres URL, pool strategy (`null`/`queue`) and sizing, janitor cadence, operation retention |
| `DOUYIN_` | Cookie, headers, proxy, download root, livestream headers, local-retention mode, Argus webSign session |
| `DOUYIN_WATCH_` | Watch polling cadences, subscription cap, dedupe window, backfill flag |
| `R2_` | Cloudflare R2 account, key, bucket, and endpoint |
| (none) | `WATCH_ENABLED`: run watch loops in this process |

`API_DEBUG=false` rejects the localhost `DATABASE_URL` default. R2 is
optional: enabling local-retention mode
(`DOUYIN_RETAIN_LOCAL_DOWNLOADS=true`) keeps each download under
`DOUYIN_DOWNLOAD_ROOT/tasks/<task_id>` and skips R2 archival. When R2
is left unconfigured the engine retains downloads implicitly rather
than discarding them.

Database connections stay idle-quiet. `DATABASE_POOL_CLASS=null`
(default) opens a fresh connection per use and holds none while idle;
`queue` keeps a capped pool (`DATABASE_POOL_SIZE`,
`DATABASE_POOL_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT`,
`DATABASE_POOL_RECYCLE_SECONDS`, `DATABASE_POOL_PRE_PING`). The
janitor heartbeat/sweep loop runs only with an explicit
`DATABASE_JANITOR_INTERVAL_SECONDS`; single processes keep boot
recovery plus a daily retention purge and otherwise never touch the
database while idle. Repositories record each call's outcome in a
`DatabaseHealthTracker` so health readers consult last-known state
instead of probing.

Periodic work is driven by hermes cron calling single-shot tools
(`dyvine.queue.claim`/`dyvine.queue.update`/`dyvine.queue.release`,
`dyvine.delivery.send_account`, ...); the plugin runs no resident
loops and holds no connections while idle.

Douyin's web APIs additionally require a per-request Argus webSign
triple (`uifid`/`timestamp`/`x-secsdk-web-signature`) that only the
SecureSDK inside a real browser session can compute (`DOUYIN_WEBSIGN_*`;
enabled by default). Dyvine keeps one headless-Chromium page purely as a
signing session -- all traffic and downloads stay on plain HTTP --
starting it lazily on the first signed request, re-signing once with a
fresh session on an Argus block, and failing fast (backing off) after
repeated signer failures. The host must provide a matching Chromium
build (`playwright install chromium`); without it the builders return
unsigned URLs exactly as before, so unaffected endpoints keep working
while gated ones fail as HTTP 403. Set
`DOUYIN_WEBSIGN_ENABLED=false` only as a kill-switch.

## Common Commands

| Task | Command |
| --- | --- |
| Install runtime dependencies | `uv sync` |
| Install development dependencies | `uv sync --all-extras` |
| Run tests | `make test` |
| Run coverage gate | `make coverage` |
| Lint | `make lint` |
| Format | `make format` |
| Validate plugin with hermes | `make doctor` |
| Clean local caches | `make clean` |

## Tool Examples

Tools take a JSON object and return a JSON string. Drive them from a
hermes session (or `registry.dispatch` in tests):

```
dyvine.users.resolve        {"url": "https://v.douyin.com/abc/"}
  -> {"kind": "user", "sec_user_id": "...", "url": "..."}

dyvine.posts.download       {"sec_user_id": "...", "mode": "post"}
  -> {"operation_id": "...", "status": "pending", ...}

dyvine.posts.download_status {"operation_id": "..."}
  -> {"operation_id": "...", "status": "completed", ...}

dyvine.queue.import_seeds   {"items": [{"sec_user_id": "...", "nickname": "..."}]}
dyvine.queue.enqueue        {"round": "weekly-2026-09-27"}
dyvine.queue.claim          {"round": "weekly-2026-09-27"}
dyvine.delivery.send_account {"nickname": "...", "chat_id": "...", ...}
```

Long tasks follow submit-then-poll: the download tools return an
`operation_id` immediately, and the matching status tools report
progress until the operation turns terminal (`completed`, `partial`,
or `failed`). `partial` is normal (deleted/private/age-gated posts);
only `interrupted by upstream error` means work remains.

## Project Structure

| Path | Purpose |
| --- | --- |
| `plugin.yaml` | Hermes manifest: tools, hooks, env, dependencies |
| `__init__.py` | Directory-plugin entry: re-exports `register` |
| `src/dyvine/` | Engine: settings, Postgres repos, services, schemas |
| `src/dyvine_hermes/` | Plugin shell: `register`, tool table, lazy engine |
| `src/dyvine_hermes/skills/` | Bundled `dyvine:dyvine-ops` skill (SOP + tool manual) |
| `alembic/` | Operator-side database migrations |
| `scripts/migrate_hermes_state_to_pg.py` | One-shot legacy-state migration |
| `tests/` | Pytest suite mirroring the source tree |
| `docs/` | Static HTML project documentation and Mermaid architecture diagrams |
| `.github/` | CI (tests, gates, doctor) and security workflows |

## Development Notes

- Keep public documentation in static HTML under `docs/` and use
  `docs/index.html` as the entry point.
- Keep `README.md`, `.env.example`, `docs/index.html`, and the contributor
  guide consistent when tools, configuration, or behavior changes.
  Automated contributors follow the contributor guide (`AGENTS.md`).
- Runtime downloads, logs, and local credentials are intentionally ignored.
- `plugin.yaml` `provides_tools` must mirror the tool table exactly,
  and every tool needs a JSON schema plus a mock-based unit test.

## Contributing

1. Create a branch from `main`.
2. Make the scoped change and update docs/tests that describe or cover it.
3. Run `make format`, `make lint`, and `uv run pytest`.
4. For behavior changes, also run the coverage gate: `make coverage`.

## License

This project is licensed under Apache License 2.0. See [LICENSE](LICENSE).
