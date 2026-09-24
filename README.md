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
  operations, download queue, seeds, send status, per-file delivery
  records, profiles, rounds, and watch subscriptions. Idle-quiet by design:
  importing the plugin
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

### Hermes dependency resolution

Hermes resolves a directory plugin's `pyproject.toml`
`[project].dependencies` (`plugin.yaml` `python_dependencies` only when
there is no `pyproject.toml`) against the pins of its own environment.
Dyvine's floors admit those pins (core `python-dotenv==1.2.2`, bedrock
extra `boto3==1.42.89`), but `f2` 0.0.1.7 publishes `==` pins
(`httpx==0.27.2`, `pydantic==2.9.*`, `websockets<13`,
`protobuf==5.28.3`, ...), so `hermes plugins enable dyvine` still
reports "No solution found". The lockfile lifts those pins with
`[tool.uv] override-dependencies`, which Hermes does not read, and the
plugin runs on the Hermes versions of the shared packages.

Install the locked packages missing from the Hermes venv with
`uv pip install --no-config --no-deps` and leave the packages Hermes
already has at their versions; `--no-config` keeps uv from applying the
checkout's overrides. [docs/index.html](docs/index.html#hermes-dependencies)
has the commands. Repeat this after every `hermes update`, which can drop
the added packages and disable the plugin.

Plugin doctor and a profile call do not exercise the request signer.
During the cutover both passed while the Playwright dependency `pyee`
from `uv.lock` was missing, and signed post-list calls returned HTTP 403
until it was installed. Verify the matching Chromium build and read one
authorized `dyvine.posts.list` page before enabling weekly sends.

The webSign signer needs the Chromium build matching `playwright==1.62.0`
(revision 1234). Where the default Playwright CDN is slow, install it
from a mirror by setting `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`, for example
to `https://cdn.npmmirror.com/binaries/playwright`.

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
| `DYVINE_WEEKLY_` | Weekly timezone, first automatic Sunday, active cutover round, Feishu group policy, and owner open ID |
| `R2_` | Cloudflare R2 account, key, bucket, and endpoint |
| (none) | `WATCH_ENABLED`: run watch loops in this process |

`API_DEBUG=false` rejects the localhost `DATABASE_URL` default. R2 is
optional: enabling local-retention mode
(`DOUYIN_RETAIN_LOCAL_DOWNLOADS=true`) keeps each download under
`DOUYIN_DOWNLOAD_ROOT/tasks/<task_id>` and skips R2 archival. When R2
is left unconfigured the engine retains downloads implicitly rather
than discarding them.

Weekly delivery downloads into account folders under
`DOUYIN_DOWNLOAD_ROOT`, which `DOUYIN_RETAIN_MAX_GB` does not bound.
`hermes dyvine media prune` deletes media older than `--older-than-days`
(default 14) from the folders of accounts whose every queue row is settled
(`completed`, `permanent_failure`, or `skipped`): delivery reads only media
posted after a round's cutoff, so a settled account never reads those files
again. A folder that any unsettled row records is kept, even when another
account shares it, and nothing outside the download root is touched. The
command takes the weekly runner's lock and skips while a run is active; run
it daily through Hermes no-agent cron, and use `--dry-run` to report
without deleting.

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

Weekly delivery is advanced by the bounded `hermes dyvine weekly run-once`
command, scheduled every 10 minutes through Hermes no-agent cron. Its
automatic round starts at or after Sunday 08:00 in the explicitly configured
`DYVINE_WEEKLY_TIMEZONE`. The required
`DYVINE_WEEKLY_FIRST_AUTO_DATE` is the first eligible Sunday; earlier
automatic rounds are not opened. The required
`DYVINE_WEEKLY_CUTOVER_ROUND` identifies the active legacy round: it and
unfinished automatic rounds dated from the first eligible Sunday onward
block a new automatic round. Older frozen history does not block the schedule.
An automatic round's queue cutoff is the Sunday 08:00 of the latest earlier
automatic round that was opened; the first round uses the Sunday before the
first eligible one. A week held back by an unfinished round therefore widens the
next window instead of being skipped.
The command processes ordered pairs of accounts back to back in seed order:
within its step and runtime bounds it advances both accounts of a pair,
records its checkpoint in Postgres, and waits for any download it starts
before exiting. It holds the next pair until both accounts in the current
pair have terminal outcomes. A new pair starts only within the first 15
minutes of an invocation, while no review state was hit, and while the
pair's estimated download fits the free disk space minus max(10 GiB, 10% of
the disk). The estimate is 2 GiB per full-feed account (full mode or no
cutoff) and, for an incremental account, 1.5 times its days since the queue
cutoff times the round's download rate: the 90th percentile of bytes per
window day among its finished accounts, or 20 MiB per day until 10 of them
recorded one. The first pair must fit too; a `disk_budget` outcome means
nothing was started. A row still parked in
`needs_reconciliation` does not hold later pairs, but it keeps blocking
automatic rounds until it is reconciled. An incremental download stops at the
queue cutoff: media posted at or before it is neither downloaded nor sent,
so a first run without a saved anchor fetches only the round's window, not
the account's whole feed. Before downloading an account, the runner checks
its Douyin profile: an author Douyin reports deactivated or banned, or who
has no posts, is skipped for good (the row becomes `skipped`, the seed is
excluded from later rounds, and the Feishu group and file ledger are kept).
A failed or unclear check never skips.
Other periodic work uses single-shot tools. The plugin runs no resident
weekly loop or idle database connection.

