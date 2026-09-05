"""Tests for the ``scripts.dyvine_batch`` CLI package (mocked HTTP)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.dyvine_batch import cli, client, config, runners  # noqa: E402


def _settings(**overrides: Any) -> config.BatchSettings:
    """Build settings with fast polling for tests."""
    defaults: dict[str, Any] = {
        "api_url": "http://api.test",
        "api_key": "key",
        "api_prefix": "/api/v1",
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 0.01,
        "timeout": 5.0,
    }
    defaults.update(overrides)
    return config.BatchSettings(**defaults)


def _transport(handler: Any) -> httpx.MockTransport:
    """Wrap a handler function as a mock transport."""
    return httpx.MockTransport(handler)


# ── config ─────────────────────────────────────────────────────────────


def test_load_dotenv_missing_file_yields_empty(tmp_path: Path) -> None:
    """A missing .env file is not an error."""
    assert config.load_dotenv(tmp_path / "nope.env") == {}


def test_load_dotenv_parses_pairs(tmp_path: Path) -> None:
    """Comments, blanks, and valueless lines are skipped."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        '# comment\n\nSECURITY_API_KEY=secret\nEMPTY\nQUOTED="v v"\n',
        encoding="utf-8",
    )
    assert config.load_dotenv(dotenv) == {
        "SECURITY_API_KEY": "secret",
        "QUOTED": "v v",
    }


def test_load_dotenv_strips_export_prefix(tmp_path: Path) -> None:
    """``export KEY=val`` resolves like server-side python-dotenv."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "export SECURITY_API_KEY=secret\n  export API_HOST = example.com\n",
        encoding="utf-8",
    )
    assert config.load_dotenv(dotenv) == {
        "SECURITY_API_KEY": "secret",
        "API_HOST": "example.com",
    }


def test_resolve_settings_empty_port_falls_back(tmp_path: Path) -> None:
    """An empty ``API_PORT=`` behaves like a missing one, not ``host:``."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SECURITY_API_KEY=k\nAPI_HOST=example.com\nAPI_PORT=\n",
        encoding="utf-8",
    )
    resolved = config.resolve_settings(
        api_url=None,
        api_key=None,
        api_prefix=None,
        include_likes=False,
        max_concurrent=3,
        poll_interval=5.0,
        timeout=30.0,
        environ={},
        dotenv_path=dotenv,
    )
    assert resolved.api_url == "http://example.com:8000"


def test_resolve_settings_dotenv_dyvine_keys(tmp_path: Path) -> None:
    """``DYVINE_*`` keys in ``.env`` sit between env and legacy keys."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DYVINE_API_KEY=dotenv-key\n"
        "DYVINE_API_URL=http://dotenv:9000\n"
        "DYVINE_API_PREFIX=/dotenv\n"
        "DYVINE_MAX_POLL_ROUNDS=7\n",
        encoding="utf-8",
    )
    kwargs: dict[str, Any] = {
        "api_url": None,
        "api_key": None,
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
        "dotenv_path": dotenv,
    }
    resolved = config.resolve_settings(**kwargs)
    assert (resolved.api_key, resolved.api_url) == ("dotenv-key", "http://dotenv:9000")
    assert (resolved.api_prefix, resolved.max_poll_rounds) == ("/dotenv", 7)
    # Real env outranks ``.env`` ``DYVINE_*``.
    over = config.resolve_settings(
        **{**kwargs, "environ": {"DYVINE_API_KEY": "env-key"}}
    )
    assert over.api_key == "env-key"


def test_resolve_settings_root_prefix_normalizes_to_empty(tmp_path: Path) -> None:
    """A bare ``/`` prefix means "no prefix", not a literal root segment."""
    resolved = config.resolve_settings(
        api_url="http://example.com:8000",
        api_key="k",
        api_prefix="/",
        include_likes=False,
        max_concurrent=3,
        poll_interval=5.0,
        timeout=30.0,
        environ={},
        dotenv_path=tmp_path / "nope.env",
    )
    assert resolved.api_prefix == ""
    assert "//" not in f"{resolved.api_prefix}/posts/x"


async def test_run_serial_submit_crash_records_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submit-time blow-up records the current user, not a stale job."""

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("submit blew up")

    def _factory() -> Any:
        return httpx.AsyncClient(
            transport=_transport(lambda request: httpx.Response(202, json={}))
        )

    monkeypatch.setattr(runners, "submit_download", _boom)
    jobs = await runners.run_serial(
        _settings(max_poll_rounds=1), ["u1", "u2"], client_factory=_factory
    )
    assert [job.user_id for job in jobs] == ["u1", "u2"]
    assert [job.status for job in jobs] == ["failed", "failed"]
    assert all(job.operation_id is None for job in jobs)


