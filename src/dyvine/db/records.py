"""Service-facing persistence records.

These dataclasses are the stable contract between the repository layer
(``dyvine.db``) and the domain services. They intentionally mirror the
HTTP response shapes (ISO-8601 timestamp strings, plain dicts) so
services never touch ORM objects or driver rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class OperationRecord:
    """Structured representation of an asynchronous operation."""

    operation_id: str
    operation_type: str
    subject_id: str
    status: str
    message: str
    progress: float | None
    total_items: int | None
    completed_items: int | None
    download_path: str | None
    error: str | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    def to_response(self) -> dict[str, Any]:
        """Convert the record into the public API response shape."""
        return {
            "operation_id": self.operation_id,
            "task_id": self.operation_id,
            "operation_type": self.operation_type,
            "subject_id": self.subject_id,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "total_items": self.total_items,
            "completed_items": self.completed_items,
            "downloaded_items": self.completed_items,
            "download_path": self.download_path,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class WatchSubscriptionRecord:
    """Structured representation of a persisted watch subscription."""

    subscription_id: str
    user_id: str
    enabled: bool
    live_poll_seconds: int
    post_poll_seconds: int
    checkpoint: dict[str, Any]
    last_live_check: str | None
    last_post_check: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class QueueEntryRecord:
    """One durable download-queue row keyed by ``{round}:{sec_user_id}``."""

    key: str
    round: str
    kind: str | None
    nickname: str
    sec_user_id: str
    chat_id: str | None
    homepage: str | None
    mode: str
    cutoff: str | None
    status: str
    operation_id: str | None
    op_status: str | None
    op_message: str | None
    attempts: int
    serial_group: str | None
    extra: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SendStatusRecord:
    """Per-account delivery counters."""

    nickname: str
    sec_user_id: str | None
    chat_id: str | None
    batch: str | None
    total_files: int | None
    sent_files: int | None
    failed_files: int | None
    status: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class UserSendStatusRecord:
    """Legacy per-username delivery counters (read-only)."""

    id: int
    username: str
    local_files: int
    sent_files: int
    failed_files: int
    status: str
    failed_details: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class SeedAccountRecord:
    """One seed-universe account with its exclusion flag."""

    sec_user_id: str
    nickname: str | None
    source_url: str | None
    source: str
    batch: str | None
    excluded: bool
    created_at: str
    updated_at: str


@dataclass(slots=True)
class UserProfileRecord:
    """Cached Douyin profile snapshot."""

    sec_user_id: str
    nickname: str | None = None
    nickname_raw: str | None = None
    avatar_url: str | None = None
    signature: str | None = None
    signature_raw: str | None = None
    uid: str | None = None
    short_id: str | None = None
    unique_id: str | None = None
    room_id: str | None = None
    city: str | None = None
    country: str | None = None
    ip_location: str | None = None
    school_name: str | None = None
    gender: int | None = None
    user_age: int | None = None
    aweme_count: int | None = None
    favoriting_count: int | None = None
    follower_count: int | None = None
    following_count: int | None = None
    total_favorited: int | None = None
    mplatform_followers_count: int | None = None
    mix_count: int | None = None
    live_status: int | None = None
    is_ban: bool | None = None
    is_block: bool | None = None
    is_blocked: bool | None = None
    is_star: bool | None = None
    last_aweme_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class DeliveryRoundRecord:
    """One delivery-round header."""

    round: str
    note: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class DeliveryGroupRecord:
    """One group's externally visible creation and topic checkpoints."""

    key: str
    round: str
    sec_user_id: str
    nickname: str
    create_name: str
    owner_open_id: str | None
    status: str
    create_uuid: str | None
    create_started_at: str | None
    chat_id: str | None
    topic_status: str
    topic_uuid: str | None
    topic_started_at: str | None
    topic_message_id: str | None
    avatar_url: str | None
    avatar_key: str | None
    legacy_source_file: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class FileDeliveryRecord:
    """A file's stable identity and exact Feishu request checkpoint."""

    media_id: str
    round: str
    sec_user_id: str
    relative_path: str
    content_sha256: str | None
    chat_id: str | None
    parent_id: str | None
    status: str
    file_key: str | None
    send_uuid: str | None
    send_started_at: str | None
    message_id: str | None
    legacy_source_path: str | None
    legacy_progress_file: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class LegacyEvidenceRecord:
    """Legacy send path that cannot safely become an automatic send."""

    evidence_id: str
    source_file: str
    legacy_path: str
    legacy_state: str
    nickname: str | None
    sec_user_id: str | None
    reason: str
    created_at: str
    updated_at: str