Douyin's web APIs additionally require a per-request Argus webSign
triple (`uifid`/`timestamp`/`x-secsdk-web-signature`) that only the
SecureSDK inside a real browser session can compute (`DOUYIN_WEBSIGN_*`;
enabled by default). Dyvine keeps one headless-Chromium page purely as a
signing session -- all traffic and downloads stay on plain HTTP --
starting it lazily on the first signed request, re-signing once with a
fresh session on an Argus block, and failing fast (backing off) after
repeated signer failures. The host must provide a matching Chromium
build (`uv run playwright install chromium` from a checkout); without it
the builders return unsigned URLs exactly as before, so unaffected
endpoints keep working while gated ones fail as HTTP 403. Set
`DOUYIN_WEBSIGN_ENABLED=false` only as a kill-switch.

## Weekly Delivery and Legacy Cutover

Set `DYVINE_WEEKLY_TIMEZONE=Asia/Shanghai`,
`DYVINE_WEEKLY_FIRST_AUTO_DATE` to the first intended automatic Sunday
(`YYYY-MM-DD`), `DYVINE_WEEKLY_CUTOVER_ROUND` to the active legacy round,
and `DYVINE_WEEKLY_OWNER_OPEN_ID=ou_...` in the Hermes runtime
environment. For this cutover, the first automatic date is `2026-09-27`
and the active legacy round is supplied privately; these are deployment
values, not project defaults. The owner ID must be a Feishu `open_id`. The runner
also uses the existing
`DATABASE_URL`, `DOUYIN_COOKIE`, `FEISHU_APP_ID`,
`FEISHU_APP_SECRET`, and `DOUYIN_DOWNLOAD_ROOT` settings. Keep the old
download root mounted at its original path until reconciliation and delivery
are complete; copied or renamed paths can defeat historical file matching.
The Playwright dependency is declared by the plugin, while the matching
Chromium browser must also be installed on the host.

Set `DYVINE_WEEKLY_GROUP_POLICY=reuse_existing` for every weekly round.
Returning accounts, identified by stable `sec_user_id`, reuse their
existing chat and topic only when Postgres has one verified historical
destination; only new accounts get a new
group. The frozen cutover inventory found one reused chat for 1,619 of
1,631 accounts present in multiple rounds. Missing or conflicting
historical chats or topics remain on hold for manual reconciliation. The
weekly CLI refuses an unset or unknown policy value.

Before migration, stop the legacy weekly cron and its restart mechanism at
an account boundary, then record the queue, weekly progress files, group
messages, and on-disk media as one cutover snapshot. Apply the Alembic
schema to the intended RDS database before importing. The migration reads
the old disk stores and takes the database URL from an environment variable,
so the password need not appear in the command line:

```bash
PYTHONPATH=src uv run python scripts/migrate_hermes_state_to_pg.py \
  --state-dir <legacy-state-dir> --seed-path <legacy-seed-users.json> \
  --weekly-glob '<legacy-weekly-progress-glob>' \
  --users-db <legacy-douyin-users.db> --dry-run

PYTHONPATH=src uv run python scripts/migrate_hermes_state_to_pg.py \
  --state-dir <legacy-state-dir> --seed-path <legacy-seed-users.json> \
  --weekly-glob '<legacy-weekly-progress-glob>' \
  --users-db <legacy-douyin-users.db>
```

Run the dry run against the frozen source first; verify source counts and
invalid rows before the write. The importer uses conflict-safe inserts and
can be rerun, but a weekly progress entry without independent delivery
evidence enters `needs_reconciliation` and cannot be claimed automatically.
Nickname-level send counters are historical clues, not proof that a given
file reached Feishu. Next, stage the legacy per-file send progress and a
private reconciliation report:

