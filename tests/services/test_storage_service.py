from __future__ import annotations

import base64
import re

import pytest

from src.dyvine.services.storage import ContentType, R2StorageService, StorageError


def build_service_without_init() -> R2StorageService:
    service = object.__new__(R2StorageService)
    return service  # type: ignore[return-value]


def test_generate_ugc_path_includes_expected_segments() -> None:
    service = build_service_without_init()

    path = R2StorageService.generate_ugc_path(
        service,
        user_id="user-123",
        original_filename="clip.mp4",
        content_type="video/mp4",
    )

    assert path.startswith("videos/user-123/")
    filename = path.split("/")[-1]
    match = re.match(r"(?P<prefix>\d{8})_(?P<encoded>[^_]+)_(?P<uuid>[a-f0-9]{8})\.mp4", filename)
    assert match is not None

    encoded_name = match.group("encoded")
    padding = "=" * (-len(encoded_name) % 4)
    decoded_name = base64.urlsafe_b64decode(encoded_name + padding).decode()
    assert decoded_name == "clip.mp4"


def test_generate_ugc_path_rejects_unsupported_content_type() -> None:
    service = build_service_without_init()

    with pytest.raises(StorageError):
        R2StorageService.generate_ugc_path(
            service,
            user_id="user-456",
            original_filename="note.txt",
            content_type="application/pdf",
        )


def test_generate_metadata_encodes_author() -> None:
    service = build_service_without_init()

    metadata = R2StorageService.generate_metadata(
        service,
        author="测试作者",
        category=ContentType.POSTS,
        content_type="image/png",
        source="unit-tests",
        language="en-US",
        version="1.2.3",
    )

    decoded_author = base64.b64decode(metadata["author"]).decode()
    assert decoded_author == "测试作者"
    assert metadata["category"] == ContentType.POSTS.value
    assert metadata["content-type"] == "image/png"
    assert metadata["language"] == "en-US"
    assert metadata["source"] == "unit-tests"
    assert metadata["version"] == "1.2.3"
