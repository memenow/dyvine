"""Path-safety helpers for jailing user-supplied filesystem locations.

User-controllable strings (livestream ``output_path``, future bulk
download targets, etc.) must never be allowed to write outside the
configured download root. ``resolve_within_root`` enforces that
invariant in a single place: relative and absolute inputs alike are
contained, and anything resolving outside the root -- via traversal
segments, absolute prefixes, or symlink indirection -- is rejected.
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


def _lexical_normalize(path: Path) -> Path:
    """Collapse ``.``/``..`` segments without touching the disk.

    Purely lexical: ``..`` pops the previous segment, never crossing
    the anchor of an absolute path. Callers normalize *before* the
    symlink walk so a ``..`` cannot hide a later segment from the scan
    (``root/nonexistent/../evil`` is ``root/evil`` and must be scanned
    as such). Normalization never replaces the resolve-then-contain
    check below: a ``..`` after a symlink resolves through the link
    target on disk, which no lexical pass can predict.
    """
    stack: list[str] = []
    for part in path.parts:
        # PurePath drops single dots at parse time, so `parts` never
        # contains "."; the branch stays as documentation of intent.
        if part == ".":  # pragma: no cover - parser-normalized away
            continue
        if part == "..":
            if stack and stack[-1] != ".." and stack[-1] != path.anchor:
                stack.pop()
                continue
            if not path.is_absolute():
                stack.append(part)
            continue
        stack.append(part)
    if not stack:
        return Path(path.anchor or ".")
    return Path(stack[0], *stack[1:])


def _reject_symlink_segments(target: Path, root: Path) -> None:
    """Reject *target* if any segment between *root* and *target*
    is a symlink on disk.

    ``Path.resolve()`` silently follows symlinks before the
    ``relative_to`` jail check, so a symlink that points to another
    directory **inside** the root would be considered legal even
    though the indirection is exactly what an attacker needs to
    redirect a follow-up ``mkdir`` / ``open``. Walking the *unresolved*
    segments rejects that case at validation time. A missing tail
    segment ends the walk: it cannot be a symlink yet, and the
    post-mutation :func:`ensure_within_root` covers the window where a
    brand-new segment is replaced by a symlink before the next syscall.

    Callers run this scan twice -- once on the raw joined path and once
    on the lexically normalized path. Either pass alone has a blind
    spot: the raw walk stops at the first missing segment (missing a
    symlink hidden behind ``nonexistent/..``), while the normalized
    walk cannot see a symlink consumed by ``..`` (``root/link/../file``
    resolves *through* ``link`` on disk even though it normalizes to
    ``root/file``).
    """
    try:
        relative = target.relative_to(root)
    except ValueError:
        # Outside the jail: the containment check below raises the
        # real error; there is nothing to scan here.
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
            break


def _resolve_inside_root(candidate: Path, root: Path, *, after_mutation: bool) -> Path:
    """Scan, resolve, and contain *candidate* under *root*.

    Single choke point shared by :func:`resolve_within_root` (pre-use
    check) and :func:`ensure_within_root` (post-mutation re-check):

    1. ``lstat``-walk the raw joined path (catches symlinks a later
       ``..`` would consume during resolution),
    2. lexically normalize and ``lstat``-walk again (catches symlinks
       a ``..`` hides from the raw walk),
    3. ``resolve()`` and require containment (catches indirections the
       walks cannot see, e.g. a mount swap under an existing segment).

    ``OSError``/``RuntimeError`` from ``resolve()`` (I/O failures,
    symlink loops) surface as ``ValidationError`` so callers only ever
    handle one failure type from the jail.
    """
    _reject_symlink_segments(candidate, root)
    normalized = _lexical_normalize(candidate)
    _reject_symlink_segments(normalized, root)
    try:
        resolved = normalized.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(
            "Could not resolve path inside the download root",
            details={"path": str(candidate), "error": str(exc)},
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            (
                "Path escaped the download root after mutation"
                if after_mutation
                else "Path escapes the configured download root"
            ),
            details={"path": str(resolved), "download_root": str(root)},
        ) from exc
    return resolved


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
        candidate = root / default_subdir if default_subdir else root
    else:
        joined = Path(raw).expanduser()
        candidate = joined if joined.is_absolute() else root / joined

    target = _resolve_inside_root(candidate, root, after_mutation=False)

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
    initial resolve and the syscall that creates the target: the
    unresolved tree is ``lstat``-scanned for newly introduced
    indirections, then re-resolved and contained again.
    """
    root = get_download_root()
    _resolve_inside_root(Path(path).expanduser(), root, after_mutation=True)


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
    # Only the documented legacy case (a path outside the jail) falls
    # back to the basename. ``OSError``/``RuntimeError`` from
    # ``resolve()`` (I/O failures, symlink loops) propagate: callers
    # pass freshly created paths inside download flows, so surfacing
    # the true cause beats recording a bogus basename.
    resolved = target.resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        # Path lives outside the jail (legacy records pre-dating the
        # validator). Surface only the basename rather than the full
        # filesystem path so callers cannot enumerate the parent
        # directory structure.
        return target.name
