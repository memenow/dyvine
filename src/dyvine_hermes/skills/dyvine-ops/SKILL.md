---
name: dyvine-ops
description: Douyin batch download to Feishu group delivery SOP (paired batches, progress tracking, cleanup) plus the dyvine plugin tool manual.
trigger: User asks to batch-download Douyin users and deliver to Feishu, or to run dyvine delivery SOP.
---

# Dyvine Ops: Batch Delivery SOP + Tool Manual

Converged from the legacy `dyvine-*` / `douyin-*` skills. The old world
(curl against `localhost:8000`, `/opt/dyvine` JSON/SQLite state files,
hand-rolled sender scripts) is gone: Postgres is the only state store
and the `dyvine.*` tools below are the only interface. The operating
rules in this file are the durable part — they still apply.

## Hard Rules (must follow)

1. **Never send a bare text probe.** The first message in a delivery
   group is always the author's profile link + nickname.
2. **Always deliver to a group.** When the user says "send to group"
   or asks for SOP delivery, create a NEW group and send there;
   never send files into the current 1:1 session.
3. **Never reuse an old group.** Every run creates a fresh group with
   the recipient as `owner_id`.
4. **Dissolve superseded groups.** Dissolve old groups via
   `DELETE /im/v1/chats/{chat_id}` (works for groups this flow
   created even when the user is owner — do not assume missing
   permission). Verify dissolution by `data.chat_status ==
   "dissolved"` (the GET does not 404); tell the user to dissolve
   manually only when the API refuses.
5. **Paired batches.** Two accounts per batch: the previous batch must
   be fully delivered AND verified before local files are cleaned and
   the next batch starts.
6. **When unsure, keep sending.** Uncertainty means continue delivery,
   not pause for questions.
7. **Every delivery account is recorded.** Any account confirmed as a
   delivery account is immediately upserted via
   `dyvine.queue.import_seeds` (`[{sec_user_id, nickname,
   source_url}]`; use `https://www.douyin.com/user/{sec_user_id}`
   when no short link is known).

## Identity and Group Creation

- Group `owner_id` must be the recipient's **`open_id`** (starts with
  `ou_`) with `owner_id_type: "open_id"`. A plain `user_id` (e.g.
  `1g3gdg98`) returns `id not exist` 400. Resolve open_ids from the
  Feishu contacts API or the approved-user mapping — never guess.
- **Group-name review `232023`**: single-character or sensitive group
  names may be rejected (`Chat information review failed`). Retry
  with an emoji/suffix appended (`橘` → `橘🍊`); keep the original
  nickname in the description, first message, and progress keys. Do
  not pre-judge which names fail — always keep the retry fallback.
- **Set the group avatar right after creation** (mandatory): download
  the author's avatar, upload via `POST /im/v1/images`
  (`image_type=avatar`), then `PUT /im/v1/chats/{chat_id}` with
  `{"avatar": image_key}`. The bot may set avatars without being
  group owner.

## Batch URL Input

Users paste Douyin share texts (`{N}- 长按复制此条消息...
https://v.douyin.com/{code}/ ...`):

1. Regex-extract every `https://v.douyin.com/{code}/` link.
2. Call `dyvine.users.resolve` per link → `sec_user_id`
   (`kind: user`) or `aweme_id` (`kind: post`).
3. Never treat trailing metadata (`X@Y.com :Zpm`) as URL parts.
4. More pastes in the same format extend the same batch.

## Download Flow

1. `dyvine.rounds.create` (idempotent round header).
2. `dyvine.queue.import_seeds` + `dyvine.queue.enqueue` (idempotent;
   excluded seeds stay out).
3. Per account: `dyvine.posts.download` with the right `mode`
   (`post`/`like`/`collection`/`music`), then poll
   `dyvine.posts.download_status` (or `dyvine.operation.get`) until
   terminal. **Never start delivery while operations still run.**
4. `partial` is normal (deleted/private/age-gated posts) — UNLESS the
   message says `interrupted by upstream error`: that account did NOT
   finish and must be re-triggered + backfilled, never signed off.
5. `total_downloaded` may exceed `total_posts` (albums split into
   files; live replays split into clips). `failed_count == 0` is the
   success signal.
6. Live rooms: `dyvine.live.download` defaults to the highest
   available quality; pass `quality` to override (invalid values
   fall back with a note).

## Delivery Flow (`dyvine.delivery.send_account`)

- Message order per group: author link + nickname FIRST, then media.
  The tool enforces SOP order; do not send files ahead of it.
- **Prefilters (no retry, ever)**: files over 30 MB are permanent
  failures (Feishu `234006`); 0-byte files are deleted locally
  (Feishu `234010`) and reported as incomplete posts.
- **Backfill dedupe**: re-runs pass `already_sent` (exact paths from
  earlier progress) and `known_permanent`; the tool skips both.
  Collect `already_sent` from ALL history rounds, not just the last.
