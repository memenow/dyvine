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


def get_download_root() -> Path:
    """Return the absolute, normalised download jail root."""
    return Path(settings.douyin.download_root).expanduser().resolve()


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