async def test_run_serial_keeps_operation_id_on_poll_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-poll blow-up fails the in-flight job, keeping its ID."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"operation_id": "op-u9"})

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("poll blew up")

    def _factory() -> Any:
        return httpx.AsyncClient(transport=_transport(_handler))

    monkeypatch.setattr(runners, "poll_job", _boom)
    jobs = await runners.run_serial(
        _settings(max_poll_rounds=2), ["u9"], client_factory=_factory
    )
    assert [job.status for job in jobs] == ["failed"]
    assert jobs[0].operation_id == "op-u9"
    assert "poll blew up" in jobs[0].message


def test_resolve_settings_precedence(tmp_path: Path) -> None:
    """Flags beat env, env beats .env, .env beats defaults."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SECURITY_API_KEY=dotenv-key\n", encoding="utf-8")
    base = {
        "api_url": None,
        "api_key": None,
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
    }
    # .env fallback.
    resolved = config.resolve_settings(
        **base, environ={}, dotenv_path=dotenv  # type: ignore[arg-type]
    )
    assert resolved.api_key == "dotenv-key"
    assert resolved.api_url == "http://localhost:8000"
    assert resolved.api_prefix == "/api/v1"
    # Env beats .env.
    resolved = config.resolve_settings(
        **base,
        environ={"DYVINE_API_KEY": "env-key", "DYVINE_API_URL": "http://env:1"},
        dotenv_path=dotenv,  # type: ignore[arg-type]
    )
    assert (resolved.api_key, resolved.api_url) == ("env-key", "http://env:1")
    # Flags beat env.
    resolved = config.resolve_settings(
        **{**base, "api_key": "flag-key", "api_prefix": "/v9"},
        environ={"DYVINE_API_KEY": "env-key"},
        dotenv_path=dotenv,  # type: ignore[arg-type]
    )
    assert (resolved.api_key, resolved.api_prefix) == ("flag-key", "/v9")


def test_resolve_settings_requires_api_key(tmp_path: Path) -> None:
    """No key anywhere is a loud configuration error."""
    with pytest.raises(config.SettingsError, match="API key"):
        config.resolve_settings(
            api_url=None,
            api_key=None,
            api_prefix=None,
            include_likes=False,
            max_concurrent=3,
            poll_interval=5.0,
            timeout=30.0,
            environ={},
            dotenv_path=tmp_path / "nope.env",
        )


def test_resolve_settings_validates_knobs() -> None:
    """Bad prefixes and ranges fail before any network happens."""
    kwargs: dict[str, Any] = {
        "api_url": None,
        "api_key": "k",
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
    }
    with pytest.raises(config.SettingsError, match="prefix"):
        config.resolve_settings(**{**kwargs, "api_prefix": "v1"})
    with pytest.raises(config.SettingsError, match="max-concurrent"):
        config.resolve_settings(**{**kwargs, "max_concurrent": 0})
    with pytest.raises(config.SettingsError, match="poll-interval"):
        config.resolve_settings(**{**kwargs, "poll_interval": 0})


def test_read_user_ids_skips_comments_and_blanks(tmp_path: Path) -> None:
    """Comment and blank lines never become jobs."""
    input_file = tmp_path / "users.txt"
    input_file.write_text("# lead\n\nu1\n  \n# mid\nu2\n", encoding="utf-8")
    assert config.read_user_ids(input_file) == ["u1", "u2"]


def test_read_user_ids_missing_file_raises(tmp_path: Path) -> None:
    """A missing input file raises ``FileNotFoundError``."""
    with pytest.raises(FileNotFoundError):
        config.read_user_ids(tmp_path / "nope.txt")


def test_read_user_ids_empty_file_raises(tmp_path: Path) -> None:
    """A file with no usable IDs is a configuration error."""
    input_file = tmp_path / "empty.txt"
    input_file.write_text("# nothing\n\n", encoding="utf-8")
    with pytest.raises(config.SettingsError, match="No usable user IDs"):
        config.read_user_ids(input_file)


# ── client ─────────────────────────────────────────────────────────────


def test_client_builds_urls_per_mode() -> None:
    """Submit/poll URLs follow the mode and the configured prefix."""
    posts = client.DyvineClient(_settings())
    assert posts.submit_url("u1").endswith("/api/v1/posts/users/u1/posts:download")
    assert posts.poll_url("op1").endswith("/api/v1/posts/operations/op1")
    likes = client.DyvineClient(_settings(include_likes=True, api_prefix="/v2"))
    assert likes.submit_url("u1").endswith(
        "/v2/users/u1/content:download?include_posts=true&include_likes=true"
    )
    assert likes.poll_url("op1").endswith("/v2/users/operations/op1")


async def test_submit_download_accepts_202() -> None:
    """A 202 response becomes a submitted job."""

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "key"
        return httpx.Response(
            202, json={"operation_id": "op-1", "message": "scheduled"}
        )

    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        job = await client.submit_download(http, client.DyvineClient(_settings()), "u1")
    assert (job.operation_id, job.status) == ("op-1", "submitted")


async def test_submit_download_maps_rejection_and_timeout() -> None:
    """Non-202 and transport errors become failed jobs, not raises."""

    def _reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad user"})

    async with httpx.AsyncClient(transport=_transport(_reject)) as http:
        job = await client.submit_download(http, client.DyvineClient(_settings()), "u1")
    assert job.status == "failed"
    assert job.message == "bad user"

    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    async with httpx.AsyncClient(transport=_transport(_timeout)) as http:
        job = await client.submit_download(http, client.DyvineClient(_settings()), "u1")
    assert (job.status, job.message) == ("failed", "提交超时")


async def test_poll_job_maps_bulk_counters() -> None:
    """Bulk status payloads map onto the job's counters.

    The payload mirrors the real ``BulkDownloadResponse`` shape, which
    carries no ``progress`` field: the fraction is derived from the
    counters (9/10 here).
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "message": "done",
                "total_posts": 10,
                "total_downloaded": 9,
                "failed_count": 1,
                "error_details": None,
            },
        )

    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        await client.poll_job(http, client.DyvineClient(_settings()), job)
    assert job.status == "completed"
    assert (job.total_downloaded, job.total_posts, job.failed_count) == (9, 10, 1)
    assert job.progress == pytest.approx(0.9)
    assert job.completed_at is not None


