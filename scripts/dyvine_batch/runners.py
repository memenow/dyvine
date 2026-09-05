"""Batch runners: ``concurrent`` (semaphore submit + poll loop) and ``serial``.

Both runners share :mod:`client` and only differ in scheduling and
reporting. ``client_factory`` builds the ``httpx.AsyncClient`` so tests
can inject a ``MockTransport``-backed client; production passes the
default factory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx

from .client import (
    TERMINAL_STATUSES,
    DownloadJob,
    DyvineClient,
    poll_job,
    submit_download,
)
from .config import BatchSettings

ClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient, Any]]


def default_client_factory(
    settings: BatchSettings,
) -> AbstractAsyncContextManager[httpx.AsyncClient, Any]:
    """Build a production HTTP client honouring the run's limits."""
    limits = httpx.Limits(max_connections=settings.max_concurrent * 2)
    return httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(settings.timeout))


async def run_concurrent(
    settings: BatchSettings,
    user_ids: list[str],
    client_factory: ClientFactory | None = None,
) -> list[DownloadJob]:
    """Submit all users concurrently, then poll to completion."""
    print(f"\n{'=' * 60}")
    print("Dyvine 批量下载启动")
    print(f"  用户总数: {len(user_ids)}")
    print(f"  并发数:   {settings.max_concurrent}")
    print(f"  API 地址: {settings.api_url}")
    print(f"  下载 likes: {settings.include_likes}")
    print(f"{'=' * 60}\n")

    factory = client_factory or (lambda: default_client_factory(settings))
    client = DyvineClient(settings)
    async with factory() as http:
        print("[阶段 1/2] 提交下载任务...")
        semaphore = asyncio.Semaphore(settings.max_concurrent)

        async def _submit_limited(user_id: str) -> DownloadJob:
            async with semaphore:
                return await submit_download(http, client, user_id)

        jobs = await asyncio.gather(*(_submit_limited(uid) for uid in user_ids))

        submitted = [job for job in jobs if job.operation_id]
        failed = [job for job in jobs if not job.operation_id]
        print(f"\n  提交完成: {len(submitted)} 成功, {len(failed)} 失败")
        for job in failed:
            print(f"    - 用户 {job.user_id}: {job.message}")
        for job in submitted:
            print(
                f"  [已提交] 用户 {job.user_id} " f"-> operation_id: {job.operation_id}"
            )

        if not submitted:
            print("\n没有成功提交的任务，退出。")
            return list(jobs)

        print(f"\n[阶段 2/2] 轮询任务进度 (每 {settings.poll_interval:.0f}s)...")
        pending = {job.operation_id: job for job in submitted if job.operation_id}
        poll_round = 0
        while pending and poll_round < settings.max_poll_rounds:
            poll_round += 1
            await asyncio.sleep(settings.poll_interval)
            targets = list(pending.values())
            results = await asyncio.gather(
                *(poll_job(http, client, job) for job in targets),
                return_exceptions=True,
            )
            still_pending: dict[str, DownloadJob] = {}
            for job, result in zip(targets, results, strict=True):
                assert job.operation_id is not None
                if isinstance(result, BaseException):
                    if isinstance(result, (asyncio.CancelledError, KeyboardInterrupt)):
                        raise result
                    # poll_job guards its own IO, so anything surfacing
                    # here is a bug: fail just this job, never the run.
                    job.status = "failed"
                    job.message = f"轮询异常: {result!r}"
                    print(f"  [FAILED] 用户 {job.user_id} {job.message}")
                    continue
                if job.status in TERMINAL_STATUSES:
                    duration = ""
                    if job.completed_at and job.submitted_at:
                        secs = job.completed_at - job.submitted_at
                        duration = f" ({secs:.0f}s)"
                    print(
                        f"  [{job.status.upper()}] 用户 {job.user_id} "
                        f"{job.total_downloaded}/{job.total_posts} "
                        f"失败:{job.failed_count}{duration}"
                    )
                    if job.error_details:
                        print(f"      错误: {job.error_details}")
                else:
                    still_pending[job.operation_id] = job
            pending = still_pending
            if pending and poll_round % 3 == 0:
                print(f"\n  --- 进行中 ({len(pending)} 个) ---")
                for job in pending.values():
                    print(
                        f"    {job.user_id}: {job.status} "
                        f"({job.progress * 100:.1f}%) {job.message}"
                    )
                print("  ----------------------\n")
        for job in pending.values():
            job.status = "failed"
            job.message = f"轮询超时: {settings.max_poll_rounds} 轮内未完成"
            print(f"  [TIMEOUT] 用户 {job.user_id} {job.message}")

    _print_summary(list(jobs))
    return list(jobs)