```bash
PYTHONPATH=src uv run python scripts/import_legacy_send_progress.py \
  --state-dir <legacy-state-dir> --seed-path <legacy-seed-users.json> \
  --progress-glob '<legacy-state-dir>/send_progress_weekly*.json' \
  --download-root <legacy-download-root> \
  --work-db <private-staging.sqlite3> \
  --reconcile-output <private-reconciliation.jsonl> --dry-run
```

The dry run writes only the local staging database and JSONL report. Review
that report, then rerun the same command without `--dry-run` against the
frozen inputs. When the frozen source includes `permanent_failures.json` or
`report_sent_index.json`, add `--permanent-failures PATH` or
`--sent-index PATH` to both invocations. Use a new `--work-db` when adding
either source to a previous staging run. A uniquely attributable,
conflict-free permanent failure is recorded in the ledger to prevent a
repeat attempt; cache-only sent-index paths remain unverified audit
evidence and do not count as delivered. Exit code 1 means historical paths
still need review; exit code 2 means a source or configuration error. Only
sent paths mapped to one stable account are imported as
`legacy_confirmed_sent` to suppress a repeat
send. Failed, ambiguous, and conflicting paths remain audit evidence, while
queue entries stay blocked. Reconcile each account against its group
messages and media files before releasing it for delivery. Preserve the old
state files and media until every round has an explicit outcome.

The progress importer records file-path evidence; it does not adopt legacy
Feishu groups. First run `scripts/adopt_legacy_groups.py` as a read-only
preview for the active round. It checks each candidate chat and topic with
Feishu and writes a private report. After reviewing every result, rerun
with `--resume --apply`; apply rechecks Feishu before idempotently writing
verified group/topic rows to Postgres. It creates no Feishu groups,
topics, or messages. Keep `DATABASE_URL` and
`DYVINE_WEEKLY_OWNER_OPEN_ID` in the environment, not command arguments:

```bash
PYTHONPATH=src uv run python scripts/adopt_legacy_groups.py \
  --queue-path <legacy-state-dir>/download_queue.json \
  --work-db <private-staging.sqlite3> --round <active-round> \
  --output <private-group-review.jsonl>

PYTHONPATH=src uv run python scripts/adopt_legacy_groups.py \
  --queue-path <legacy-state-dir>/download_queue.json \
  --work-db <private-staging.sqlite3> --round <active-round> \
  --output <private-group-review.jsonl> --resume --apply
```

If a legacy topic key is missing, or its recorded root was deleted, opt
in to full chat-history discovery with `--discover-missing-topics --round
<active-round>` and a separate report path. The legacy sender posted a
profile root whenever it started delivering to a chat and recorded the most
recent one, so the preview adopts the latest live app-authored root that
links to exactly this account's profile (a post or a text message). A chat
whose root predates exact links is accepted only when exactly one
app-authored root carries a Douyin short link and none names any profile.
The resumed apply rechecks the chat history before adopting it. Keep the
discovery journal separate from the default adoption journal:

```bash
PYTHONPATH=src uv run python scripts/adopt_legacy_groups.py \
  --queue-path <legacy-state-dir>/download_queue.json \
  --work-db <private-staging.sqlite3> --round <active-round> \
  --discover-missing-topics --output <private-discovery-review.jsonl>

PYTHONPATH=src uv run python scripts/adopt_legacy_groups.py \
  --queue-path <legacy-state-dir>/download_queue.json \
  --work-db <private-staging.sqlite3> --round <active-round> \
  --discover-missing-topics --output <private-discovery-review.jsonl> \
  --resume --apply
```

Then audit the active round's full Feishu chat and topic-thread history,
including groups with zero file messages. The audit reads Postgres and
Feishu, writes a private `0600` JSONL, and does not send or change database
state. Add `--resume` if interrupted:

```bash
PYTHONPATH=src uv run python scripts/audit_feishu_delivery.py \
  --legacy-report <private-reconciliation.jsonl> --round <active-round> \
  --output <private-feishu-audit.jsonl>
```

For a supplemental audit of exact queue keys, provide `--keys-file` with
`--round` and a new output journal. The keys file must be an owned regular
`0600` UTF-8 file with one complete round-prefixed queue key per line;
every key must exist in the selected round's evidence. This audit remains
read-only and does not change the original full-round journal.

```bash
PYTHONPATH=src uv run python scripts/audit_feishu_delivery.py \
  --legacy-report <private-reconciliation.jsonl> --round <active-round> \
  --keys-file <private-queue-keys.txt> \
  --output <private-supplemental-audit.jsonl>
```

