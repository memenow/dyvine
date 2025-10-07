from __future__ import annotations

from src.dyvine.services.users import sanitize_filename


def test_sanitize_filename_removes_non_ascii_and_reserved_characters() -> None:
    sanitized = sanitize_filename("My Video 📱 <2024>.mp4")

    assert "📱" not in sanitized
    assert "<" not in sanitized
    assert ">" not in sanitized
    assert sanitized.endswith(".mp4")


def test_sanitize_filename_replaces_directory_separators() -> None:
    sanitized = sanitize_filename("文件名/with\\special:chars")
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert ":" not in sanitized
    assert sanitized == "with_special_chars"


def test_sanitize_filename_returns_fallback_for_empty_result() -> None:
    assert sanitize_filename("🎥📹🎬") == "untitled"


def test_sanitize_filename_trims_whitespace_and_underscores() -> None:
    sanitized = sanitize_filename(" sample_name _.mp4 ")
    assert not sanitized.startswith(" ")
    assert not sanitized.endswith(" ")
    assert sanitized.rstrip("_") == sanitized
