"""Tests for ``dyvine.core.path_safety``.

Covers the jail check, the post-mutation re-verification helper, and
the symlink-segment guards added to defend against TOCTOU swaps. The
tests run against a per-test ``download_root`` rooted at ``tmp_path``
so they do not collide with the developer's real download tree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dyvine.core import path_safety
from dyvine.core.exceptions import ValidationError


@pytest.fixture
def jail_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Repoint ``settings.douyin.download_root`` at a per-test directory."""
    from dyvine.core.settings import settings as live_settings

    root = tmp_path / "jail"
    root.mkdir()
    monkeypatch.setattr(live_settings.douyin, "download_root", str(root))
    monkeypatch.setattr(path_safety.settings.douyin, "download_root", str(root))
    return root.resolve()


def test_resolve_within_root_relative_path(jail_root: Path) -> None:
    """Verify resolve within root relative path."""
    target = path_safety.resolve_within_root("livestreams")
    assert target == jail_root / "livestreams"


def test_resolve_within_root_returns_root_when_input_blank(jail_root: Path) -> None:
    """Verify resolve within root returns root when input blank."""
    target = path_safety.resolve_within_root(None)
    assert target == jail_root


def test_resolve_within_root_uses_default_subdir_for_blank_input(
    jail_root: Path,
) -> None:
    """Verify resolve within root uses default subdir for blank input."""
    target = path_safety.resolve_within_root("", default_subdir="livestreams")
    assert target == jail_root / "livestreams"


def test_resolve_within_root_rejects_absolute_path_outside_root(
    jail_root: Path,
) -> None:
    """Verify resolve within root rejects absolute path outside root."""
    with pytest.raises(ValidationError):
        # Any absolute path outside the jail exercises the same check;
        # this fixture avoids naming system password files (hermes'
        # install-time scanner flags that literal as critical even in
        # tests that assert the access is rejected).
        path_safety.resolve_within_root("/outside/the/jail.txt")


def test_resolve_within_root_rejects_traversal(jail_root: Path) -> None:
    """Verify resolve within root rejects traversal."""
    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("../escape")


def test_resolve_within_root_rejects_symlink_pointing_outside(
    jail_root: Path, tmp_path: Path
) -> None:
    """A symlink inside the jail that targets a path outside is rejected.

    ``Path.resolve()`` follows the symlink to its real target, after
    which the ``relative_to(root)`` check raises ``ValueError`` and we
    surface ``ValidationError``.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    outside = tmp_path / "outside"
    outside.mkdir()
    (jail_root / "evil").symlink_to(outside)

    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("evil")


def test_resolve_within_root_rejects_symlink_segment_inside_jail(
    jail_root: Path,
) -> None:
    """A symlink-as-directory anywhere along the path is rejected.

    The symlink points to a directory that is itself inside the jail —
    ``relative_to`` would still pass — but the symlink-segment scan
    rejects the indirection so a TOCTOU swap of an existing segment
    cannot exfiltrate writes through a follow-up ``mkdir``.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    real_dir = jail_root / "real"
    real_dir.mkdir()
    (jail_root / "alias").symlink_to(real_dir)

    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("alias/subpath")


def test_resolve_within_root_must_exist_rejects_missing(jail_root: Path) -> None:
    """Verify resolve within root must exist rejects missing."""
    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("never-created", must_exist=True)


def test_ensure_within_root_passes_for_real_directory(jail_root: Path) -> None:
    """Verify ensure within root passes for real directory."""
    inner = jail_root / "ok"
    inner.mkdir()
    path_safety.ensure_within_root(inner)


