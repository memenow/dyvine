"""Frozen queue and legacy topic keys identify exact group candidates."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import legacy_group_candidates as candidates  # noqa: E402


def _queue(round_name: str, sec: str, nickname: str, chat: str) -> dict[str, str]:
    return {
        "round": round_name,
        "sec_user_id": sec,
        "nickname": nickname,
        "chat_id": chat,
    }


def test_topics_embed_chat_id_and_queue_matches_exact_round() -> None:
    entries = [
        _queue("weekly0913", "sec-a", "Alpha", "oc_one"),
        _queue("weekly0920", "sec-a", "Alpha", "oc_two"),
        _queue("weekly0913", "sec-b", "Beta", "oc_three"),
    ]
    topics = [
        ("/f1", "weekly0913", "Alpha:oc_one", "om_one"),
        ("/f2", "weekly0920", "Alpha:oc_two", "om_two"),
        ("/f3", "weekly0913", "Beta:oc_other", "om_other"),
        ("/f4", "weekly0913", "Bare", "om_bare"),
    ]

    rows, unmatched = candidates._candidates(entries, topics)

    assert [row.topic_message_id for row in rows] == ["om_one", "om_two", None]
    assert rows[2].issue == "missing_or_conflicting_topic"
    assert {row["reason"] for row in unmatched} == {
        "no_exact_queue_identity",
        "topic_key_has_no_chat_id",
    }


def test_same_round_shared_nickname_uses_distinct_chat_topics() -> None:
    entries = [
        _queue("r", "sec-a", "Twin", "oc_one"),
        _queue("r", "sec-b", "Twin", "oc_two"),
    ]
    topics = [
        ("/one", "r", "Twin:oc_one", "om_one"),
        ("/two", "r", "Twin:oc_two", "om_two"),
    ]

    rows, unmatched = candidates._candidates(entries, topics)

    assert [row.issue for row in rows] == [None, None]
    assert [row.sec_user_id for row in rows] == ["sec-a", "sec-b"]
    assert [row.topic_message_id for row in rows] == ["om_one", "om_two"]
    assert unmatched == []


def test_round_account_and_shared_chat_conflicts_are_held() -> None:
    entries = [
        _queue("r", "sec-c", "Gamma", "oc_three"),
        _queue("r", "sec-c", "Gamma", "oc_other"),
        _queue("r", "sec-d", "Delta", "oc_shared"),
        _queue("r", "sec-e", "Echo", "oc_shared"),
    ]
    topics = [
        ("/f", "r", row["nickname"] + ":" + row["chat_id"], f"om_{index}")
        for index, row in enumerate(entries)
    ]

    rows, _ = candidates._candidates(entries, topics)

    assert [row.issue for row in rows] == [
        "round_account_has_multiple_queue_rows",
        "round_account_has_multiple_queue_rows",
        "round_chat_has_multiple_queue_rows",
        "round_chat_has_multiple_queue_rows",
    ]


def test_changed_and_reused_nicknames_across_rounds_keep_exact_identity() -> None:
    entries = [
        _queue("weekly0906", "sec-a", "Old", "oc_old"),
        _queue("weekly0913", "sec-a", "New", "oc_new"),
        _queue("weekly0913", "sec-b", "Old", "oc_reused"),
    ]
    topics = [
        ("/old", "weekly0906", "Old:oc_old", "om_old"),
        ("/new", "weekly0913", "New:oc_new", "om_new"),
        ("/reused", "weekly0913", "Old:oc_reused", "om_reused"),
    ]

    rows, unmatched = candidates._candidates(entries, topics)

    assert [row.issue for row in rows] == [None, None, None]
    assert [row.topic_message_id for row in rows] == [
        "om_old",
        "om_new",
        "om_reused",
    ]
    assert [row.sec_user_id for row in rows] == ["sec-a", "sec-a", "sec-b"]
    assert unmatched == []


def test_conflicting_topics_for_exact_account_chat_stay_held() -> None:
    entries = [_queue("r", "sec-a", "Old", "oc_one")]
    topics = [
        ("/first", "r", "Old:oc_one", "om_one"),
        ("/second", "r", "Old:oc_one", "om_two"),
    ]

    rows, _ = candidates._candidates(entries, topics)

    assert rows[0].issue == "missing_or_conflicting_topic"
    assert rows[0].topic_message_id is None


def test_incomplete_queue_identity_is_held_without_matching() -> None:
    entries = [
        {"round": "r", "sec_user_id": "sec-a", "nickname": "Old"},
        {"round": "r", "sec_user_id": "", "nickname": "Old", "chat_id": "oc_one"},
        {"round": "r", "sec_user_id": "sec-a", "nickname": "  ", "chat_id": "oc_one"},
        "not-an-object",
    ]
    topics = [("/f", "r", "Old:oc_one", "om_one")]

    rows, unmatched = candidates._candidates(entries, topics)

    assert [row.issue for row in rows] == ["incomplete_queue_identity"] * 4
    assert [row.topic_message_id for row in rows] == [None] * 4
    assert [row["reason"] for row in unmatched] == ["no_exact_queue_identity"]


def test_script_uses_no_runtime_asserts() -> None:
    tree = ast.parse(
        (ROOT / "scripts" / "legacy_group_candidates.py").read_text(encoding="utf-8")
    )
    assert [
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)
    ] == []