- **0/0 accounts must be backfilled**: a built group with zero
  delivered files (download failed before any post) is the worst
  outcome — re-trigger the download and run a backfill delivery reusing
  the same group topic; never leave it.
- **Same-nickname accounts** (distinct `sec_user_id`s) must never run
  in parallel: queue/download/deliver them strictly serially, because
  download directories and progress keys are nickname-based.
- Progress queries: `dyvine.delivery.status` per account;
  `dyvine.queue.status` per round.

## Leak Scan and Mass Failures

- The "latest op" per account is ordered by `created_at`, never by
  filename or log order. Latest op `failed`/`interrupted` →
  re-trigger + backfill; `completed` with `sent=0` → check
  `dyvine.delivery.status` history first (post-cutoff incremental
  accounts with no new posts are legitimately 0).
- Concentrated mass failures (dozens of accounts at once) usually
  mean an expired `DOUYIN_COOKIE` — refresh it. Scattered
  `interrupted` after a refresh means Douyin-side volume throttling:
  converge over multiple rounds, do not churn the cookie.

## Scheduling and Liveness

- Tools are single-shot and idempotent; periodic work is driven by
  hermes cron, never by in-plugin loops.
- Workers claim via `dyvine.queue.claim` (serial-group aware) and
  checkpoint via `dyvine.queue.update`; `dyvine.queue.release`
  requeues entries whose claimer stopped heartbeating.
- When idle (no tool called), the plugin holds zero connections and
  does zero work.

## Credentials

- Feishu credentials come from hermes defaults (`~/.hermes/.env`,
  `FEISHU_APP_ID`/`FEISHU_APP_SECRET`) at runtime — never from the
  repo, Postgres, or logs.
- `DOUYIN_COOKIE` is configured via the plugin's `requires_env` at
  install; it is the only Douyin login state.
- Never inline secrets in terminal commands or files (they get
  masked as `***` and break scripts). `R2_*` only when archival is
  wanted.

## Tool Manual (35 tools, `dyvine` toolset)

Results are JSON strings. DB-only tools need just `DATABASE_URL`;
Douyin-backed tools additionally need `DOUYIN_COOKIE`.

- `dyvine.profile.get`: Fetch a Douyin user profile by sec_user_id.
- `dyvine.social.following`: List accounts a user follows.
- `dyvine.social.followers`: List accounts following a user.
- `dyvine.identity.get`: Resolve who the configured DOUYIN_COOKIE authenticates as.
- `dyvine.users.resolve`: Follow a Douyin share short link to its user/post identity.
- `dyvine.posts.list`: List one page of a user's posts.
- `dyvine.posts.download`: Start a background bulk download (modes: post, like, collection, music).
- `dyvine.posts.download_status`: Poll a bulk/download operation by id.
- `dyvine.single.download`: Download one post (video or album) inline and return file paths.
- `dyvine.mix.download`: Start a background download of a mix album by mix_id.
- `dyvine.collects.list`: List the cookie owner's collects folders.
- `dyvine.collects.download`: Start a background download of a collects folder.
- `dyvine.comments.list`: List top-level comments of a post.
- `dyvine.stats.get`: Fetch upstream statistics of a post.
- `dyvine.feed.user`: List a user's feed videos.
- `dyvine.feed.related`: List posts related to one post.
- `dyvine.feed.friend`: List friend-feed videos for the cookie owner.
- `dyvine.live.download`: Download a livestream (default highest quality; quality override optional).
- `dyvine.live.im`: Fetch live-room IM state for a viewer identity.
- `dyvine.live.following`: List live rooms of followed accounts (cookie owner).
- `dyvine.queue.import_seeds`: Upsert seed accounts [{sec_user_id, nickname?, source_url?}].
- `dyvine.queue.enqueue`: Enqueue all non-excluded seeds into a round (idempotent).
- `dyvine.queue.claim`: Claim the oldest pending entry (serial-group aware).
- `dyvine.queue.update`: Patch a queue entry (status/checkpoint/counters).
- `dyvine.queue.list`: List queue entries oldest-first with filters.
- `dyvine.queue.status`: Tally queue entries by status (one round or all).
- `dyvine.queue.release`: Requeue entries whose claimer stopped heartbeating.
- `dyvine.rounds.create`: Create a delivery round header (idempotent).
- `dyvine.rounds.list`: List delivery rounds in creation order.
- `dyvine.delivery.send_account`: Deliver one account's pending media files to its Feishu group.
- `dyvine.delivery.status`: Read delivery counters by nickname (or sec_user_id).
- `dyvine.notify.send`: Send a message via hermes-native channels (non-Feishu).
- `dyvine.profiles.upsert`: Insert or patch a cached profile snapshot.
- `dyvine.profiles.get`: Fetch a cached profile snapshot.
- `dyvine.operation.get`: Fetch any operation row by id.