async def test_poll_job_zero_total_posts_progress() -> None:
    """Zero totals never divide by zero; progress stays 0."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "total_posts": 0,
                "total_downloaded": 0,
                "failed_count": 0,
            },
        )

    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        await client.poll_job(http, client.DyvineClient(_settings()), job)
    assert job.progress == 0.0


async def test_submit_download_non_dict_payload_fails() -> None:
    """A 202 with a non-object payload becomes a failed job, not a crash."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json=["op-1"])

    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        job = await client.submit_download(http, client.DyvineClient(_settings()), "u1")
    assert job.status == "failed"
    assert "operation_id" in job.message or "payload" in job.message


async def test_submit_download_missing_operation_id_fails() -> None:
    """A 202 without an operation id is un-pollable, so it fails fast."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"message": "scheduled"})

    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        job = await client.submit_download(http, client.DyvineClient(_settings()), "u1")
    assert job.status == "failed"
    assert "operation_id" in job.message


async def test_run_serial_times_out_unfinished_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A job that never turns terminal fails after the round cap."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"operation_id": "op-u1"})
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "total_posts": 2,
                "total_downloaded": 1,
                "failed_count": 0,
            },
        )

    def _factory() -> Any:
        return httpx.AsyncClient(transport=_transport(_handler))

    jobs = await runners.run_serial(
        _settings(max_poll_rounds=2), ["u1"], client_factory=_factory
    )
    assert [job.status for job in jobs] == ["failed"]
    assert "轮询超时" in jobs[0].message


async def test_run_concurrent_times_out_unfinished_jobs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Concurrent leftovers fail after the round cap instead of looping."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            user = request.url.path.split("/")[-2]
            return httpx.Response(202, json={"operation_id": f"op-{user}"})
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "total_posts": 2,
                "total_downloaded": 1,
                "failed_count": 0,
            },
        )

    def _factory() -> Any:
        return httpx.AsyncClient(transport=_transport(_handler))

    jobs = await runners.run_concurrent(
        _settings(max_poll_rounds=2), ["u1", "u2"], client_factory=_factory
    )
    assert [job.status for job in jobs] == ["failed", "failed"]
    out, _ = capsys.readouterr()
    assert "TIMEOUT" in out


def test_resolve_settings_trailing_slash_prefix() -> None:
    """A trailing-slash prefix is normalised before URL joining."""
    kwargs: dict[str, Any] = {
        "api_url": "http://x:8000",
        "api_key": "k",
        "api_prefix": "/api/v1/",
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
    }
    resolved = config.resolve_settings(**kwargs)
    assert resolved.api_prefix == "/api/v1"
    assert (
        client.DyvineClient(resolved).submit_url("u1")
        == "http://x:8000/api/v1/posts/users/u1/posts:download"
    )


def test_resolve_settings_max_poll_rounds() -> None:
    """Flag beats env, env beats the 720 default; garbage fails fast."""
    kwargs: dict[str, Any] = {
        "api_url": None,
        "api_key": "k",
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
    }
    assert (
        config.resolve_settings(**kwargs).max_poll_rounds
        == config.DEFAULT_MAX_POLL_ROUNDS
    )
    env_kwargs = {**kwargs, "environ": {"DYVINE_MAX_POLL_ROUNDS": "10"}}
    assert config.resolve_settings(**env_kwargs).max_poll_rounds == 10
    flag_kwargs = {
        **kwargs,
        "max_poll_rounds": 3,
        "environ": {"DYVINE_MAX_POLL_ROUNDS": "10"},
    }
    assert config.resolve_settings(**flag_kwargs).max_poll_rounds == 3
    with pytest.raises(config.SettingsError, match="max-poll-rounds"):
        config.resolve_settings(**{**kwargs, "max_poll_rounds": 0})
    bad_kwargs = {**kwargs, "environ": {"DYVINE_MAX_POLL_ROUNDS": "lots"}}
    with pytest.raises(config.SettingsError, match="max-poll-rounds"):
        config.resolve_settings(**bad_kwargs)


async def test_poll_job_maps_likes_counters() -> None:
    """Generic operation payloads map onto bulk-style counters."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "progress": 50.0,
                "total_items": 4,
                "completed_items": 2,
                "error": None,
            },
        )

    settings = _settings(include_likes=True)
    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job)
    assert job.status == "running"
    assert job.progress == pytest.approx(0.5)