Frozen legacy records have no reliable topic linkage or per-file Feishu
receipt IDs, and source media may no longer be on disk. In that case, a
one-time group-level attestation compares the complete app-sent Feishu
file-name multiset and
count with all imported historical file paths for one stable account.
`propose_group_attested.py` only proposes `release_pending_group_attested`
for groups whose identity, historical counts, file names, and complete audit
all match; every other row stays on hold. The proposal is a new private
JSONL and makes no Postgres changes:

```bash
PYTHONPATH=src uv run python scripts/propose_group_attested.py \
  --source-report <private-reconciliation.jsonl> \
  --feishu-audit <private-feishu-audit.jsonl> \
  --legacy-work-db <private-staging.sqlite3> \
  --active-round <active-round> \
  --output <private-group-attested-proposal.jsonl>
```

Preview that proposal with the unchanged source report, complete Feishu
audit, and same staging database. Review its held reasons and `plan_sha256`
and `expected_count`; apply only the exact reviewed plan by passing those
two values to the write command. This releases eligible pending queue
entries; it does not invent per-file receipts or send messages.

```bash
PYTHONPATH=src uv run python scripts/apply_queue_reconciliation.py \
  --report <private-group-attested-proposal.jsonl> \
  --active-round <active-round> \
  --audit-source-report <private-reconciliation.jsonl> \
  --feishu-audit <private-feishu-audit.jsonl> \
  --legacy-work-db <private-staging.sqlite3> \
  --output <private-reconciliation-preview.json>

PYTHONPATH=src uv run python scripts/apply_queue_reconciliation.py \
  --report <private-group-attested-proposal.jsonl> \
  --active-round <active-round> \
  --audit-source-report <private-reconciliation.jsonl> \
  --feishu-audit <private-feishu-audit.jsonl> \
  --legacy-work-db <private-staging.sqlite3> \
  --output <private-reconciliation-apply.json> \
  --apply --expect-plan-sha256 <preview-plan-sha256> \
  --expected-count <preview-expected-count>
```

A chat usually also holds files from rounds the imported progress never
covered, so the whole-chat multiset rarely matches. The weekly runner only
re-sends media posted after the queue cutoff, so the window proof
(`release_pending_window_attested`) compares just that part: the app-sent,
non-deleted Feishu file names posted after the cutoff must equal the upload
names of the legacy ledger's sends in the same window. An extra Feishu file
would be sent twice and an extra ledger file never, so either difference
holds the row. Ambiguous, cache-only, or failed legacy sends need no
separate proof, because the chat shows whether each one arrived. Pass the
runner's `DYVINE_WEEKLY_TIMEZONE` so cutoffs match; it is bound into the
plan digest.

```bash
PYTHONPATH=src uv run python scripts/propose_group_attested.py \
  --action release_pending_window_attested --timezone Asia/Shanghai \
  --source-report <private-reconciliation.jsonl> \
  --feishu-audit <private-feishu-audit.jsonl> \
  --legacy-work-db <private-staging.sqlite3> \
  --active-round <active-round> \
  --output <private-window-attested-proposal.jsonl>
```

Preview and apply that proposal with the `apply_queue_reconciliation.py`
commands above, adding `--timezone` with the same value.

When the chat and the ledger disagree, `--action
release_pending_feishu_adopted` takes the audited chat as the record of what
was sent, within the queue cutoff's window when the row has one, as delivery
applies it, or the whole feed otherwise. Chat files the ledger lacks are adopted as
`legacy_confirmed_sent` rows in the `feishu_adopted` round: an untruncated
name at its exact path, and a shortened name, which lost its media slot, as
one post-level row that covers every media of that post. Ledger rows the
chat does not hold move to `legacy_disproved` as `legacy_not_in_chat`, so the
runner sends them again; a shortened ledger name counts as held while the
chat has any file of its post. A file name without a post time is outside
every scope: the runner never sends such media in a window, and no media
path carries that name. The proposal carries the exact plan, the
apply recomputes it from the audit and Postgres and holds the row on any
difference, and the released row gets a fresh download.

An account whose queue rows or legacy progress name another chat than its
current group stays held, because that chat may hold sends the runner would
repeat. `audit_other_chats.py` reads each such older chat for exact queue
keys, read-only: a dissolved chat, whose history Feishu keeps from its
members, or one with no app file inside the row's scope is cleared. Pass the
journal as `--other-chat-audit` to both the proposal and the apply; its
digest joins the plan digest, and any older chat it does not clear still
holds the row.

