"""Tests for shared utility helpers."""

from __future__ import annotations

from dyvine.services.users import sanitize_filename


def test_sanitize_filename_removes_non_ascii_and_reserved_characters() -> None:
    """Verify sanitize filename removes non ascii and reserved characters."""
    sanitized = sanitize_filename("My Video 📱 <2024>.mp4")

    assert "📱" not in sanitized
    assert "<" not in sanitized
    assert ">" not in sanitized
    assert sanitized.endswith(".mp4")


def test_sanitize_filename_replaces_directory_separators() -> None:
    """Verify sanitize filename replaces directory separators."""
    sanitized = sanitize_filename("Résumé/with\\special:chars")
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert ":" not in sanitized
    assert sanitized == "Rsum_with_special_chars"


def test_sanitize_filename_returns_fallback_for_empty_result() -> None:
    """Verify sanitize filename returns fallback for empty result."""
    assert sanitize_filename("🎥📹🎬") == "untitled"


def test_sanitize_filename_trims_whitespace_and_underscores() -> None:
    """Verify sanitize filename trims whitespace and underscores."""
    sanitized = sanitize_filename(" sample_name _.mp4 ")
    assert not sanitized.startswith(" ")
    assert not sanitized.endswith(" ")
    assert sanitized.rstrip("_") == sanitized