async def test_poll_job_likes_null_progress() -> None:
    """``progress: null`` (contract-legal) normalises to 0, not TypeError."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "progress": None,
                "total_items": 4,
                "completed_items": 2,
                "error": None,
            },
        )

    settings = _settings(include_likes=True)
    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job)
    assert job.status == "running"
    assert job.progress == 0.0


async def test_poll_job_likes_non_numeric_progress() -> None:
    """A garbage ``progress`` value degrades to 0 instead of crashing."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "running",
                "message": "working",
                "progress": "halfway",
                "total_items": 4,
                "completed_items": 2,
                "error": None,
            },
        )

    settings = _settings(include_likes=True)
    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_handler)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job)
    assert job.status == "running"
    assert job.progress == 0.0
    assert (job.total_downloaded, job.total_posts) == (2, 4)
    assert job.completed_at is None


async def test_poll_job_handles_404_and_errors() -> None:
    """404 is terminal; other failures only annotate for retry."""
    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    settings = _settings()

    def _gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "gone"})

    async with httpx.AsyncClient(transport=_transport(_gone)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job)
    assert (job.status, job.message) == ("not_found", "任务未找到")

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "oops"})

    job2 = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    async with httpx.AsyncClient(transport=_transport(_boom)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job2)
    assert job2.status == "submitted"  # retried next round
    assert "500" in job2.message


# ── runners ────────────────────────────────────────────────────────────


def _scripted_factory(
    polls_before_done: int = 1,
) -> Any:
    """Build a client factory serving one submit + scripted polls."""
    calls = {"polls": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            user = request.url.path.split("/")[-2]
            return httpx.Response(
                202, json={"operation_id": f"op-{user}", "message": "ok"}
            )
        calls["polls"] += 1
        if calls["polls"] <= polls_before_done:
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "message": "working",
                    "progress": 0.5,
                    "total_posts": 2,
                    "total_downloaded": 1,
                    "failed_count": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "message": "done",
                "progress": 1.0,
                "total_posts": 2,
                "total_downloaded": 2,
                "failed_count": 0,
            },
        )

    def _factory() -> Any:
        return httpx.AsyncClient(transport=_transport(_handler))

    return _factory


async def test_run_concurrent_completes_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Concurrent mode submits every user and polls to the summary."""
    jobs = await runners.run_concurrent(
        _settings(), ["u1", "u2"], client_factory=_scripted_factory()
    )
    assert [job.status for job in jobs] == ["completed", "completed"]
    out, _ = capsys.readouterr()
    assert "批量下载完成报告" in out
    assert "总下载作品数:    4" in out


async def test_run_serial_completes_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Serial mode downloads users one at a time with a report."""
    jobs = await runners.run_serial(
        _settings(), ["u1"], client_factory=_scripted_factory()
    )
    assert [job.status for job in jobs] == ["completed"]
    out, _ = capsys.readouterr()
    assert "汇总报告" in out
    assert "总下载作品数: 2" in out


