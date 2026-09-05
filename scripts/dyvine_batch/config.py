"""Configuration resolution for the batch download CLI.

Precedence for every knob is: explicit CLI flag, then ``DYVINE_*``
environment, then the repo ``.env`` file, then the built-in default.
All functions here are pure (the ``.env`` path and environ mapping are
arguments), so importing this module never touches the filesystem.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_API_PREFIX = "/api/v1"
#: Default cap on poll rounds per job (serial) or per run (concurrent).
#: At the default 5s interval this is about an hour.
DEFAULT_MAX_POLL_ROUNDS = 720


class SettingsError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class BatchSettings:
    """Fully resolved batch-run configuration."""

    api_url: str
    api_key: str
    api_prefix: str
    include_likes: bool
    max_concurrent: int
    poll_interval: float
    timeout: float
    max_poll_rounds: int = DEFAULT_MAX_POLL_ROUNDS


def load_dotenv(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a ``.env`` file into a dict; missing files yield ``{}``.

    Only ``KEY=value`` lines are honoured; blanks, comments, and
    valueless lines are skipped. Surrounding quotes are stripped, and
    a leading ``export`` token is dropped so the same file resolves
    identically to server-side python-dotenv (which accepts it).
    """
    env: dict[str, str] = {}
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return env
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key == "export" or key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export") :].strip()
        if key:
            env[key] = value.strip().strip("\"'")
    return env


def resolve_settings(
    *,
    api_url: str | None,
    api_key: str | None,
    api_prefix: str | None,
    include_likes: bool,
    max_concurrent: int,
    poll_interval: float,
    timeout: float,
    max_poll_rounds: int | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] = ".env",
) -> BatchSettings:
    """Resolve CLI flags + environment + ``.env`` into settings.

    Raises:
        SettingsError: If no API key resolves, or a numeric knob is
            out of range.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    dotenv = load_dotenv(dotenv_path)

    key = api_key or env.get("DYVINE_API_KEY") or dotenv.get("SECURITY_API_KEY", "")
    if not key:
        raise SettingsError(
            "Missing API key: pass --api-key, set DYVINE_API_KEY, "
            "or set SECURITY_API_KEY in .env"
        )

    url = api_url or env.get("DYVINE_API_URL") or ""
    if not url:
        host = (dotenv.get("API_HOST") or "").strip()
        # An empty ``API_PORT=`` must fall back like a missing one,
        # not build ``http://host:`` and fail whole runs late.
        port = (dotenv.get("API_PORT") or "").strip() or "8000"
        url = f"http://{host}:{port}" if host else DEFAULT_API_URL
    url = url.rstrip("/")

    prefix = (
        api_prefix
        or env.get("DYVINE_API_PREFIX")
        or dotenv.get("API_PREFIX")
        or DEFAULT_API_PREFIX
    )
    if not prefix.startswith("/"):
        raise SettingsError(f"API prefix must start with '/': {prefix!r}")
    # A trailing slash would double up against endpoint paths
    # (``/api/v1/`` + ``/posts/...``), which the server answers 404.
    # A bare ``/`` means "no prefix", not the literal root path, so it
    # normalizes to ``""`` — otherwise joins would produce ``//posts``.
    prefix = prefix.rstrip("/")

    if max_concurrent < 1:
        raise SettingsError("--max-concurrent must be at least 1")
    # ``float("nan")`` defeats ``<=`` comparisons (always False), so
    # reject non-finite values explicitly instead of crashing later.
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise SettingsError("--poll-interval must be a positive number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise SettingsError("--timeout must be a positive number")

    raw_rounds = (
        max_poll_rounds
        if max_poll_rounds is not None
        else env.get("DYVINE_MAX_POLL_ROUNDS")
    )
    try:
        rounds = DEFAULT_MAX_POLL_ROUNDS if raw_rounds is None else int(raw_rounds)
    except (TypeError, ValueError):
        raise SettingsError(
            f"--max-poll-rounds must be an integer: {raw_rounds!r}"
        ) from None
    if rounds < 1:
        raise SettingsError("--max-poll-rounds must be at least 1")

    return BatchSettings(
        api_url=url,
        api_key=key,
        api_prefix=prefix,
        include_likes=include_likes,
        max_concurrent=max_concurrent,
        poll_interval=poll_interval,
        timeout=timeout,
        max_poll_rounds=rounds,
    )


def read_user_ids(path: str | os.PathLike[str]) -> list[str]:
    """Read user IDs from a text file (one per line).

    Blank lines and ``#`` comments are skipped.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        SettingsError: If the file holds no usable IDs.
    """
    input_path = Path(path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    user_ids = [
        line.strip()
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not user_ids:
        raise SettingsError(f"No usable user IDs in {input_path}")
    return user_ids
