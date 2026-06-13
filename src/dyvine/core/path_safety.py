"""Path-safety helpers for jailing user-supplied filesystem locations.

User-controllable strings (livestream ``output_path``, future bulk
download targets, etc.) must never be allowed to write outside the
configured download root. ``resolve_within_root`` enforces that
invariant in a single place so router/schema callers cannot accidentally
build a path that escapes the jail via traversal segments or absolute
prefixes.
"""

from __future__ import annotations

from pathlib import Path

from .exceptions import ValidationError
from .settings import settings

TASK_WORKSPACE_SUBDIR = "tasks"


def get_download_root() -> Path:
    """Return the absolute, normalised download jail root."""
    return Path(settings.douyin.download_root).expanduser().resolve()


def get_task_workspace_root() -> Path:
    """Return the root that holds operation-owned download workspaces."""
    return get_download_root() / TASK_WORKSPACE_SUBDIR


def _reject_symlink_segments(target: Path, root: Path) -> None:
    """Reject *target* if any segment between *root* and *target*
    is a symlink on disk.

    ``Path.resolve()`` silently follows symlinks before the
    ``relative_to`` jail check, so a symlink that points to another
    directory **inside** the root would be considered legal even
    though the indirection is exactly what an attacker needs to
    redirect a follow-up ``mkdir`` / ``open``. Walking the *unresolved*
    segments rejects that case at validation time. Segments that do
    not yet exist are skipped because they cannot be symlinks yet —
    the post-mutation :func:`ensure_within_root` covers the window
    where a brand-new segment is replaced by a symlink before the
    next syscall.
    """
    try:
        relative = target.relative_to(root)
    except ValueError:
        # ``resolve_within_root`` already raised; defensive guard only.
        return

    walked = root
    for part in relative.parts:
        walked = walked / part
        # ``Path.is_symlink`` calls ``lstat`` and does NOT follow links,
        # which is the property that lets us catch indirections that
        # ``Path.resolve()`` would otherwise hide.
        if walked.is_symlink():
            raise ValidationError(
                "Path traverses a symlink inside the download root",
                details={"segment": str(walked), "download_root": str(root)},
            )
        if not walked.exists():
            # Future segments will be created by ``mkdir`` and cannot
            # be symlinks yet; the rest of the walk has nothing to
            # check.
            break


def resolve_within_root(
    raw: str | Path | None,
    *,
    default_subdir: str | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a user-supplied path inside the download jail.

    Args:
        raw: User input. ``None`` falls back to ``download_root`` (or
            ``download_root / default_subdir`` when ``default_subdir`` is
            provided).
        default_subdir: Optional subdirectory appended to the root when
            ``raw`` is ``None``.
        must_exist: When ``True`` the resolved target must already exist;
            useful for read paths.

    Returns:
        The absolute resolved path.

    Raises:
        ValidationError: When ``raw`` resolves outside ``download_root``
            (path traversal) or fails the optional existence check.
    """
    root = get_download_root()
    if raw is None or raw == "":
        target_unresolved = root / default_subdir if default_subdir else root
    else:
        candidate = Path(raw).expanduser()
        target_unresolved = candidate if candidate.is_absolute() else root / candidate

    # Reject indirection through symlink segments BEFORE ``resolve()``
    # silently follows them, so an in-jail alias pointing at another
    # in-jail directory is treated as suspicious rather than legal.
    _reject_symlink_segments(target_unresolved, root)

    target = target_unresolved.resolve()

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "Path escapes the configured download root",
            details={
                "input": str(raw) if raw is not None else None,
                "download_root": str(root),
            },
        ) from exc

    if must_exist and not target.exists():
        raise ValidationError(
            "Resolved path does not exist",
            details={"path": str(target)},
        )

    return target


def ensure_within_root(path: Path) -> None:
    """Re-verify that *path* still resolves under ``download_root``.

    Callers that mutate the filesystem (e.g. ``Path.mkdir(parents=True)``)
    after :func:`resolve_within_root` should invoke this helper as the
    final step of the jail check. It defends against the residual TOCTOU
    where a directory segment is swapped for a symlink between the
    initial resolve and the syscall that creates the target. The
    re-resolve runs ``Path.resolve()`` again so any newly introduced
    symlink is followed, and the symlink-segment scan rejects any
    indirection through the freshly-mutated tree.
    """
    root = get_download_root()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ValidationError(
            "Could not re-resolve path after filesystem mutation",
            details={"path": str(path), "error": str(exc)},
        ) from exc

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "Path escaped the download root after mutation",
            details={"path": str(resolved), "download_root": str(root)},
        ) from exc

    _reject_symlink_segments(resolved, root)


def relative_to_download_root(path: str | Path | None) -> str | None:
    """Render *path* as a string relative to the download root.

    Used when persisting paths into operation records so the public API
    response surface never leaks the absolute on-disk layout.
    """
    if path is None:
        return None
    target = Path(path).expanduser()
    if not target.is_absolute():
        return str(target)
    root = get_download_root()
    try:
        return str(target.resolve().relative_to(root))
    except ValueError:
        # Path lives outside the jail (legacy records pre-dating the
        # validator). Surface only the basename rather than the full
        # filesystem path so callers cannot enumerate the parent
        # directory structure.
        return target.name