async def run_serial(
    settings: BatchSettings,
    user_ids: list[str],
    client_factory: ClientFactory | None = None,
) -> list[DownloadJob]:
    """Download users one at a time, polling each to completion."""
    print(f"共 {len(user_ids)} 个用户，开始串行下载...")
    factory = client_factory or (lambda: default_client_factory(settings))
    client = DyvineClient(settings)
    jobs: list[DownloadJob] = []
    async with factory() as http:
        for index, user_id in enumerate(user_ids, 1):
            print(f"\n>>> [{index}/{len(user_ids)}]")
            print(f"\n{'=' * 50}")
            print(f"用户: {user_id}")
            print(f"{'=' * 50}")
            try:
                job = await submit_download(http, client, user_id)
                if not job.operation_id:
                    print(f"  提交失败: {job.message}")
                    jobs.append(job)
                    continue
                print(f"  已提交, operation_id: {job.operation_id}")
                for _ in range(settings.max_poll_rounds):
                    await asyncio.sleep(settings.poll_interval)
                    await poll_job(http, client, job)
                    print(
                        f"  状态: {job.status} | "
                        f"已下载: {job.total_downloaded}/{job.total_posts} | "
                        f"失败: {job.failed_count} | "
                        f"进度: {job.progress * 100:.1f}% | {job.message}"
                    )
                    if job.status in TERMINAL_STATUSES:
                        break
                else:
                    job.status = "failed"
                    job.message = f"轮询超时: {settings.max_poll_rounds} 轮内未完成"
                    print(f"  超时: {job.message}")
                jobs.append(job)
            except Exception as exc:
                print(f"  异常: {exc}")
                if job.operation_id:
                    # Already submitted server-side: keep the ID so the
                    # operation stays trackable, like concurrent mode.
                    job.status = "failed"
                    job.message = str(exc)
                    jobs.append(job)
                else:
                    jobs.append(
                        DownloadJob(user_id=user_id, status="failed", message=str(exc))
                    )

    print(f"\n{'=' * 50}")
    print("汇总报告")
    print(f"{'=' * 50}")
    total_downloaded = 0
    for job in jobs:
        total_downloaded += job.total_downloaded
        print(
            f"  {job.user_id[:20]}...  状态={job.status}  "
            f"已下载={job.total_downloaded}"
        )
    print(f"\n总下载作品数: {total_downloaded}")
    return jobs


def _print_summary(jobs: list[DownloadJob]) -> None:
    """Print the concurrent run's completion report."""
    total = len(jobs)
    submitted = [job for job in jobs if job.operation_id]
    completed_ok = [job for job in jobs if job.status == "completed"]
    partial = [job for job in jobs if job.status == "partial"]
    failed = [job for job in jobs if job.status in ("failed", "not_found")]
    total_downloaded = sum(job.total_downloaded for job in submitted)
    total_failed = sum(job.failed_count for job in submitted)

    print(f"\n{'=' * 60}")
    print("批量下载完成报告")
    print(f"{'=' * 60}")
    print(f"  总用户数:        {total}")
    print(f"  成功提交:        {len(submitted)}")
    print(f"  完全完成:        {len(completed_ok)}")
    print(f"  部分完成:        {len(partial)}")
    print(f"  失败/未找到:     {len(failed)}")
    print(f"  总下载作品数:    {total_downloaded}")
    print(f"  总失败数:        {total_failed}")
    print(f"{'=' * 60}\n")

    if failed:
        print("失败详情:")
        for job in failed:
            print(f"  - {job.user_id}: {job.message}")
        print()
