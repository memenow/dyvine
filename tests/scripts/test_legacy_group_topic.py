"""Adopt raw legacy topic keys only with an exact frozen chat identity."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import import_legacy_send_progress as importer  # noqa: E402


def _queue(round_name: str, sec: str, nickname: str, chat: str) -> dict[str, str]:
    return {
        "round": round_name,
        "sec_user_id": sec,
        "nickname": nickname,
        "chat_id": chat,
    }


def test_colon_nickname_uses_last_chat_separator_and_existing_work_db(
    tmp_path: Path,
) -> None:
    connection = importer.staging._work_database(tmp_path / "work.sqlite3")
    raw_key = "A:oc_inside:name:oc_chat_one"
    first = "/f/send_progress_weekly0913_w1_L0.json"
    later = "/f/send_progress_weekly0913_w2_L0.json"
    connection.executemany(
        "INSERT INTO source_topics VALUES (?, ?, ?, ?)",
        [
            (first, "weekly0913", raw_key, "topic-one"),
            (later, "weekly0913", raw_key, "topic-one"),
        ],
    )
    connection.commit()
    entries = [_queue("weekly0913", "sec-one", "A:oc_inside:name", "oc_chat_one")]

    groups = importer._group_topic_candidates(
        connection, entries, {"A:oc_inside:name": {"sec-one"}}
    )

    assert groups[("weekly0913", "sec-one")]["topic_message_id"] == "topic-one"
    assert groups[("weekly0913", "sec-one")]["raw_topic_key"] == raw_key
    assert groups[("weekly0913", "sec-one")]["source_file"] == later
    assert importer._topic_identity("A:oc_") is None
    assert importer._topic_identity(":oc_chat") is None
    connection.close()


def test_duplicate_nicknames_require_distinct_chats_and_stable_topics(
    tmp_path: Path,
) -> None:
    connection = importer.staging._work_database(tmp_path / "work.sqlite3")
    source = "/f/send_progress_weekly0913_w1_L0.json"
    connection.executemany(
        "INSERT INTO source_topics VALUES (?, ?, ?, ?)",
        [
            (source, "weekly0913", "Twin:oc_chat_one", "topic-one"),
            (source, "weekly0913", "Twin:oc_chat_two", "topic-two"),
            (source, "weekly0913", "Twin", "nickname-only-topic"),
        ],
    )
    entries = [
        _queue("weekly0913", "sec-one", "Twin", "oc_chat_one"),
        _queue("weekly0913", "sec-two", "Twin", "oc_chat_two"),
    ]
    mapping = {"Twin": {"sec-one", "sec-two"}}

    groups = importer._group_topic_candidates(connection, entries, mapping)
    assert set(groups) == {("weekly0913", "sec-one"), ("weekly0913", "sec-two")}
    assert groups[("weekly0913", "sec-two")]["topic_message_id"] == "topic-two"

    connection.execute(
        "INSERT INTO source_topics VALUES (?, ?, ?, ?)",
        (
            "/f/send_progress_weekly0913_w2_L0.json",
            "weekly0913",
            "Twin:oc_chat_one",
            "conflicting-topic",
        ),
    )
    groups = importer._group_topic_candidates(connection, entries, mapping)
    assert set(groups) == {("weekly0913", "sec-two")}
    assert (
        importer._group_topic_candidates(connection, entries + [entries[1]], mapping)
        == {}
    )
    connection.close()
