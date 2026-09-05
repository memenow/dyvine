"""Async Dyvine API client for batch download jobs.

All IO goes through an injected ``httpx.AsyncClient`` so tests can
drive these functions with ``httpx.MockTransport`` instead of the
network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from .config import BatchSettings

#: Operation states that end polling for a job.
TERMINAL_STATUSES = frozenset({"completed", "failed", "partial", "not_found"})


@dataclass
class DownloadJob:
    """Mutable per-user download state tracked across polls."""

    user_id: str
    operation_id: str | None = None
    status: str = "pending"
    message: str = ""
    progress: float = 0.0
    total_posts: int = 0
    total_downloaded: int = 0
    failed_count: int = 0
    error_details: str | None = None
    submitted_at: float = field(default_factory=time.time)
    completed_at: float | None = None


class DyvineClient:
    """URL builder + header carrier for one batch run."""

    def __init__(self, settings: BatchSettings) -> None:
        """Bind the client to resolved ``settings``."""
        self.settings = settings
        self.headers = {
            "X-API-Key": settings.api_key,
            "Content-Type": "application/json",
        }

    def submit_url(self, user_id: str) -> str:
        """Return the submit endpoint for ``user_id``."""
        base = f"{self.settings.api_url}{self.settings.api_prefix}"
        if self.settings.include_likes:
            return (
                f"{base}/users/{user_id}/content:download"
                "?include_posts=true&include_likes=true"
            )
        return f"{base}/posts/users/{user_id}/posts:download"

    def poll_url(self, operation_id: str) -> str:
        """Return the status endpoint for ``operation_id``."""
        base = f"{self.settings.api_url}{self.settings.api_prefix}"
        if self.settings.include_likes:
            return f"{base}/users/operations/{operation_id}"
        return f"{base}/posts/operations/{operation_id}"


async def submit_download(
    http: httpx.AsyncClient, client: DyvineClient, user_id: str
) -> DownloadJob:
    """Submit one user's download; transport errors become failed jobs."""
    try:
        response = await http.post(
            client.submit_url(user_id),
            headers=client.headers,
            timeout=client.settings.timeout,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected payload: {type(data).__name__}")
    except (httpx.TimeoutException, TimeoutError):
        return DownloadJob(
            user_id=user_id,
            status="failed",
            message="提交超时",
            error_details="Request timeout",
        )
    except Exception as exc:  # network down, DNS, refused, ...
        return DownloadJob(
            user_id=user_id,
            status="failed",
            message=f"提交异常: {exc}",
            error_details=str(exc),
        )
    if response.status_code == 202:
        operation_id = data.get("operation_id")
        if not operation_id:
            return DownloadJob(
                user_id=user_id,
                status="failed",
                message="提交响应缺少 operation_id",
                error_details=str(data),
            )
        return DownloadJob(
            user_id=user_id,
            operation_id=operation_id,
            status="submitted",
            message=data.get("message", "Download scheduled"),
        )
    return DownloadJob(
        user_id=user_id,
        status="failed",
        message=data.get("message", f"HTTP {response.status_code}"),
        error_details=str(data),
    )


async def poll_job(
    http: httpx.AsyncClient, client: DyvineClient, job: DownloadJob
) -> DownloadJob:
    """Refresh ``job`` in place from the status endpoint.

    Transport errors only annotate the message so the next poll round
    retries; a 404 marks the job ``not_found`` (terminal).
    """
    assert job.operation_id is not None, "cannot poll a job that never submitted"
    try:
        response = await http.get(
            client.poll_url(job.operation_id),
            headers=client.headers,
            timeout=client.settings.timeout,
        )
    except (httpx.TimeoutException, TimeoutError):
        job.message = "轮询超时"
        return job
    except Exception as exc:
        job.message = f"轮询异常: {exc}"
        return job
    if response.status_code == 404:
        job.status = "not_found"
        job.message = "任务未找到"
        job.completed_at = time.time()
        return job
    if response.status_code != 200:
        job.message = f"轮询 HTTP {response.status_code}"
        return job
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected payload: {type(data).__name__}")
    except Exception as exc:
        # Malformed payload: annotate and let the next poll round
        # retry instead of killing the whole batch run.
        job.message = f"轮询响应解析失败: {exc}"
        return job
    job.status = data.get("status", "unknown")
    job.message = data.get("message", "")
    if client.settings.include_likes:
        # The likes path polls /users/operations/{id}, which returns the
        # generic OperationResponse (total_items / completed_items,
        # progress in 0-100, no per-post failed count). Map those onto
        # the job's bulk-style counters so progress is not stuck at 0/0.
        job.progress = data.get("progress", 0.0) / 100
        job.total_posts = data.get("total_items", 0) or 0
        job.total_downloaded = data.get("completed_items", 0) or 0
        job.failed_count = 0
        job.error_details = data.get("error")
    else:
        # ``BulkDownloadResponse`` carries no ``progress`` field, so the
        # fraction is derived from the counters it does emit.
        total = data.get("total_posts", 0) or 0
        done = data.get("total_downloaded", 0) or 0
        job.progress = (done / total) if total > 0 else 0.0
        job.total_posts = total
        job.total_downloaded = done
        job.failed_count = data.get("failed_count", 0)
        job.error_details = data.get("error_details")
    if job.status in TERMINAL_STATUSES:
        job.completed_at = time.time()
    return job