```bash
PYTHONPATH=src uv run python scripts/audit_other_chats.py \
  --legacy-report <private-reconciliation.jsonl> --round <active-round> \
  --legacy-work-db <private-staging.sqlite3> \
  --keys-file <private-queue-keys.txt> \
  --output <private-other-chat-audit.jsonl>
```

The legacy queue records `skipped_404` for accounts the user ordered skipped
for a round: the group is kept and nothing is sent. No other action can close
such a row in the active round, so it would block every automatic round.
`propose_user_ordered_skips.py` writes a private JSONL holding only those
rows, each with `skip_user_ordered`. Preview and apply it the same way; no
audit inputs are needed. The policy re-checks each row's migrated queue
status and holds any account with a new send attempt; applied rows become
`skipped`.

```bash
PYTHONPATH=src uv run python scripts/propose_user_ordered_skips.py \
  --source-report <private-reconciliation.jsonl> \
  --active-round <active-round> \
  --output <private-user-skip-proposal.jsonl>

PYTHONPATH=src uv run python scripts/apply_queue_reconciliation.py \
  --report <private-user-skip-proposal.jsonl> \
  --active-round <active-round> \
  --output <private-user-skip-preview.json>
```

Apply by rerunning the preview command with `--apply`, the preview's
`plan_sha256` as `--expect-plan-sha256`, and its `expected_count` as
`--expected-count`.

An author Douyin reports deactivated or banned, or who has no posts, can
never be delivered, yet the unclosed row would block every automatic round.
`propose_unavailable_author_skips.py` checks the Douyin profile of each
active-round row still in `needs_reconciliation` (it reads `DATABASE_URL`)
and writes a private JSONL with `skip_author_unavailable` and the evidence
(reason, post count, check time) for those authors only. A failed or unclear
profile answer is counted and never proposed. Preview and apply it the same
way. Applied rows become `skipped`, the seed is excluded from later rounds,
and the Feishu group is kept.

```bash
PYTHONPATH=src uv run python scripts/propose_unavailable_author_skips.py \
  --source-report <private-reconciliation.jsonl> \
  --active-round <active-round> \
  --output <private-author-skip-proposal.jsonl>
```

For inspection, run `hermes dyvine weekly run-once --dry-run`; use
`--round weekly-YYYY-MM-DD --dry-run` to inspect a named round. The
non-dry-run command advances account pairs in seed order, within bounds on
files, runtime, and free disk space. A new file's delivery key includes its account, account-relative path,
and content hash. The ledger retains the uploaded `file_key`, send UUID,
and Feishu message ID. A legacy send covers a re-downloaded file with the
same account-relative path, or with the same post creation stamp and media
slot (`_video.mp4`, `_image_3.webp`, ...) when a caption edit renamed it,
so the file is not sent twice. Consecutive weekly windows overlap, so a
confirmed send by this runner covers a caption-renamed re-download the same
way. Upload names longer than 50 characters are shortened; post media keeps
its slot suffix, so the images of one post keep distinct names in the group.
The Feishu audit and the window proof still match legacy uploads by the
legacy truncation (the first 40 characters of the stem plus the extension).
An uncertain send or unadopted legacy chat remains
on hold for message-level reconciliation; never replay a file merely
because a summary counter or operation ID is missing.
The `dyvine.delivery.send_account` tool also uses this ledger: pass `round`
and `sec_user_id` for a definite identity. Without them, the tool requires
a unique matching queue entry. It refuses an unverified group/topic or
legacy skip-path lists until those records are reconciled.

On the Hermes host, install `scripts/dyvine_weekly_run_once.sh` as
`~/.hermes/scripts/dyvine-weekly.sh`. Create the new cron paused and
verify its configuration before activation:

```bash
install -D -m 700 scripts/dyvine_weekly_run_once.sh \
  "$HOME/.hermes/scripts/dyvine-weekly.sh"
hermes cron create '*/10 * * * *' --no-agent --script dyvine-weekly.sh \
  --deliver local --name dyvine-weekly --paused \
  --paused-reason 'cutover verification'
```

The wrapper keeps routine output off the cron message channel. Keep both
the old and new cron jobs paused until group adoption, the full Feishu audit,
and the exact-digest reconciliation plan have been reviewed, one read-only
post-list page succeeds, and `DYVINE_WEEKLY_GROUP_POLICY=reuse_existing`
is configured. Once the
new path is verified, resume only this
job with `hermes cron resume <new-job-id>`; keep the old weekly sender
paused. Preserve the legacy state and media through acceptance.

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
dyvine.delivery.send_account {"round": "weekly-2026-09-27", "sec_user_id": "...", "nickname": "...", "chat_id": "...", ...}
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
