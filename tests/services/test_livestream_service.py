"""Unit tests for the LivestreamService pure-logic helpers.

These tests cover the static/class methods that don't require network
access or the f2 runtime, making them fast and deterministic.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import dyvine.services.livestreams as livestreams_mod
from dyvine.core.exceptions import LivestreamError
from dyvine.core.operations import OperationStore
from dyvine.services.livestreams import LivestreamService

# ---------------------------------------------------------------------------
# _extract_stream_map
# ---------------------------------------------------------------------------


class TestExtractStreamMap:
    def test_returns_m3u8_pull_url_when_present(self) -> None:
        live_filter = SimpleNamespace(
            m3u8_pull_url={"FULL_HD1": "https://stream/hd.m3u8"}
        )
        result = LivestreamService._extract_stream_map(live_filter)
        assert result == {"FULL_HD1": "https://stream/hd.m3u8"}

    def test_falls_back_to_hls_pull_url(self) -> None:
        live_filter = SimpleNamespace(hls_pull_url={"SD1": "https://stream/sd.m3u8"})
        result = LivestreamService._extract_stream_map(live_filter)
        assert result == {"SD1": "https://stream/sd.m3u8"}

    def test_prefers_m3u8_over_hls(self) -> None:
        live_filter = SimpleNamespace(
            m3u8_pull_url={"HD1": "https://m3u8"},
            hls_pull_url={"HD1": "https://hls"},
        )
        result = LivestreamService._extract_stream_map(live_filter)
        assert result == {"HD1": "https://m3u8"}

    def test_returns_empty_when_no_attributes(self) -> None:
        live_filter = SimpleNamespace()
        assert LivestreamService._extract_stream_map(live_filter) == {}

    def test_skips_non_dict_m3u8(self) -> None:
        live_filter = SimpleNamespace(m3u8_pull_url="not-a-dict")
        assert LivestreamService._extract_stream_map(live_filter) == {}

    def test_none_m3u8_returns_empty_dict(self) -> None:
        """m3u8_pull_url=None evaluates to {} via `or`, which is a valid dict."""
        live_filter = SimpleNamespace(
            m3u8_pull_url=None,
            hls_pull_url={"SD2": "https://fallback"},
        )
        assert LivestreamService._extract_stream_map(live_filter) == {}


# ---------------------------------------------------------------------------
# _select_stream_url
# ---------------------------------------------------------------------------


class TestSelectStreamUrl:
    def test_prefers_full_hd(self) -> None:
        stream_map = {
            "SD1": "https://sd",
            "FULL_HD1": "https://fullhd",
            "HD1": "https://hd",
        }
        assert LivestreamService._select_stream_url(stream_map) == "https://fullhd"

    def test_falls_back_to_hd(self) -> None:
        stream_map = {"SD1": "https://sd", "HD1": "https://hd"}
        assert LivestreamService._select_stream_url(stream_map) == "https://hd"

    def test_falls_back_to_any_value(self) -> None:
        stream_map = {"CUSTOM": "https://custom"}
        assert LivestreamService._select_stream_url(stream_map) == "https://custom"

    def test_returns_none_for_empty_map(self) -> None:
        assert LivestreamService._select_stream_url({}) is None

    def test_skips_empty_string_values(self) -> None:
        stream_map = {"FULL_HD1": "", "HD1": "https://hd"}
        assert LivestreamService._select_stream_url(stream_map) == "https://hd"

    def test_skips_non_string_values(self) -> None:
        stream_map: dict[str, Any] = {"FULL_HD1": 123, "HD1": "https://hd"}
        assert LivestreamService._select_stream_url(stream_map) == "https://hd"


# ---------------------------------------------------------------------------
# _stream_map_from_room_data
# ---------------------------------------------------------------------------


class TestStreamMapFromRoomData:
    def test_returns_empty_for_none(self) -> None:
        hls, status, flv = LivestreamService._stream_map_from_room_data(None)
        assert hls == {}
        assert status is None
        assert flv == {}

    def test_returns_empty_for_invalid_json(self) -> None:
        hls, status, flv = LivestreamService._stream_map_from_room_data("{bad json")
        assert hls == {}
        assert status is None
        assert flv == {}

    def test_extracts_status(self) -> None:
        data = json.dumps({"status": 2})
        _, status, _ = LivestreamService._stream_map_from_room_data(data)
        assert status == 2

    def test_extracts_hls_from_quality_map(self) -> None:
        data = json.dumps(
            {
                "status": 2,
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps(
                                {
                                    "data": {
                                        "hd1": {
                                            "main": {
                                                "hls": "https://hls-stream/hd1.m3u8"
                                            }
                                        }
                                    }
                                }
                            )
                        }
                    }
                },
            }
        )
        hls, status, flv = LivestreamService._stream_map_from_room_data(data)
        assert status == 2
        assert hls == {"HD1": "https://hls-stream/hd1.m3u8"}
        assert flv == {}

    def test_extracts_flv_from_quality_map(self) -> None:
        data = json.dumps(
            {
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps(
                                {
                                    "data": {
                                        "sd1": {
                                            "main": {
                                                "flv": "https://flv-stream/sd1.flv"
                                            }
                                        }
                                    }
                                }
                            )
                        }
                    }
                }
            }
        )
        _, _, flv = LivestreamService._stream_map_from_room_data(data)
        assert flv == {"SD1": "https://flv-stream/sd1.flv"}

    def test_extracts_flv_from_raw_flv_map(self) -> None:
        data = json.dumps(
            {"stream_url": {"flv_pull_url": {"full_hd1": "https://raw-flv/fullhd.flv"}}}
        )
        _, _, flv = LivestreamService._stream_map_from_room_data(data)
        assert flv == {"FULL_HD1": "https://raw-flv/fullhd.flv"}

    def test_quality_map_flv_takes_priority_over_raw(self) -> None:
        data = json.dumps(
            {
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps(
                                {
                                    "data": {
                                        "hd1": {
                                            "main": {
                                                "flv": "https://quality-flv/hd1.flv"
                                            }
                                        }
                                    }
                                }
                            )
                        }
                    },
                    "flv_pull_url": {"hd1": "https://raw-flv/hd1.flv"},
                }
            }
        )
        _, _, flv = LivestreamService._stream_map_from_room_data(data)
        assert flv["HD1"] == "https://quality-flv/hd1.flv"

    def test_falls_back_to_ll_hls(self) -> None:
        data = json.dumps(
            {
                "stream_url": {
                    "live_core_sdk_data": {
                        "pull_data": {
                            "stream_data": json.dumps(
                                {
                                    "data": {
                                        "sd1": {
                                            "main": {
                                                "ll_hls": "https://ll-hls/sd1.m3u8"
                                            }
                                        }
                                    }
                                }
                            )
                        }
                    }
                }
            }
        )
        hls, _, _ = LivestreamService._stream_map_from_room_data(data)
        assert hls == {"SD1": "https://ll-hls/sd1.m3u8"}


# ---------------------------------------------------------------------------
# _live_filter_to_dict
# ---------------------------------------------------------------------------


class TestLiveFilterToDict:
    def test_converts_successfully(self) -> None:
        class FakeLiveFilter:
            def _to_dict(self) -> dict[str, str]:
                return {"room_id": "123", "title": "test"}

        result = LivestreamService._live_filter_to_dict(FakeLiveFilter())
        assert result == {"room_id": "123", "title": "test"}

    def test_falls_back_on_missing_method(self) -> None:
        result = LivestreamService._live_filter_to_dict(SimpleNamespace())
        assert result == {}

    def test_falls_back_on_exception(self) -> None:
        class BrokenFilter:
            def _to_dict(self) -> dict[str, str]:
                raise RuntimeError("broken")

        result = LivestreamService._live_filter_to_dict(BrokenFilter())
        assert result == {}


# ---------------------------------------------------------------------------
# _parse_url
# ---------------------------------------------------------------------------


class TestParseUrl:
    def test_full_url(self) -> None:
        host, path, last = LivestreamService._parse_url(
            "https://live.douyin.com/123456"
        )
        assert host == "live.douyin.com"
        assert path == "/123456"
        assert last == "123456"

    def test_bare_number_parsed_as_host(self) -> None:
        """Bare numbers become the hostname after https:// is prepended.

        Bare numeric IDs are handled in _resolve_webcast_id via .isdigit(),
        not in _parse_url.
        """
        host, path, last = LivestreamService._parse_url("987654321")
        assert host == "987654321"
        assert last == ""

    def test_strips_query_and_fragment(self) -> None:
        _, _, last = LivestreamService._parse_url(
            "https://live.douyin.com/123?foo=bar#baz"
        )
        assert last == "123"

    def test_trailing_slash(self) -> None:
        _, _, last = LivestreamService._parse_url("https://live.douyin.com/123/")
        assert last == "123"

    def test_user_profile_url(self) -> None:
        host, path, last = LivestreamService._parse_url(
            "https://www.douyin.com/user/MS4wLjABAAAA"
        )
        assert host == "www.douyin.com"
        assert path == "/user/MS4wLjABAAAA"
        assert last == "MS4wLjABAAAA"

    def test_without_scheme(self) -> None:
        host, _, _ = LivestreamService._parse_url("live.douyin.com/123")
        assert host == "live.douyin.com"


# ---------------------------------------------------------------------------
# _resolve_streams
# ---------------------------------------------------------------------------


class TestResolveStreams:
    def _make_service_stub(self) -> LivestreamService:
        """Create a LivestreamService without calling __init__."""
        return object.__new__(LivestreamService)

    def test_prefers_profile_room_info(self) -> None:
        svc = self._make_service_stub()
        profile = {
            "stream_map": {"HD1": "https://profile-hls"},
            "flv_pull_url": {"HD1": "https://profile-flv"},
        }
        hls, flv = svc._resolve_streams(None, profile)
        assert hls == {"HD1": "https://profile-hls"}
        assert flv == {"HD1": "https://profile-flv"}

    def test_falls_back_to_live_filter(self) -> None:
        svc = self._make_service_stub()
        live_filter = SimpleNamespace(
            m3u8_pull_url={"SD1": "https://filter-hls"},
            flv_pull_url={"SD1": "https://filter-flv"},
        )
        hls, flv = svc._resolve_streams(live_filter, None)
        assert hls == {"SD1": "https://filter-hls"}
        assert flv == {"SD1": "https://filter-flv"}

    def test_returns_empty_when_nothing(self) -> None:
        svc = self._make_service_stub()
        hls, flv = svc._resolve_streams(None, None)
        assert hls == {}
        assert flv == {}


@pytest.mark.asyncio
async def test_run_stream_download_fails_without_artifact(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-1",
        status="pending",
        message="scheduled",
        download_path=str(tmp_path / "room-1_live.flv"),
    )
    service = object.__new__(LivestreamService)
    service.operation_store = store
    service.download_jobs = {"room-1": object()}

    class FakeDownloader:
        def __init__(self, kwargs: dict[str, Any]) -> None:
            pass

        async def __aenter__(self) -> "FakeDownloader":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def create_stream_tasks(
            self,
            download_kwargs: dict[str, Any],
            webcast_payload: dict[str, Any],
            output_dir: Path,
        ) -> None:
            return None

    original_downloader = livestreams_mod.DouyinDownloader
    livestreams_mod.DouyinDownloader = FakeDownloader
    try:
        await service._run_stream_download(
            operation.operation_id,
            "room-1",
            {},
            {},
            tmp_path,
            tmp_path / "room-1_live.flv",
        )
    finally:
        livestreams_mod.DouyinDownloader = original_downloader

    refreshed = store.get_operation(operation.operation_id)
    assert refreshed.status == "failed"
    assert refreshed.error == "Expected livestream artifact was not created"
    assert refreshed.download_path is None


@pytest.mark.asyncio
async def test_download_stream_deduplicates_by_room_id(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    service = object.__new__(LivestreamService)
    service.settings = SimpleNamespace(douyin_cookie="cookie")
    service.downloader_config = {"headers": {}, "proxies": {}, "cookie": "cookie"}
    service.download_jobs = {"room-42": object()}
    service.user_service = None
    service.operation_store = store
    service.douyin_handler = None

    service._parse_url = lambda url: ("live.douyin.com", "/abc", "abc")  # type: ignore[method-assign]

    async def resolve_webcast_id(*args, **kwargs):
        return "webcast-1", {"room_id": "room-42", "status": 2}

    async def load_live_filter(*args, **kwargs):
        return None

    service._resolve_webcast_id = resolve_webcast_id  # type: ignore[method-assign]
    service._load_live_filter = load_live_filter  # type: ignore[method-assign]
    service._resolve_streams = lambda live_filter, profile: (  # type: ignore[method-assign]
        {"HD1": "https://stream"},
        {},
    )
    service._select_stream_url = lambda stream_map: "https://stream"  # type: ignore[method-assign]

    with pytest.raises(LivestreamError, match="Already downloading this stream"):
        await service.download_stream("https://live.douyin.com/abc")


@pytest.mark.asyncio
async def test_get_download_status_falls_back_to_room_id(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = store.create_operation(
        operation_type="livestream_download",
        subject_id="room-99",
        status="completed",
        message="done",
        download_path=str(tmp_path / "room-99_live.flv"),
    )
    service = object.__new__(LivestreamService)
    service.operation_store = store

    response = await service.get_download_status("room-99")

    assert response.operation_id == operation.operation_id
    assert response.subject_id == "room-99"
    assert response.status == "completed"


@pytest.mark.asyncio
async def test_get_download_status_rejects_non_livestream_operation(tmp_path) -> None:
    store = OperationStore(str(tmp_path / "operations.db"))
    operation = store.create_operation(
        operation_type="user_content_download",
        subject_id="user-1",
        status="pending",
        message="scheduled",
    )
    service = object.__new__(LivestreamService)
    service.operation_store = store

    with pytest.raises(livestreams_mod.DownloadError):
        await service.get_download_status(operation.operation_id)


@pytest.mark.asyncio
async def test_download_stream_serializes_concurrent_requests_same_room(
    tmp_path,
) -> None:
    """Two concurrent callers for the same room must not both schedule a job.

    Simulates the race where both coroutines reach the metadata await before
    either has registered an entry in ``download_jobs``. Exactly one call must
    succeed, the other must raise ``LivestreamError``, and only a single
    operation record must exist for the room.
    """
    store = OperationStore(str(tmp_path / "operations.db"))
    service = object.__new__(LivestreamService)
    service.settings = SimpleNamespace(douyin_cookie="cookie")
    service.downloader_config = {"headers": {}, "proxies": {}, "cookie": "cookie"}
    service.download_jobs = {}
    service.user_service = None
    service.operation_store = store
    service.douyin_handler = None
    service._dedupe_lock = asyncio.Lock()

    service._parse_url = lambda url: ("live.douyin.com", "/abc", "abc")  # type: ignore[method-assign]

    async def resolve_webcast_id(*args, **kwargs):
        return "webcast-1", {"room_id": "room-77", "status": 2}

    async def load_live_filter(*args, **kwargs):
        # Yield control once so both callers enter the await before either
        # reaches the dedupe-check / registration section.
        await asyncio.sleep(0)
        return None

    service._resolve_webcast_id = resolve_webcast_id  # type: ignore[method-assign]
    service._load_live_filter = load_live_filter  # type: ignore[method-assign]
    service._resolve_streams = lambda live_filter, profile: (  # type: ignore[method-assign]
        {"HD1": "https://stream"},
        {},
    )
    service._select_stream_url = lambda stream_map: "https://stream"  # type: ignore[method-assign]

    # Prevent the background task from performing a real download.
    class NoopDownloader:
        def __init__(self, kwargs: dict[str, Any]) -> None:
            pass

        async def __aenter__(self) -> "NoopDownloader":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def create_stream_tasks(
            self,
            download_kwargs: dict[str, Any],
            webcast_payload: dict[str, Any],
            output_dir: Path,
        ) -> None:
            return None

    original_downloader = livestreams_mod.DouyinDownloader
    livestreams_mod.DouyinDownloader = NoopDownloader
    try:
        results = await asyncio.gather(
            service.download_stream(
                "https://live.douyin.com/abc",
                output_path=str(tmp_path / "dl"),
            ),
            service.download_stream(
                "https://live.douyin.com/abc",
                output_path=str(tmp_path / "dl"),
            ),
            return_exceptions=True,
        )
    finally:
        livestreams_mod.DouyinDownloader = original_downloader
        # Await the scheduled background task so it settles before teardown.
        job = service.download_jobs.get("room-77")
        if job is not None:
            await job

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1, f"Expected exactly one success, got {results!r}"
    assert len(failures) == 1, f"Expected exactly one failure, got {results!r}"
    assert isinstance(failures[0], LivestreamError)
    assert str(failures[0]) == "Already downloading this stream"

    # Only one operation record should exist for the room.
    latest = store.get_latest_operation_for_subject(
        "room-77", operation_type="livestream_download"
    )
    assert latest is not None
    assert latest.subject_id == "room-77"
    connection = store._connect()  # type: ignore[attr-defined]
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM operations WHERE subject_id = ? "
            "AND operation_type = ?",
            ("room-77", "livestream_download"),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1
