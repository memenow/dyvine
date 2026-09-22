"""Tool table for the dyvine hermes plugin (thin service wrappers).

Every handler takes the tool ``args`` dict, runs exactly one service
call against the process :func:`engine
<dyvine_hermes.context.get_engine>`, and returns JSON-safe data.
Handlers hold no business logic: f2 orchestration lives in
:mod:`dyvine.services`, persistence in :mod:`dyvine.db`.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from dyvine_hermes.context import get_engine

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


def serialize(result: Any) -> str:
    """Render a handler result for the hermes tool pipeline.

    The registry accepts only strings (plus the multimodal
    envelope); anything else becomes a ``tool_result_contract``
    error. Handlers already normalize via :func:`jsonable`, so
    this is a straight dump with ``default=str`` as a backstop.
    """
    return json.dumps(result, ensure_ascii=False, default=str)


def as_tool_handler(handler: Handler) -> Handler:
    """Wrap a raw handler with the string-result contract."""

    @functools.wraps(handler)
    async def run(args: dict[str, Any]) -> Any:
        return serialize(await handler(args))

    return run


def jsonable(value: Any) -> Any:
    """Convert service results into JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return jsonable(model_dump(mode="json"))
        except TypeError:
            return jsonable(model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    return str(value)


def _string(name: str, description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(name: str, description: str, default: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _schema(
    properties: dict[str, dict[str, Any]], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One plugin tool: name, description, schema, async handler."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


async def _profile_get(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.users.get_user_info(args["sec_user_id"]))


async def _social_following(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.users.get_following(
            args["sec_user_id"], count=int(args.get("count", 20))
        )
    )


async def _social_followers(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.users.get_followers(
            args["sec_user_id"], count=int(args.get("count", 20))
        )
    )


async def _identity_get(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.users.get_login_identity())


async def _users_resolve(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.users.resolve_share_url(args["url"]))


# ---------------------------------------------------------------------------
# posts
# ---------------------------------------------------------------------------


async def _posts_list(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_user_posts(
            args["sec_user_id"], max_cursor=int(args.get("max_cursor", 0))
        )
    )


async def _posts_download(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.start_bulk_download(
            args["sec_user_id"],
            max_cursor=int(args.get("max_cursor", 0)),
            mode=str(args.get("mode", "post")),
        )
    )


async def _posts_download_status(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.posts.get_bulk_download_status(args["operation_id"]))


async def _single_download(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.posts.download_single_post(args["aweme_id"]))


async def _mix_download(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.posts.start_mix_download(args["mix_id"]))


async def _collects_list(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.posts.list_collects())


async def _collects_download(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.posts.start_collects_download(args["collects_id"]))


async def _comments_list(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_post_comments(
            args["aweme_id"], count=int(args.get("count", 20))
        )
    )


async def _stats_get(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_post_stats(
            args["aweme_id"], aweme_type=int(args.get("aweme_type", 0))
        )
    )


async def _feed_user(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_user_feed(
            args["sec_user_id"], count=int(args.get("count", 20))
        )
    )


async def _feed_related(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_related_posts(
            args["aweme_id"], count=int(args.get("count", 20))
        )
    )


async def _feed_friend(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.posts.get_friend_feed(count=int(args.get("count", 20)))
    )


# ---------------------------------------------------------------------------
# livestreams
# ---------------------------------------------------------------------------


async def _live_download(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.livestreams.download_stream(
            args["url"],
            output_path=args.get("output_path"),
            quality=args.get("quality"),
        )
    )


async def _live_im(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.livestreams.get_live_im(args["room_id"], args["unique_id"])
    )


async def _live_following(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.livestreams.get_following_lives())


# ---------------------------------------------------------------------------
# queue / rounds
# ---------------------------------------------------------------------------


async def _queue_import_seeds(args: dict[str, Any]) -> Any:
    engine = get_engine()
    items = args.get("items", [])
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    accepted = await engine.queue.import_seeds(items, batch=args.get("batch"))
    return {"accepted": accepted}


async def _queue_enqueue(args: dict[str, Any]) -> Any:
    engine = get_engine()
    created = await engine.queue.enqueue_round(
        args["round"],
        mode=args["mode"],
        cutoff=args.get("cutoff"),
        note=args.get("note"),
    )
    return {"round": args["round"], "created": created}


async def _queue_claim(args: dict[str, Any]) -> Any:
    engine = get_engine()
    claimed = await engine.queue.claim_next(round=args.get("round"))
    return jsonable(claimed)


async def _queue_update(args: dict[str, Any]) -> Any:
    engine = get_engine()
    raw_fields = args.get("fields", {})
    if not isinstance(raw_fields, dict):
        raise ValueError("fields must be an object")
    fields = dict(raw_fields)
    return jsonable(await engine.queue.report_progress(args["key"], **fields))


async def _queue_list(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(
        await engine.queue.list_entries(
            round=args.get("round"),
            status=args.get("status"),
            limit=int(args.get("limit", 100)),
            offset=int(args.get("offset", 0)),
        )
    )


async def _queue_status(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.queue.round_status(args.get("round")))


async def _queue_release(args: dict[str, Any]) -> Any:
    engine = get_engine()
    released = await engine.queue.release_stale(
        stale_after_seconds=float(args.get("stale_after_seconds", 600.0)),
        max_attempts=int(args.get("max_attempts", 8)),
    )
    return {"released": released}


async def _rounds_create(args: dict[str, Any]) -> Any:
    engine = get_engine()
    await engine.queue.ensure_round(args["round"], args.get("note"))
    return {"round": args["round"]}


async def _rounds_list(args: dict[str, Any]) -> Any:
    engine = get_engine()
    rounds = await engine.round_repo.list_rounds()
    return jsonable(rounds)


# ---------------------------------------------------------------------------
# delivery / notify
# ---------------------------------------------------------------------------


async def _delivery_send_account(args: dict[str, Any]) -> Any:
    from dyvine.services.delivery import FeishuCredentials, FeishuGroupChannel

    engine = get_engine()
    parsed_cutoff = None
    cutoff = args.get("cutoff")
    if cutoff:
        text = str(cutoff).strip().replace("T", " ", 1)
        for candidate in (text[:19], text[:10]):
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed_cutoff = datetime.strptime(candidate, pattern)
                    break
                except ValueError:
                    continue
            if parsed_cutoff is not None:
                break
    channel = FeishuGroupChannel(FeishuCredentials.from_hermes_default())
    result = await channel.send_account(
        nickname=args["nickname"],
        chat_id=args["chat_id"],
        homepage=args["homepage"],
        user_dir=Path(args["user_dir"]),
        cutoff=parsed_cutoff,
        starter_message_id=args.get("starter_message_id"),
        already_sent=set(args.get("already_sent", [])),
        already_failed=set(args.get("already_failed", [])),
        known_permanent=set(args.get("known_permanent", [])),
    )
    # Persist the counters next to the queue state, mirroring the
    # sender's upsert (DELETE-then-INSERT semantics preserved by the
    # repository upsert keyed on nickname).
    await engine.send_status.upsert_send_status(
        nickname=result.nickname,
        sec_user_id=args.get("sec_user_id"),
        chat_id=result.chat_id,
        batch=args.get("batch", "batch"),
        total_files=result.total_files,
        sent_files=result.sent_files,
        failed_files=result.failed_files,
        status=result.status,
    )
    return jsonable(result)


async def _delivery_status(args: dict[str, Any]) -> Any:
    engine = get_engine()
    if args.get("sec_user_id"):
        row = await engine.send_status.get_send_status_by_sec(args["sec_user_id"])
        return jsonable(row)
    return jsonable(await engine.send_status.get_send_status(args["nickname"]))


async def _notify_send(args: dict[str, Any]) -> Any:
    from dyvine.services.delivery import send_via_hermes

    return jsonable(
        await send_via_hermes(
            target=args["target"],
            message=args["message"],
            subject=args.get("subject"),
            timeout_seconds=float(args.get("timeout_seconds", 120.0)),
        )
    )


# ---------------------------------------------------------------------------
# profiles / operations
# ---------------------------------------------------------------------------


async def _profiles_upsert(args: dict[str, Any]) -> Any:
    engine = get_engine()
    fields = dict(args.get("fields", {}))
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    return jsonable(
        await engine.profiles.upsert_profile(sec_user_id=args["sec_user_id"], **fields)
    )


async def _profiles_get(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.profiles.get_profile(args["sec_user_id"]))


async def _operation_get(args: dict[str, Any]) -> Any:
    engine = get_engine()
    return jsonable(await engine.operations.get_operation(args["operation_id"]))


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "dyvine.profile.get",
        "Fetch a Douyin user profile by sec_user_id.",
        _schema({"sec_user_id": _string("sec", "Douyin sec_user_id")}, ["sec_user_id"]),
        _profile_get,
    ),
    ToolSpec(
        "dyvine.social.following",
        "List accounts a user follows.",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "count": _integer("count", "Max entries", 20),
            },
            ["sec_user_id"],
        ),
        _social_following,
    ),
    ToolSpec(
        "dyvine.social.followers",
        "List accounts following a user.",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "count": _integer("count", "Max entries", 20),
            },
            ["sec_user_id"],
        ),
        _social_followers,
    ),
    ToolSpec(
        "dyvine.identity.get",
        "Resolve who the configured DOUYIN_COOKIE authenticates as.",
        _schema({}, []),
        _identity_get,
    ),
    ToolSpec(
        "dyvine.users.resolve",
        "Follow a Douyin share short link to its user/post identity.",
        _schema({"url": _string("url", "Share URL")}, ["url"]),
        _users_resolve,
    ),
    ToolSpec(
        "dyvine.posts.list",
        "List one page of a user's posts.",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "max_cursor": _integer("cursor", "Pagination cursor", 0),
            },
            ["sec_user_id"],
        ),
        _posts_list,
    ),
    ToolSpec(
        "dyvine.posts.download",
        "Start a background bulk download (modes: post, like, collection, music).",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "mode": _string("mode", "Feed mode (default post)"),
                "max_cursor": _integer("cursor", "Start cursor", 0),
            },
            ["sec_user_id"],
        ),
        _posts_download,
    ),
    ToolSpec(
        "dyvine.posts.download_status",
        "Poll a bulk/download operation by id.",
        _schema({"operation_id": _string("id", "Operation id")}, ["operation_id"]),
        _posts_download_status,
    ),
    ToolSpec(
        "dyvine.single.download",
        "Download one post (video or album) inline and return file paths.",
        _schema({"aweme_id": _string("id", "Post aweme_id")}, ["aweme_id"]),
        _single_download,
    ),
    ToolSpec(
        "dyvine.mix.download",
        "Start a background download of a mix album by mix_id.",
        _schema({"mix_id": _string("id", "Mix id")}, ["mix_id"]),
        _mix_download,
    ),
    ToolSpec(
        "dyvine.collects.list",
        "List the cookie owner's collects folders.",
        _schema({}, []),
        _collects_list,
    ),
    ToolSpec(
        "dyvine.collects.download",
        "Start a background download of a collects folder.",
        _schema(
            {"collects_id": _string("id", "Collects folder id")},
            ["collects_id"],
        ),
        _collects_download,
    ),
    ToolSpec(
        "dyvine.comments.list",
        "List top-level comments of a post.",
        _schema(
            {
                "aweme_id": _string("id", "Post aweme_id"),
                "count": _integer("count", "Max comments", 20),
            },
            ["aweme_id"],
        ),
        _comments_list,
    ),
    ToolSpec(
        "dyvine.stats.get",
        "Fetch upstream statistics of a post.",
        _schema(
            {
                "aweme_id": _string("id", "Post aweme_id"),
                "aweme_type": _integer("type", "Post kind code", 0),
            },
            ["aweme_id"],
        ),
        _stats_get,
    ),
    ToolSpec(
        "dyvine.feed.user",
        "List a user's feed videos.",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "count": _integer("count", "Max items", 20),
            },
            ["sec_user_id"],
        ),
        _feed_user,
    ),
    ToolSpec(
        "dyvine.feed.related",
        "List posts related to one post.",
        _schema(
            {
                "aweme_id": _string("id", "Post aweme_id"),
                "count": _integer("count", "Max items", 20),
            },
            ["aweme_id"],
        ),
        _feed_related,
    ),
    ToolSpec(
        "dyvine.feed.friend",
        "List friend-feed videos for the cookie owner.",
        _schema({"count": _integer("count", "Max items", 20)}, []),
        _feed_friend,
    ),
    ToolSpec(
        "dyvine.live.download",
        "Download a livestream (default highest quality; quality override optional).",
        _schema(
            {
                "url": _string("url", "Room, user, or webcast id"),
                "quality": _string("q", "Quality label (optional)"),
                "output_path": _string("out", "Output dir (optional)"),
            },
            ["url"],
        ),
        _live_download,
    ),
    ToolSpec(
        "dyvine.live.im",
        "Fetch live-room IM state for a viewer identity.",
        _schema(
            {
                "room_id": _string("room", "Room id"),
                "unique_id": _string("user", "Viewer unique id"),
            },
            ["room_id", "unique_id"],
        ),
        _live_im,
    ),
    ToolSpec(
        "dyvine.live.following",
        "List live rooms of followed accounts (cookie owner).",
        _schema({}, []),
        _live_following,
    ),
    ToolSpec(
        "dyvine.queue.import_seeds",
        "Upsert seed accounts [{sec_user_id, nickname?, source_url?}].",
        _schema(
            {
                "items": {
                    "type": "array",
                    "description": "Seed entries",
                    "items": {"type": "object"},
                },
                "batch": _string("batch", "Batch label (optional)"),
            },
            ["items"],
        ),
        _queue_import_seeds,
    ),
    ToolSpec(
        "dyvine.queue.enqueue",
        "Enqueue all non-excluded seeds into a round (idempotent).",
        _schema(
            {
                "round": _string("round", "Round name"),
                "mode": _string("mode", "Download mode"),
                "cutoff": _string("cutoff", "Cutoff datetime (optional)"),
                "note": _string("note", "Round note (optional)"),
            },
            ["round", "mode"],
        ),
        _queue_enqueue,
    ),
    ToolSpec(
        "dyvine.queue.claim",
        "Claim the oldest pending entry (serial-group aware).",
        _schema({"round": _string("round", "Round filter (optional)")}, []),
        _queue_claim,
    ),
    ToolSpec(
        "dyvine.queue.update",
        "Patch a queue entry (status/checkpoint/counters).",
        _schema(
            {
                "key": _string("key", "Entry key {round}:{sec}"),
                "fields": {
                    "type": "object",
                    "description": "Fields to update",
                },
            },
            ["key", "fields"],
        ),
        _queue_update,
    ),
    ToolSpec(
        "dyvine.queue.list",
        "List queue entries oldest-first with filters.",
        _schema(
            {
                "round": _string("round", "Round filter (optional)"),
                "status": _string("status", "Status filter (optional)"),
                "limit": _integer("limit", "Max rows", 100),
                "offset": _integer("offset", "Skip rows", 0),
            },
            [],
        ),
        _queue_list,
    ),
    ToolSpec(
        "dyvine.queue.status",
        "Tally queue entries by status (one round or all).",
        _schema({"round": _string("round", "Round filter (optional)")}, []),
        _queue_status,
    ),
    ToolSpec(
        "dyvine.queue.release",
        "Requeue entries whose claimer stopped heartbeating.",
        _schema(
            {
                "stale_after_seconds": {
                    "type": "number",
                    "description": "Stale threshold",
                    "default": 600.0,
                },
                "max_attempts": _integer("n", "Max attempts", 8),
            },
            [],
        ),
        _queue_release,
    ),
    ToolSpec(
        "dyvine.rounds.create",
        "Create a delivery round header (idempotent).",
        _schema(
            {
                "round": _string("round", "Round name"),
                "note": _string("note", "Note (optional)"),
            },
            ["round"],
        ),
        _rounds_create,
    ),
    ToolSpec(
        "dyvine.rounds.list",
        "List delivery rounds in creation order.",
        _schema({}, []),
        _rounds_list,
    ),
    ToolSpec(
        "dyvine.delivery.send_account",
        "Deliver one account's pending media files to its Feishu group.",
        _schema(
            {
                "nickname": _string("n", "Account nickname"),
                "chat_id": _string("chat", "Feishu chat id"),
                "homepage": _string("home", "Douyin homepage URL"),
                "user_dir": _string("dir", "Local media directory"),
                "sec_user_id": _string("sec", "Sec id for status rows"),
                "cutoff": _string("cutoff", "Incremental cutoff (optional)"),
                "batch": _string("batch", "Batch label (default batch)"),
                "starter_message_id": _string("mid", "Reuse topic (optional)"),
                "already_sent": {
                    "type": "array",
                    "description": "Sent paths to skip",
                    "items": {"type": "string"},
                },
                "already_failed": {
                    "type": "array",
                    "description": "Failed paths to skip",
                    "items": {"type": "string"},
                },
                "known_permanent": {
                    "type": "array",
                    "description": "Permanent-failure paths",
                    "items": {"type": "string"},
                },
            },
            ["nickname", "chat_id", "homepage", "user_dir"],
        ),
        _delivery_send_account,
    ),
    ToolSpec(
        "dyvine.delivery.status",
        "Read delivery counters by nickname (or sec_user_id).",
        _schema(
            {
                "nickname": _string("n", "Account nickname"),
                "sec_user_id": _string("sec", "Sec id (optional)"),
            },
            [],
        ),
        _delivery_status,
    ),
    ToolSpec(
        "dyvine.notify.send",
        "Send a message via hermes-native channels (non-Feishu).",
        _schema(
            {
                "target": _string("t", "hermes send target"),
                "message": _string("m", "Message text"),
                "subject": _string("s", "Subject (optional)"),
                "timeout_seconds": {
                    "type": "number",
                    "description": "Timeout",
                    "default": 120.0,
                },
            },
            ["target", "message"],
        ),
        _notify_send,
    ),
    ToolSpec(
        "dyvine.profiles.upsert",
        "Insert or patch a cached profile snapshot.",
        _schema(
            {
                "sec_user_id": _string("sec", "Douyin sec_user_id"),
                "fields": {
                    "type": "object",
                    "description": "Snapshot fields",
                },
            },
            ["sec_user_id", "fields"],
        ),
        _profiles_upsert,
    ),
    ToolSpec(
        "dyvine.profiles.get",
        "Fetch a cached profile snapshot.",
        _schema({"sec_user_id": _string("sec", "Douyin sec_user_id")}, ["sec_user_id"]),
        _profiles_get,
    ),
    ToolSpec(
        "dyvine.operation.get",
        "Fetch any operation row by id.",
        _schema({"operation_id": _string("id", "Operation id")}, ["operation_id"]),
        _operation_get,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)
