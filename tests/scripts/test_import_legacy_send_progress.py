"""Operator checks for stream parsing and conservative legacy send import."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOWNLOAD_ROOT = PurePosixPath("/opt/dyvine/data/douyin/downloads")


def _load_script() -> Any:
    path = ROOT / "scripts" / "import_legacy_send_progress.py"
    spec = importlib.util.spec_from_file_location("import_legacy_send_progress", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _path(media_type: str, nickname: str, filename: str) -> str:
    return str(
        DOWNLOAD_ROOT / "douyin" / media_type / nickname / "2026-09-13_post" / filename
    )


class FakeLedger:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], dict[str, str | None]] = {}
        self.permanent: dict[tuple[str, str], dict[str, str | None]] = {}
        self.evidence: dict[tuple[str, str, str], dict[str, str | None]] = {}
        self.groups: dict[tuple[str, str], dict[str, str]] = {}

    async def reserve_legacy_sent_batch(
        self, rows: list[dict[str, str | None]]
    ) -> tuple[int, int]:
        inserted = 0
        for row in rows:
            key = (str(row["sec_user_id"]), str(row["relative_path"]))
            if key not in self.files:
                self.files[key] = row
                inserted += 1
        return inserted, len(rows) - inserted

    async def reserve_legacy_permanent_failure_batch(
        self, rows: list[dict[str, str | None]]
    ) -> tuple[int, int]:
        inserted = 0
        for row in rows:
            key = (str(row["sec_user_id"]), str(row["relative_path"]))
            if key not in self.files and key not in self.permanent:
                self.permanent[key] = row
                inserted += 1
        return inserted, len(rows) - inserted

    async def upsert_legacy_evidence_batch(
        self, rows: list[dict[str, str | None]]
    ) -> tuple[int, int]:
        inserted = 0
        for row in rows:
            key = (
                str(row["source_file"]),
                str(row["legacy_state"]),
                str(row["legacy_path"]),
            )
            if key not in self.evidence:
                self.evidence[key] = row
                inserted += 1
        return inserted, len(rows) - inserted

    async def import_legacy_group_topic(self, **row: str) -> None:
        key = (row["round"], row["sec_user_id"])
        if key in self.groups and self.groups[key] != row:
            raise ValueError("conflicting legacy group")
        self.groups[key] = row


def _sources(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    queue = state / "download_queue.json"
    seed = tmp_path / "seed_users.json"
    _write_json(
        queue,
        {
            "entries": [
                {
                    "key": "weekly0913:sec-alpha",
                    "round": "weekly0913",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "oc_oldchat",
                    "status": "op_done",
                },
                {
                    "key": "weekly0913:sec-one",
                    "round": "weekly0913",
                    "nickname": "Twin",
                    "sec_user_id": "sec-one",
                    "status": "pending",
                },
                {
                    "key": "weekly0913:sec-beta",
                    "round": "weekly0913",
                    "nickname": "Beta",
                    "sec_user_id": "sec-beta",
                    "status": "pending",
                },
                {
                    "key": "weekly0920:sec-alpha",
                    "round": "weekly0920",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "oc_oldchat2",
                    "status": "op_done",
                },
            ]
        },
    )
    _write_json(
        seed,
        [
            {"nickname": "Alpha", "sec_user_id": "sec-alpha"},
            {"nickname": "Twin", "sec_user_id": "sec-two"},
            {"nickname": "Beta", "sec_user_id": "sec-beta"},
        ],
    )
    first = state / "send_progress_weekly0913_w167_L0.json"
    second = state / "send_progress_weekly0920_w168_L0.json"
    _write_json(
        first,
        {
            "sent": [
                _path("post", "Alpha", "one.mp4"),
                _path("post", "Alpha", "collision.mp4"),
                _path("post", "Twin", "ambiguous.mp4"),
                "/outside/douyin/post/Alpha/file.mp4",
            ],
            "failed": [_path("post", "Alpha", "failed.mp4")],
            "users": {"Alpha": {"chat_id": "oc_oldchat", "sent": 200}},
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
            "completed": False,
        },
    )
    _write_json(
        second,
        {
            "sent": [
                _path("post", "Alpha", "one.mp4"),
                _path("photo", "Alpha", "collision.mp4"),
            ],
            "failed": [_path("post", "Alpha", "failed.mp4")],
            "users": {},
            "user_topics": {},
            "completed": True,
        },
    )
    return state, seed, first, second


def _args(script: Any, state: Path, seed: Path, work_db: Path, *more: str) -> Any:
    return script.parse_args(
        [
            "--state-dir",
            str(state),
            "--seed-path",
            str(seed),
            "--work-db",
            str(work_db),
            *more,
        ]
    )


async def test_import_only_marks_unique_noncolliding_sent_paths(tmp_path: Path) -> None:
    script = _load_script()
    state, seed, first, _second = _sources(tmp_path)
    ledger = FakeLedger()
    args = _args(script, state, seed, tmp_path / "checkpoint.sqlite3")

    report = await script.run(args, ledger)

    assert (report.sources, report.staged, report.reused) == (2, 2, 0)
    assert (report.sent, report.sent_unique, report.sent_duplicates) == (6, 5, 1)
    assert (report.failed, report.failed_unique, report.failed_duplicates) == (2, 1, 1)
    assert (report.safe_sent, report.needs_review, report.inserted) == (1, 5, 1)
    assert (report.audit_inserted, report.audit_existing) == (6, 0)
    assert list(ledger.files) == [("sec-alpha", "2026-09-13_post/one.mp4")]
    imported = next(iter(ledger.files.values()))
    assert imported["legacy_source_path"] == _path("post", "Alpha", "one.mp4")
    assert imported["legacy_progress_file"] == str(first.resolve())
    assert imported["chat_id"] is None and imported["parent_id"] is None
    assert {value["legacy_state"] for value in ledger.evidence.values()} == {
        "sent_ambiguous",
        "failed",
    }
    assert len(ledger.evidence) == 6
    assert {
        value["source_file"]
        for value in ledger.evidence.values()
        if value["legacy_state"] == "failed"
    } == {str(first.resolve()), str(_second.resolve())}
    assert report.group_candidates == 1
    assert ledger.groups == {}


async def test_checkpoint_rerun_is_idempotent_and_rejects_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    state, seed, first, _second = _sources(tmp_path)
    ledger = FakeLedger()
    args = _args(script, state, seed, tmp_path / "checkpoint.sqlite3")
    sent_inserts: list[str] = []
    original_database = script.staging._work_database
    original_identity = script.staging._identity
    identity_calls = 0

    def tracked_identity(path: str, root: PurePosixPath) -> Any:
        nonlocal identity_calls
        identity_calls += 1
        return original_identity(path, root)

    def tracked_database(path: Path) -> Any:
        connection = original_database(path)
        connection.set_trace_callback(
            lambda sql: (
                sent_inserts.append(sql)
                if "INSERT OR IGNORE INTO sent_paths" in sql
                else None
            )
        )
        return connection

    monkeypatch.setattr(script.staging, "_work_database", tracked_database)
    monkeypatch.setattr(script.staging, "_identity", tracked_identity)
    partial = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--progress-glob",
        str(first),
        "--dry-run",
    )
    await script.run(partial)
    resumed = await script.run(args, ledger)
    assert (resumed.staged, resumed.reused) == (1, 1)
    assert len(sent_inserts) == 5
    assert identity_calls == 7
    fresh = await script.run(
        _args(script, state, seed, tmp_path / "fresh.sqlite3", "--dry-run")
    )
    assert (resumed.sent, resumed.sent_unique, resumed.safe_sent) == (
        fresh.sent,
        fresh.sent_unique,
        fresh.safe_sent,
    )

    repeated = await script.run(args, ledger)
    assert (repeated.staged, repeated.reused) == (0, 2)
    assert (repeated.inserted, repeated.existing) == (0, 1)
    assert (repeated.audit_inserted, repeated.audit_existing) == (0, 6)
    assert len(ledger.files) == 1 and len(ledger.evidence) == 6
    assert len(ledger.groups) == 0

    first.write_text(first.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpointed source changed"):
        await script.run(args, ledger)


async def test_dry_run_never_calls_delivery_repository(tmp_path: Path) -> None:
    script = _load_script()
    state, seed, _first, _second = _sources(tmp_path)
    args = _args(script, state, seed, tmp_path / "checkpoint.sqlite3", "--dry-run")
    report = await script.run(args)
    assert (report.safe_sent, report.needs_review) == (1, 5)
    assert (report.inserted, report.existing) == (0, 0)
    assert report.group_candidates == 1


async def test_reconciliation_lists_every_queue_entry_without_releasing_it(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, _first, _second = _sources(tmp_path)
    output = tmp_path / "reconciliation.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(output),
    )

    await script.run(args)

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 4
    assert [row["classification"] for row in rows] == [
        "needs_review",
        "needs_review",
        "resume_required",
        "needs_review",
    ]
    assert rows[0]["first_seen_safe_sent_paths"] == 1
    assert rows[0]["snapshot_sent_entries"] == 2
    assert rows[0]["ambiguous_sent_paths"] == 1
    assert rows[0]["failed_paths"] == 1
    assert rows[3]["first_seen_safe_sent_paths"] == 0
    assert rows[3]["snapshot_sent_entries"] == 2
    assert all(row["send_blocked"] for row in rows)
    assert output.stat().st_mode & 0o777 == 0o600


async def test_checkpoint_rejects_changed_identity_sources(tmp_path: Path) -> None:
    script = _load_script()
    state, seed, _first, _second = _sources(tmp_path)
    args = _args(script, state, seed, tmp_path / "checkpoint.sqlite3", "--dry-run")
    await script.run(args)
    seeds = json.loads(seed.read_text(encoding="utf-8"))
    seeds.append({"nickname": "New", "sec_user_id": "sec-new"})
    _write_json(seed, seeds)

    with pytest.raises(ValueError, match="changed since checkpoint"):
        await script.run(args)


async def test_reconciliation_cannot_replace_a_source_file(tmp_path: Path) -> None:
    script = _load_script()
    state, seed, first, _second = _sources(tmp_path)
    before = first.read_bytes()
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(first),
    )
    with pytest.raises(ValueError, match="replace a source"):
        await script.run(args)
    assert first.read_bytes() == before


async def test_terminal_candidate_requires_nonzero_done_and_matching_chat(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, first, second = _sources(tmp_path)
    second.unlink()
    _write_json(
        state / "download_queue.json",
        {
            "entries": [
                {
                    "key": "weekly0913:sec-alpha",
                    "round": "weekly0913",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "oc_oldchat",
                    "status": "op_done",
                }
            ]
        },
    )
    _write_json(seed, [{"nickname": "Alpha", "sec_user_id": "sec-alpha"}])
    path = _path("post", "Alpha", "one.mp4")
    _write_json(
        first,
        {
            "sent": [path],
            "failed": [],
            "users": {
                "Alpha": {
                    "chat_id": "oc_oldchat",
                    "done": True,
                    "failed": 0,
                    "sent": 1,
                    "total": 1,
                }
            },
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
        },
    )
    output = tmp_path / "review.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--reconcile-output",
        str(output),
    )
    ledger = FakeLedger()

    report = await script.run(args, ledger)

    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["classification"] == "terminal_candidate"
    assert row["terminal_candidate"] is True
    assert row["candidate_total_files"] == 1
    assert row["candidate_chat_id"] == "oc_oldchat"
    assert row["adoptable_topic_message_id"] == "old-topic"
    assert row["group_topic_raw_key"] == "Alpha:oc_oldchat"
    assert row["send_blocked"] and not row["feishu_history_verified"]
    assert report.group_candidates == 1
    assert (await script.run(args, ledger)).reused == 1

    _write_json(
        first,
        {
            "sent": [],
            "failed": [],
            "users": {
                "Alpha": {
                    "chat_id": "oc_oldchat",
                    "done": True,
                    "failed": 0,
                    "sent": 0,
                    "total": 0,
                }
            },
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
        },
    )
    zero_args = _args(
        script,
        state,
        seed,
        tmp_path / "zero_checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(output),
    )
    await script.run(zero_args)
    zero_row = json.loads(output.read_text(encoding="utf-8").strip())
    assert zero_row["classification"] == "needs_review"
    assert zero_row["legacy_zero_zero_snapshots"] == 1
    assert zero_row["terminal_candidate"] is False


async def test_reconciliation_keeps_duplicate_legacy_keys_as_distinct_accounts(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, first, second = _sources(tmp_path)
    second.unlink()
    _write_json(
        state / "download_queue.json",
        {
            "entries": [
                {
                    "key": "weekly0913:stale",
                    "round": "weekly0913",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "chat-a",
                    "status": "pending",
                },
                {
                    "key": "weekly0913:stale",
                    "round": "weekly0913",
                    "nickname": "Beta",
                    "sec_user_id": "sec-beta",
                    "chat_id": "chat-b",
                    "status": "pending",
                },
            ]
        },
    )
    _write_json(
        seed,
        [
            {"nickname": "Alpha", "sec_user_id": "sec-alpha"},
            {"nickname": "Beta", "sec_user_id": "sec-beta"},
        ],
    )
    _write_json(first, {"sent": [], "failed": [], "users": {}, "user_topics": {}})
    output = tmp_path / "review.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(output),
    )

    await script.run(args)

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["key"] for row in rows] == [
        "weekly0913:sec-alpha",
        "weekly0913:sec-beta",
    ]
    assert [row["legacy_key"] for row in rows] == [
        "weekly0913:stale",
        "weekly0913:stale",
    ]
    assert [row["source_index"] for row in rows] == [0, 1]


async def test_optional_extra_sources_are_audited_without_confirming_cache_sends(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, _first, _second = _sources(tmp_path)
    permanent_source = state / "permanent_failures.json"
    sent_index = state / "report_sent_index.json"
    queue_path = state / "download_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["entries"].append(
        {
            "round": "weekly0913",
            "nickname": "Gamma",
            "sec_user_id": "sec-gamma",
            "status": "pending",
        }
    )
    _write_json(queue_path, queue)
    seeds = json.loads(seed.read_text(encoding="utf-8"))
    seeds.append({"nickname": "Gamma", "sec_user_id": "sec-gamma"})
    _write_json(seed, seeds)
    safe_failure = _path("post", "Beta", "permanent_only.mp4")
    conflicting_failure = _path("post", "Beta", "cache_conflict.mp4")
    cached_only = _path("post", "Alpha", "cache_only.mp4")
    missing_source = "/missing/send_progress_weekly0913_w999_L0.json"
    _write_json(
        permanent_source,
        [
            safe_failure,
            conflicting_failure,
            _path("post", "Alpha", "one.mp4"),
            _path("post", "Twin", "unmapped.mp4"),
        ],
    )
    _write_json(
        sent_index,
        {
            "files": {
                missing_source: {
                    "m": 1.5,
                    "s": 123,
                    "delta": [
                        cached_only,
                        cached_only,
                        _path("post", "Alpha", "one.mp4"),
                        _path("post", "Twin", "unknown.mp4"),
                        conflicting_failure,
                        _path("post", "Gamma", "cache_only.mp4"),
                        "/outside/not-account.mp4",
                    ],
                }
            }
        },
    )
    output = tmp_path / "review.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--permanent-failures",
        str(permanent_source),
        "--sent-index",
        str(sent_index),
        "--reconcile-output",
        str(output),
    )
    ledger = FakeLedger()

    report = await script.run(args, ledger)

    assert (report.permanent_paths, report.permanent_safe) == (4, 1)
    assert (report.permanent_inserted, report.permanent_existing) == (1, 0)
    assert (report.cache_paths, report.cache_only) == (6, 5)
    assert list(ledger.permanent) == [
        ("sec-beta", "2026-09-13_post/permanent_only.mp4")
    ]
    assert next(iter(ledger.permanent.values()))["legacy_source_path"] == safe_failure
    assert {row["legacy_state"] for row in ledger.evidence.values()} >= {
        "permanent_failure",
        "permanent_failure_unresolved",
        "sent_unverified",
    }
    cached = [
        row
        for row in ledger.evidence.values()
        if row["legacy_state"] == "sent_unverified"
    ]
    assert len(cached) == 5
    assert any(row["legacy_path"] == cached_only for row in cached)
    assert any(row["sec_user_id"] is None for row in cached)
    rows = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["cache_only_unverified_paths"] == 2
    assert rows[0]["permanent_unresolved_paths"] == 1
    assert rows[2]["permanent_failure_paths"] == 1
    assert rows[2]["cache_only_unverified_paths"] == 2
    assert rows[3]["cache_only_unverified_paths"] == 2
    assert rows[4]["legacy_status"] == "pending"
    assert rows[4]["permanent_failure_paths"] == 0
    assert rows[4]["cache_only_unverified_paths"] == 2
    assert rows[4]["classification"] == "needs_review"
    assert all(row["send_blocked"] for row in rows)
    repeated = await script.run(args, ledger)
    assert (repeated.permanent_inserted, repeated.permanent_existing) == (0, 1)
    sent_index.write_text(sent_index.read_text() + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpointed extra source changed"):
        await script.run(args, ledger)


async def test_terminal_candidate_rejects_failed_path_repeated_from_older_round(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, current, other = _sources(tmp_path)
    other.unlink()
    prior = state / "send_progress_weekly0906_w166_L0.json"
    failed_path = _path("post", "Alpha", "failed.mp4")
    _write_json(prior, {"sent": [], "failed": [failed_path], "users": {}})
    _write_json(
        current,
        {
            "sent": [_path("post", "Alpha", "one.mp4")],
            "failed": [failed_path],
            "users": {
                "Alpha": {
                    "chat_id": "oc_oldchat",
                    "done": True,
                    "failed": 0,
                    "sent": 1,
                    "total": 1,
                }
            },
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
        },
    )
    _write_json(
        state / "download_queue.json",
        {
            "entries": [
                {
                    "round": "weekly0913",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "oc_oldchat",
                    "status": "op_done",
                }
            ]
        },
    )
    _write_json(seed, [{"nickname": "Alpha", "sec_user_id": "sec-alpha"}])
    output = tmp_path / "review.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(output),
    )

    await script.run(args)

    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["failed_paths"] == 1
    assert row["snapshot_failed_entries"] == 1
    assert row["classification"] == "needs_review"
    assert row["terminal_candidate"] is False


async def test_terminal_candidate_requires_latest_complete_snapshot(
    tmp_path: Path,
) -> None:
    script = _load_script()
    state, seed, first, other = _sources(tmp_path)
    other.unlink()
    later = state / "send_progress_weekly0913_w168_L0.json"
    _write_json(
        state / "download_queue.json",
        {
            "entries": [
                {
                    "round": "weekly0913",
                    "nickname": "Alpha",
                    "sec_user_id": "sec-alpha",
                    "chat_id": "oc_oldchat",
                    "status": "op_done",
                }
            ]
        },
    )
    _write_json(seed, [{"nickname": "Alpha", "sec_user_id": "sec-alpha"}])
    old_path = _path("post", "Alpha", "one.mp4")
    _write_json(
        first,
        {
            "sent": [old_path],
            "failed": [],
            "users": {
                "Alpha": {
                    "chat_id": "oc_oldchat",
                    "done": True,
                    "failed": 0,
                    "sent": 1,
                    "total": 1,
                }
            },
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
        },
    )
    _write_json(
        later,
        {
            "sent": [old_path],
            "failed": [],
            "users": {
                "Alpha": {
                    "chat_id": "oc_oldchat",
                    "done": False,
                    "failed": 0,
                    "sent": 1,
                    "total": 2,
                }
            },
            "user_topics": {"Alpha:oc_oldchat": "old-topic"},
        },
    )
    os.utime(first, ns=(first.stat().st_atime_ns, later.stat().st_mtime_ns + 1))
    output = tmp_path / "review.jsonl"
    args = _args(
        script,
        state,
        seed,
        tmp_path / "checkpoint.sqlite3",
        "--dry-run",
        "--reconcile-output",
        str(output),
    )

    await script.run(args)

    row = json.loads(output.read_text(encoding="utf-8").strip())
    assert row["classification"] == "needs_review"
    assert row["terminal_candidate"] is False
    assert row["first_seen_safe_sent_paths"] == 1
    assert row["snapshot_sent_entries"] == 1
