"""Tests for shared utility helpers."""

from __future__ import annotations

from dyvine.services.users import sanitize_filename


def test_sanitize_filename_preserves_unicode_and_replaces_reserved() -> None:
    """Unicode (emoji/CJK) survives; reserved characters become underscores."""
    sanitized = sanitize_filename("My Video 📱 <2024>.mp4")

    assert sanitized == "My Video 📱 _2024_.mp4"


def test_sanitize_filename_replaces_directory_separators() -> None:
    """Verify sanitize filename replaces directory separators."""
    sanitized = sanitize_filename("Résumé/with\\special:chars")
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert ":" not in sanitized
    assert sanitized == "Résumé_with_special_chars"


def test_sanitize_filename_strips_controls_and_format_characters() -> None:
    """Controls and invisible format chars go; ZWJ emoji sequences stay."""
    assert sanitize_filename("a\x00b/c:d") == "ab_c_d"
    # U+202A LEFT-TO-RIGHT EMBEDDING is a bidi spoofing vector: dropped.
    assert sanitize_filename("\u202axb\u202c.mp4") == "xb.mp4"
    # U+200D ZERO WIDTH JOINER glues emoji sequences: preserved.
    assert "👨\u200d👩" in sanitize_filename("family 👨\u200d👩 test.mp4")


def test_sanitize_filename_returns_fallback_for_empty_result() -> None:
    """Verify sanitize filename returns fallback for empty result."""
    assert sanitize_filename("<<<>") == "untitled"
    assert sanitize_filename("\x00\x01\x02") == "untitled"


def test_sanitize_filename_trims_whitespace_and_underscores() -> None:
    """Verify sanitize filename trims whitespace and underscores."""
    sanitized = sanitize_filename(" sample_name _.mp4 ")
    assert not sanitized.startswith(" ")
    assert not sanitized.endswith(" ")
    assert sanitized.rstrip("_") == sanitized


def test_sanitize_filename_preserves_cjk_characters() -> None:
    """Douyin titles are Chinese-first; CJK text must survive sanitizing."""
    sanitized = sanitize_filename("中文标题测试.mp4")
    assert "中文标题测试" in sanitized
    assert sanitized.endswith(".mp4")


def test_sanitize_filename_preserves_emoji() -> None:
    """Emoji render fine on modern filesystems and must be preserved."""
    sanitized = sanitize_filename("My Video 📱.mp4")
    assert "📱" in sanitized
    assert sanitized.endswith(".mp4")