async def test_run_concurrent_reports_submit_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Users that fail to submit are reported, not retried."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad user"})

    def _factory() -> Any:
        return httpx.AsyncClient(transport=_transport(_handler))

    jobs = await runners.run_concurrent(_settings(), ["u1"], client_factory=_factory)
    assert [job.status for job in jobs] == ["failed"]
    out, _ = capsys.readouterr()
    assert "没有成功提交的任务" in out


# ── cli ────────────────────────────────────────────────────────────────


def test_cli_help_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` names both runners."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0
    out, _ = capsys.readouterr()
    assert "concurrent" in out and "serial" in out


def test_cli_missing_key_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No API key anywhere exits 1 without touching the network."""
    monkeypatch.delenv("DYVINE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    users = tmp_path / "users.txt"
    users.write_text("u1\n", encoding="utf-8")
    assert cli.main(["concurrent", str(users)]) == 1


def test_cli_missing_input_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing input file exits 1."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serial", str(tmp_path / "nope.txt")]) == 1


def test_cli_directory_input_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory passed as input exits 1 without a traceback."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serial", str(tmp_path)]) == 1


def test_cli_undecodable_input_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-UTF-8 input file exits 1 without a traceback."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    users = tmp_path / "users.txt"
    users.write_bytes("u1\n\xff\xfe\n".encode("latin-1"))
    assert cli.main(["serial", str(users)]) == 1


def _stub_runner(return_value: Any = None, error: Any = None) -> Any:
    """Build an async runner double returning or raising on demand."""

    async def _run(*args: Any, **kwargs: Any) -> Any:
        if error is not None:
            raise error
        return return_value if return_value is not None else []

    return _run


def test_cli_success_is_exit_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean run exits 0."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runners, "run_serial", _stub_runner())
    users = tmp_path / "users.txt"
    users.write_text("u1\n", encoding="utf-8")
    assert cli.main(["serial", str(users)]) == 0


def test_cli_unexpected_crash_is_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A runner blow-up exits 2 with a message, not a traceback."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runners, "run_serial", _stub_runner(error=RuntimeError("x")))
    users = tmp_path / "users.txt"
    users.write_text("u1\n", encoding="utf-8")
    assert cli.main(["serial", str(users)]) == 2
    _, err = capsys.readouterr()
    assert "运行失败" in err


def test_cli_keyboard_interrupt_is_exit_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C exits 130."""
    monkeypatch.setenv("DYVINE_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runners, "run_serial", _stub_runner(error=KeyboardInterrupt()))
    users = tmp_path / "users.txt"
    users.write_text("u1\n", encoding="utf-8")
    assert cli.main(["serial", str(users)]) == 130


async def test_poll_job_malformed_json_retries() -> None:
    """A 200 with garbage bytes annotates the job instead of crashing."""
    job = client.DownloadJob(user_id="u1", operation_id="op-1", status="submitted")
    settings = _settings()

    def _garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json{{{")

    async with httpx.AsyncClient(transport=_transport(_garbage)) as http:
        await client.poll_job(http, client.DyvineClient(settings), job)
    assert job.status == "submitted"  # retried next round
    assert "解析失败" in job.message


def test_resolve_settings_rejects_non_finite() -> None:
    """NaN/inf timeouts fail fast instead of crashing later."""
    kwargs: dict[str, Any] = {
        "api_url": None,
        "api_key": "k",
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
    }
    with pytest.raises(config.SettingsError, match="poll-interval"):
        config.resolve_settings(**{**kwargs, "poll_interval": float("nan")})
    with pytest.raises(config.SettingsError, match="timeout"):
        config.resolve_settings(**{**kwargs, "timeout": float("inf")})


def test_resolve_settings_prefix_from_dotenv(tmp_path: Path) -> None:
    """``API_PREFIX`` in .env is honoured (env still wins)."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("SECURITY_API_KEY=k\nAPI_PREFIX=/v2\n", encoding="utf-8")
    base: dict[str, Any] = {
        "api_url": None,
        "api_key": None,
        "api_prefix": None,
        "include_likes": False,
        "max_concurrent": 3,
        "poll_interval": 5.0,
        "timeout": 30.0,
        "environ": {},
        "dotenv_path": dotenv,
    }
    assert config.resolve_settings(**base).api_prefix == "/v2"
    resolved = config.resolve_settings(
        **{**base, "environ": {"DYVINE_API_PREFIX": "/v3"}}
    )
    assert resolved.api_prefix == "/v3"