def test_ensure_within_root_rejects_post_mutation_symlink_swap(
    jail_root: Path, tmp_path: Path
) -> None:
    """Simulate a TOCTOU swap: a real directory replaced by a symlink.

    ``resolve_within_root`` has already cleared the path, the caller
    then runs ``mkdir(parents=True)``, and an attacker swaps an
    ancestor for a symlink before ``ensure_within_root`` runs. The
    re-resolve catches the new indirection.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    target_dir = jail_root / "victim"
    target_dir.mkdir()

    outside = tmp_path / "attacker"
    outside.mkdir()

    # Replace ``victim`` with a symlink to ``outside`` to mimic the
    # post-resolve swap.
    target_dir.rmdir()
    target_dir.symlink_to(outside)

    with pytest.raises(ValidationError):
        path_safety.ensure_within_root(target_dir)


def test_relative_to_download_root_relative_input(jail_root: Path) -> None:
    """Verify relative to download root relative input."""
    assert path_safety.relative_to_download_root("livestreams/abc.flv") == (
        "livestreams/abc.flv"
    )


def test_get_task_workspace_root_uses_download_root(jail_root: Path) -> None:
    """Task workspaces live under the configured download root."""
    assert path_safety.get_task_workspace_root() == (
        jail_root / path_safety.TASK_WORKSPACE_SUBDIR
    )


def test_relative_to_download_root_absolute_inside_root(jail_root: Path) -> None:
    """Verify relative to download root absolute inside root."""
    inside = jail_root / "livestreams" / "abc.flv"
    assert path_safety.relative_to_download_root(inside) == os.path.join(
        "livestreams", "abc.flv"
    )


def test_relative_to_download_root_absolute_outside_root_returns_basename(
    jail_root: Path, tmp_path: Path
) -> None:
    """Legacy paths that pre-date the jail check render as their basename.

    Returning the absolute path would leak the on-disk layout to API
    consumers; returning ``None`` would lose the artefact reference.
    The basename keeps the operation record self-describing without
    exposing the parent tree.
    """
    outside = tmp_path / "legacy" / "artifact.flv"
    outside.parent.mkdir(parents=True)
    outside.write_text("payload")
    assert path_safety.relative_to_download_root(outside) == "artifact.flv"


def test_relative_to_download_root_none_passthrough() -> None:
    """Verify relative to download root none passthrough."""
    assert path_safety.relative_to_download_root(None) is None


def test_resolve_within_root_rejects_symlink_hidden_behind_dotdot(
    jail_root: Path, tmp_path: Path
) -> None:
    """``nonexistent/../evil`` must be scanned as ``evil``, not skipped.

    The old walk stopped at the first missing segment, so a ``..``
    after it hid the rest of the path from the symlink scan.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    outside = tmp_path / "outside"
    outside.mkdir()
    (jail_root / "evil").symlink_to(outside)

    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("ghost/../evil/payload")


def test_resolve_within_root_rejects_symlink_consumed_by_dotdot(
    jail_root: Path, tmp_path: Path
) -> None:
    """``link/../file`` resolves *through* ``link`` on disk.

    Lexical normalization alone would reduce this to ``root/file`` and
    miss the indirection, so the raw joined path is scanned too.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    outside = tmp_path / "outside"
    outside.mkdir()
    (jail_root / "link").symlink_to(outside)

    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("link/../file")


def test_resolve_within_root_reports_symlink_loop_as_validation_error(
    jail_root: Path,
) -> None:
    """A symlink loop must surface as ``ValidationError``, not ``RuntimeError``."""
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    (jail_root / "loop-a").symlink_to(jail_root / "loop-b")
    (jail_root / "loop-b").symlink_to(jail_root / "loop-a")

    with pytest.raises(ValidationError):
        path_safety.resolve_within_root("loop-a/payload")


def test_resolve_within_root_allows_absolute_path_inside_root(
    jail_root: Path,
) -> None:
    """Containment -- not relative spelling -- is the invariant.

    Absolute inputs resolving inside the jail are legal; only
    outside-jail resolutions are rejected.
    """
    inside = jail_root / "livestreams"
    assert path_safety.resolve_within_root(str(inside)) == inside


def test_ensure_within_root_rejects_in_jail_symlink_ancestor(
    jail_root: Path,
) -> None:
    """Post-mutation re-check applies the same strictness as resolve.

    An in-jail symlink to another in-jail directory is rejected even
    though the resolved target stays inside the jail.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    real_dir = jail_root / "real"
    real_dir.mkdir()
    alias = jail_root / "alias"
    alias.symlink_to(real_dir)

    with pytest.raises(ValidationError):
        path_safety.ensure_within_root(alias / "subpath")


def test_relative_to_download_root_loop_never_leaks_absolute(
    jail_root: Path,
) -> None:
    """A symlink loop never leaks the absolute path and never fakes a basename.

    On runtimes whose non-strict ``resolve()`` reports loops the
    ``OSError``/``RuntimeError`` propagates (callers pass freshly
    created paths inside download flows, so the operation fails with
    its true cause); otherwise the render must not contain the jail
    root prefix. Either way the legacy basename fallback is reserved
    for genuinely outside-jail records.
    """
    if sys.platform.startswith("win"):
        pytest.skip("Symlinks on Windows require elevated privileges")

    (jail_root / "loop-a").symlink_to(jail_root / "loop-b")
    (jail_root / "loop-b").symlink_to(jail_root / "loop-a")

    try:
        rendered = path_safety.relative_to_download_root(jail_root / "loop-a" / "x")
    except (OSError, RuntimeError):
        return
    assert rendered is not None
    assert str(jail_root) not in rendered
